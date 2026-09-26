"""视觉能力实测探测 —— 专治"自定义模型完全不知道能不能看图".

为什么需要（2026-09-26）：预设目录能覆盖的模型有限，用户接自定义/中转网关模型时
**没人知道**它收不收图片。名称规则（含 "-vl"/"vision" 等）只是猜，而猜错有两类，
代价完全不同：

  猜"不支持"其实支持 → 白白多走一次外挂识图，还丢细节（可忍受）
  猜"支持"其实不支持 → 图片发给不收图的端点：要么 400 中断对话，更糟的是**不少
                        中转网关会静默丢掉 image_url 字段**、正常返回一句话，
                        模型于是对着一张自己没看见的图编内容（不可接受）

所以探测必须回答两件事，而不能只看 HTTP 200：

  ① 接口收不收 image_url（400/415 类报错可判定）
  ② 它**真的看了图**吗（只有"图里有可验证的答案"才能判定）

做法：本机用 Pillow 画一张带随机 4 位数字的 PNG，问它"图里数字是多少"，比对答案。
数字对不上时再补一轮随机纯色图（问颜色）作二次判定，排除"能看图但不擅长认数字"。
只有拿到明确信号才写 `model_vision_probe`；网络/鉴权/端点错误一律**不写**任何结论
（否则一次断网就把模型永久标成"不支持视觉"）。

★ 只在用户于设置页点「探测」时执行，绝不在启动或对话过程中自动跑 —— 它是一次
  真实推理请求，会计费、会在服务商侧留日志。
"""

from __future__ import annotations

import base64
import io
import random
import re
from typing import Any

__all__ = ["make_digit_image", "make_solid_image", "classify_probe_error", "probe_vision"]

# 探测用字符集去掉 0/1：与字母 O/l 易混、且笔画太细，低分辨率下容易造成假阴性
_DIGITS = "23456789"
_PALETTE: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("红", (220, 30, 30)),
    ("蓝", (30, 80, 220)),
    ("绿", (30, 180, 70)),
    ("橙", (250, 150, 20)),
    ("紫", (150, 60, 200)),
)

# ── 错误分类关键词 ────────────────────────────────────────────────
# ★ "不收图片"的判定必须**同时**出现「图像类名词」与「拒绝词」：只匹配
#   "not support" 会误判 —— 实测 "temperature not supported" 这类**参数**报错也含
#   该词，而探测结论会落盘，一旦误判就把一个其实能看图的模型永久写成"不支持"，
#   代价不对称（宁可返回 unknown 让 UI 提示重试，也不猜）。
_MEDIA_HINTS = ("image", "multimodal", "vision", "modality", "picture", "photo",
                "content type", "图片", "图像")
_REJECT_HINTS = ("not support", "unsupported", "invalid", "unexpected",
                 "does not accept", "cannot process", "only text", "text-only",
                 "not allowed", "rejected")
_AUTH_HINTS = ("401", "403", "unauthorized", "forbidden", "invalid api key",
               "incorrect api key", "authentication", "permission denied")



# ── 测试图生成（纯本地，不联网）─────────────────────────────────────


