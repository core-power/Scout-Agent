"""图片分析工具 — 调用 Vision API 分析图片内容."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry


class VisionTool(ToolDefinition):
    """图片分析工具 — 识别内容、提取文字、描述场景."""

    name = "vision"
    pure_read = True
    description = "分析图片内容。可以描述图片、提取文字(OCR)、识别物体、颜色等。支持本地图片路径和图片URL。"
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
        try:
            from scout.config import ConfigManager
            cm = ConfigManager()
            cfg = cm.load()
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

        if not api_key:
            from scout.config.paths import DATA_DIR
            cfg_hint = str(DATA_DIR / "config.json")
            return Observation(tool_name="vision", success=False, output=f"未配置 API Key（请检查 {cfg_hint} 或 OPENAI_API_KEY/DASHSCOPE_API_KEY）")

        try:
            # 构建消息
            if image.startswith("http"):
                image_url = image
            else:
                # 本地文件转 base64
                p = Path(image)
                if not p.exists():
                    return Observation(tool_name="vision", success=False, output=f"文件不存在: {image}")
                b64 = base64.b64encode(p.read_bytes()).decode()
                ext = p.suffix.lower().lstrip(".")
                if ext in ("jpg", "jpeg"):
                    mime = "image/jpeg"
                elif ext == "png":
                    mime = "image/png"
                elif ext == "gif":
                    mime = "image/gif"
                elif ext == "webp":
                    mime = "image/webp"
                else:
                    mime = "image/png"
                image_url = f"data:{mime};base64,{b64}"

            async with httpx.AsyncClient(timeout=60) as client:
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
        except Exception as e:
            return Observation(tool_name="vision", success=False, output=str(e))


ToolRegistry.register(VisionTool())
