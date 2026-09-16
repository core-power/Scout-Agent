"""Web API 适配器 — OpenAI 兼容 API + SSE 流式 + WebSocket.

借鉴 OpenClaw 的 /v1/chat/completions 设计，使 Scout 可被任何 OpenAI 兼容客户端调用。
"""

# 注意：不使用 from __future__ import annotations。
# 本模块含 FastAPI 闭包路由（如 /a2a/tasks/send 的 body 参数），字符串化注解
# 会导致 pydantic ForwardRef 解析失败（class-not-fully-defined）。
# W1 拆分（2026-09-14）：路由组 mixin —— auth/a2a/voice
from scout.adapters.web.routes.a2a import A2aRoutes
from scout.adapters.web.routes.auth import AuthRoutes
from scout.adapters.web.routes.voice import VoiceRoutes
from scout.adapters.web.routes.automation import AutomationRoutes
from scout.adapters.web.routes.channels import ChannelRoutes
from scout.adapters.web.routes.sessions import SessionRoutes
from scout.adapters.web.routes.memory import MemoryRoutes
from scout.adapters.web.routes.knowledge import KnowledgeRoutes
from scout.adapters.web.routes.goals import GoalRoutes
from scout.adapters.web.callbacks import WebCallbacks
from scout.adapters.web.routes.config import ConfigRoutes
from scout.adapters.web.routes.skills import SkillRoutes
from scout.adapters.web.routes.observability import ObservabilityRoutes
from scout.adapters.web.routes.integrations import IntegrationRoutes
from scout.adapters.web.routes.chat import ChatRoutes
from scout.adapters.web.routes.ws import WsRoutes

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel as PydanticModel
from sse_starlette.sse import EventSourceResponse

from scout.core.callbacks import Callbacks, NullCallbacks
from scout.core.types import Message, Role, Session
from scout.engine.agent import Agent
from scout.tools.registry import ToolRegistry
from scout.config import ConfigManager, LLMConfig
from scout.security.policy import ALLOWED_PATH_PREFIXES, DANGEROUS_PATTERNS, SYSTEM_DIRS
from scout.security.auth import AuthManager, rotate_secret, verify_token

logger = logging.getLogger(__name__)


# ── API Key 脱敏 / 回填解析 ────────────────────────────────
# 2026-08-31：前端输入框回填的是脱敏值（sk-abc...wxyz 或 ***），
# 保存/测试时若收到脱敏值必须回落已存明文，绝不能把掩码当新 key 落盘。





# ── 请求/响应模型 ──────────────────────────────────────────







# ── Web 回调 — 通过 SSE/WebSocket 推送事件 ─────────────────



# ── Web 适配器 ──────────────────────────────────────────────

