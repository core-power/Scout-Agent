"""配置/安全/用量路由组（/api/config/*、/api/models、/api/security、/api/usage、/api/routing）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from types import SimpleNamespace
from scout.security.policy import (
    ALLOWED_PATH_PREFIXES,
    DANGEROUS_PATTERNS,
    PERMISSION_MODES,
    SYSTEM_DIRS,
)

# logger 归一：与原 web.py 日志器名一致（行为不变）
import logging

logger = logging.getLogger("scout.adapters.web")

# ── Provider 预设（2026-09-26 移到 scout/llm/model_catalog.py）──
# /api/context/stats 需要 resolve_model_context_length() 按模型查
# context_length 标定上下文圆环分母，预设必须可跨路由复用。
from scout.llm.model_catalog import PROVIDER_PRESETS as _PROVIDER_PRESETS


def _resolve_ctx_with_source(provider: str, model: str) -> tuple[int, str]:
    """查模型上下文窗口，返回 (tokens, source).

    source ∈ {"name", "preset", ""}：
      - "name"  = 由模型名自带的窗口后缀推断（doubao-pro-32k → 32000）
      - "preset"= 命中厂商预设目录里标注的 context_length
      - ""      = 都没命中，由调用方回退默认值
    ★ 2026-09-23：/api/context/stats 据此把上下文圆环的分母跟随当前所选模型
    （如 doubao-pro-32k → 32000），而不是恒用 128000——否则 32k 模型的占用
    百分比会低估 4 倍。预设原内联在 list_providers 里，为复用提升为模块级。
    """
    p = str(provider or "").strip().lower()
    m = str(model or "").strip()
    if not p or not m:
        return 0, ""
    # ① 先按模型名自带的窗口后缀推断（自定义端点/未收录模型也能认出来）：
    #    doubao-pro-32k → 32000、xxx-128k → 128000、gemini-1m → 1000000
    #    注意只匹配 k/m 单位后缀，避免把参数量（qwen3.8-27b 的 27b）误当窗口
    import re as _re
    _mm = _re.search(r"[-_@](\d+(?:\.\d+)?)\s*([km])(?:[-_]|$)", m.lower())
    if _mm:
        try:
            num = float(_mm.group(1))
            unit = _mm.group(2)
            if unit == "k" and 4 <= num <= 10000:
                return int(num * 1000), "name"
            if unit == "m" and 1 <= num <= 10:
                return int(num * 1000000), "name"
        except (TypeError, ValueError):
            pass
    for preset in _PROVIDER_PRESETS:
        if str(preset.get("id", "")).lower() != p:
            continue
        for mm in preset.get("models", []):
            if str(mm.get("id", "")) == m:
                try:
                    return int(mm.get("context_length") or 0), "preset"
                except (TypeError, ValueError):
                    return 0, ""
    return 0, ""


def resolve_model_context_length(provider: str, model: str) -> int:
    """只取窗口数值（无标注返回 0，由调用方回退默认值）— 兼容旧调用方。"""
    return _resolve_ctx_with_source(provider, model)[0]


# ── 视觉判定：唯一决策点在 scout/llm/vision_route.py ──────────────────
# ★ 2026-09-26：capability_key / resolve_model_vision / _VISION_FALLBACKS /
# resolve_vision_fallback / resolve_vision_route 原本都写在本文件（Web 路由层），
# 而引擎与工具层反过来 import 它们 —— 分层倒置；更关键的是聊天附件路径走的是
# 另一套判断（`agent._vision_enabled` 只问"模型能不能看图"、从不问路由），于是
# "设置里配的视觉模型"对用户发的图片基本不起作用，同一轮里两条链路可以给出两个
# 不同答案。现统一收敛到核心层：本文件只转发，保留既有导入路径与测试用的旧函数名。
from scout.llm.vision_route import (
    VISION_FALLBACKS as _VISION_FALLBACKS,
    capability_key,
    native_vision,
    resolve_vision_fallback,
    resolve_vision_route,
)


def resolve_model_vision(provider: str, model: str) -> tuple[bool, str]:
    """兼容旧签名：不传 cfg，仅按 preset 目录 + 名称规则判定能否看图.

    新代码请用 `native_vision(provider, model, cfg)` 或 `resolve_vision_route(cfg)`
    —— 它们会把用户的显式声明与实测探测算进来，本函数不会。
    """
    return native_vision(provider, model)




# ── 思考强度：统一档位 → 各家参数（2026-09-24）──
# 各家参数名完全不同（Qwen 用 thinking_budget、OpenAI o/GPT-5 用 reasoning_effort、
# Claude 用 thinking.budget_tokens、OpenRouter 用 reasoning.effort），发错参数会 400。
# 因此 UI 只暴露统一档位，由这里按厂商翻译成该模型认识的参数。
_THINKING_BUDGETS = {"low": 1024, "medium": 8192, "high": 32768}


def resolve_thinking_style(provider: str, model: str) -> str:
    """判定模型的思考参数风格.

    返回其一：
      qwen          — enable_thinking + thinking_budget（Qwen3 / 百炼 DashScope）
      openai        — reasoning_effort（o1/o3/o4/GPT-5，不接受 enable_thinking）
      anthropic     — thinking.{type,budget_tokens}（Claude；OpenRouter 走 openrouter）
      openrouter    — reasoning.{effort}（OpenRouter 聚合层，模型名含 "/"）
      gemini        — reasoning_effort（Google OpenAI 兼容层）
      bool_only     — 仅 enable_thinking 布尔（DeepSeek / GLM / Kimi / 其它）
    """
    p = str(provider or "").strip().lower()
    m = str(model or "").strip().lower()
    if p == "openrouter" or ("/" in m and not m.startswith("anthropic/") and p not in ("dashscope",)):
        return "openrouter"
    if p in ("claude", "anthropic") or m.startswith("anthropic/") or m.startswith("claude"):
        return "anthropic"
    if p in ("gemini", "google") or m.startswith("gemini") or m.startswith("google/"):
        return "gemini"
    if p == "openai" or m.startswith("gpt-5"):
        return "openai"
    import re as _re2
    if _re2.match(r"^o\d", m):
        return "openai"
    if p in ("dashscope", "qwen", "bailian") or m.startswith("qwen"):
        return "qwen"
    return "bool_only"


def build_thinking_extra(style: str, effort: str) -> tuple[dict, str]:
    """把统一档位翻译成该模型的请求参数.

    返回 (extra_body, 人类可读说明)。extra_body 为空 dict 表示不注入任何参数
    （auto：由模型/服务端默认决定）。
    """
    e = str(effort or "auto").strip().lower()
    if e not in ("off", "low", "medium", "high"):
        return {}, "未注入（auto：沿用模型默认）"
    if style == "openai":
        # o 系列 / GPT-5：只能给 reasoning_effort，且推理无法完全关闭
        lvl = "low" if e == "off" else e
        return {"reasoning_effort": lvl}, f"reasoning_effort={lvl}" + (
            "（该系列无法完全关闭推理，off 已按 low 发送）" if e == "off" else "")
    if style == "anthropic":
        if e == "off":
            return {"thinking": {"type": "disabled"}}, "thinking.type=disabled"
        return (
            {"thinking": {"type": "enabled", "budget_tokens": _THINKING_BUDGETS[e]}},
            f"thinking.type=enabled, budget_tokens={_THINKING_BUDGETS[e]}",
        )
    if style == "openrouter":
        if e == "off":
            return {"reasoning": {"effort": "low"}}, "reasoning.effort=low（该通道不支持完全关闭）"
        return {"reasoning": {"effort": e}}, f"reasoning.effort={e}"
    if style == "gemini":
        lvl = "low" if e == "off" else e
        return {"reasoning_effort": lvl}, f"reasoning_effort={lvl}"
    if style == "qwen":
        if e == "off":
            return {"enable_thinking": False}, "enable_thinking=false"
        return (
            {"enable_thinking": True, "thinking_budget": _THINKING_BUDGETS[e]},
            f"enable_thinking=true, thinking_budget={_THINKING_BUDGETS[e]}",
        )
    # bool_only：DeepSeek / GLM / Kimi / 未收录模型 —— 只有开关，没有 budget
    if e == "off":
        return {"enable_thinking": False}, "enable_thinking=false（该模型不支持强度分档）"
    return (
        {"enable_thinking": True},
        "enable_thinking=true（该模型不支持强度分档，仅开关思维链）",
    )










def resolve_model_capabilities(
    provider: str,
    model: str,
    context_overrides: dict | None = None,
    vision_overrides: dict | None = None,
    effort: str = "auto",
    vision_model: str = "",
    vision_mode: dict | None = None,
    vision_probe: dict | None = None,
    vision_disabled: bool = False,
    vision_provider: str = "",
) -> dict:
    """汇总一个模型的三项可配能力（供设置 UI 与 /api/context/stats 复用）.

    ★ 2026-09-26：同时回传**规范能力键** `capability_key` 与视觉路由 2.0 的完整
    状态（mode / probe / needs_choice / would_be / vision_disabled）。前端此前自己
    拼 `provider+':'+model` 且不做小写归一，provider 大小写不一致时用户的能力开关
    会写进一个永不命中的键却显示"已保存"（缺陷 D8）—— 键规则今后只有后端一份。
    """
    key = capability_key(provider, model)
    ctx_over = (context_overrides or {}).get(key) or 0
    try:
        ctx_over = int(ctx_over)
    except (TypeError, ValueError):
        ctx_over = 0
    if ctx_over > 0:
        ctx, ctx_src = ctx_over, "user"
    else:
        ctx, ctx_src = _resolve_ctx_with_source(provider, model)
    if ctx <= 0:
        ctx, ctx_src = 0, ""

    cfg_obj = SimpleNamespace(
        provider=str(provider or ""),
        model=str(model or ""),
        base_url="",
        vision_provider=str(vision_provider or ""),
        vision_model=str(vision_model or ""),
        model_vision_overrides=dict(vision_overrides or {}),
        model_vision_mode=dict(vision_mode or {}),
        model_vision_probe=dict(vision_probe or {}),
        vision_disabled=bool(vision_disabled),
    )
    # 事实与偏好分开回传：native=该模型能否直收图片；path=当前配置下谁来看图
    vision, vis_src = native_vision(provider, model, cfg_obj)
    route = resolve_vision_route(cfg_obj)

    style = resolve_thinking_style(provider, model)
    extra, applied = build_thinking_extra(style, effort)

    return {
        "provider": str(provider or ""),
        "model": str(model or ""),
        "capability_key": key,  # ★ 前端必须用它写回，不再自行拼接
        "context_length": ctx,
        "context_source": ctx_src,  # user / preset / name / ""（未识别）
        "thinking_style": style,
        "thinking_effort": effort,
        "thinking_extra": extra,
        "thinking_applied": applied,
        "thinking_supported_levels": (
            ["auto", "off", "low", "medium", "high"]
        ),
        # ── 视觉：轴 A 事实 ──
        "vision": vision,
        "vision_source": vis_src,  # probe / override / preset / name / ""
        "vision_native": route.get("native", vision),
        "vision_mode": (vision_mode or {}).get(key, "auto"),
        "vision_probe": (vision_probe or {}).get(key),
        # ── 视觉：轴 B 结果 ──
        "vision_route": route["path"],  # main / fallback / none
        "vision_route_model": (
            route["model"] if route["path"] == "fallback" else ""
        ),
        "vision_route_source": route["source"],  # self/user/override/auto-fallback/off/...
        "vision_reason": route.get("reason", ""),
        "vision_needs_choice": bool(route.get("needs_choice")),
        "vision_would_be": route.get("would_be", ""),
        "vision_disabled": bool(vision_disabled),
        "vision_mode_options": ["auto", "native", "no_main", "off"],
    }


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
            # ── 模型能力（2026-09-24）：上下文窗口 / 思考强度 / 视觉，按模型记忆 ──
            if "model_context_overrides" in req and isinstance(req["model_context_overrides"], dict):
                over = getattr(config, "model_context_overrides", None) or {}
                over = dict(over)
                for k, v in req["model_context_overrides"].items():
                    try:
                        n = int(v)
                    except (TypeError, ValueError):
                        continue
                    if n > 0:
                        over[str(k)] = n
                    else:
                        over.pop(str(k), None)  # 0/空 = 清除覆盖，回到自动识别
                config.model_context_overrides = over
            if "model_vision_overrides" in req and isinstance(req["model_vision_overrides"], dict):
                vover = dict(getattr(config, "model_vision_overrides", None) or {})
                for k, v in req["model_vision_overrides"].items():
                    if v is None:
                        vover.pop(str(k), None)  # null = 清除覆盖，回到自动判断
                    else:
                        vover[str(k)] = bool(v)
                config.model_vision_overrides = vover
            # ── 视觉路由 2.0（2026-09-26）：模式表 / 探测结果 / 全局开关 ──
            # mode 是 overrides 布尔表的后继（一个布尔塞不下"主模型收不收图"与
            # "要不要视觉"两件事），写 mode 时清掉同键的旧布尔，避免两个来源打架。
            if "model_vision_mode" in req and isinstance(req["model_vision_mode"], dict):
                vmodes = dict(getattr(config, "model_vision_mode", None) or {})
                vover2 = dict(getattr(config, "model_vision_overrides", None) or {})
                for k, v in req["model_vision_mode"].items():
                    key = str(k)
                    if v is None or str(v).strip().lower() in ("", "auto"):
                        vmodes.pop(key, None)      # auto/空 = 交回自动判定
                        vover2.pop(key, None)      # 同时清除旧布尔覆盖
                        continue
                    val = str(v).strip().lower()
                    if val in ("native", "no_main", "off"):
                        vmodes[key] = val
                        vover2.pop(key, None)
                config.model_vision_mode = vmodes
                config.model_vision_overrides = vover2
            if "model_vision_probe" in req and isinstance(req["model_vision_probe"], dict):
                vprobe = dict(getattr(config, "model_vision_probe", None) or {})
                for k, v in req["model_vision_probe"].items():
                    key = str(k)
                    if v is None:
                        vprobe.pop(key, None)      # null = 作废该模型的探测结果
                    else:
                        vprobe[key] = bool(v)
                config.model_vision_probe = vprobe
            if "vision_disabled" in req:
                config.vision_disabled = bool(req["vision_disabled"])
            if "reasoning_effort" in req:
                eff = str(req["reasoning_effort"] or "auto").strip().lower()
                if eff in ("auto", "off", "low", "medium", "high"):
                    config.reasoning_effort = eff
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
            if "permission_mode" in req:
                # 权限开关（输入框）：ask=高危询问 / auto=全部放行 / strict=逐条询问
                mode = str(req["permission_mode"] or "ask").strip().lower()
                if mode in PERMISSION_MODES:
                    config.permission_mode = mode
                    # 立即生效：同步运行时安全层，无需重启 Agent
                    if self._agent and getattr(self._agent, "security", None):
                        self._agent.security.set_permission_mode(mode)
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

            raw_providers = _PROVIDER_PRESETS

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

        @self.app.get("/api/models/capabilities")
        async def model_capabilities(request: Request):
            """查询某模型的三项可配能力（上下文窗口 / 思考强度 / 视觉）.

            ★ 2026-09-24：设置面板「模型能力」卡片据此渲染，并把用户手动覆盖
            （model_context_overrides / model_vision_overrides / reasoning_effort）
            一并算进去 —— 用户改完立即能看到"实际生效值 + 来源"。
            """
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            config = self.config_mgr.load()
            provider = request.query_params.get("provider")
            model = request.query_params.get("model")
            if provider is None:
                provider = config.provider
            if model is None:
                model = config.model
            return resolve_model_capabilities(
                provider,
                model,
                context_overrides=getattr(config, "model_context_overrides", None) or {},
                vision_overrides=getattr(config, "model_vision_overrides", None) or {},
                effort=str(getattr(config, "reasoning_effort", "auto") or "auto").lower(),
                vision_model=str(getattr(config, "vision_model", "") or ""),
                # 视觉路由 2.0：模式/探测/全局开关也要参与渲染，否则 UI 显示的还是
                # 旧布尔表推导的结论，用户改了 mode 却看不到变化
                vision_mode=getattr(config, "model_vision_mode", None) or {},
                vision_probe=getattr(config, "model_vision_probe", None) or {},
                vision_disabled=bool(getattr(config, "vision_disabled", False)),
                vision_provider=str(getattr(config, "vision_provider", "") or ""),
            )

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
                provider, api_key, base_url, model = self._resolve_llm_target(req)
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

        # ── 视觉能力实测（2026-09-26）──────────────────────────────
        @self.app.post("/api/models/probe-vision")
        async def probe_vision_capability(request: Request):
            """实测「这个模型能不能看图」—— 只在用户点探测按钮时执行，绝不自动跑.

            为什么需要：自定义/中转网关模型从名字判断不了视觉能力，而猜错的代价
            不对称 —— 不少网关会**静默丢掉 image_url 字段**并正常回话，此时按"支持"
            使用，模型就是对着看不见的图编内容。所以探测用本机生成的随机数字图做
            可验证问答（实现见 scout/llm/vision_probe），HTTP 200 本身不算结论。
            """
            if not self._require_auth(request):
                return JSONResponse({"error": "未授权"}, status_code=401)
            try:
                req = await request.json()
            except Exception:
                return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)

            import os as _os

            provider, api_key, base_url, model = self._resolve_llm_target(req)
            if not api_key:
                return JSONResponse(
                    {"error": f"provider {provider} 还没有可用 API Key，无法探测"},
                    status_code=400,
                )
            try:
                timeout = float(_os.getenv("SCOUT_VISION_PROBE_TIMEOUT", "25") or 25)
            except ValueError:
                timeout = 25.0

            from scout.llm.vision_probe import probe_vision

            res = await probe_vision(api_key, base_url, model, timeout=timeout)
            key = capability_key(provider, model)
            written: bool | None = None
            cfg = self.config_mgr.load()
            # verdict 为 None（网络/鉴权/端点问题、或 200 但读不出内容）时**不落盘**：
            # 一次断网就把模型永久标成"不支持视觉"是不可接受的副作用。
            if res.get("verdict") is not None:
                probes = dict(getattr(cfg, "model_vision_probe", None) or {})
                written = bool(res["verdict"])
                probes[key] = written
                cfg.model_vision_probe = probes
                self.config_mgr.save(cfg)

            caps = resolve_model_capabilities(
                provider,
                model,
                context_overrides=getattr(cfg, "model_context_overrides", None) or {},
                vision_overrides=getattr(cfg, "model_vision_overrides", None) or {},
                effort=str(getattr(cfg, "reasoning_effort", "auto") or "auto").lower(),
                vision_model=str(getattr(cfg, "vision_model", "") or ""),
                vision_mode=getattr(cfg, "model_vision_mode", None) or {},
                vision_probe=getattr(cfg, "model_vision_probe", None) or {},
                vision_disabled=bool(getattr(cfg, "vision_disabled", False)),
                vision_provider=str(getattr(cfg, "vision_provider", "") or ""),
            )
            return {
                "status": "ok",
                "result": res.get("result"),
                "verdict": res.get("verdict"),
                "written": written,
                "detail": res.get("detail", ""),
                "rounds": res.get("rounds", []),
                "endpoint": base_url,
                "capability_key": key,
                "capabilities": caps,
            }

    def _resolve_llm_target(self, req: dict) -> tuple[str, str, str, str]:
        """按「测试连接」那一套规则解析 (provider, api_key, base_url, model).

        ★ 2026-09-26 抽出复用：凭证解析的每个坑都是踩出来的（Key 首尾空白是 401
        高频根因、前端可能回填脱敏值、base_url 必须按 请求→主配置→凭据区 三级回落，
        否则会出现"新 key + 旧端点"跨区错位）。视觉探测与连接测试必须共用一份，
        不然两处会漂移 —— 而探测还要把结论落盘，漂移的代价更大。
        """
        provider = str(req.get("provider", "dashscope") or "").strip() or "dashscope"
        api_key = str(req.get("api_key", "") or "").strip()
        base_url = str(req.get("base_url", "") or "").strip()
        model = str(req.get("model", "") or "").strip() or "qwen-plus"
        stored_key, stored_url = self.config_mgr.get_provider_credentials(provider)
        # 输入框为空 / 回填脱敏值时回落已存明文；末尾二次 strip 治愈历史脏 key
        api_key = _resolve_key(api_key, stored_key).strip()
        if not base_url:
            cfg = self.config_mgr.load()
            if cfg.provider == provider and (cfg.base_url or "").strip():
                base_url = cfg.base_url.strip()
            else:
                base_url = (stored_url or "").strip()
        return provider, api_key, base_url, model

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
                    "permission_mode": getattr(s, "permission_mode", "ask"),
                    "allow_tools": list(s.allow_tools),
                    "deny_tools": list(s.deny_tools),
                    "dangerous_patterns": len(DANGEROUS_PATTERNS),
                    "sandbox": sandbox_info,
                }
            return {
                "auto_approve": True,
                "permission_mode": "ask",
                "allow_tools": [],
                "deny_tools": [],
                "sandbox": {},
            }

        @self.app.post("/api/security")
        async def set_security(req: dict):
            """更新安全配置."""
            if self._agent and self._agent.security:
                s = self._agent.security
                if "auto_approve" in req:
                    s.auto_approve = bool(req["auto_approve"])
                if "permission_mode" in req:
                    mode = str(req["permission_mode"] or "ask").strip().lower()
                    if mode in PERMISSION_MODES:
                        s.set_permission_mode(mode)
                if "allow_tools" in req:
                    s.allow_tools = set(req["allow_tools"])
                if "deny_tools" in req:
                    s.deny_tools = set(req["deny_tools"])
                return {"status": "ok"}
            return JSONResponse({"error": "安全层未启用"}, status_code=400)

    def _setup_usage_routes(self):
        """LLM 用量监控 API."""

        # ── LLM 用量监控 API ──

        # ★ 2026-09-25：sync sqlite3 查询（~100-200ms）不能放在 async def 里——
        # 会阻塞整个事件循环（LLM 流式/WebSocket 全卡住）。改成普通 def，
        # FastAPI 自动放线程池执行。
        @self.app.get("/api/usage/summary")
        def usage_summary(period: str = "day"):
            """获取 token 消耗统计. period: day/week/month/year."""
            from scout.llm.tracker import LLMUsageTracker
            tracker = LLMUsageTracker()
            return tracker.get_summary(period)

        @self.app.get("/api/usage/daily")
        def usage_daily(days: int = 30):
            """获取每日 token 消耗趋势."""
            from scout.llm.tracker import LLMUsageTracker
            tracker = LLMUsageTracker()
            return {"data": tracker.get_daily(days)}

        @self.app.get("/api/usage/recent")
        def usage_recent(limit: int = 20):
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

