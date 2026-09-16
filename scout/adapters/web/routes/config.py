"""配置/安全/用量路由组（/api/config/*、/api/models、/api/security、/api/usage、/api/routing）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from scout.security.policy import ALLOWED_PATH_PREFIXES, DANGEROUS_PATTERNS, SYSTEM_DIRS

# logger 归一：与原 web.py 日志器名一致（行为不变）
import logging

logger = logging.getLogger("scout.adapters.web")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class ConfigRoutes:
    """配置/安全/用量路由组（/api/config/*、/api/models、/api/security、/api/usage、/api/routing）（mixin）."""

    def _setup_config_routes(self):
        """配置管理 API."""

        # ── 配置管理 API ──

        @self.app.get("/api/config")
        async def get_config(request: Request):
            """获取当前配置（API Key 脱敏）."""
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            config = self.config_mgr.load()
            data = config.model_dump()
            # 脱敏 API Key
            if data.get("api_key"):
                data["api_key"] = _mask_key(data["api_key"])
                data["has_api_key"] = True
            else:
                data["has_api_key"] = False
            # 脱敏多搜索引擎源的 api_key（同样只保留首尾，保存时用 ... 标记未修改）
            engines = data.get("search_engines")
            if isinstance(engines, list):
                masked = []
                for e in engines:
                    if isinstance(e, dict):
                        e = dict(e)
                        k = e.get("api_key") or ""
                        if k:
                            e["api_key"] = _mask_key(k)
                        masked.append(e)
                    else:
                        masked.append(e)
                data["search_engines"] = masked
            return data

        @self.app.post("/api/config")
        async def save_config(request: Request):
            """保存配置并重建 Agent."""
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            try:
                req = await request.json()
            except Exception:
                return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
            config = self.config_mgr.load()
            warnings = []

            # 更新字段
            if "provider" in req:
                config.provider = req["provider"]
            if "model" in req:
                config.model = req["model"]
            if "base_url" in req:
                # 2026-09-04：落盘前 strip —— 脏 URL（首尾空格/换行）会让正式聊天链路 401；
                # 显式提交空串时回落到凭据区该 provider 已存 URL，治愈"UI 空输入框清空端点"；
                # 凭据区也没有时保留旧值不清空（宁可用旧端点，也不能回落 SDK 官方默认）
                _new_url = str(req["base_url"] or "").strip()
                if not _new_url:
                    _new_url = (config.provider_base_urls or {}).get(
                        config.provider or "", "").strip()
                if _new_url:
                    config.base_url = _new_url
            if "api_key" in req and req["api_key"]:
                # 2026-08-31：传入脱敏回显值（…/***）时回落已存明文，绝不覆盖为掩码
                config.api_key = _resolve_key(str(req["api_key"]), config.api_key or "")
            if "max_turns" in req:
                config.max_turns = int(req["max_turns"])
            if "temperature" in req:
                config.temperature = float(req["temperature"])
            # system_prompt 已禁止自定义：不接受配置更新（避免外部内容破坏前缀缓存）
            if "deep_thinking" in req:
                config.deep_thinking = bool(req["deep_thinking"])
            if "agent_mode" in req:
                mode = str(req["agent_mode"]).strip().lower()
                if mode in ("react", "multi_agent"):
                    config.agent_mode = mode
            if "vision_model" in req:
                config.vision_model = req["vision_model"]
            if "embedding_model" in req:
                config.embedding_model = req["embedding_model"]
            if "image_model" in req:
                config.image_model = req["image_model"]
            # 视觉/图像/Embedding 模型独立厂商（空字符串 = 跟随主 provider）
            if "vision_provider" in req:
                config.vision_provider = str(req["vision_provider"] or "").strip()
            if "image_provider" in req:
                config.image_provider = str(req["image_provider"] or "").strip()
            if "embedding_provider" in req:
                config.embedding_provider = str(req["embedding_provider"] or "").strip()
            if "web_host" in req:
                config.web_host = str(req["web_host"] or "").strip() or "127.0.0.1"
            if "web_port" in req:
                config.web_port = int(req["web_port"])
            if "sandbox_mode" in req:
                config.sandbox_mode = req["sandbox_mode"]
            if "auto_approve" in req:
                config.auto_approve = bool(req["auto_approve"])
            if "allow_app_launch" in req:
                config.allow_app_launch = bool(req["allow_app_launch"])
            if "language" in req:
                lang = str(req["language"]).strip().lower()
                if lang in ("auto", "zh", "en"):
                    config.language = lang
            if "restore_last_session" in req:
                config.restore_last_session = bool(req["restore_last_session"])
            if "restore_last_model" in req:
                config.restore_last_model = bool(req["restore_last_model"])
            if "search_engine" in req:
                config.search_engine = str(req["search_engine"] or "").strip()
            if "search_engines" in req:
                # 多搜索引擎源：{name,type,url,api_key,enabled}
                # api_key 为脱敏值（含 ...）时保留该源已存的 key
                existing = {
                    (str(e.get("name", "")), str(e.get("type", "")), str(e.get("url", ""))): e.get("api_key", "")
                    for e in (config.search_engines or []) if isinstance(e, dict)
                }
                engines = []
                for item in (req["search_engines"] or []):
                    if not isinstance(item, dict):
                        continue
                    etype = str(item.get("type") or "custom").strip().lower()
                    url = str(item.get("url") or "").strip()
                    name = str(item.get("name") or "").strip() or etype
                    api_key = str(item.get("api_key") or "").strip()
                    # 2026-08-31：脱敏回显值回落已存明文
                    api_key = _resolve_key(api_key, existing.get((name, etype, url), ""))
                    enabled = bool(item.get("enabled", True))
                    engines.append({
                        "name": name, "type": etype, "url": url,
                        "api_key": api_key, "enabled": enabled,
                    })
                config.search_engines = engines

            # 保存
            self.config_mgr.save(config)

            # 重建 Agent：api_key 为空时跳过重建（仅保存配置，避免 OpenAI SDK 校验失败返回 500）
            if not (config.api_key or "").strip():
                result = {"status": "ok", "message": "配置已保存（API Key 未配置，稍后在设置中填写后生效）"}
                if warnings:
                    result["warning"] = "; ".join(warnings)
                return result
            try:
                self._rebuild_agent(config)
                result = {"status": "ok", "message": "配置已保存并生效"}
                if warnings:
                    result["warning"] = "; ".join(warnings)
                return result
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # ── 多 Provider API Key 管理 ──

        @self.app.get("/api/config/keys")
        async def list_saved_keys(request: Request):
            """列出已保存 key 的 provider（不泄露明文）+ 当前激活项.

            2026-08-31: 新增 masked_keys（脱敏回显，如 sk-abc***xyz），
            供前端输入框回填 —— 即使 WebView2 localStorage 被清空，
            设置页也能看到"已保存"的 Key，避免每次更新后误以为配置丢失而重填。
            """
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            config = self.config_mgr.load()
            saved = config.provider_keys or {}
            return {
                "keys": self.config_mgr.list_provider_keys(),
                "masked_keys": {p: _mask_key(k) for p, k in saved.items() if k},
                "base_urls": self.config_mgr.list_provider_base_urls(),
                "active": config.provider,
                "active_model": config.model,
                "has_active_key": bool(config.api_key),
            }

        @self.app.put("/api/config/keys/{provider}")
        async def save_provider_key(provider: str, request: Request):
            """保存某 provider 的 API key + base_url（key 加密落盘）；activate=True 时切换为当前激活."""
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            try:
                req = await request.json()
            except Exception:
                return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
            api_key = str(req.get("api_key", "")).strip()
            base_url = str(req.get("base_url", "")).strip() or None
            if not api_key and not base_url:
                return JSONResponse({"error": "api_key 或 base_url 至少填一项"}, status_code=400)
            activate = bool(req.get("activate", True))
            if api_key:
                # 2026-08-31：前端回填的是脱敏值（…/***），必须回落已存明文再保存，
                # 否则掩码会被当作新 key 落盘导致配置失效
                stored_key, _ = self.config_mgr.get_provider_credentials(provider)
                effective_key = _resolve_key(api_key, stored_key)
                if effective_key:
                    self.config_mgr.save_provider_key(
                        provider, effective_key, activate=activate, base_url=base_url
                    )
                else:
                    # 仅回传脱敏值且无已存明文 → 只更新 base_url
                    self.config_mgr.save_provider_base_url(provider, base_url, activate=activate)
            else:
                self.config_mgr.save_provider_base_url(provider, base_url, activate=activate)
            config = self.config_mgr.load()
            if activate:
                # 切换激活后重建 Agent，使新 key/base_url 生效
                try:
                    self._rebuild_agent(config)
                except Exception as e:
                    return JSONResponse({"error": str(e)}, status_code=500)
            return {"status": "ok", "message": "已保存"}

        @self.app.post("/api/config/keys/activate")
        async def activate_saved_key(request: Request):
            """切换当前激活 provider（key 取自已保存的 provider_keys）."""
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            try:
                req = await request.json()
            except Exception:
                return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
            provider = str(req.get("provider", "")).strip()
            if not provider:
                return JSONResponse({"error": "provider 不能为空"}, status_code=400)
            ok = self.config_mgr.activate_provider(
                provider,
                model=req.get("model"),
                base_url=req.get("base_url"),
            )
            if not ok:
                return JSONResponse(
                    {"error": f"provider '{provider}' 未保存 API Key，请先保存"},
                    status_code=404,
                )
            config = self.config_mgr.load()
            try:
                self._rebuild_agent(config)
                return {"status": "ok", "message": f"已切换至 {provider}"}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.get("/api/config/providers")
        async def list_providers():
            """列出支持的 Provider 预设 — 含模型能力标签，按发布时间降序排列."""
            def _sort(models):
                """按 released 降序排列，无日期的排最后."""
                return sorted(models, key=lambda m: m.get("released", "0000-00"), reverse=True)

            raw_providers = [
                {
                    "id": "dashscope",
                    "name": "阿里云 DashScope (百炼)",
                    "default_model": "qwen3.7-plus",
                    "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "models": [
                {"id": "qwen3.8-max", "name": "Qwen3.8 Max (旗舰·最新)", "capabilities": ["text","code","reasoning"], "released": "2026-07"},
                {"id": "qwen3.7-max", "name": "Qwen3.7 Max (旗舰)", "capabilities": ["text","code","reasoning"], "released": "2026-06"},
                {"id": "qwen3.7-plus", "name": "Qwen3.7 Plus (多模态·推荐)", "capabilities": ["text","vision","code"], "released": "2026-06"},
                {"id": "qwen3.7-flash", "name": "Qwen3.7 Flash (快速)", "capabilities": ["text","code"], "released": "2026-06"},
                {"id": "qwen3-max", "name": "Qwen3 Max", "capabilities": ["text","code","reasoning"], "released": "2025-10"},
                {"id": "qwen3.6-plus", "name": "Qwen3.6 Plus (多模态)", "capabilities": ["text","vision","code"], "released": "2025-08"},
                {"id": "qwen3-235b-a22b", "name": "Qwen3 235B (开源旗舰·推理)", "capabilities": ["text","code","reasoning"], "released": "2025-04"},
                {"id": "qwen3-32b", "name": "Qwen3 32B (开源)", "capabilities": ["text","code","reasoning"], "released": "2025-04"},
                {"id": "qwq-plus", "name": "QwQ Plus (推理专用)", "capabilities": ["text","code","reasoning"], "released": "2025-01"},
                {"id": "qwen-plus", "name": "通义千问 Plus (高性价比)", "capabilities": ["text","code"], "released": "2024-05"},
                {"id": "qwen-turbo", "name": "通义千问 Turbo (最快)", "capabilities": ["text"], "released": "2024-05"},
                {"id": "qwen-max", "name": "通义千问 Max", "capabilities": ["text","code"], "released": "2023-11"},
                {"id": "qwen-long", "name": "通义千问 Long (超长文本)", "capabilities": ["text"], "context_length": 10000000, "released": "2024-05"},
                {"id": "qwen3-coder-plus", "name": "Qwen3 Coder Plus (代码专用)", "capabilities": ["text","code"], "released": "2025-04"},
                {"id": "qwen-coder-plus", "name": "通义千问 Coder", "capabilities": ["text","code"], "released": "2024-05"},
                {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro (百炼·最新)", "capabilities": ["text","code","reasoning"], "released": "2026-05"},
                {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash (百炼·快速)", "capabilities": ["text","code"], "released": "2026-05"},
                {"id": "deepseek-v4-flash-0731", "name": "DeepSeek V4 Flash 0731 (百炼)", "capabilities": ["text","code"], "released": "2026-07"},
                {"id": "kimi/kimi-k3", "name": "Kimi K3 (百炼·最新)", "capabilities": ["text","code","reasoning"], "released": "2026-01"},
                {"id": "glm-5.2", "name": "GLM-5.2 (百炼·最新)", "capabilities": ["text","code","reasoning"], "released": "2026-04"},
                {"id": "MiniMax/MiniMax-M3", "name": "MiniMax M3 (百炼)", "capabilities": ["text","code","reasoning"], "released": "2025-12"},
                {"id": "xiaomi/mimo-v2.5-pro", "name": "小米 MiMo v2.5 Pro (百炼)", "capabilities": ["text","code"], "released": "2025-09"},
                ],
                    "vision_models": [
                {"id": "qwen3.7-plus", "name": "Qwen3.7 Plus (推荐)", "released": "2026-06"},
                {"id": "qwen3.6-plus", "name": "Qwen3.6 Plus", "released": "2025-08"},
                {"id": "qwen-vl-max", "name": "通义千问 VL Max (最强)", "released": "2024-08"},
                {"id": "qwen-vl-plus", "name": "通义千问 VL Plus", "released": "2024-08"},
                ],
                    "embedding_models": [
                {"id": "qwen3.7-text-embedding", "name": "Qwen3.7 Text Embedding (最新)", "released": "2026-07"},
                {"id": "qwen3-text-embedding-4b", "name": "Qwen3 Text Embedding 4B (1024维)", "released": "2025-05"},
                {"id": "qwen3-text-embedding-0.6b", "name": "Qwen3 Text Embedding 0.6B (轻量·1024维)", "released": "2025-05"},
                {"id": "text-embedding-v5", "name": "Text Embedding V5 (1024维·最新)", "released": "2025-11"},
                {"id": "text-embedding-v4", "name": "Text Embedding V4 (1024维)", "released": "2025-01"},
                {"id": "text-embedding-v3", "name": "Text Embedding V3 (1024维)", "released": "2024-01"},
                {"id": "text-embedding-v2", "name": "Text Embedding V2 (1536维)", "released": "2023-01"},
                ],
                    "image_models": [
                {"id": "qwen-image-3.0-pro", "name": "Qwen Image 3.0 Pro (最新·高质量)", "released": "2026-03"},
                {"id": "qwen-image-3.0", "name": "Qwen Image 3.0", "released": "2026-03"},
                {"id": "qwen-image-2.0-pro", "name": "Qwen Image 2.0 Pro (推荐)", "released": "2026-04"},
                {"id": "wan2.7-image-pro", "name": "通义万相 2.7 Pro", "released": "2026-01"},
                {"id": "wan2.7-image", "name": "通义万相 2.7", "released": "2026-01"},
                {"id": "qwen-image-max", "name": "Qwen Image Max", "released": "2025-12"},
                {"id": "qwen-image-plus-2026-01-09", "name": "Qwen Image Plus (2026-01)", "released": "2026-01"},
                ],
                },
                {
                    "id": "deepseek",
                    "name": "DeepSeek",
                    "default_model": "deepseek-chat",
                    "default_base_url": "https://api.deepseek.com/v1",
                    "models": [
                {"id": "deepseek-chat", "name": "DeepSeek-V4 (通用对话·最新)", "capabilities": ["text","code"], "released": "2026-05"},
                {"id": "deepseek-reasoner", "name": "DeepSeek-R1 (深度推理·满血)", "capabilities": ["text","code","reasoning"], "released": "2025-01"},
                {"id": "deepseek-v3.1", "name": "DeepSeek-V3.1 (增强版)", "capabilities": ["text","code"], "released": "2025-10"},
                {"id": "deepseek-r1-distill-llama-70b", "name": "DeepSeek-R1 蒸馏 70B (经济)", "capabilities": ["text","reasoning"], "released": "2025-01"},
                {"id": "deepseek-r1-distill-qwen-32b", "name": "DeepSeek-R1 蒸馏 32B (经济)", "capabilities": ["text","reasoning"], "released": "2025-01"},
                ],
                },
                {
                    "id": "zhipu",
                    "name": "智谱 BigModel",
                    "default_model": "glm-5.2",
                    "default_base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "models": [
                {"id": "glm-5.2", "name": "GLM-5.2 (旗舰·最新)", "capabilities": ["text","code","reasoning"], "released": "2026-04"},
                {"id": "glm-5-plus", "name": "GLM-5 Plus (增强)", "capabilities": ["text","code","reasoning"], "released": "2025-12"},
                {"id": "glm-5-flash", "name": "GLM-5 Flash (快速·免费)", "capabilities": ["text","code"], "released": "2025-12"},
                {"id": "glm-5", "name": "GLM-5", "capabilities": ["text","code","reasoning"], "released": "2025-09"},
                {"id": "glm-4-plus", "name": "GLM-4 Plus", "capabilities": ["text","code"], "released": "2024-08"},
                {"id": "glm-4", "name": "GLM-4", "capabilities": ["text","code"], "released": "2024-06"},
                {"id": "glm-4-air", "name": "GLM-4 Air (轻量)", "capabilities": ["text"], "released": "2024-06"},
                {"id": "glm-4-flash", "name": "GLM-4 Flash (免费)", "capabilities": ["text"], "released": "2024-06"},
                {"id": "glm-4-long", "name": "GLM-4 Long (超长文本)", "capabilities": ["text"], "context_length": 128000, "released": "2024-08"},
                {"id": "glm-4v-plus", "name": "GLM-4V Plus (视觉理解·最新)", "capabilities": ["text","vision"], "released": "2024-08"},
                {"id": "glm-4v", "name": "GLM-4V (视觉)", "capabilities": ["text","vision"], "released": "2024-06"},
                ],
                    "vision_models": [
                {"id": "glm-4v-plus", "name": "GLM-4V Plus (推荐)", "released": "2024-08"},
                {"id": "glm-4v", "name": "GLM-4V", "released": "2024-06"},
                ],
                    "embedding_models": [
                {"id": "embedding-3", "name": "智谱 Embedding-3 (2048维)", "released": "2024-08"},
                {"id": "embedding-2", "name": "智谱 Embedding-2 (1024维)", "released": "2023-01"},
                ],
                    "image_models": [
                {"id": "cogview-4", "name": "CogView-4 (最新)", "released": "2025-09"},
                {"id": "cogview-3-plus", "name": "CogView-3 Plus", "released": "2024-12"},
                {"id": "cogview-3-flash", "name": "CogView-3 Flash (免费)", "released": "2024-12"},
                ],
                },
                {
                    "id": "moonshot",
                    "name": "Moonshot (Kimi)",
                    "default_model": "kimi-k3",
                    "default_base_url": "https://api.moonshot.cn/v1",
                    "models": [
                {"id": "kimi-k3", "name": "Kimi K3 (旗舰·最新)", "capabilities": ["text","code","reasoning"], "released": "2026-01"},
                {"id": "kimi-k2-thinking", "name": "Kimi K2 Thinking (推理增强)", "capabilities": ["text","code","reasoning"], "released": "2025-08"},
                {"id": "kimi-k2", "name": "Kimi K2", "capabilities": ["text","code","reasoning"], "released": "2025-07"},
                {"id": "moonshot-v1-8k", "name": "Kimi 8K", "capabilities": ["text","code"], "context_length": 8000, "released": "2023-10"},
                {"id": "moonshot-v1-32k", "name": "Kimi 32K", "capabilities": ["text","code"], "context_length": 32000, "released": "2023-10"},
                {"id": "moonshot-v1-128k", "name": "Kimi 128K (超长上下文)", "capabilities": ["text","code"], "context_length": 128000, "released": "2023-10"},
                {"id": "moonshot-v1-256k", "name": "Kimi 256K (超长上下文)", "capabilities": ["text","code"], "context_length": 256000, "released": "2024-05"},
                ],
                    "embedding_models": [
                {"id": "embedding-1", "name": "Moonshot Embedding (1024维)", "released": "2024-03"},
                ],
                },
                {
                    "id": "volcano",
                    "name": "火山引擎 (豆包)",
                    "default_model": "doubao-1.5-pro-32k",
                    "default_base_url": "https://ark.cn-beijing.volces.com/api/v3",
                    "models": [
                {"id": "doubao-1.5-pro-32k", "name": "豆包 1.5 Pro 32K (最新)", "capabilities": ["text","code"], "context_length": 32000, "released": "2025-01"},
                {"id": "doubao-1.5-pro-256k", "name": "豆包 1.5 Pro 256K (超长)", "capabilities": ["text","code"], "context_length": 256000, "released": "2025-01"},
                {"id": "doubao-1.5-lite-32k", "name": "豆包 1.5 Lite 32K (经济)", "capabilities": ["text"], "context_length": 32000, "released": "2025-01"},
                {"id": "doubao-pro-32k", "name": "豆包 Pro 32K", "capabilities": ["text","code"], "context_length": 32000, "released": "2024-05"},
                {"id": "doubao-pro-128k", "name": "豆包 Pro 128K", "capabilities": ["text","code"], "context_length": 128000, "released": "2024-05"},
                {"id": "doubao-vision-pro", "name": "豆包 Vision Pro (视觉理解)", "capabilities": ["text","vision"], "released": "2024-08"},
                {"id": "doubao-1.5-vision-pro-32k", "name": "豆包 1.5 Vision Pro (最新视觉)", "capabilities": ["text","vision"], "released": "2025-01"},
                ],
                    "vision_models": [
                {"id": "doubao-1.5-vision-pro-32k", "name": "豆包 1.5 Vision Pro (推荐)", "released": "2025-01"},
                {"id": "doubao-vision-pro", "name": "豆包 Vision Pro", "released": "2024-08"},
                ],
                    "embedding_models": [
                {"id": "doubao-embedding-large-text-250715", "name": "豆包 Embedding Large (1024维·最新)", "released": "2025-07"},
                {"id": "doubao-embedding-large-text-241215", "name": "豆包 Embedding Large (1024维)", "released": "2024-12"},
                {"id": "doubao-embedding", "name": "豆包 Embedding (1024维)", "released": "2024-05"},
                ],
                },
                {
                    "id": "openai",
                    "name": "OpenAI",
                    "default_model": "gpt-4o",
                    "default_base_url": "https://api.openai.com/v1",
                    "models": [
                {"id": "gpt-4.1", "name": "GPT-4.1 (最新·多模态)", "capabilities": ["text","vision","code"], "released": "2025-04"},
                {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini (高性价比)", "capabilities": ["text","vision","code"], "released": "2025-04"},
                {"id": "gpt-4.1-nano", "name": "GPT-4.1 Nano (最轻量)", "capabilities": ["text","code"], "released": "2025-04"},
                {"id": "o3", "name": "o3 (深度推理·最强)", "capabilities": ["text","code","reasoning"], "released": "2025-04"},
                {"id": "o4-mini", "name": "o4 Mini (推理·快速)", "capabilities": ["text","code","reasoning"], "released": "2025-04"},
                {"id": "gpt-4o", "name": "GPT-4o (多模态)", "capabilities": ["text","vision","code"], "released": "2024-05"},
                {"id": "gpt-4o-mini", "name": "GPT-4o Mini (高性价比)", "capabilities": ["text","vision","code"], "released": "2024-07"},
                {"id": "o1", "name": "o1 (推理)", "capabilities": ["text","code","reasoning"], "released": "2024-09"},
                {"id": "o1-mini", "name": "o1 Mini (推理)", "capabilities": ["text","reasoning"], "released": "2024-09"},
                {"id": "o3-mini", "name": "o3 Mini (推理)", "capabilities": ["text","code","reasoning"], "released": "2025-01"},
                {"id": "gpt-4-turbo", "name": "GPT-4 Turbo", "capabilities": ["text","vision","code"], "released": "2023-11"},
                ],
                    "vision_models": [
                {"id": "gpt-4.1", "name": "GPT-4.1 (推荐)", "released": "2025-04"},
                {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "released": "2025-04"},
                {"id": "gpt-4o", "name": "GPT-4o", "released": "2024-05"},
                {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "released": "2024-07"},
                ],
                    "embedding_models": [
                {"id": "text-embedding-3-large", "name": "Embedding 3 Large (3072维)", "released": "2024-01"},
                {"id": "text-embedding-3-small", "name": "Embedding 3 Small (1536维)", "released": "2024-01"},
                {"id": "text-embedding-ada-002", "name": "Embedding Ada 002 (1536维·经典)", "released": "2022-12"},
                ],
                    "image_models": [
                {"id": "gpt-image-1", "name": "GPT Image 1 (最新)", "released": "2025-04"},
                {"id": "dall-e-3", "name": "DALL-E 3 (高质量)", "released": "2023-10"},
                {"id": "dall-e-2", "name": "DALL-E 2 (经济)", "released": "2022-11"},
                ],
                },
                {
                    "id": "claude",
                    "name": "Anthropic Claude",
                    "default_model": "claude-sonnet-4-20250514",
                    "default_base_url": "https://api.anthropic.com/v1",
                    "models": [
                {"id": "claude-opus-4-20250514", "name": "Claude Opus 4 (最强·最新)", "capabilities": ["text","vision","code","reasoning"], "released": "2025-05"},
                {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4 (推荐·最新)", "capabilities": ["text","vision","code","reasoning"], "released": "2025-05"},
                {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet", "capabilities": ["text","vision","code"], "released": "2024-10"},
                {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku (快速)", "capabilities": ["text","vision","code"], "released": "2024-10"},
                {"id": "claude-3-opus-20240229", "name": "Claude 3 Opus", "capabilities": ["text","vision","code"], "released": "2024-02"},
                ],
                    "vision_models": [
                {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4 (推荐)", "released": "2025-05"},
                {"id": "claude-opus-4-20250514", "name": "Claude Opus 4", "released": "2025-05"},
                {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet", "released": "2024-10"},
                ],
                },
                {
                    "id": "gemini",
                    "name": "Google Gemini",
                    "default_model": "gemini-2.5-pro",
                    "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "models": [
                {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro (旗舰·最新·推理)", "capabilities": ["text","vision","code","reasoning"], "context_length": 2000000, "released": "2025-03"},
                {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash (快速·推理·最新)", "capabilities": ["text","vision","code","reasoning"], "released": "2025-03"},
                {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash (多模态)", "capabilities": ["text","vision","code"], "released": "2024-12"},
                {"id": "gemini-2.0-flash-thinking-exp", "name": "Gemini 2.0 Thinking (推理实验)", "capabilities": ["text","vision","code","reasoning"], "released": "2024-12"},
                {"id": "gemini-1.5-pro", "name": "Gemini 1.5 Pro (超长上下文)", "capabilities": ["text","vision","code"], "context_length": 2000000, "released": "2024-02"},
                {"id": "gemini-1.5-flash", "name": "Gemini 1.5 Flash", "capabilities": ["text","vision","code"], "released": "2024-02"},
                ],
                    "vision_models": [
                {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro (推荐)", "released": "2025-03"},
                {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "released": "2025-03"},
                {"id": "gemini-2.0-flash", "name": "Gemini 2.0 Flash", "released": "2024-12"},
                ],
                    "embedding_models": [
                {"id": "gemini-embedding-001", "name": "Gemini Embedding (最新·3072维)", "released": "2025-10"},
                {"id": "text-embedding-004", "name": "Gemini Text Embedding (768维)", "released": "2024-12"},
                {"id": "text-embedding-001", "name": "Gemini Text Embedding 001 (旧版)", "released": "2023-12"},
                ],
                },
                {
                    "id": "openrouter",
                    "name": "OpenRouter (聚合)",
                    "default_model": "anthropic/claude-sonnet-4",
                    "default_base_url": "https://openrouter.ai/api/v1",
                    "models": [
                {"id": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4", "capabilities": ["text","vision","code","reasoning"], "released": "2025-05"},
                {"id": "anthropic/claude-opus-4", "name": "Claude Opus 4 (最强)", "capabilities": ["text","vision","code","reasoning"], "released": "2025-05"},
                {"id": "google/gemini-2.5-pro", "name": "Gemini 2.5 Pro", "capabilities": ["text","vision","code","reasoning"], "released": "2025-03"},
                {"id": "google/gemini-2.5-flash", "name": "Gemini 2.5 Flash", "capabilities": ["text","vision","code","reasoning"], "released": "2025-03"},
                {"id": "openai/gpt-4.1", "name": "GPT-4.1", "capabilities": ["text","vision","code"], "released": "2025-04"},
                {"id": "openai/o3", "name": "o3 (推理)", "capabilities": ["text","code","reasoning"], "released": "2025-04"},
                {"id": "qwen/qwen3-235b-a22b", "name": "Qwen3 235B (开源旗舰)", "capabilities": ["text","code","reasoning"], "released": "2025-04"},
                {"id": "deepseek/deepseek-r1", "name": "DeepSeek R1", "capabilities": ["text","code","reasoning"], "released": "2025-01"},
                {"id": "deepseek/deepseek-chat", "name": "DeepSeek V3", "capabilities": ["text","code"], "released": "2024-12"},
                {"id": "meta-llama/llama-3.3-70b-instruct", "name": "Llama 3.3 70B", "capabilities": ["text","code"], "released": "2024-12"},
                {"id": "google/gemini-2.0-flash-exp:free", "name": "Gemini 2.0 Flash (免费)", "capabilities": ["text","vision","code"], "released": "2024-12"},
                ],
                    "embedding_models": [
                {"id": "openai/text-embedding-3-large", "name": "Embedding 3 Large", "released": "2024-01"},
                {"id": "openai/text-embedding-3-small", "name": "Embedding 3 Small", "released": "2024-01"},
                ],
                },
                ]

                    # 按发布时间降序排列所有模型列表
            for p in raw_providers:
                if "models" in p:
                    p["models"] = _sort(p["models"])
                if "vision_models" in p:
                    p["vision_models"] = _sort(p["vision_models"])
                if "embedding_models" in p:
                    p["embedding_models"] = _sort(p["embedding_models"])
                if "image_models" in p:
                    p["image_models"] = _sort(p["image_models"])

            return {"providers": raw_providers}

        @self.app.get("/api/models")
        async def list_chat_models():
            """聊天框模型选择器 — 返回**所有已配置 key 的 provider** 的模型（分组）.

            ★ 2026-09-14：支持跨 provider —— 只要某厂商配置过 API Key，其模型
            就出现在输入框旁的下拉里，可直接切换（后端按 (provider, model)
            构造本轮 LLM，不改全局配置）。
            """
            config = self.config_mgr.load()
            _keys = getattr(config, "provider_keys", None) or {}
            try:
                providers_data = await list_providers()
            except Exception as e:
                logger.warning(f"加载模型预设失败: {e}")
                providers_data = {"providers": []}
            presets = {p.get("id"): p for p in providers_data.get("providers", []) if p.get("id")}

            # 已配置凭据的 provider：provider_keys 有值，或等于当前 provider（用全局 api_key）
            _configured: list[str] = []
            for pid in list(presets.keys()):
                if not pid:
                    continue
                has = bool(str(_keys.get(pid) or "").strip()) or (
                    pid == config.provider and bool(str(config.api_key or "").strip())
                )
                if has:
                    _configured.append(pid)
            if config.provider and config.provider not in _configured and str(config.api_key or "").strip():
                _configured.append(config.provider)

            groups: list[dict] = []
            flat: list[dict] = []
            for pid in _configured:
                preset = presets.get(pid) or {}
                models = [
                    {
                        "id": m["id"],
                        "name": m.get("name", m["id"]),
                        "capabilities": m.get("capabilities", []),
                        "provider": pid,
                    }
                    for m in preset.get("models", [])
                ]
                # 当前 provider 且当前模型不在预设中（自建端点/自定义模型）→ 置顶
                if pid == config.provider and config.model and not any(
                    m["id"] == config.model for m in models
                ):
                    models.insert(0, {
                        "id": config.model,
                        "name": f"{config.model}（当前配置）",
                        "capabilities": [],
                        "provider": pid,
                    })
                if not models:
                    continue
                groups.append({
                    "provider": pid,
                    "label": preset.get("name", pid),
                    "is_current": pid == config.provider,
                    "models": models,
                })
                flat.extend(models)

            return {
                "configured": bool(config.provider and config.api_key),
                "provider": config.provider,
                "current_model": config.model,
                "provider_groups": groups,
                "models": flat,
            }

        @self.app.post("/api/config/test")
        async def test_config(request: Request):
            """测试 LLM 连接."""
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            try:
                req = await request.json()
            except Exception:
                return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
            from scout.llm.providers.registry import create_provider
            try:
                provider = str(req.get("provider", "dashscope") or "").strip() or "dashscope"
                # 方案1 (2026-09-04)：Key 全链路去空白 —— 复制粘贴带空格/换行是 401 高频根因
                api_key = str(req.get("api_key", "") or "").strip()
                base_url = str(req.get("base_url", "") or "").strip()
                model = str(req.get("model", "") or "").strip() or "qwen-plus"
                stored_key, stored_url = self.config_mgr.get_provider_credentials(provider)
                # 2026-08-31：输入框为空 / 回填脱敏值时回落已存明文，避免测试用掩码连接
                # 末尾二次 strip：治愈历史落盘的首尾带空白脏 key
                api_key = _resolve_key(api_key, stored_key).strip()
                # 方案2 (2026-09-04)：base_url 回落顺序 请求传入 → 主配置 → 凭据区。
                # 旧逻辑只回落凭据区 stored_url —— 当 key 来自请求/主配置而 URL 却取
                # 凭据区旧值时形成"新 key + 旧端点"跨区错位 → Authorization failed。
                if not base_url:
                    cfg = self.config_mgr.load()
                    if cfg.provider == provider and (cfg.base_url or "").strip():
                        base_url = cfg.base_url.strip()
                    else:
                        base_url = (stored_url or "").strip()
                llm = create_provider(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    base_url=base_url or None,
                )
                resp = await llm.complete([{"role": "user", "content": "Hi"}])
                # 回显实际 endpoint（含 bare host 自动补 /v1 的规范化结果），
                # key/URL 跨区错位、端点填错一眼可见
                endpoint = str(getattr(llm.client, "base_url", "") or base_url or "")
                return {
                    "status": "ok",
                    "message": f"连接成功: {resp.content[:50]}",
                    "endpoint": endpoint,
                    "model": model,
                }
            except Exception as e:
                err = str(e)
                # 401 类错误附排查提示：令牌无模型权限等问题只能在服务商侧解决，
                # 提示清单把"代码问题"与"配置/权限问题"一刀切开
                low = err.lower()
                if any(k in low for k in ("authorization", "401", "unauthorized",
                                          "invalid api key", "incorrect api key", "api key")):
                    err += ("\n排查: ① Key 是否属于该端点（中转 Key 不能打官方/反之）"
                             "；② 中转令牌是否有该模型权限；③ Base URL 是否缺 /v1"
                             "；④ Key 是否带空格换行")
                return JSONResponse({"error": err}, status_code=400)

    def _setup_security_routes(self):
        """安全策略 API."""

        # ── 安全策略 API ──

        @self.app.get("/api/security")
        async def get_security():
            """获取安全配置."""
            if self._agent and self._agent.security:
                s = self._agent.security
                sandbox_info = {}
                if hasattr(self._agent, 'sandbox_mgr') and self._agent.sandbox_mgr:
                    sandbox_info = self._agent.sandbox_mgr.to_dict()
                return {
                    "auto_approve": s.auto_approve,
                    "allow_tools": list(s.allow_tools),
                    "deny_tools": list(s.deny_tools),
                    "dangerous_patterns": len(DANGEROUS_PATTERNS),
                    "sandbox": sandbox_info,
                }
            return {"auto_approve": True, "allow_tools": [], "deny_tools": [], "sandbox": {}}

        @self.app.post("/api/security")
        async def set_security(req: dict):
            """更新安全配置."""
            if self._agent and self._agent.security:
                s = self._agent.security
                if "auto_approve" in req:
                    s.auto_approve = bool(req["auto_approve"])
                if "allow_tools" in req:
                    s.allow_tools = set(req["allow_tools"])
                if "deny_tools" in req:
                    s.deny_tools = set(req["deny_tools"])
                return {"status": "ok"}
            return JSONResponse({"error": "安全层未启用"}, status_code=400)

    def _setup_usage_routes(self):
        """LLM 用量监控 API."""

        # ── LLM 用量监控 API ──

        @self.app.get("/api/usage/summary")
        async def usage_summary(period: str = "day"):
            """获取 token 消耗统计. period: day/week/month/year."""
            from scout.llm.tracker import LLMUsageTracker
            tracker = LLMUsageTracker()
            return tracker.get_summary(period)

        @self.app.get("/api/usage/daily")
        async def usage_daily(days: int = 30):
            """获取每日 token 消耗趋势."""
            from scout.llm.tracker import LLMUsageTracker
            tracker = LLMUsageTracker()
            return {"data": tracker.get_daily(days)}

        @self.app.get("/api/usage/recent")
        async def usage_recent(limit: int = 20):
            """获取最近的调用记录."""
            from scout.llm.tracker import LLMUsageTracker
            tracker = LLMUsageTracker()
            return {"data": tracker.get_recent(limit)}

        @self.app.get("/api/routing/stats")
        async def routing_stats():
            """路由统计（智能路由已移除 2026-08-14，保留接口返回空）."""
            result = {
                "enabled": False,
                "note": "智能路由/工具缓存已移除（2026-08-14）",
            }
            return result
def _mask_key(key: str) -> str:
    """API Key 脱敏显示：sk-abc123...wxyz；<=12 位一律 ***（防泄露短 key）."""
    if not key:
        return ""
    return key[:8] + "..." + key[-4:] if len(key) > 12 else "***"

def _resolve_key(incoming: str, stored: str = "") -> str:
    """把前端可能回传的脱敏值解析为应落盘的明文.

    规则：
    - 空值 → 保留 stored（不修改）
    - '***'（短 key 掩码）→ 保留 stored
    - 含 '...' 且与 stored 的脱敏形态一致，或长度明显小于真实 key（< 24）→ 保留 stored
    - 其余按新明文处理
    """
    incoming = (incoming or "").strip()
    if not incoming:
        return stored or ""
    if incoming == "***":
        return stored or ""
    if "..." in incoming:
        if stored and incoming == _mask_key(stored):
            return stored
        if len(incoming) < 24:
            return stored or ""
    return incoming