def _font(size: int) -> Any:
    """尽量拿到可缩放字体；Pillow 老版本 load_default 不接受 size，逐级降级."""
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)  # Pillow ≥ 10
    except TypeError:
        pass
    for name in ("DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:  # noqa: BLE001 — 该字体不可用就换下一个
            continue
    return ImageFont.load_default()



def make_digit_image(seed: int | None = None) -> tuple[bytes, str]:
    """画一张随机 4 位数字 PNG，返回 (png 字节, 期望答案)."""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    answer = "".join(rnd.choice(_DIGITS) for _ in range(4))
    w, h = 340, 110
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    f = _font(72)
    try:
        box = d.textbbox((0, 0), answer, font=f)
        d.text(((w - (box[2] - box[0])) / 2 - box[0], (h - (box[3] - box[1])) / 2 - box[1]),
               answer, font=f, fill="black")
    except Exception:  # noqa: BLE001 — 无位图字体可量测时退化为左上对齐
        d.text((12, 12), answer, font=f, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), answer


def make_solid_image(seed: int | None = None) -> tuple[bytes, str]:
    """画一张纯色 PNG，返回 (png 字节, 期望颜色名)."""
    from PIL import Image

    rnd = random.Random(seed)
    name, rgb = rnd.choice(_PALETTE)
    img = Image.new("RGB", (200, 200), rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), name


# ── 结果判定 ─────────────────────────────────────────────────────


def classify_probe_error(status: int, body: str) -> str:
    """把失败响应分类为 unsupported / auth / transport / unknown.

    只有 `unsupported` 是能力结论；`auth` / `transport` / `unknown` 一律**不下结论**
    —— 否则一次断网或 Base URL 填错，就把模型永久标成"不支持视觉"。
    """
    low = (body or "").lower()
    if status in (401, 403):
        return "auth"
    if any(k in low for k in _AUTH_HINTS) and status not in (400, 415, 422):
        return "auth"
    if status == 404:
        # 多为 base_url 缺 /v1 或模型名不存在 —— 属于"没问到点上"，不能判能力
        return "transport"
    if status in (400, 415, 422):
        if any(k in low for k in _MEDIA_HINTS) and any(k in low for k in _REJECT_HINTS):
            return "unsupported"
        return "unknown"   # 与图片无关（参数/格式/限流），或提到图片但没拒绝
    return "unknown"


def _extract_digits(text: str) -> str:
    """从回答里抠出第一串 ≥4 位连续数字（模型常带"图中数字是 4567"这类前缀）."""
    m = re.search(r"\d{4,}", text or "")
    return m.group(0)[:4] if m else ""


async def _ask(api_key: str, base_url: str, model: str, png: bytes, question: str,
               timeout: float) -> tuple[str, str, int, str]:
    """发一次带图请求，返回 (kind, content, status, err).

    kind ∈ ok / error；错误时 err 带原始信息，status 为 HTTP 码（0=传输层失败）。
    """
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
        "max_tokens": 40,
        "temperature": 0,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        return "error", "", 0, f"请求超时（{timeout:.0f}s）"
    except Exception as e:  # noqa: BLE001 — 连接/DNS/证书等都属传输层，不下能力结论
        return "error", "", 0, f"{type(e).__name__}: {e}"
    if r.status_code != 200:
        return "error", "", r.status_code, (r.text or "")[:400]
    try:
        j = r.json()
    except Exception:  # noqa: BLE001 — 200 但不是 JSON：网关吞了 image 后行为异常
        return "error", "", r.status_code, "响应不是 JSON"
    choices = j.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    content = msg.get("content")
    if not isinstance(content, str):
        # 部分网关把 content 放 reasoning/返回 None
        content = msg.get("reasoning_content") or ""
    return "ok", str(content).strip(), r.status_code, ""


async def probe_vision(api_key: str, base_url: str, model: str,
                       timeout: float = 25.0, seed: int | None = None) -> dict:
    """实测该模型能否看图.

    返回 {result, verdict, detail, rounds}：
      result ∈ supported / unsupported / unverified / auth / transport / no_image_lib
      verdict ∈ True / False / None（None = 未取得可信结论，**不应落盘**）
    """
    rounds: list[dict] = []
    try:
        png, answer = make_digit_image(seed)
    except Exception as e:  # noqa: BLE001 — 无 Pillow 时无法探测
        return {"result": "no_image_lib", "verdict": None,
                "detail": f"无法生成测试图片（{type(e).__name__}: {e}）", "rounds": []}

    q1 = "图中显示的四位数字是什么？只输出这四位数字，不要任何解释、标点或其他文字。"
    kind, content, status, err = await _ask(api_key, base_url, model, png, q1, timeout)
    if kind == "error":
        cls = classify_probe_error(status, err) if status else "transport"
        detail = err or f"HTTP {status}"
        if cls == "unsupported":
            rounds.append({"asked": "digits", "http": status, "error": detail})
            return {"result": "unsupported", "verdict": False,
                    "detail": "端点明确拒绝图片输入（image_url 不被支持）", "rounds": rounds}
        return {"result": cls, "verdict": None,
                "detail": f"{detail}（未取得能力结论，请检查 Key / Base URL / 模型名）",
                "rounds": rounds}

    got = _extract_digits(content)
    rounds.append({"asked": "digits", "expected": answer, "got": got, "reply": content[:80]})
    if got == answer:
        return {"result": "supported", "verdict": True,
                "detail": f"数字识别正确（{answer}）→ 确认能直接看图", "rounds": rounds}

    # 数字没读对：可能是"能看但不擅长度数认字"，也可能是网关静默丢了图片字段。
    # 补一轮纯色图（几乎不可能猜错语义）来分离这两种情况。
    c_png, color = make_solid_image(seed)
    q2 = "这张图片是什么颜色？只回答一个颜色词（红/蓝/绿/橙/紫），不要其他内容。"
    kind2, content2, status2, err2 = await _ask(api_key, base_url, model, c_png, q2, timeout)
    rounds.append({"asked": "color", "expected": color, "got": "", "reply": content2[:80]})
    if kind2 == "error":
        cls2 = classify_probe_error(status2, err2)
        if cls2 == "unsupported":
            return {"result": "unsupported", "verdict": False,
                    "detail": "纯色图请求被端点拒绝 → 不收图片输入", "rounds": rounds}
    elif color in (content2 or ""):
        return {"result": "supported", "verdict": True,
                "detail": f"数字识别有偏差但颜色判断正确（{color}）→ 能看图，认字能力一般",
                "rounds": rounds}

    return {"result": "unverified", "verdict": None, "rounds": rounds,
            "detail": "接口返回 200 但两轮都没读出图中内容 —— 最常见的原因是中转网关"
                      "静默丢弃了 image_url 字段（此时按「支持」使用会让模型对着看不见"
                      "的图编内容）。建议：指定一个视觉模型来识图，或换官方端点再探测"}
