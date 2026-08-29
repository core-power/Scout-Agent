"""图片生成工具 — 调用 OpenAI/DashScope/智谱 图像生成 API."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import datetime

import httpx

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry


class ImageGenTool(ToolDefinition):
    """图片生成工具 — 支持多 Provider，模型从 config 读取."""

    name = "image_generation"
    description = "根据文本描述生成图片。支持生成插画、照片、图标、海报等视觉内容。"
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "图片描述（建议用英文，效果更好）"},
            "size": {"type": "string", "description": "图片尺寸: 1024x1024(默认), 1792x1024, 1024x1792", "default": "1024x1024"},
            "save_path": {"type": "string", "description": "保存路径（默认 /tmp/scout_images/）", "default": ""},
        },
        "required": ["prompt"],
    }
    annotations = ToolAnnotations(read_only=False, open_world=True)

    def _get_config(self) -> dict:
        """从 config.json 读取配置."""
        try:
            from scout.config import ConfigManager
            cm = ConfigManager()
            cfg = cm.load()
            data = {
                "provider": cfg.provider,
                "api_key": cfg.api_key,
                "base_url": cfg.base_url,
                "image_model": cfg.image_model,
            }
            # 图像模型独立厂商：设置了 image_provider 且与主厂商不同时，
            # 使用该厂商已保存的 api_key/base_url
            if cfg.image_provider and cfg.image_provider != cfg.provider:
                pkey, purl = cm.get_provider_credentials(cfg.image_provider)
                if pkey:
                    data["api_key"] = pkey
                if purl:
                    data["base_url"] = purl
            return data
        except Exception:
            return {}

    async def execute(self, prompt: str, size: str = "1024x1024", save_path: str = "") -> Observation:
        cfg = self._get_config()
        image_model = cfg.get("image_model", "")
        api_key = cfg.get("api_key", "")
        cfg.get("provider", "")
        base_url = cfg.get("base_url", "")

        if not image_model:
            # 降级到环境变量
            api_key = os.getenv("OPENAI_API_KEY", "")
            if api_key:
                image_model = "dall-e-3"
                base_url = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
            else:
                dashscope_key = os.getenv("ARK_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")
                if dashscope_key:
                    api_key = dashscope_key
                    image_model = "wanx-v1"
                else:
                    return Observation(
                        tool_name="image_generation",
                        success=False,
                        output="未配置图像生成模型。请在设置中选择图像生成模型，或设置 OPENAI_API_KEY / DASHSCOPE_API_KEY 环境变量。",
                    )

        # 根据模型名分派
        if image_model.startswith("wan") or image_model.startswith("qwen-image"):
            return await self._gen_dashscope(prompt, size, save_path, api_key, image_model, base_url)
        elif image_model.startswith("cogview"):
            return await self._gen_zhipu(prompt, size, save_path, api_key, image_model, base_url)
        else:
            return await self._gen_openai(prompt, size, save_path, api_key, image_model, base_url)

    async def _gen_openai(self, prompt: str, size: str, save_path: str, api_key: str, model: str, base_url: str) -> Observation:
        """OpenAI DALL-E / GPT Image."""
        if not base_url:
            base_url = "https://api.openai.com/v1"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base_url}/images/generations",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "prompt": prompt,
                        "n": 1,
                        "size": size,
                        "response_format": "b64_json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                b64 = data["data"][0]["b64_json"]

            path = self._save_image(b64, save_path, "openai")
            return self._ok(path, model, prompt)
        except Exception as e:
            return Observation(tool_name="image_generation", success=False, output=f"OpenAI 生成失败: {e}")

    async def _gen_dashscope(self, prompt: str, size: str, save_path: str, api_key: str, model: str, base_url: str) -> Observation:
        """DashScope 图像生成 — qwen-image(通义万相) 走 multimodal-generation，wanx 走 text2image."""
        if not api_key:
            return Observation(tool_name="image_generation", success=False, output="未配置 DASHSCOPE_API_KEY")
        if not base_url:
            base_url = "https://dashscope.aliyuncs.com"

        # 尺寸统一为 width*height 格式（DashScope 要求，x 会报 Invalid size format）
        size_ds = size.replace("x", "*") if "x" in size else size

        # ── qwen-image / wan2.x 系列 → multimodal-generation（同步返回 url） ──
        # 修复(2026-08-18)：wan2.7-image-pro 等新模型也走 multimodal-generation，
        # text2image 异步仅老版 wanx-* 支持。
        if model.startswith("qwen-image") or model.startswith("wan2"):
            try:
                url = f"{base_url}/api/v1/services/aigc/multimodal-generation/generation"
                async with httpx.AsyncClient(timeout=180) as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={
                            "model": model,
                            "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                            "parameters": {"n": 1, "size": size_ds},
                        },
                    )
                    if resp.status_code != 200:
                        err = resp.text[:300]
                        return Observation(tool_name="image_generation", success=False, output=f"DashScope(qwen-image) 生成失败: {err}")
                    data = resp.json()
                    content = data.get("output", {}).get("choices", [{}])[0].get("message", {}).get("content", [])
                    b64 = None
                    for c in content:
                        # qwen-image 返回 {image: url字符串} 或 {type: image_url, image: {url/b64_json}}
                        if "image" in c or "image_url" in c:
                            img_info = c.get("image") or c.get("image_url") or {}
                            # image 字段可能是 dict（{b64_json/url}）也可能是字符串 URL
                            if isinstance(img_info, str):
                                img_url = img_info
                            else:
                                b64 = img_info.get("b64_json") or img_info.get("b64")
                                img_url = img_info.get("url") if isinstance(img_info, dict) else None
                            if not b64 and img_url:
                                async with httpx.AsyncClient(timeout=60) as dl:
                                    ir = await dl.get(img_url)
                                    b64 = base64.b64encode(ir.content).decode()
                            break
                    if not b64:
                        return Observation(tool_name="image_generation", success=False, output=f"DashScope(qwen-image) 返回无图片数据: {str(data)[:300]}")
                    path = self._save_image(b64, save_path, "qwen-image")
                    return self._ok(path, model, prompt)
            except Exception as e:
                return Observation(tool_name="image_generation", success=False, output=f"DashScope(qwen-image) 生成失败: {e}")

        # ── wanx 系列 → text2image（异步任务 + 轮询） ──
        ds_url = f"{base_url}/api/v1/services/aigc/text2image/image-synthesis"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    ds_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "X-DashScope-Async": "enable",
                    },
                    json={
                        "model": model,
                        "input": {"prompt": prompt},
                        "parameters": {"n": 1, "size": size_ds},
                    },
                )
                resp.raise_for_status()
                task = resp.json()
                task_id = task.get("output", {}).get("task_id")
                if not task_id:
                    return Observation(tool_name="image_generation", success=False, output=f"DashScope 未返回 task_id: {task}")

                # 轮询任务状态
                for _ in range(60):
                    await asyncio.sleep(3)
                    async with httpx.AsyncClient(timeout=30) as poller:
                        r = await poller.get(
                            f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                            headers={"Authorization": f"Bearer {api_key}"},
                        )
                        result = r.json()
                        status = result.get("output", {}).get("task_status")
                        if status == "SUCCEEDED":
                            img_url = result["output"]["results"][0]["url"]
                            async with httpx.AsyncClient(timeout=30) as dl:
                                img_resp = await dl.get(img_url)
                                b64 = base64.b64encode(img_resp.content).decode()
                            path = self._save_image(b64, save_path, "dashscope")
                            return self._ok(path, model, prompt)
                        elif status == "FAILED":
                            return Observation(tool_name="image_generation", success=False, output="DashScope 生成失败")

                return Observation(tool_name="image_generation", success=False, output="DashScope 生成超时")
        except Exception as e:
            return Observation(tool_name="image_generation", success=False, output=f"DashScope 生成失败: {e}")

    async def _gen_zhipu(self, prompt: str, size: str, save_path: str, api_key: str, model: str, base_url: str) -> Observation:
        """智谱 CogView."""
        if not base_url:
            base_url = "https://open.bigmodel.cn/api/paas/v4"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{base_url}/images/generations",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "prompt": prompt,
                        "size": size,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                # 智谱返回 URL 或 b64
                item = data.get("data", [{}])[0]
                if "url" in item:
                    img_url = item["url"]
                    async with httpx.AsyncClient(timeout=30) as dl:
                        img_resp = await dl.get(img_url)
                        b64 = base64.b64encode(img_resp.content).decode()
                elif "b64_json" in item:
                    b64 = item["b64_json"]
                else:
                    return Observation(tool_name="image_generation", success=False, output="智谱返回格式异常")

            path = self._save_image(b64, save_path, "zhipu")
            return self._ok(path, model, prompt)
        except Exception as e:
            return Observation(tool_name="image_generation", success=False, output=f"智谱生成失败: {e}")

    def _save_image(self, b64: str, save_path: str, provider: str) -> str:
        """保存 base64 图片到文件."""
        if not save_path:
            from scout.core.platform import get_temp_dir
            save_dir = get_temp_dir("images")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = str(save_dir / f"img_{provider}_{ts}.png")

        img_bytes = base64.b64decode(b64)
        with open(save_path, "wb") as f:
            f.write(img_bytes)
        return save_path

    def _ok(self, path: str, model: str, prompt: str) -> Observation:
        """成功返回 — 附带 downloadable 元数据，前端据此渲染图片预览 + 下载卡片."""
        return Observation(
            tool_name="image_generation",
            success=True,
            output=f"图片已生成并保存到: {path}\n模型: {model}\n描述: {prompt}",
            metadata={
                "path": path,
                "downloadable": True,
                "file_name": os.path.basename(path),
                "file_size": os.path.getsize(path) if os.path.exists(path) else 0,
                "is_image": True,
            },
        )


ToolRegistry.register(ImageGenTool())