class WebAdapter(
    AuthRoutes, A2aRoutes, VoiceRoutes, ChannelRoutes, AutomationRoutes,
    SessionRoutes, MemoryRoutes, KnowledgeRoutes, GoalRoutes,
    ConfigRoutes, SkillRoutes, ObservabilityRoutes, IntegrationRoutes,
    ChatRoutes, WsRoutes,
):
    """Web API 适配器 — 挂载到 FastAPI app."""

    def __init__(self, app: FastAPI, agent: Agent | None = None, port: int = 8848):
        self.app = app
        self._agent = agent
        self.port = port
        self._sessions: dict[str, Session] = {}
        self.config_mgr = ConfigManager()
        self.auth_mgr = AuthManager()
        self._webhooks_path = _SCOUT_DATA_DIR / "webhooks.json"
        self._webhooks_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 渠道管理器 — 单例模式
        from scout.adapters.channel_manager import ChannelManager
        self._channel_manager = ChannelManager()
        # 加载已保存的渠道配置
        saved_config = self._channel_manager.load_config()
        if saved_config:
            self._channel_manager = ChannelManager.from_config(saved_config)

        # 启动时根据已保存配置重建 Agent（加载智能路由等设置）
        # ★ 2026-09-01 修复：此前 `if agent is not None:` 导致绿色版 launcher
        # （create_web_app() 不传 agent）启动后 self._agent 恒为 None ——
        # 配置文件里明明有 API Key 也无法对话，用户每次重启/更新后都必须
        # 到设置里点一次"保存"才能使用。现改为无条件加载配置，有 key 即重建。
        try:
            config = self.config_mgr.load()
            if config.api_key:
                self._rebuild_agent(config)
        except Exception as e:
            logger.warning(f"Failed to load config and rebuild agent: {e}")

        # ★ 断裂点修复 2: WebSocket 连接管理 + EventBus 订阅
        # ★ 2026-09-01：后台任务集 — create_task 必须持引用,
        # 否则事件循环仅持弱引用,任务可能在执行中被 GC 静默丢弃
        self._bg_tasks: set = set()
        self._active_ws_connections: set = set()
        self._pending_confirmations: dict[str, asyncio.Future] = {}  # Human-in-the-Loop 确认请求
        self._setup_event_bus_subscription()

        # 通知分发器 — 跨渠道主动推送（IM/邮件），复用 channel_manager
        self._setup_notify_dispatcher()

        # 文件系统监听 — 主动感知目录变化（复用 bus，事件驱动自动化）
        self._setup_file_watcher()

        # 语音模块 — 按环境变量构建 ASR/TTS（无配置时为空处理器，不影响启动）
        from scout.voice.factory import build_voice_handler
        self._voice_handler = build_voice_handler()

        self._setup_routes()

    # ── Session store 解析 ──
    # ★ 2026-08-29：exe 启动初期 self._agent 可能为 None（create_web_app() 不传 agent，
    # 需等配置加载后才 rebuild）。若会话历史读取依赖 self._agent，会导致启动瞬间
    # 误判"会话不存在"→ 前端反复弹"该对话不存在，已创建新对话"。这里统一兜底到
    # 全局 session store，保证无论 agent 是否就绪都能正确读写历史会话。
    def _session_store(self):
        if self._agent and self._agent.session_store:
            return self._agent.session_store
        try:
            from scout.session.store import get_session_store
            return get_session_store()
        except Exception:  # noqa: BLE001
            logger.warning("get_session_store() 失败，会话存储不可用", exc_info=True)
            return None

    # ── Webhook 存储 ──

    def _load_webhooks(self) -> list[dict]:
        if self._webhooks_path.exists():
            with open(self._webhooks_path, encoding="utf-8") as f:
                return json.load(f)
        return []

    def _save_all_webhooks(self, hooks: list[dict]) -> None:
        with open(self._webhooks_path, "w", encoding="utf-8") as f:
            json.dump(hooks, f, indent=2, ensure_ascii=False)

    def _get_webhooks(self) -> list[dict]:
        return self._load_webhooks()

    def _find_webhook(self, token: str) -> dict | None:
        for h in self._load_webhooks():
            if h.get("id") == token:
                return h
        return None

    def _save_webhook(self, webhook: dict) -> None:
        hooks = self._load_webhooks()
        # upsert
        found = False
        for i, h in enumerate(hooks):
            if h.get("id") == webhook["id"]:
                hooks[i] = webhook
                found = True
                break
        if not found:
            hooks.append(webhook)
        self._save_all_webhooks(hooks)

    def _delete_webhook(self, token: str) -> None:
        hooks = [h for h in self._load_webhooks() if h.get("id") != token]
        self._save_all_webhooks(hooks)

    # ── 自动化执行器（P0 无人值守运行栈，2026-08-13）──

    def _get_automation_runner(self):
        """懒加载 AutomationRunner — Agent 重建后自动重新绑定."""
        if not self._agent:
            return None
        runner = getattr(self, "_automation_runner", None)
        if runner is None or getattr(runner, "agent", None) is not self._agent:
            try:
                from scout.automation.runner import AutomationRunner
                runner = AutomationRunner(self._agent)
                runner.attach()  # 挂载触发器事件订阅
                self._automation_runner = runner
            except Exception as e:
                logger.warning(f"AutomationRunner 初始化失败: {e}")
                return None
        return runner

    # ── 回合用量统计：聚合某 session 在时间窗内的 LLM 调用（token/缓存/耗时）──
    def _collect_ws_usage(self, session_id: str, start_ts: float) -> dict:
        """查询 usage.db 中该 session 在 [start_ts, now] 内的调用聚合.

        返回 {tokens, prompt, completion, cached, cache_hit_rate, calls, avg_latency_ms}。
        失败时返回空统计（不阻塞主流程）。
        """
        try:
            from scout.llm.tracker import token_tracker
            from datetime import datetime
            rows = token_tracker._query(
                """SELECT
                       SUM(prompt_tokens) as prompt, SUM(completion_tokens) as completion,
                       SUM(total_tokens) as total, SUM(cached_tokens) as cached,
                       COUNT(*) as calls, AVG(latency_ms) as avg_latency
                   FROM llm_usage
                   WHERE session_id = ? AND timestamp >= ?""",
                (session_id, datetime.fromtimestamp(start_ts).isoformat()),
            )
            r = rows[0] if rows else {}
            prompt = r.get("prompt") or 0
            cached = r.get("cached") or 0
            rate = round(cached / prompt, 4) if prompt else 0.0
            source = "api"
            # ── 兜底：API 未返回真实 cached（DashScope 流式常见）时，
            #    用本地前缀稳定率推断缓存命中率（2026-08-16）──
            if rate == 0.0:
                try:
                    from scout.llm.prompt_cache import get_prompt_cache_optimizer
                    local_rate = get_prompt_cache_optimizer().get_session_hit_ratio(session_id)
                    if local_rate is not None:
                        rate = local_rate
                        source = "local"
                except Exception:
                    pass
            result = {
                "tokens": int(r.get("total") or 0),
                "prompt": int(prompt),
                "completion": int(r.get("completion") or 0),
                "cached": int(cached),
                "cache_hit_rate": rate,
                "calls": int(r.get("calls") or 0),
                "avg_latency_ms": int(r.get("avg_latency") or 0),
                "cache_source": source,  # api=上游真实值 / local=本地前缀稳定性推断
            }
            # 该 session 的历史累计 token（不限时间窗），供重进会话后仍能看到
            # 完整消耗；本轮消耗见 tokens 字段。
            try:
                rows_all = token_tracker._query(
                    """SELECT SUM(total_tokens) as total, COUNT(*) as calls
                       FROM llm_usage WHERE session_id = ?""",
                    (session_id,),
                )
                ra = rows_all[0] if rows_all else {}
                result["session_total_tokens"] = int(ra.get("total") or 0)
                result["session_total_calls"] = int(ra.get("calls") or 0)
            except Exception:
                result["session_total_tokens"] = result["tokens"]
                result["session_total_calls"] = result["calls"]
            return result
        except Exception as e:
            import traceback
            logging.getLogger(__name__).warning(f"_collect_ws_usage 查询失败: {e!r}\n{traceback.format_exc()}")
            return {"tokens": 0, "calls": 0, "cache_hit_rate": 0.0, "avg_latency_ms": 0}

    # ── 聊天模型选择（2026-08-13）──

    def _get_chat_llm(self, model: str, provider: str = ""):
        """为指定 (provider, model) 创建/复用 LLM provider（聊天框模型切换用）.

        ★ 2026-09-14 支持跨 provider：用 ``provider_keys`` / ``provider_base_urls``
        中该 provider 的凭据。此前固定用当前配置的 provider —— 在输入框里切到
        别家厂商的模型时，实际仍走原 provider（静默取错模型/报 404）。
        按 (provider, model) 缓存（最多 5 个，LRU）。返回 None 表示凭据缺失或与
        全局配置完全一致（无需覆盖）。
        """
        if not model:
            return None
        config = self.config_mgr.load()
        _p = (provider or config.provider or "").strip()
        _keys = getattr(config, "provider_keys", None) or {}
        _urls = getattr(config, "provider_base_urls", None) or {}
        # 该 provider 的 key/base_url；当前 provider 回退全局字段（防御性去空白）
        _key = str(_keys.get(_p) or "").strip() or (
            str(config.api_key or "").strip() if _p == config.provider else ""
        )
        _base = str(_urls.get(_p) or "").strip() or (
            str(config.base_url or "").strip() if _p == config.provider else ""
        )
        if not _p or not _key:
            logger.warning("聊天模型切换缺少凭据: provider=%s model=%s", _p, model)
            return None
        if _p == config.provider and model == config.model:
            return None  # 与全局配置完全一致 → 直接用主 agent 的 llm

        if not hasattr(self, "_chat_llm_cache"):
            self._chat_llm_cache = {}  # key: (provider, model) -> llm
        cache_key = (_p, model)
        cached = self._chat_llm_cache.get(cache_key)
        if cached:
            # LRU: 移到最近使用
            self._chat_llm_cache.pop(cache_key)
            self._chat_llm_cache[cache_key] = cached
            return cached

        try:
            from scout.llm.providers.registry import create_provider
            llm = create_provider(
                provider=_p,
                api_key=_key,
                model=model,
                base_url=_base,
                max_retries=config.max_retries,
                retry_backoff_base=config.retry_backoff_base,
                retry_backoff_max=config.retry_backoff_max,
                stream_timeout=config.stream_timeout,
                request_timeout=config.request_timeout,
            )
            self._chat_llm_cache[cache_key] = llm
            # 缓存上限（LRU 淘汰最旧）
            while len(self._chat_llm_cache) > 5:
                oldest = next(iter(self._chat_llm_cache))
                self._chat_llm_cache.pop(oldest, None)
            return llm
        except Exception as e:
            logger.warning(f"创建模型 provider 失败 ({model}): {e}")
            return None

    @property
    def agent(self) -> Agent | None:
        return self._agent

    def _rebuild_agent(self, config: LLMConfig) -> Agent:
        """根据新配置重建 Agent."""
        from scout.llm.providers.registry import create_provider
        # 重试/超时参数（从配置读取，统一注入所有 provider）
        retry_kwargs = {
            "max_retries": config.max_retries,
            "retry_backoff_base": config.retry_backoff_base,
            "retry_backoff_max": config.retry_backoff_max,
            "stream_timeout": config.stream_timeout,
            "request_timeout": config.request_timeout,
        }
        # 2026-09-04：防御性去空白 —— 治愈历史落盘的脏 key/URL（首尾空白是 401 根因之一）；
        # 顶层 base_url/api_key 为空时回落到"服务商凭据区"已存值 —— 治愈 UI 保存空输入框
        # 把顶层端点清空的场景（否则自定义 EAS/中转端点会被替换成 SDK 官方默认 -> ReadTimeout）；
        # 与 test_config 方案2（请求传入 -> 主配置 -> 凭据区）保持一致
        provider = (config.provider or "").strip()
        api_key = (config.api_key or "").strip()
        if not api_key:
            api_key = (config.provider_keys or {}).get(provider, "").strip()
        base_url = (config.base_url or "").strip()
        if not base_url:
            base_url = (config.provider_base_urls or {}).get(provider, "").strip()
        base_url = base_url or None
        llm = create_provider(
            provider=provider,
            api_key=api_key,
            model=config.model,
            base_url=base_url,
            **retry_kwargs,
        )
        # 模型 Fallback：支持多级 fallback 链（从配置读取）
        fallback_models = config.fallback_models or []
        if not fallback_models and config.fallback_model:
            fallback_models = [config.fallback_model]
        
        if fallback_models:
            from scout.llm.providers.fallback import FallbackProvider
            fallback_llms = []
            for fb_model in fallback_models:
                fb_llm = create_provider(
                    provider=provider,
                    api_key=api_key,
                    model=fb_model,
                    base_url=base_url,
                    **retry_kwargs,
                )
                fallback_llms.append(fb_llm)
            llm = FallbackProvider(primary=llm, fallback=fallback_llms)

        # 双模型已移除（2026-08-14），单模型运行
        # system_prompt 已禁止自定义（2026-08-25）：统一使用内置模板，保证前缀稳定可缓存

        # Embedding：按 embedding_model 配置选择 provider（local/API/关闭）
        # 支持独立厂商（embedding_provider 非空且 ≠ 主 provider 时，用该厂商已保存的
        # key/base_url，与 vision/image 的独立厂商机制一致；未保存 key 时回退主配置）
        from scout.memory.vector.embeddings import select_embedding_provider, EMBEDDING_DISABLED
        try:
            emb_provider = (config.embedding_provider or "").strip()
            # 复用上方清洗后的 key/url（脏值 401 防御统一收口）
            emb_key, emb_base = api_key, base_url or ""
            if emb_provider and emb_provider != config.provider:
                _k, _u = self.config_mgr.get_provider_credentials(emb_provider)
                if _k:
                    emb_key, emb_base = _k, _u or emb_base
                else:
                    logging.getLogger(__name__).warning(
                        f"Embedding 独立厂商 '{emb_provider}' 未保存 API Key，回退主配置"
                    )
            embedding_provider = select_embedding_provider(
                embedding_model=config.embedding_model,
                api_key=emb_key,
                base_url=emb_base,
            )
            if embedding_provider is None:
                embedding_provider = EMBEDDING_DISABLED  # 显式关闭，禁止 Agent 兜底回本地
        except Exception as _emb_err:
            logging.getLogger(__name__).warning(
                f"Embedding provider 初始化失败，退化为纯文本检索: {_emb_err}"
            )
            embedding_provider = EMBEDDING_DISABLED

        # ── 记忆工程化注入（E4）：会话结束自动沉淀 + 跨会话组装 + 压缩前抽取 ──
        # 此前未注入 → 记忆只召回不沉淀，跨会话记忆长期为空（"记忆丢失"根因之一）。
        # 任何组件失败都只告警，不影响 Agent 启动（记忆退化为仅靠手动 memory_save）。
        memory_extractor = None
        context_assembler = None
        memory_flush = None
        try:
            from scout.context import ContextAssembler, MemoryFlush, SessionMemoryExtractor
            from scout.memory.store import get_memory_store
            from scout.session.store import get_session_store

            _mstore = get_memory_store()
            _sstore = get_session_store()
            memory_extractor = SessionMemoryExtractor(memory_store=_mstore, llm=llm)
            context_assembler = ContextAssembler(memory_store=_mstore, session_store=_sstore)
            memory_flush = MemoryFlush(llm=llm, memory_store=_mstore)
            logger.info("记忆工程化已注入: extractor / assembler / flush")
        except Exception as _mem_err:
            logger.warning(
                f"记忆工程化注入失败（跨会话记忆退化为手动 memory_save）: {_mem_err}"
            )

        new_agent = Agent(
            llm=llm,
            max_turns=config.max_turns or 60,  # 2026-08-31：0 值兜底，防止旧配置缺省导致预算 0 步立即耗尽
            max_loop_seconds=config.max_loop_seconds,
            temperature=config.temperature,
            deep_thinking=config.deep_thinking,
            agent_mode=config.agent_mode,
            embedding_provider=embedding_provider,
            auto_approve=config.auto_approve,
            language=config.language,
            memory_extractor=memory_extractor,
            context_assembler=context_assembler,
            memory_flush=memory_flush,
            # ── 智能路由/双模型已移除（2026-08-14）──
        )
        self._agent = new_agent

        # 应用沙箱配置
        if new_agent.sandbox_mgr:
            new_agent.sandbox_mgr.set_mode(config.sandbox_mode or "off")

        # 初始化星夜凝萃
        try:
            from scout.automation.starlight import init_starlight
            init_starlight(new_agent)
        except Exception as e:
            logger.warning(f"Failed to initialize starlight distillation: {e}")

        return new_agent

    def _setup_event_bus_subscription(self):
        """订阅 EventBus 的 notification 事件，广播到所有 WebSocket 连接."""
        try:
            from scout.bus.hub import bus
            bus.on("notification", self._on_notification_event)
        except Exception as e:
            logger.warning(f"Failed to subscribe to EventBus: {e}")

    async def _on_notification_event(self, data: dict):
        """EventBus 回调：广播通知到所有活跃 WebSocket 连接."""
        await self.broadcast_notification(data)

    def _setup_notify_dispatcher(self):
        """初始化通知分发器 — 订阅 notification 事件并跨渠道推送（IM/邮件）."""
        try:
            from scout.notify.dispatcher import get_dispatcher
            dispatcher = get_dispatcher(self._channel_manager)
            dispatcher.attach_to_bus()
            self._notify_dispatcher = dispatcher
        except Exception as e:
            logger.warning(f"通知分发器初始化失败: {e}")
            self._notify_dispatcher = None

    def _setup_file_watcher(self):
        """初始化文件系统监听器 — 感知目录变化并广播 fs.event 事件.

        监听任务在 FastAPI lifespan 启动时统一拉起（见 server.py），
        此处仅创建实例并注入 bus。
        """
        try:
            from scout.bus.hub import bus as event_bus
            from scout.automation.watcher import get_watcher
            watcher = get_watcher(bus=event_bus)
            self._file_watcher = watcher
        except Exception as e:
            logger.warning(f"文件监听器初始化失败: {e}")
            self._file_watcher = None

    async def broadcast_notification(self, data: dict):
        """向所有活跃的 WebSocket 连接广播通知."""
        if not self._active_ws_connections:
            return

        payload = {
            "type": "notification",
            "data": data,
        }

        disconnected = set()
        for ws in self._active_ws_connections:
            try:
                await ws.send_json(payload)
            except Exception:
                disconnected.add(ws)

        self._active_ws_connections -= disconnected

    def _require_auth(self, request: Request) -> bool:
        """鉴权校验：登录认证开关关闭（默认）时放行；
        开启时未设置凭证放行（本地首次使用），已设置凭证则要求有效 token.

        token 提取优先级：Authorization: Bearer <token> header → query param token。
        与 WebSocket 端点鉴权语义一致（security/auth.py 的 verify_token）。
        """
        # 登录认证开关关闭 → 放行（与全局中间件一致，配置热加载）
        try:
            from scout.config.manager import ConfigManager
            if not getattr(ConfigManager().load(), "auth_enabled", False):
                return True
        except Exception:
            pass
        if not self.auth_mgr.has_credentials():
            return True
        token = request.headers.get("Authorization", "")
        if token.startswith("Bearer "):
            token = token[7:]
        elif not token:
            token = request.query_params.get("token", "")
        return bool(token) and bool(verify_token(token))

    def _setup_routes(self):
        """注册所有 API 路由 — 按功能领域拆分为子方法."""
        self._setup_auth_routes()
        self._setup_config_routes()
        # 注意：/api/traces/by-session 等字面量路由必须先于
        # /api/traces/{trace_id}（_setup_observability_routes）注册，
        # 否则动态路由会抢占字面量路由，导致 by-session 返回"追踪不存在"
        self._setup_trace_routes()
        self._setup_observability_routes()
        self._setup_goal_routes()
        self._setup_checkpoint_routes()
        self._setup_a2a_routes()
        self._setup_session_routes()
        self._setup_memory_routes()
        self._setup_starlight_routes()
        self._setup_knowledge_routes()
        self._setup_skill_routes()
        self._setup_usage_routes()
        self._setup_security_routes()
        self._setup_cron_routes()
        self._setup_event_routes()
        self._setup_chat_routes()
        self._setup_tool_routes()
        self._setup_channel_routes()
        self._setup_mcp_routes()
        self._setup_webhook_routes()
        self._setup_automation_routes()
        self._setup_agent_routes()
        self._setup_gateway_routes()
        self._setup_plugin_routes()
        self._setup_voice_routes()
        self._setup_websocket_endpoint()

    @staticmethod
    def _normalize_repo_url(url: str) -> str:
        """把 GitHub/Gitee 的页面 URL 归一化为可 git clone 的仓库根地址.

        处理：
        - https://github.com/owner/repo/blob/main/README.md  → https://github.com/owner/repo
        - https://github.com/owner/repo/tree/main/docs       → https://github.com/owner/repo
        - https://github.com/owner/repo/raw/main/xxx         → https://github.com/owner/repo
        - https://github.com/owner/repo                      → 原样
        - 末尾 .git 保留
        """
        import re as _re
        url = (url or "").strip()
        if not url:
            return url
        # 去掉尾部斜杠
        url = url.rstrip("/")
        # 匹配 github.com / gitee.com / gitlab.com 后的 owner/repo 前缀
        m = _re.match(r"^(https?://(?:github\.com|gitee\.com|gitlab\.com)/[^/]+/[^/]+)", url)
        if m:
            base = m.group(1)
            # 若原 URL 仅到 owner/repo（含 .git 结尾），保留原样；否则用归一化后的仓库根
            return base
        return url

    @staticmethod
    def _fetch_github_tarball(url: str, target_dir: str, timeout: int = 40) -> bool:
        """从 codeload.github.com 下载 GitHub 仓库 tarball 并解压到 target_dir.

        github.com 主站在部分网络环境不稳定（TCP 卡死），但 codeload.github.com
        （文件分发通道）通常稳定。此方法用它绕过主站，下载默认分支的最新代码。

        Returns: 是否成功
        """
        import re as _re, urllib.request, tarfile, os, shutil as _sh

        # 从仓库 URL 提取 owner/repo
        m = _re.match(r"^https?://github\.com/([^/]+)/([^/]+)", url)
        if not m:
            return False
        owner, repo = m.group(1), m.group(2).rstrip(".git")

        # 下载默认分支 tarball（main 优先，失败回退 master）
        for branch in ("main", "master"):
            dl_url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
            tar_path = target_dir + ".tar.gz"
            try:
                req = urllib.request.Request(dl_url, headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        continue
                    with open(tar_path, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                # 解压：tarball 顶层是 "<repo>-<branch>" 目录，把内容平铺到 target_dir
                with tarfile.open(tar_path, "r:gz") as tar:
                    members = tar.getmembers()
                    top_dir = None
                    if members:
                        first = members[0].name.split("/")[0]
                        top_dir = first
                    # 安全解压：校验成员路径防路径穿越（../ 或绝对路径一律拒绝）
                    target_abs = os.path.abspath(target_dir)
                    for m in members:
                        member_path = os.path.abspath(os.path.join(target_dir, m.name))
                        if not member_path.startswith(target_abs + os.sep) and member_path != target_abs:
                            raise RuntimeError(f"拒绝不安全的压缩包路径: {m.name}")
                    tar.extractall(target_dir)
                if top_dir:
                    # 把 <repo>-<branch> 内的内容移动到 target_dir 根
                    import shutil as _sh
                    src = os.path.join(target_dir, top_dir)
                    if os.path.isdir(src):
                        for item in os.listdir(src):
                            _sh.move(os.path.join(src, item), os.path.join(target_dir, item))
                        _sh.rmtree(src, ignore_errors=True)
                try:
                    os.remove(tar_path)
                except Exception:
                    pass
                return True
            except Exception as e:
                logging.getLogger(__name__).debug(f"GitHub tarball 下载失败 ({branch}): {e}")
                continue
        return False

