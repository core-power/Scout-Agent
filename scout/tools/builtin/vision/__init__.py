"""图片分析工具 — 有专属视觉模型时走 VL API，否则本地 OCR 兜底.

路由策略（2026-09-04）：
- resolve_mode() 决策: 显式配置了 vision_model 且 ≠ 主 model（即专门为视觉配置的
  模型）→ "vl"；否则 → "ocr"（本地 RapidOCR，无需网络/Key）。
  —— 治愈「把纯文本主模型(qwen3.8-27b 等)填进视觉模型字段 → VL 必失败」。
- "vl" 路径失败（模型不支持图片 / 超时 / 4xx）→ 自动降级本地 OCR，保证有输出。
- OCR 只能提取图中文字，无法描述画面；输出会明确标注该限制。
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
from pathlib import Path

import httpx

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# OCR 引擎惰性单例：仅在需要 OCR 时初始化（加载 onnx 模型较慢，避免拖慢启动）
_OCR_ENGINE: object | None = None


async def _wait_for_file(image: str, attempts: int = 4, interval: float = 0.5) -> bool:
    """本地路径存在性检查（带短暂等待重试）.

    背景（2026-09-04 实测竞态）：agent 并行发起"裁剪截图 + vision 读图"时，
    裁剪脚本的输出文件尚在写盘途中，vision 的 exists() 在文件出现前一瞬间
    执行 → 误报"文件不存在"→ 浪费两轮反思/重试。等待窗口 2s 对竞态免疫
    （真不存在的文件也只多花 2s，远低于一轮反思往返的成本）。
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

    规则：
    1. 无 API Key → OCR（本地识别不需要任何凭据）；
    2. vision_model 非空且 ≠ 主 model → 说明用户显式配置了专属视觉模型 → VL；
    3. 其余（vision_model 为空，或 vision_model 与主 model 相同——后者通常是把
       纯文本主模型误填进视觉字段）→ OCR，避免拿纯文本模型发 image_url 白失败。
    """
    if not (getattr(cfg, "api_key", "") or "").strip():
        return "ocr"
    vision_model = (getattr(cfg, "vision_model", "") or "").strip()
    main_model = (getattr(cfg, "model", "") or "").strip()
    if vision_model and vision_model != main_model:
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
    """OCR 兜底：支持本地路径与 http(s) URL（URL 先下载到临时文件）."""
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
        return await asyncio.to_thread(_ocr_sync, img_path)
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


async def _call_vision(api_key: str, base_url: str, model: str, image: str, question: str) -> Observation:
    """VL 路径：OpenAI 兼容 /chat/completions 发 image_url."""
    try:
        if image.startswith("http"):
            image_url = image
        else:
            if not await _wait_for_file(image):
                return Observation(tool_name="vision", success=False, output=f"文件不存在: {image}")
            p = Path(image)
            b64 = base64.b64encode(p.read_bytes()).decode()
            ext = p.suffix.lower().lstrip(".")
            mime = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp",
            }.get(ext, "image/png")
            image_url = f"data:{mime};base64,{b64}"

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
                                {"type": "text", "text": question},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        }
                    ],
                    "max_tokens": 1000,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            answer = data["choices"][0]["message"]["content"]
        return Observation(tool_name="vision", success=True, output=answer)
    except Exception as e:  # noqa: BLE001
        return Observation(tool_name="vision", success=False, output=str(e))


class VisionTool(ToolDefinition):
    """图片分析工具 — 识别内容、提取文字、描述场景.

    路由：配置了专属视觉模型（vision_model ≠ 主 model）→ 直接调视觉模型；
    未配置 → 本地 OCR（只能提取图中文字，无法描述画面）。
    支持本地图片路径和图片URL。
    """

    name = "vision"
    pure_read = True
    description = "分析图片内容。可以描述图片、提取文字(OCR)、识别物体、颜色等。支持本地图片路径和图片URL。配合 desktop 工具时：把 desktop screenshot 返回的图片路径传入，并在 question 中要求返回目标元素的像素坐标（如\"搜索输入框的中心坐标是多少\"），得到的坐标可直接用于 desktop 的 click/click_control。"
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
