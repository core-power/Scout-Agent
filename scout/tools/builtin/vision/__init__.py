"""图片分析工具 — 通过多模态视觉模型（VL）分析图片.

路由策略（2026-09-07 简化）：
- resolve_mode(): vision_model 非空 → "vl"；未配置 → "none"。
- "none"（未配置视觉模型）→ 直接返回友好提示，不再走本地 OCR 兜底
  （2026-09-07 决策：移除 RapidOCR/cv2 依赖，为项目减负约 160MB；
  OCR 只能提取文字无法描述画面，且用户明确要求去掉该依赖）。
- "vl" 失败（模型不支持图片/超时/4xx）→ 返回失败原因，由主模型决策。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry


async def _wait_for_file(image: str, attempts: int = 12, interval: float = 0.3) -> bool:
    """本地路径存在性检查（带短暂等待重试）.

    背景（2026-09-04 实测竞态）：agent 并行发起"裁剪截图 + vision 读图"时，
    截图工具的输出文件尚在写盘途中，vision 的 exists() 在文件出现前一瞬间
    执行 → 误报"文件不存在"→ 浪费两轮反思/重试。等待窗口从 2s 提高到 ~3.6s
    （12×0.3），覆盖桌面大窗口 PrintWindow/降采样保存的写盘耗时（真不存在的
    文件也只多花 3.6s，仍远低于一轮反思往返的成本）。
    """
    p = Path(image)
    for i in range(max(1, attempts)):
        if p.exists():
            return True
        if i < attempts - 1:
            await asyncio.sleep(interval)
    return False


def resolve_mode(cfg) -> str:
    """路由决策：返回 "vl"（已配置视觉模型）或 "none"（未配置）.

    规则（2026-09-07 简化）：vision_model 非空 → "vl"（即使与主 model 同名——
    同名也可能是多模态模型，实测 qwen3.8-27b 支持 image_url 输入）；
    否则 → "none"，由调用方返回未配置提示（不再有本地 OCR 兜底）。
    """
    vision_model = (getattr(cfg, "vision_model", "") or "").strip()
    return "vl" if vision_model else "none"


def _parse_crop(crop: str, w: int, h: int) -> tuple[int, int, int, int] | None:
    """解析 'x,y,w,h'（支持中英文逗号/x/*分隔），并裁剪到图内合法区域."""
    try:
        parts = [
            int(float(v))
            for v in re.split(r"[,，xX*]\s*", (crop or "").strip())
            if v.strip()
        ]
    except (TypeError, ValueError):
        return None
    if len(parts) != 4:
        return None
    x, y, cw, ch = parts
    x = max(0, min(x, w - 1))
    y = max(0, min(y, h - 1))
    cw = max(1, min(cw, w - x))
    ch = max(1, min(ch, h - y))
    return x, y, cw, ch


def _crop_local_image(src: Path, crop: str) -> tuple[Path | None, str]:
    """按 'x,y,w,h' 裁剪本地图片并落盘到源图旁（固定名便于同图同区域复用）.

    同时改写副本的 .meta.json（win_left/top 加上裁剪偏移），使
    desktop click img=<裁剪图> 的自动坐标换算依旧成立——模型无需手算。
    返回 (裁剪图路径|None, 错误说明|空)。
    """
    try:
        from PIL import Image

        img = Image.open(src)
        img.load()
        box = _parse_crop(crop, *img.size)
        if box is None:
            return None, "crop 参数无效（应为 'x,y,w,h' 图片内像素坐标）"
        x, y, cw, ch = box
        out = src.with_name(f"{src.stem}_crop{src.suffix or '.png'}")
        img.crop((x, y, x + cw, y + ch)).save(str(out))
        meta_p = src.with_suffix(".meta.json")
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
                scale = float(meta.get("scale") or 1.0) or 1.0
                meta2 = dict(meta)
                meta2["path"] = str(out)
                meta2["shot_w"], meta2["shot_h"] = cw, ch
                meta2["win_left"] = int(meta.get("win_left", 0) or 0) + int(x / scale + 0.5)
                meta2["win_top"] = int(meta.get("win_top", 0) or 0) + int(y / scale + 0.5)
                out.with_suffix(".meta.json").write_text(
                    json.dumps(meta2, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001 — meta 缺失只影响坐标换算，不影响读图
                pass
        return out, ""
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def get_vl_config() -> tuple[str, str, str, object]:
    """读取 VL 调用配置，返回 (api_key, base_url, model, cfg_proxy).

    2026-09-08 从 _execute_raw 抽出：desktop 的 locate/find 定位链路复用同一套
    配置来源（配置文件 > 环境变量 > 默认），避免两处漂移。
    """
    api_key = ""
    base_url = ""
    model = ""
    cfg_proxy = None
    try:
        from scout.config import ConfigManager
        cm = ConfigManager()
        cfg = cm.load()
        cfg_proxy = cfg
        api_key = cfg.api_key or ""
        base_url = cfg.base_url or ""
        model = (cfg.vision_model or cfg.model or "").strip()
        # 视觉模型独立厂商：设置了 vision_provider 且与主厂商不同时，
        # 使用该厂商已保存的 api_key/base_url
        if cfg.vision_provider and cfg.vision_provider != cfg.provider:
            pkey, purl = cm.get_provider_credentials(cfg.vision_provider)
            if pkey:
                api_key = pkey
            if purl:
                base_url = purl
    except Exception:  # noqa: BLE001 — 配置读取失败走环境变量兜底
        pass
    if not api_key:
        api_key = os.getenv("OPENAI_API_KEY", "")
    if not base_url:
        base_url = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    if not model:
        model = os.getenv("VISION_MODEL", "gpt-4o-mini")
    return api_key, base_url, model, cfg_proxy


async def _call_vision(
    api_key: str, base_url: str, model: str, image: str, question: str, crop: str = ""
) -> Observation:
    """VL 路径：OpenAI 兼容 /chat/completions 发 image_url.

    2026-09-06 追加空输出自动重试：实测 15%+ 的 VL 调用返回 HTTP 200 但
    content 为空（模型对复杂定位题拒答 / 思考模型把内容全放 reasoning_content），
    空结果上抛会触发主模型换问法反复重试同图（10-14s/次 × 多轮）。
    故 content 为空时用补强指令自动重试一次；仍空则返回 failure，由上层
    _execute_raw 自动降级 OCR——绝不让空结果直接上抛给主模型。
    2026-09-08 追加 crop：VL 图像 token 按分辨率（patch 数）计费，只裁剪目标
    局部区域可使 token 与推理时延近似线性下降。
    """
    try:
        crop_note = ""
        if crop and image.startswith(("http://", "https://")):
            crop_note = "\n[vision crop] crop 仅支持本地图片，URL 输入已忽略该参数。"
        if image.startswith("http"):
            image_url = image
        else:
            if not await _wait_for_file(image):
                return Observation(tool_name="vision", success=False, output=f"文件不存在: {image}")
            p = Path(image)
            if crop:
                out_p, err = _crop_local_image(p, crop)
                if out_p is not None:
                    p = out_p
                    crop_note = (
                        f"\n[vision crop] 已裁剪保存: {out_p}；desktop click 请用 img={out_p}，"
                        "VL 返回的坐标可直接使用（工具自动换算）。"
                        if out_p.with_suffix(".meta.json").exists()
                        else f"\n[vision crop] 已裁剪保存: {out_p}；"
                        "源图无坐标元数据，返回坐标为该裁剪图内像素。"
                    )
                else:
                    crop_note = f"\n[vision crop] 裁剪失败（{err}），已按原图完整分析。"
            # ★ VL 路径不做低对比度增强（2026-09-07）：多模态大模型在海量
            #   自然图像+截图上训练，对浅色低对比度 UI 本身鲁棒；CLAHE 拉伸
            #   会放大噪声、改变色彩分布，反而使图偏离 VL 的训练分布（负优化）。
            #   增强只保留在 OCR 路径（传统检测+识别模型确实吃对比度）。
            b64 = base64.b64encode(p.read_bytes()).decode()
            ext = p.suffix.lower().lstrip(".")
            mime = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp",
            }.get(ext, "image/png")
            image_url = f"data:{mime};base64,{b64}"

        async def _post(text: str) -> str | None:
            """单次 POST；HTTP 异常向上抛(由外层 except 转 failure)，空 content 返回 None."""
            async with httpx.AsyncClient(timeout=90) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": text},
                                    {"type": "image_url", "image_url": {"url": image_url}},
                                ],
                            }
                        ],
                        "max_tokens": 1000,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
            ans = data["choices"][0]["message"].get("content")
            return (ans or "").strip() or None

        answer = await _post(question)
        if answer is None:  # HTTP 200 但 content 为空 → 补强指令重试一次
            answer = await _post(
                question
                + "\n\n【重要】你上一次返回了空内容。请务必以文字形式直接回答本题："
                "若无法给出精确数值/坐标，请描述你实际看到的内容并给出最接近的估计，"
                "绝对不要返回空字符串或仅返回标点。"
            )
        if answer is None:
            return Observation(
                tool_name="vision", success=False,
                output="视觉模型连续两次返回空内容（HTTP 200 但 content 为空），请改用其他方式获取该信息或更换问题再试",
            )
        if crop_note:
            answer = answer + crop_note
        return Observation(tool_name="vision", success=True, output=answer)
    except Exception as e:  # noqa: BLE001
        return Observation(tool_name="vision", success=False, output=str(e))


# ── 同图指纹去重（2026-09-06）：规则见 VisionTool.execute 头注释 ──
# 背景：GUI 任务里模型会对同一张截图反复确认（09-05 实测 45 张图被问 80 次，
# 每次 VL 16-21s）。同一图片内容 + 同一问题 → 答案必然一致；但若点击后界面未变
# （hash 相同），反复复用旧答案会掩盖"操作未生效"，故重复命中时只返回"图未变"
# 中性提示（不含坐标、不含旧结论），逼模型换策略或触发新的真实分析。
# 设计红线：一切可能出错的路径一律放行真实分析（fail-open）；任何结论最多被拦一次。
_DEDUP_TTL = 90.0  # 秒：仅 TTL 内重复视为可疑；跨阶段回到同一界面自动失效
_DEDUP_ENABLED = os.getenv("SCOUT_VISION_DEDUP", "1") != "0"  # 事故一键回滚开关

# hash -> {"q_norm": str, "ts": float(monotonic), "warned": bool}
_IMG_CACHE: dict[str, dict] = {}

_DEDUP_HINT = (
    "（vision 去重提示）这张图片的内容与 90 秒内的上一次分析完全相同（内容指纹一致）。"
    "vision 只陈述该客观事实，不做任何推断。"
    "若你预期界面会因上一步操作而变化，请改用 desktop 的 read_controls 定位控件，"
    "或重新截图获取最新状态后再决策；如需对当前图片重新分析，请直接再次调用本工具。"
)


def _image_fingerprint(image: str) -> str | None:
    """本地图片内容 SHA-256；URL/缺失/读取失败一律返回 None（不参与去重，放行）. """
    if image.startswith(("http://", "https://")):
        return None
    try:
        p = Path(image)
        if not p.exists():
            return None
        h = hashlib.sha256()
        h.update(str(p.stat().st_size).encode("utf-8"))  # 大小参与哈希，防同尺寸巧合
        with open(p, "rb") as f:
            h.update(f.read())
        return h.hexdigest()
    except Exception:  # noqa: BLE001 - 任何读图失败都不应阻塞正常调用
        return None


_Q_STRIP = str.maketrans("", "", " \t\r\n，。！？、,.!?；;：:【】「」[]()（）<>《》\"'“”‘’")


def _norm_question(q: str) -> str:
    """归一化问题：仅去空白/标点/轻语气虚词，不做任何语义映射（宁 miss 勿误并）。"""
    s = (q or "").translate(_Q_STRIP)
    for w in ("请", "麻烦", "帮我", "再", "一下", "看看", "吗", "呢", "啊", "了", "这个", "那个"):
        s = s.replace(w, "")
    return s


def _dedup_shortcircuit(fp: str, q_norm: str) -> bool:
    """判定是否短路（命中则置 warned=True 并返回 True）.

    fail-open：以下任一条件不满足 → 返回 False，放行真实分析：
    - 图首见（无缓存项）
    - 距上次真实分析超过 TTL（可能跨阶段回到同一界面）
    - 问题实质不同（不跨问题复用结论）
    - 已被警告过一次（第二次必放行，保证模型总能拿到真实答案，防死锁）
    """
    now = time.monotonic()
    e = _IMG_CACHE.get(fp)
    if not e:
        return False
    if now - e["ts"] > _DEDUP_TTL:
        return False
    if e.get("q_norm") != q_norm:
        return False
    if e.get("warned"):
        return False
    e["warned"] = True
    return True


class VisionTool(ToolDefinition):
    """图片分析工具 — 通过视觉模型（VL）描述画面/提取信息.

    路由：配置了视觉模型（vision_model 非空）→ 调视觉模型（多模态，支持
    描述画面/元素坐标/读文字）；未配置 → 返回"无法读取图片"的提示（无本地
    OCR 兜底，2026-09-07 移除该依赖）。
    支持本地图片路径和图片URL。
    """

    name = "vision"
    pure_read = True
    description = "分析图片内容。可以描述图片、读取图中文字、识别物体、颜色等（需已配置视觉模型）。支持本地图片路径和图片URL。支持 crop='x,y,w,h' 局部读图（只裁剪目标区域，token 与耗时大幅下降；定位类问题优先用）。配合 desktop 工具时：把 desktop screenshot 返回的图片路径（含 .meta.json，记录了 scale 与窗口偏移）传入，并在 question 中要求返回目标元素的该图片内像素坐标（如\"搜索输入框中心在图片内的坐标\"）；随后把坐标与截图路径一起传给 desktop click 的 x/y + img=<截图 path>，desktop 会按 meta 自动换算为屏幕坐标——截图可能被降采样，切勿手算缩放（若点击时省略 img，坐标将被当作屏幕绝对坐标；用了 crop 则必须用回执里的裁剪图路径作 img=）。★ 看界面时请**直接调用本工具并省略 image**（内部自动截屏），不要先 desktop screenshot 再传路径——那会多一次工具往返与一次决策轮次（GUI 长任务步数直接翻倍）。"
    parameters = {
        "type": "object",
        "properties": {
            "image": {
                "type": "string",
                "description": "本地图片路径或图片URL。★ 留空则自动截屏当前屏幕（或按 window/process 限定的窗口）——"
                "GUI 任务看界面时首选留空，省去先调 desktop screenshot 的一次往返。",
            },
            "question": {"type": "string", "description": "关于图片的问题（如: 描述这张图片 / 提取文字 / 图中有什么）"},
            "window": {"type": "string", "description": "可选：自动截屏时限定窗口标题（精确匹配，需与 process 二选一或同用）"},
            "process": {"type": "string", "description": "可选：自动截屏时限定进程名子串（如 Weixin/Feishu/WeMeet，比标题更稳）"},
            "crop": {
                "type": "string",
                "description": "可选 'x,y,w,h'（该图片内像素坐标）：只分析该局部区域（按钮/弹窗/列表行等），"
                "token 与推理耗时大幅下降。定位类问题优先裁剪目标区域再读。返回坐标为裁剪图内坐标——"
                "desktop click 请用回执中的裁剪图路径作 img= 自动换算，勿手算。",
            },
        },
        # ★ 2026-09-15：image 不再必填 —— 留空即自动截屏（一次调用完成"看界面"）
        "required": ["question"],
    }
    annotations = ToolAnnotations(read_only=True, open_world=True)

    async def execute(
        self,
        image: str = "",
        question: str = "",
        crop: str = "",
        window: str = "",
        process: str = "",
    ) -> Observation:
        """同图指纹去重包装层（2026-09-06）+ 自动截屏（2026-09-15）.

        仅在"同一图片内容、TTL 内、同一问题、未被警告过"这一种可证明安全的
        场景短路；其余（跨问题/跨时间窗/URL 图/指纹不可得/上次失败/已警告过）
        一律放行真实分析，保证模型永远能拿到最新真实答案。
        缓存仅在真实分析成功且输出非空时写入（失败/空输出不缓存 → 下次必放行）。

        ★ 2026-09-15：``image`` 留空时**自动截屏**（复用 desktop 工具），把
        GUI 任务里的「screenshot → vision」两次工具调用压缩为一次——此前每次
        "看界面"都要多一次 LLM 往返，且模型常忘记跟进读图（截图路径被浪费）。
        """
        if not image:
            image, _err = await self._auto_capture(window=window, process=process)
            if not image:
                return Observation(tool_name="vision", success=False, output=_err)

        fp = None
        q_norm = None
        if _DEDUP_ENABLED:
            fp = _image_fingerprint(image)
            q_norm = _norm_question(question)
            if fp and _dedup_shortcircuit(fp, q_norm):
                return Observation(tool_name="vision", success=True, output=_DEDUP_HINT)
        obs = await self._execute_raw(image, question, crop)
        if _DEDUP_ENABLED and fp and q_norm is not None and obs.success and obs.output:
            _IMG_CACHE[fp] = {"q_norm": q_norm, "ts": time.monotonic(), "warned": False}
        return obs

    @staticmethod
    async def _auto_capture(window: str = "", process: str = "") -> tuple[str, str]:
        """自动截屏（复用 desktop 工具），返回 (图片路径, 错误说明).

        从 desktop 截图回执中取图片路径：优先 metadata（path/img/图片路径字段），
        兜底用正则从输出文本提取 .png/.jpg 路径。desktop 不可用（非 Windows /
        未启用）时返回明确指引，让模型改用其它方式（或自行提供 image）。
        """
        import re as _re

        from scout.tools.registry import ToolRegistry

        tool = ToolRegistry.get_tool("desktop")
        if tool is None:
            return "", (
                "未提供 image 且当前环境没有 desktop 工具（非 Windows 或未启用），无法自动截屏。"
                "请传入已有的图片路径/URL，或改用 desktop 的 read_controls 获取界面信息。"
            )
        kw: dict = {"action": "screenshot"}
        if window:
            kw["title"] = window
        if process:
            kw["process"] = process
        try:
            obs = await tool.execute(**kw)
        except TypeError:
            obs = await tool.execute(action="screenshot")
        except Exception as e:  # noqa: BLE001
            return "", f"自动截屏失败: {type(e).__name__}: {e}"

        if not getattr(obs, "success", False):
            return "", f"自动截屏失败: {(getattr(obs, 'output', '') or '')[:200]}"

        meta = getattr(obs, "metadata", None) or {}
        for k in ("path", "img", "image", "file", "shot"):
            v = meta.get(k)
            if isinstance(v, str) and v:
                return v, ""
        m = _re.search(r"([A-Za-z]:\\[^\s\"'`]+\.(?:png|jpg|jpeg|webp)|/[^\s\"'`]+\.(?:png|jpg|jpeg|webp))",
                       getattr(obs, "output", "") or "", _re.I)
        if m:
            return m.group(1), ""
        return "", f"自动截屏成功但未找到图片路径，回执: {(getattr(obs, 'output', '') or '')[:200]}"

    async def _execute_raw(self, image: str, question: str, crop: str = "") -> Observation:
        # ── 配置来源优先级：配置文件 > 环境变量 > 默认（get_vl_config 统一读取，
        # desktop locate/find 定位链路复用同一函数，避免两处漂移）──
        api_key, base_url, model, cfg_proxy = get_vl_config()

        # ── 路由决策：已配置 vision_model → VL；未配置 → 友好提示（无 OCR 兜底）──
        mode = resolve_mode(cfg_proxy) if cfg_proxy is not None else ("vl" if api_key and model else "none")
        if mode == "none":
            return Observation(
                tool_name="vision", success=False,
                output="当前未配置视觉模型，无法读取图片。请在「设置 → 模型配置」中填写视觉模型（vision_model）后重试；"
                "未配置前此工具不可用，请改用其他方式获取信息（如 desktop 的 read_controls）。",
            )
        if not api_key:
            from scout.config.paths import DATA_DIR
            cfg_hint = str(DATA_DIR / "config.json")
            return Observation(tool_name="vision", success=False, output=f"未配置 API Key（请检查 {cfg_hint} 或 OPENAI_API_KEY/DASHSCOPE_API_KEY）")
        obs = await _call_vision(api_key, base_url, model, image, question, crop)
        if obs.success:
            return obs
        # VL 失败：无 OCR 兜底（2026-09-07），直接如实返回失败原因，由主模型决策下一步
        return obs


ToolRegistry.register(VisionTool())
