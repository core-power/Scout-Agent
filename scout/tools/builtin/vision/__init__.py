"""图片分析工具 — 有专属视觉模型时走 VL API，否则本地 OCR 兜底.

路由策略（2026-09-06 修订）：
- resolve_mode() 决策: 显式配置了 vision_model（非空）→ "vl"，直接用该模型发
  image_url 走 VL。
  —— 2026-09-06 实测: qwen3.8-27b（主模型与 vision_model 同名配置）支持 image_url
  多模态输入。此前把「vision_model == 主 model」一律判为"纯文本模型误填"→ 强制
  降级本地 OCR，导致 agent 看不到画面、只能读文字，办公 GUI 任务空转烧 token。
  现改为: 配了 vision_model 就尝试 VL，失败再由 "vl" 路径自动降级本地 OCR，
  不损失可用性。
- "vl" 路径失败（模型不支持图片 / 超时 / 4xx）→ 自动降级本地 OCR，保证有输出。
- OCR 只能提取图中文字，无法描述画面；输出会明确标注该限制。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

import httpx

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# OCR 引擎惰性单例：仅在需要 OCR 时初始化（加载 onnx 模型较慢，避免拖慢启动）
_OCR_ENGINE: object | None = None

# ── 低对比度自动增强（2026-09-06）：浅色主题 GUI 截图灰字白底对比弱 ──
# 微信/系统浅色界面里大量浅灰小字（~180 灰）落在白底（~248 白）上，VL/OCR 识别差。
# 实测该工具面对多次"看不清文字/坐标不准"。对"浅背景(mean>180)"或"低动态范围"
# 图做 CLAHE(L通道) + 对比度拉伸 + 轻度锐化，把灰字压深、边缘更锐。
# 红线1：绝不改变图像尺寸（只改像素值），否则 desktop 的 scale/meta 坐标换算会错乱。
# 红线2：深色背景(mean<100)一律不碰（避免把深色 UI 拉出噪声）。
# 红线3：依赖缺失/读图失败 → 返回 None 原样发送，不影响可用性。
_ENH_MEAN_LIGHT = 180.0   # 浅色背景：灰字白底，必增强
_ENH_MEAN_DARK = 100.0    # 深色背景：白字黑底，不碰
_ENH_SPREAD_MIN = 130.0   # 中间调：p2-p98 动态范围低于此值视为低对比
_ENH_STD_MIN = 42.0       # 中间调：灰度标准差低于此值视为对比过弱


def _needs_enhance(np, gray) -> bool:
    """低对比度判定（接收 numpy 与灰度数组）. """
    m = float(gray.mean())
    s = float(gray.std())
    if m > _ENH_MEAN_LIGHT:
        return True
    if m < _ENH_MEAN_DARK:
        return False
    lo, hi = np.percentile(gray, [2, 98])
    return (hi - lo) < _ENH_SPREAD_MIN or s < _ENH_STD_MIN


def _apply_enhance(img, np, cv2):
    """增强链路：LAB L 通道 CLAHE → 逐通道对比度拉伸 → 轻度锐化（与 DPI 无关，不改尺寸）. """
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l2 = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(l)
    out = cv2.cvtColor(cv2.merge((l2, a, b)), cv2.COLOR_LAB2BGR)
    chans = []
    for c in cv2.split(out):
        lo, hi = np.percentile(c, [1, 99])
        if hi - lo < 1:
            chans.append(c)
            continue
        chans.append(np.clip((c.astype(np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8))
    out = cv2.merge(chans)
    blur = cv2.GaussianBlur(out, (0, 0), 1.0)
    return cv2.addWeighted(out, 1.4, blur, -0.4, 0)


def _enhance_image(image: str) -> bytes | None:
    """低对比度自动增强：低对比 → 返回增强后的 PNG bytes；无需增强/失败 → None.

    注意：cv2.imread 在 Windows 上不支持含中文/特殊字符路径，统一用
    np.fromfile + cv2.imdecode 读取以兜住中文路径。
    """
    try:
        import numpy as np
        import cv2
    except ImportError:
        return None
    try:
        data = np.fromfile(image, dtype=np.uint8)
        if data.size == 0:
            return None
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if not _needs_enhance(np, gray):
            return None
        out = _apply_enhance(img, np, cv2)
        ok, buf = cv2.imencode(".png", out)
        return bytes(buf) if ok else None
    except Exception:  # noqa: BLE001 - 增强失败绝不应阻塞正常调用
        return None


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
    """路由决策：返回 "vl"（视觉模型）或 "ocr"（本地识别）.

    规则（2026-09-06 修订）：
    1. 无 API Key → OCR（本地识别不需要任何凭据）；
    2. vision_model 非空 → VL。注意：即使 vision_model 与主 model 相同，也可能是
       同一个多模态模型（实测 qwen3.8-27b 支持 image_url 输入），一律尝试 VL；
       VL 失败（不支持图片/超时/4xx）会自动降级本地 OCR，不影响可用性。
    3. vision_model 为空 → OCR。
    """
    if not (getattr(cfg, "api_key", "") or "").strip():
        return "ocr"
    vision_model = (getattr(cfg, "vision_model", "") or "").strip()
    if vision_model:
        return "vl"
    return "ocr"


def _get_ocr_engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # 惰性 import，降启动开销
        except ImportError as e:
            raise RuntimeError(
                "本地 OCR 组件未安装，请执行 pip install rapidocr-onnxruntime"
            ) from e
        try:
            _OCR_ENGINE = RapidOCR()
        except Exception as e:  # noqa: BLE001 - 模型缺失/损坏时给出可读提示
            raise RuntimeError(f"本地 OCR 引擎初始化失败：{e}") from e
    return _OCR_ENGINE


def _ocr_sync(img_path: str) -> list[str]:
    """同步 OCR 识别（放线程池执行），返回逐行文字."""
    engine = _get_ocr_engine()
    result, _elapse = engine(img_path)  # result: [[box, text, score], ...] 或 None
    if not result:
        return []
    return [str(item[1]) for item in result if len(item) >= 2 and str(item[1]).strip()]


async def _run_ocr(image: str) -> list[str]:
    """OCR 兜底：支持本地路径与 http(s) URL（URL 先下载到临时文件）.

    2026-09-06：本地/下载后的图片先经 _enhance_image 低对比度增强再送 OCR
    （浅色主题灰字白底识别差的问题）；增强产物写入独立临时文件，不覆盖原图，
    用完即清理。增强失败不影响原流程。
    """
    img_path = image
    tmp: str | None = None
    try:
        if image.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(image)
                resp.raise_for_status()
            ext = Path(image).suffix or ".png"
            fd, tmp = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, "wb") as f:
                f.write(resp.content)
            img_path = tmp
        else:
            if not await _wait_for_file(image):
                raise FileNotFoundError(f"文件不存在: {image}")
            img_path = str(Path(image))
        # 低对比度增强：增强产物写临时文件，不覆盖原图
        enh = _enhance_image(img_path)
        if enh is not None:
            fd, e_tmp = tempfile.mkstemp(suffix=".png")
            with os.fdopen(fd, "wb") as f:
                f.write(enh)
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            tmp = e_tmp
            img_path = e_tmp
        return await asyncio.to_thread(_ocr_sync, img_path)
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


async def _call_vision(api_key: str, base_url: str, model: str, image: str, question: str) -> Observation:
    """VL 路径：OpenAI 兼容 /chat/completions 发 image_url.

    2026-09-06 追加空输出自动重试：实测 15%+ 的 VL 调用返回 HTTP 200 但
    content 为空（模型对复杂定位题拒答 / 思考模型把内容全放 reasoning_content），
    空结果上抛会触发主模型换问法反复重试同图（10-14s/次 × 多轮）。
    故 content 为空时用补强指令自动重试一次；仍空则返回 failure，由上层
    _execute_raw 自动降级 OCR——绝不让空结果直接上抛给主模型。
    """
    try:
        if image.startswith("http"):
            image_url = image
        else:
            if not await _wait_for_file(image):
                return Observation(tool_name="vision", success=False, output=f"文件不存在: {image}")
            p = Path(image)
            # 低对比度(浅色主题)自动增强：增强后以 PNG bytes 发送，不覆盖原图
            enh = _enhance_image(str(p))
            if enh is not None:
                b64 = base64.b64encode(enh).decode()
                mime = "image/png"
            else:
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
                output="视觉模型连续两次返回空内容（HTTP 200 但 content 为空），交由 OCR 降级路径处理",
            )
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
    """图片分析工具 — 识别内容、提取文字、描述场景.

    路由：配置了视觉模型（vision_model 非空）→ 直接调视觉模型（VL，支持多模态
    描述画面/元素坐标）；VL 失败自动降级本地 OCR；完全未配置 → 本地 OCR
    （只能提取图中文字，无法描述画面）。
    支持本地图片路径和图片URL。
    """

    name = "vision"
    pure_read = True
    description = "分析图片内容。可以描述图片、提取文字(OCR)、识别物体、颜色等。支持本地图片路径和图片URL。配合 desktop 工具时：把 desktop screenshot 返回的图片路径（含 .meta.json，记录了 scale 与窗口偏移）传入，并在 question 中要求返回目标元素的该图片内像素坐标（如\"搜索输入框中心在图片内的坐标\"）；随后把坐标与截图路径一起传给 desktop click 的 x/y + img=<截图 path>，desktop 会按 meta 自动换算为屏幕坐标——截图可能被降采样，切勿手算缩放（若点击时省略 img，坐标将被当作屏幕绝对坐标）。"
    parameters = {
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "本地图片路径或图片URL"},
            "question": {"type": "string", "description": "关于图片的问题（如: 描述这张图片 / 提取文字 / 图中有什么）"},
        },
        "required": ["image", "question"],
    }
    annotations = ToolAnnotations(read_only=True, open_world=True)

    async def execute(self, image: str, question: str) -> Observation:
        """同图指纹去重包装层（2026-09-06）.

        仅在"同一图片内容、TTL 内、同一问题、未被警告过"这一种可证明安全的
        场景短路；其余（跨问题/跨时间窗/URL 图/指纹不可得/上次失败/已警告过）
        一律放行真实分析，保证模型永远能拿到最新真实答案。
        缓存仅在真实分析成功且输出非空时写入（失败/空输出不缓存 → 下次必放行）。
        """
        fp = None
        q_norm = None
        if _DEDUP_ENABLED:
            fp = _image_fingerprint(image)
            q_norm = _norm_question(question)
            if fp and _dedup_shortcircuit(fp, q_norm):
                return Observation(tool_name="vision", success=True, output=_DEDUP_HINT)
        obs = await self._execute_raw(image, question)
        if _DEDUP_ENABLED and fp and q_norm is not None and obs.success and obs.output:
            _IMG_CACHE[fp] = {"q_norm": q_norm, "ts": time.monotonic(), "warned": False}
        return obs

    async def _execute_raw(self, image: str, question: str) -> Observation:
        # ── 配置来源优先级：配置文件 > 环境变量 > 默认 ──
        # 修复(2026-08-17)：此前只读环境变量导致界面配置的 vision_model 不生效，
        # 视觉工具一直用默认 gpt-4o-mini 调用，与界面配置不一致。
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
        except Exception:
            pass
        if not api_key:
            api_key = os.getenv("OPENAI_API_KEY", "")
        if not base_url:
            base_url = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
        if not model:
            model = os.getenv("VISION_MODEL", "gpt-4o-mini")

        # ── 路由决策：有专属视觉模型 → VL；否则本地 OCR 兜底 ──
        mode = resolve_mode(cfg_proxy) if cfg_proxy is not None else ("vl" if api_key else "ocr")
        if mode == "vl":
            if not api_key:
                from scout.config.paths import DATA_DIR
                cfg_hint = str(DATA_DIR / "config.json")
                return Observation(tool_name="vision", success=False, output=f"未配置 API Key（请检查 {cfg_hint} 或 OPENAI_API_KEY/DASHSCOPE_API_KEY）")
            obs = await _call_vision(api_key, base_url, model, image, question)
            if obs.success:
                return obs
            # VL 失败 → 自动降级本地 OCR（模型不支持图片/网络/超时等）
            try:
                texts = await _run_ocr(image)
            except Exception as e:  # noqa: BLE001
                return Observation(
                    tool_name="vision", success=False,
                    output=f"视觉模型调用失败：{obs.output}\n本地 OCR 兜底也失败：{e}",
                )
            if not texts:
                return Observation(tool_name="vision", success=True, output="视觉模型调用失败（见上方原因），本地 OCR 未识别到文字（图片可能不含文字）。")
            brief = obs.output[:200]
            return Observation(
                tool_name="vision", success=True,
                output=f"（视觉模型调用失败：{brief}，已回退本地 OCR 提取文字）\n识别到的文字：\n" + "\n".join(texts),
            )

        # OCR 路径：无需 API Key / 网络
        try:
            texts = await _run_ocr(image)
        except Exception as e:  # noqa: BLE001
            return Observation(tool_name="vision", success=False, output=str(e))
        if not texts:
            return Observation(
                tool_name="vision", success=True,
                output="图片中未识别到文字（可能是不含文字的图片/照片）。注意：未配置专属视觉模型，当前用本地 OCR 只能提取文字，无法描述画面内容；如需描述请配置视觉模型。",
            )
        return Observation(
            tool_name="vision", success=True,
            output="（未配置专属视觉模型，使用本地 OCR 提取图中文字，无法描述画面）\n识别到的文字：\n" + "\n".join(texts),
        )


ToolRegistry.register(VisionTool())
