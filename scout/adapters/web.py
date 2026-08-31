"""Web API 适配器 — OpenAI 兼容 API + SSE 流式 + WebSocket.

借鉴 OpenClaw 的 /v1/chat/completions 设计，使 Scout 可被任何 OpenAI 兼容客户端调用。
"""

# 注意：不使用 from __future__ import annotations。
# 本模块含 FastAPI 闭包路由（如 /a2a/tasks/send 的 body 参数），字符串化注解
# 会导致 pydantic ForwardRef 解析失败（class-not-fully-defined）。
import asyncio
import json
import logging
import re
import time
import uuid
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


# ── 请求/响应模型 ──────────────────────────────────────────

class ChatRequest(PydanticModel):
    """OpenAI 兼容的 chat 请求."""
    model: str = "scout"
    messages: list[dict]
    stream: bool = False
    temperature: float = 0.7
    max_tokens: int | None = None


class ChatChoice(PydanticModel):
    index: int = 0
    message: dict
    finish_reason: str = "stop"


class ChatResponse(PydanticModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str = "scout"
    choices: list[ChatChoice]


# ── Web 回调 — 通过 SSE/WebSocket 推送事件 ─────────────────

class WebCallbacks(Callbacks):
    """Web 回调 — 直接通过 WebSocket 推送事件给前端."""

    def __init__(self, ws: WebSocket | None = None):
        self.ws = ws
        self.events: asyncio.Queue = asyncio.Queue()  # 保留兼容

    def set_ws(self, ws: WebSocket):
        self.ws = ws

    async def _push(self, event_type: str, data: dict):
        payload = {"type": event_type, "data": data, "timestamp": datetime.now().isoformat()}
        if self.ws:
            try:
                await self.ws.send_json(payload)
            except Exception:
                await self.events.put(payload)
        else:
            await self.events.put(payload)

    async def on_tool_progress(self, tool_name: str, stage: str, message: str, metadata: dict | None = None):
        data = {"tool_name": tool_name, "stage": stage, "message": message}
        if metadata:
            data["metadata"] = metadata
        await self._push("tool_progress", data)

    async def on_thinking(self, started: bool):
        await self._push("thinking", {"started": started})

    async def on_reasoning(self, content: str):
        await self._push("reasoning", {"content": content})

    async def on_clarify(self, question: str) -> str:
        await self._push("clarify", {"question": question})
        return ""

    async def on_step(self, step: int, total_budget: int):
        await self._push("step", {"step": step, "total": total_budget})

    async def on_stream_delta(self, text: str):
        await self._push("stream_delta", {"text": text})

    async def on_tool_gen(self, tool_name: str, args: dict):
        await self._push("tool_gen", {"tool_name": tool_name, "args": args})

    async def on_status(self, status: str):
        await self._push("status", {"status": status})

    async def on_reflection(self, hint: str):
        await self._push("reflection", {"hint": hint})

    async def on_goals_extracted(self, goals: list[dict]):
        await self._push("goals_extracted", {"goals": goals})

    async def on_confirm(self, request_id: str, tool_name: str, args: dict, reason: str) -> bool:
        """请求用户确认 — 通过 WebSocket 推送确认请求并等待响应."""
        import asyncio
        # 创建 Future 等待用户响应
        future = asyncio.Future()
        # 存储到 WebAdapter 的 pending_confirmations
        if hasattr(self, '_adapter') and self._adapter:
            self._adapter._pending_confirmations[request_id] = future
        # 推送确认请求到前端
        await self._push("confirm_request", {
            "request_id": request_id,
            "tool_name": tool_name,
            "args": args,
            "reason": reason
        })
        # 等待用户响应（超时 60 秒）
        try:
            approved = await asyncio.wait_for(future, timeout=60.0)
            return approved
        except asyncio.TimeoutError:
            return False  # 超时默认拒绝

    async def on_file(self, file_path: str, file_name: str = "", file_size: int = 0) -> None:
        """推送文件事件给前端（2026-08-12 修复: 此前 file 事件只发 bus 不推 WebSocket）"""
        await self._push("file", {
            "file_path": file_path,
            "file_name": file_name or (file_path.split("/")[-1] if file_path else ""),
            "file_size": file_size,
        })


# ── Web 适配器 ──────────────────────────────────────────────

class WebAdapter:
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
        if agent is not None:
            try:
                config = self.config_mgr.load()
                if config.api_key:
                    self._rebuild_agent(config)
            except Exception as e:
                logger.warning(f"Failed to load config and rebuild agent: {e}")

        # ★ 断裂点修复 2: WebSocket 连接管理 + EventBus 订阅
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
            with open(self._webhooks_path) as f:
                return json.load(f)
        return []

    def _save_all_webhooks(self, hooks: list[dict]) -> None:
        with open(self._webhooks_path, "w") as f:
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
            import logging, traceback
            logging.getLogger(__name__).warning(f"_collect_ws_usage 查询失败: {e!r}\n{traceback.format_exc()}")
            return {"tokens": 0, "calls": 0, "cache_hit_rate": 0.0, "avg_latency_ms": 0}

    # ── 聊天模型选择（2026-08-13）──

    def _get_chat_llm(self, model: str):
        """为指定模型创建/复用 LLM provider（聊天框模型切换用）.

        使用当前配置的 provider/api_key/base_url/重试参数，仅替换 model。
        provider 按 model 缓存（最多 5 个，LRU），避免每条消息都重建客户端。
        返回 None 表示配置不完整或 model 与当前一致（无需覆盖）。
        """
        if not model:
            return None
        config = self.config_mgr.load()
        if not config.provider or not config.api_key:
            return None
        if model == config.model:
            return None  # 与全局配置一致，直接用主 agent 的 llm

        if not hasattr(self, "_chat_llm_cache"):
            self._chat_llm_cache = {}  # key: (provider, model) -> llm
        cache_key = (config.provider, model)
        cached = self._chat_llm_cache.get(cache_key)
        if cached:
            # LRU: 移到最近使用
            self._chat_llm_cache.pop(cache_key)
            self._chat_llm_cache[cache_key] = cached
            return cached

        try:
            from scout.llm.providers.registry import create_provider
            llm = create_provider(
                provider=config.provider,
                api_key=config.api_key,
                model=model,
                base_url=config.base_url,
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
        llm = create_provider(
            provider=config.provider,
            api_key=config.api_key,
            model=config.model,
            base_url=config.base_url,
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
                    provider=config.provider,
                    api_key=config.api_key,
                    model=fb_model,
                    base_url=config.base_url,
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
            emb_key, emb_base = config.api_key, config.base_url
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
            import logging
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

    def _setup_auth_routes(self):
        """认证相关 API."""

        # ── 认证 API ──

        @self.app.post("/api/auth/login")
        async def login(req: Request):
            """登录 — 返回 JWT token."""
            body = await req.json()
            username = body.get("username", "").strip()
            password = body.get("password", "")
            
            if not username or not password:
                return JSONResponse({"error": "用户名和密码不能为空"}, status_code=400)
            
            # 判断是否是首次（尚未设置凭证）
            is_first = not self.auth_mgr.has_credentials()
            if is_first:
                # 首次初始化凭证仅允许本机回环来源：防止默认配置下
                # 外部或本地恶意进程抢先注册凭证（先到先得抢占）。
                host = (req.client.host if req.client else "") or ""
                if host not in ("127.0.0.1", "::1", "localhost"):
                    return JSONResponse(
                        {"error": "首次初始化仅允许本机访问，请通过 127.0.0.1 访问服务"},
                        status_code=403,
                    )
            
            token = self.auth_mgr.login(username, password)
            if token:
                return {
                    "status": "ok",
                    "token": token,
                    "username": username,
                    "is_first_login": is_first,
                }
            return JSONResponse({"error": "用户名或密码错误"}, status_code=401)

        @self.app.get("/api/auth/check")
        async def auth_check(token: str = ""):
            """检查 token 是否有效."""
            if not self.auth_mgr.has_credentials():
                return {"authenticated": True, "setup_required": True}
            
            payload = verify_token(token)
            if payload:
                return {"authenticated": True, "username": payload.get("sub", "")}
            return {"authenticated": False}

        @self.app.post("/api/auth/change-password")
        async def change_password(req: Request):
            """修改密码."""
            body = await req.json()
            old_pwd = body.get("old_password", "")
            new_pwd = body.get("new_password", "")
            
            if not old_pwd or not new_pwd:
                return JSONResponse({"error": "密码不能为空"}, status_code=400)
            
            if self.auth_mgr.change_password(old_pwd, new_pwd):
                return {"status": "ok", "message": "密码已修改"}
            return JSONResponse({"error": "旧密码错误"}, status_code=401)

        @self.app.get("/api/auth/status")
        async def auth_status():
            """获取认证状态 — 是否需要登录（基于登录认证开关）. """
            config = self.config_mgr.load()
            login_required = bool(config.auth_enabled)
            return {
                "login_required": login_required,
                "username": self.auth_mgr.get_username() if login_required else "",
            }

        @self.app.post("/api/auth/setup")
        async def auth_setup(req: Request):
            """启用/关闭登录认证开关，并设置用户名密码. """
            body = await req.json()
            enabled = bool(body.get("enabled"))
            username = str(body.get("username", "")).strip()
            password = body.get("password", "")

            config = self.config_mgr.load()

            if enabled:
                if not username or not password:
                    return JSONResponse({"error": "用户名和密码不能为空"}, status_code=400)
                if len(password) < 6:
                    return JSONResponse({"error": "密码长度至少 6 位"}, status_code=400)
                # 设置凭证并轮换 JWT 密钥，使历史 token 立即失效
                self.auth_mgr.set_credentials(username, password)
                try:
                    rotate_secret()
                except Exception:
                    pass
            config.auth_enabled = enabled
            self.config_mgr.save(config)
            return {
                "status": "ok",
                "enabled": enabled,
                "username": username if enabled else "",
            }

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
                config.base_url = req["base_url"]
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
            """聊天框模型选择器 — 返回当前配置 provider 的可选模型列表."""
            config = self.config_mgr.load()
            result = {
                "configured": bool(config.provider and config.api_key),
                "provider": config.provider,
                "current_model": config.model,
                "models": [],
            }
            if not config.provider:
                return result
            # 复用 list_providers 的预设数据
            try:
                providers_data = await list_providers()
                preset = next(
                    (p for p in providers_data.get("providers", [])
                     if p.get("id") == config.provider),
                    None,
                )
                if preset:
                    for m in preset.get("models", []):
                        result["models"].append({
                            "id": m["id"],
                            "name": m.get("name", m["id"]),
                            "capabilities": m.get("capabilities", []),
                        })
            except Exception as e:
                logger.warning(f"加载模型预设失败: {e}")
            # 当前模型不在预设里（自定义 provider/模型）时，置顶显示
            if config.model and not any(m["id"] == config.model for m in result["models"]):
                result["models"].insert(0, {
                    "id": config.model,
                    "name": f"{config.model}（当前配置）",
                    "capabilities": [],
                })
            return result

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
                provider = req.get("provider", "dashscope")
                api_key = str(req.get("api_key", "") or "")
                base_url = str(req.get("base_url", "") or "").strip()
                stored_key, stored_url = self.config_mgr.get_provider_credentials(provider)
                # 2026-08-31：输入框为空 / 回填脱敏值时回落已存明文，避免测试用掩码连接
                api_key = _resolve_key(api_key, stored_key)
                if not base_url:
                    base_url = stored_url
                llm = create_provider(
                    provider=provider,
                    api_key=api_key,
                    model=req.get("model", "qwen-plus"),
                    base_url=base_url,
                )
                resp = await llm.complete([{"role": "user", "content": "Hi"}])
                return {"status": "ok", "message": f"连接成功: {resp.content[:50]}"}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)

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
                import logging
                logging.getLogger(__name__).debug(f"GitHub tarball 下载失败 ({branch}): {e}")
                continue
        return False

    def _setup_skill_routes(self):
        """技能 API."""

        # ── 技能 API ──

        @self.app.get("/api/skills")
        async def list_skills():
            """列出所有技能."""
            if self._agent and self._agent.skill_mgr:
                skills = self._agent.skill_mgr.list_skills()
                return {"skills": [s.model_dump() for s in skills]}
            return {"skills": []}

        @self.app.delete("/api/skills/{name}")
        async def delete_skill(name: str):
            """卸载技能（从 $SCOUT_DATA_DIR/skills 删除 SKILL.md 目录）."""
            if not self._agent or not getattr(self._agent, "skill_mgr", None):
                return JSONResponse({"error": "技能系统未启用"}, status_code=503)
            ok = self._agent.skill_mgr.remove_skill(name, scope="user")
            if not ok:
                return JSONResponse({"error": f"技能 {name} 不存在或已删除"}, status_code=404)
            return {"status": "ok", "message": f"已卸载技能 {name}"}

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

    def _setup_cron_routes(self):
        """定时任务 API."""

        # ── Cron API ──

        CRON_FILE = _SCOUT_DATA_DIR / "cron_tasks.json"

        def _get_cron_mgr():
            """全局 CronManager（懒加载）— 带持久化 + 自动化执行接入.

            修复（2026-08-13）：原本 CronManager 只是数据容器，调度循环未启动、
            无 agent 回调，UI 创建的任务永远不会执行。现在：
            1. 任务持久化到 $SCOUT_DATA_DIR/cron_tasks.json（重启不丢）
            2. 绑定 AutomationRunner 作为执行器（策略门控 + 留痕 + 验证）
            3. 启动调度循环
            """
            from scout.automation.cron import CronManager, CronTask
            if not hasattr(self, "_cron_mgr"):
                mgr = CronManager()
                # 1. 加载持久化任务
                try:
                    if CRON_FILE.exists():
                        for d in json.loads(CRON_FILE.read_text(encoding="utf-8")):
                            task = CronTask(
                                name=d["name"], schedule=d["schedule"],
                                task=d["task"], agent_id=d.get("agent_id", "default"),
                            )
                            task.enabled = d.get("enabled", True)
                            mgr.add(task)
                except Exception as e:
                    logger.warning(f"cron 任务加载失败: {e}")

                # 2. 执行回调：走 AutomationRunner（无人值守运行栈）
                async def _run_cron_task(task):
                    runner = self._get_automation_runner()
                    if runner:
                        asyncio.create_task(runner.run_task(
                            task.task,
                            {"trigger_type": "cron", "trigger_id": task.name},
                        ))
                    elif self._agent:
                        import copy as _copy
                        from scout.core.callbacks import NullCallbacks
                        from scout.core.types import Session as _Session
                        agent_copy = _copy.copy(self._agent)
                        agent_copy.callbacks = NullCallbacks()
                        asyncio.create_task(agent_copy.run_conversation(
                            task.task, _Session(id=str(uuid.uuid4()))
                        ))

                mgr.set_agent_callback(_run_cron_task)

                # 3. 启动调度循环（懒加载发生在请求处理中，必有事件循环）
                try:
                    asyncio.get_running_loop()
                    asyncio.create_task(mgr.start())
                except RuntimeError:
                    logger.warning("无事件循环，cron 调度循环未启动")

                self._cron_mgr = mgr
            return self._cron_mgr

        def _save_cron_tasks():
            """持久化 cron 任务到磁盘."""
            try:
                CRON_FILE.parent.mkdir(parents=True, exist_ok=True)
                CRON_FILE.write_text(
                    json.dumps([t.to_dict() for t in self._cron_mgr.list_tasks()],
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(f"cron 任务保存失败: {e}")

        @self.app.get("/api/cron")
        async def list_cron():
            """列出定时任务."""
            mgr = _get_cron_mgr()
            return {"tasks": [t.to_dict() for t in mgr.list_tasks()]}

        @self.app.post("/api/cron")
        async def add_cron(req: dict):
            """添加定时任务."""
            from scout.automation.cron import CronTask
            mgr = _get_cron_mgr()
            name = req.get("name", "unnamed")
            if mgr.get_task(name):
                return JSONResponse({"error": f"任务名已存在: {name}"}, status_code=400)
            task = CronTask(
                name=name,
                schedule=req.get("schedule", "每60秒"),
                task=req.get("task", ""),
            )
            mgr.add(task)
            _save_cron_tasks()
            return {"status": "ok", "task": task.to_dict()}

        @self.app.delete("/api/cron/{name}")
        async def del_cron(name: str):
            """删除定时任务."""
            mgr = _get_cron_mgr()
            mgr.remove(name)
            _save_cron_tasks()
            return {"status": "ok"}

    def _setup_event_routes(self):
        """事件历史 API."""

        # ── 事件历史 API ──

        @self.app.get("/api/events")
        async def get_events(limit: int = 20):
            """获取事件历史."""
            if self._agent and self._agent.bus:
                events = self._agent.bus.get_history(limit=limit)
                return {"events": events}
            return {"events": []}

        @self.app.get("/api/events/dlq")
        async def get_dlq(limit: int = 20):
            """获取死信队列（事件处理失败的记录）."""
            bus = (self._agent.bus if self._agent else None)
            if bus:
                return {"dlq": bus.get_dlq(limit=limit), "size": bus.dlq_size}
            # 无 agent 时尝试全局 bus
            try:
                from scout.bus.hub import bus as global_bus
                return {"dlq": global_bus.get_dlq(limit=limit), "size": global_bus.dlq_size}
            except Exception:
                return {"dlq": [], "size": 0}

        @self.app.delete("/api/events/dlq")
        async def clear_dlq():
            """清空死信队列."""
            bus = (self._agent.bus if self._agent else None)
            cleared = 0
            if bus:
                cleared = bus.clear_dlq()
            else:
                try:
                    from scout.bus.hub import bus as global_bus
                    cleared = global_bus.clear_dlq()
                except Exception:
                    pass
            return {"status": "ok", "cleared": cleared}

    def _setup_session_routes(self):
        """会话历史 API."""

        # ── 文件下载 API ──
        
        @self.app.get("/api/files/download")
        async def download_file(path: str):
            """下载文件 — 仅允许工作空间内（安全修复 2026-08-09）."""
            from fastapi.responses import FileResponse
            import os
            
            path = os.path.expanduser(path)
            if not os.path.exists(path):
                return JSONResponse({"error": f"文件不存在: {path}"}, status_code=404)
            if not os.path.isfile(path):
                return JSONResponse({"error": f"不是文件: {path}"}, status_code=400)
            
            # 安全校验：只允许下载安全目录内的文件（2026-08-12 放宽，与 shell cwd 白名单一致）
            # - 用户主目录 / /tmp / /home / /data / /opt / /srv / /mnt / /media / /workspace
            # - 严格拦截系统敏感目录（/etc /usr /bin /sbin /lib /boot /sys /proc /dev /var /root）
            file_abs = os.path.abspath(path)
            for _sd in SYSTEM_DIRS:
                if file_abs == _sd or file_abs.startswith(_sd + os.sep):
                    return JSONResponse({"error": f"安全拦截: 不允许下载系统目录文件 {file_abs}"}, status_code=403)
            _home = os.path.expanduser("~")
            if os.name == "nt":
                # Windows：放行任意盘符根（系统目录已在上面硬拦截）
                _drive, _ = os.path.splitdrive(file_abs)
                _allow_prefixes = [_home + os.sep] + (["/polarfs"] if _drive else [])
                _drive_root_ok = bool(_drive)
            else:
                # 通用允许前缀 + web 下载特有 /polarfs
                _allow_prefixes = [_home + os.sep] + list(ALLOWED_PATH_PREFIXES) + ["/polarfs"]
                _drive_root_ok = False
            if file_abs == _home:
                pass  # 主目录本身允许
            elif _drive_root_ok:
                pass  # Windows 盘符根放行（系统目录已拦）
            elif not any(file_abs.startswith(p) for p in _allow_prefixes):
                return JSONResponse({"error": "安全拦截: 只允许下载用户目录/临时目录/数据目录内的文件"}, status_code=403)
            
            filename = os.path.basename(path)
            # 图片类型返回可内联预览的 media_type，其余保持 octet-stream
            import mimetypes
            _img_types = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
                ".svg": "image/svg+xml", ".ico": "image/x-icon",
            }
            ext = os.path.splitext(path)[1].lower()
            media_type = _img_types.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
            return FileResponse(
                path,
                filename=filename,
                media_type=media_type,
            )

        # ── 会话历史 API ──

        @self.app.get("/api/sessions")
        async def list_sessions(limit: int = 20):
            """列出历史会话."""
            _sstore = self._session_store()
            if _sstore:
                sessions = _sstore.list_sessions(limit=limit)
                return {"sessions": sessions}
            return {"sessions": []}

        @self.app.get("/api/sessions/search")
        async def search_sessions(q: str = "", limit: int = 20):
            """跨会话搜索消息内容 — FTS5 全文索引 + LIKE 模糊匹配."""
            if not q.strip():
                return JSONResponse({"error": "搜索关键词不能为空"}, status_code=400)
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            results = self._agent.session_store.search_messages(q, limit=limit)
            # 补充会话标题
            for r in results:
                sid = r.get("session_id") or r.get("sid", "")
                if sid:
                    s = self._agent.session_store.load_session(sid)
                    r["session_title"] = s.extra.get("title", "") if s else ""
                    r["session_preview"] = (s.messages[0].content[:50] if s and s.messages else "")
            return {"query": q, "results": results, "total": len(results)}

        @self.app.get("/api/sessions/{session_id}")
        async def get_session(session_id: str):
            """加载指定会话."""
            _sstore = self._session_store()
            if _sstore:
                session = _sstore.load_session(session_id)
                if session:
                    msgs = []
                    for idx, m in enumerate(session.messages):
                        msg = {"role": m.role.value, "content": m.content, "index": idx}
                        # 透传思考过程（reasoning），供前端重进会话时恢复"思考/运行过程"
                        if m.reasoning:
                            msg["reasoning"] = m.reasoning
                        if m.role == Role.USER:
                            # 剥离注入的动态上下文（runtime_context/memories/skills），避免泄漏到前端
                            import re as _re
                            _c = msg["content"]
                            _c = _re.sub(r"<runtime_context>[\s\S]*?</runtime_context>", "", _c)
                            _c = _re.sub(r"<memories>[\s\S]*?</memories>", "", _c)
                            _c = _re.sub(r"<skills>[\s\S]*?</skills>", "", _c)
                            msg["content"] = _c.strip()
                        if m.metadata:
                            # 附件信息
                            if m.metadata.get("attachments"):
                                msg["attachments"] = m.metadata["attachments"]
                            # 标记带工具调用的中间消息
                            if m.metadata.get("tool_calls"):
                                msg["metadata"] = {"tool_calls": m.metadata["tool_calls"]}
                            # tool 消息的工具名和完整 metadata
                            if m.role.value == "tool":
                                msg["metadata"] = m.metadata
                        msgs.append(msg)
                    return {
                        "id": session.id,
                        "status": session.status,
                        "messages": msgs,
                        "suggestions": (session.extra or {}).get("suggestions", []),
                        "files": (session.extra or {}).get("files", []),
                    }
            return JSONResponse({"error": "会话不存在"}, status_code=404)

        @self.app.delete("/api/sessions/{session_id}")
        async def delete_session(session_id: str):
            """删除会话."""
            # ★ 2026-08-29：改用 _session_store()，不再依赖 self._agent——
            # exe 启动时 agent 为 None，此前删除一律返回 400"会话存储未启用"，会话删不掉。
            store = self._session_store()
            if store:
                store.delete_session(session_id)
                return {"status": "ok"}
            return JSONResponse({"error": "会话存储未启用"}, status_code=400)

        @self.app.patch("/api/sessions/{session_id}")
        async def rename_session(session_id: str, req: Request):
            """重命名会话标题."""
            body = await req.json()
            title = body.get("title", "").strip()
            if not title:
                return JSONResponse({"error": "标题不能为空"}, status_code=400)
            store = self._session_store()
            if store:
                store.rename_session(session_id, title)
                return {"status": "ok"}
            return JSONResponse({"error": "会话存储未启用"}, status_code=400)

        @self.app.post("/api/sessions/{session_id}/fork")
        async def fork_session(session_id: str, req: Request):
            """从指定会话 fork 出一个新分支会话.

            body 可选参数:
              - up_to_seq: int，只复制到第 up_to_seq 条消息为止（从某处回退再分叉）
              - title: str，新会话标题
            """
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            body = {}
            try:
                body = await req.json()
            except Exception:
                body = {}
            up_to_seq = body.get("up_to_seq")
            title = body.get("title") or ""
            new_session_id = str(uuid.uuid4())
            store = self._agent.session_store
            try:
                result = await store.async_fork(
                    session_id, new_session_id,
                    up_to_seq=up_to_seq, title=title,
                )
            except Exception as e:
                logger.error(f"会话 fork 失败: {e}")
                return JSONResponse({"error": f"会话 fork 失败: {e}"}, status_code=500)
            if not result:
                return JSONResponse({"error": "源会话不存在"}, status_code=404)
            return {"status": "ok", "session": result}

        @self.app.get("/api/sessions/{session_id}/lineage")
        async def get_session_lineage(session_id: str):
            """查询会话的分叉血缘链（父会话 → 本会话 → 子分支）."""
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            store = self._agent.session_store
            try:
                # 找父链
                lineage = []
                cur_id = session_id
                seen = set()
                while cur_id and cur_id not in seen:
                    seen.add(cur_id)
                    s = await store.async_get(cur_id)
                    if not s:
                        break
                    lineage.append({"id": s.get("id"), "title": s.get("title") or "", "parent_id": s.get("parent_id") or ""})
                    cur_id = s.get("parent_id")
                lineage.reverse()
                # 找子分支
                children = []
                db = await store._ensure_storage()
                rows = await db.fetchall(
                    "SELECT id, title, parent_id, lineage_id, updated_at FROM sessions WHERE parent_id = $1",
                    (session_id,),
                )
                for r in rows:
                    children.append({"id": r["id"], "title": r["title"] or "", "updated_at": r["updated_at"]})
                return {"lineage": lineage, "children": children}
            except Exception as e:
                logger.error(f"查询会话血缘失败: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.delete("/api/sessions/{session_id}/messages/{message_id}")
        async def delete_message(session_id: str, message_id: int):
            """删除单条消息并截断后续消息，同步清理记忆."""
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            store = self._agent.session_store
            session = store.load_session(session_id)
            if not session:
                return JSONResponse({"error": "会话不存在"}, status_code=404)
            # 收集被删除消息的内容，用于清理记忆
            deleted_msgs = session.messages[message_id:]
            # 截断到指定消息之前（删除该消息及之后所有消息）
            session.messages = session.messages[:message_id]
            session.status = "idle"
            store.save_session(session)
            # 同步清理记忆 — 按被删除消息的内容模糊匹配
            mem_deleted = 0
            if self._agent.memory_store:
                for msg in deleted_msgs:
                    if msg.content and len(msg.content) > 5:
                        mem_deleted += self._agent.memory_store.delete_by_content(msg.content)
            return {"status": "ok", "messages": len(session.messages), "memories_deleted": mem_deleted}

        @self.app.put("/api/sessions/{session_id}/messages/{message_id}")
        async def edit_message(session_id: str, message_id: int, req: Request):
            """编辑用户消息 — 截断后续内容，更新文本，同步清理记忆."""
            if not self._agent or not self._agent.session_store:
                return JSONResponse({"error": "会话存储未启用"}, status_code=400)
            body = await req.json()
            new_content = body.get("content", "").strip()
            if not new_content:
                return JSONResponse({"error": "内容不能为空"}, status_code=400)
            store = self._agent.session_store
            session = store.load_session(session_id)
            if not session:
                return JSONResponse({"error": "会话不存在"}, status_code=404)
            # 收集被截断消息的内容，用于清理记忆
            old_content = session.messages[message_id].content if message_id < len(session.messages) else ""
            deleted_msgs = session.messages[message_id + 1:]
            # 截断到该用户消息（保留到并包括该消息），更新内容
            session.messages = session.messages[:message_id + 1]
            session.messages[-1] = Message(
                role=session.messages[-1].role,
                content=new_content,
                timestamp=datetime.now(),
            )
            session.status = "idle"
            store.save_session(session)
            # 同步清理记忆 — 旧消息内容和后续消息内容
            mem_deleted = 0
            if self._agent.memory_store:
                if old_content and len(old_content) > 5:
                    mem_deleted += self._agent.memory_store.delete_by_content(old_content)
                for msg in deleted_msgs:
                    if msg.content and len(msg.content) > 5:
                        mem_deleted += self._agent.memory_store.delete_by_content(msg.content)
            return {"status": "ok", "session_id": session_id, "content": new_content, "memories_deleted": mem_deleted}

    def _setup_memory_routes(self):
        """记忆系统 API."""

        # ── 记忆 API ──

        @self.app.get("/api/memory")
        async def list_memory(limit: int = 20):
            """列出记忆."""
            if self._agent and self._agent.memory_store:
                memories = self._agent.memory_store.list_recent(limit=limit)
                return {"memories": [m.to_dict() for m in memories]}
            return {"memories": []}

        @self.app.get("/api/memory/search")
        async def search_memory(q: str = "", limit: int = 20):
            """搜索记忆."""
            if self._agent and self._agent.memory_store and q:
                results = self._agent.memory_store.search(q, limit=limit)
                return {"memories": [m.to_dict() for m in results]}
            return {"memories": []}

        @self.app.post("/api/memory")
        async def add_memory(req: dict):
            """手动添加记忆."""
            if self._agent and self._agent.memory_store:
                content = req.get("content", "").strip()
                if content:
                    self._agent.memory_store.add(
                        content=content,
                        category=req.get("category", "general"),
                        importance=req.get("importance", 0.5),
                    )
                    return {"status": "ok"}
                return JSONResponse({"error": "内容不能为空"}, status_code=400)
            return JSONResponse({"error": "记忆系统未启用"}, status_code=400)

        @self.app.delete("/api/memory/{memory_id}")
        async def delete_memory(memory_id: int):
            """删除记忆."""
            if self._agent and self._agent.memory_store:
                self._agent.memory_store.delete(memory_id)
                return {"status": "ok"}
            return JSONResponse({"error": "记忆系统未启用"}, status_code=400)

        @self.app.put("/api/memory/{memory_id}")
        async def update_memory(memory_id: int, req: Request):
            """更新记忆内容."""
            if not self._agent or not self._agent.memory_store:
                return JSONResponse({"error": "记忆系统未启用"}, status_code=400)
            body = await req.json()
            self._agent.memory_store.update(
                memory_id,
                content=body.get("content"),
                category=body.get("category"),
                importance=body.get("importance"),
            )
            return {"status": "ok"}

    def _setup_starlight_routes(self):
        """星夜凝萃 API."""

        # ── 星夜凝萃 API ──

        @self.app.get("/api/starlight/status")
        async def starlight_status():
            """获取星夜凝萃状态."""
            from scout.automation.starlight import get_starlight
            distiller = get_starlight()
            if not distiller:
                return {"enabled": False, "message": "星夜凝萃未初始化"}
            return distiller.get_status()

        @self.app.post("/api/starlight/run")
        async def starlight_run(req: Request):
            """手动触发星夜凝萃."""
            from scout.automation.starlight import get_starlight
            distiller = get_starlight()
            if not distiller:
                return JSONResponse({"error": "星夜凝萃未初始化"}, status_code=400)

            force = False
            try:
                body = await req.json()
                force = body.get("force", False)
            except Exception as e:
                logger.warning(f"Failed to parse starlight run request: {e}")

            try:
                result = await distiller.run(force=force)
                return result
            except Exception as e:
                import logging
                logging.getLogger(__name__).exception("星夜凝萃异常")
                return JSONResponse({"error": f"凝萃失败: {e}"}, status_code=500)

        @self.app.post("/api/starlight/config")
        async def starlight_config(req: Request):
            """更新星夜凝萃配置."""
            from scout.automation.starlight import get_starlight
            distiller = get_starlight()
            if not distiller:
                return JSONResponse({"error": "星夜凝萃未初始化"}, status_code=400)

            body = await req.json()
            try:
                # 如果 schedule_hour 变更，需要重启调度器
                old_hour = distiller.config.get("schedule_hour")
                distiller.set_config(**body)
                new_hour = distiller.config.get("schedule_hour")
                
                if old_hour != new_hour and distiller._scheduler_task and not distiller._scheduler_task.done():
                    import logging
                    logging.getLogger(__name__).info(f"星夜凝萃调度时间变更: {old_hour}:00 → {new_hour}:00，重启调度器")
                    distiller.stop_scheduler()
                    distiller.start_scheduler()
                
                return {"status": "ok", "config": distiller.get_status()}
            except Exception as e:
                return JSONResponse({"error": f"配置更新失败: {e}"}, status_code=400)

    def _setup_knowledge_routes(self):
        """知识库 API."""

        # ── 知识库 API ──

        @self.app.get("/api/knowledge")
        async def list_knowledge():
            """列出所有知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            pages = []
            if KNOWLEDGE_DIR.exists():
                for md_file in sorted(KNOWLEDGE_DIR.rglob("*.md")):
                    if md_file.name == "index.md":
                        continue
                    rel_path = str(md_file.relative_to(KNOWLEDGE_DIR))
                    try:
                        content = md_file.read_text(encoding="utf-8")
                        # 提取标题
                        title = md_file.stem.replace("-", " ").title()
                        for line in content.split("\n"):
                            if line.startswith("# "):
                                title = line[2:].strip()
                                break
                        # 提取摘要（跳过 YAML front matter）
                        summary = ""
                        in_frontmatter = False
                        for line in content.split("\n"):
                            stripped = line.strip()
                            if stripped == "---":
                                in_frontmatter = not in_frontmatter
                                continue
                            if in_frontmatter:
                                continue
                            if stripped and not stripped.startswith("#") and not stripped.startswith(">") and not stripped.startswith("```") and not stripped.startswith("|"):
                                summary = stripped[:100]
                                break
                        stat = md_file.stat()
                        pages.append({
                            "path": rel_path,
                            "title": title,
                            "summary": summary,
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                        })
                    except Exception:
                        continue
            return {"pages": pages}

        @self.app.get("/api/knowledge/{path:path}")
        async def read_knowledge(path: str):
            """读取知识页面内容."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            full_path = (KNOWLEDGE_DIR / path).resolve()
            if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                return JSONResponse({"error": "路径不合法"}, status_code=400)
            if not full_path.exists():
                return JSONResponse({"error": f"页面不存在: {path}"}, status_code=404)
            content = full_path.read_text(encoding="utf-8")
            return {"path": path, "content": content}

        @self.app.post("/api/knowledge")
        async def save_knowledge(req: dict):
            """保存知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            import os
            path = req.get("path", "")
            content = req.get("content", "")
            if not path or not content:
                return JSONResponse({"error": "path 和 content 不能为空"}, status_code=400)
            
            full_path = (KNOWLEDGE_DIR / path).resolve()
            if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                return JSONResponse({"error": "路径不合法"}, status_code=400)
            
            full_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = full_path.with_suffix(".tmp")
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, full_path)
            
            # 更新索引
            index_path = KNOWLEDGE_DIR / "index.md"
            title = Path(path).stem.replace("-", " ").title()
            for line in content.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            entry = f"- [{title}]({path})"
            if index_path.exists():
                idx_content = index_path.read_text(encoding="utf-8")
                if path not in idx_content:
                    idx_content = idx_content.rstrip() + f"\n{entry}\n"
                    tmp = index_path.with_suffix(".tmp")
                    tmp.write_text(idx_content, encoding="utf-8")
                    os.replace(tmp, index_path)
            else:
                tmp = index_path.with_suffix(".tmp")
                tmp.write_text(f"# 知识库索引\n\n{entry}\n", encoding="utf-8")
                os.replace(tmp, index_path)
            
            return {"status": "ok", "path": path}

        @self.app.delete("/api/knowledge/{path:path}")
        async def delete_knowledge(path: str):
            """删除知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            import os
            full_path = (KNOWLEDGE_DIR / path).resolve()
            if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                return JSONResponse({"error": "路径不合法"}, status_code=400)
            if full_path.exists():
                full_path.unlink()
            return {"status": "ok"}

        @self.app.get("/api/knowledge/search")
        async def search_knowledge(q: str = "", limit: int = 20):
            """搜索知识库."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR, _global_index
            if not q:
                return {"results": []}
            results = _global_index.search(q, KNOWLEDGE_DIR)
            return {"results": [
                {"path": path, "score": score, "summary": summary}
                for path, score, summary in results[:limit]
            ]}

        @self.app.post("/api/knowledge/upload")
        async def upload_knowledge(req: Request):
            """上传文件并解析为知识页面."""
            from scout.tools.builtin.knowledge import KNOWLEDGE_DIR
            from scout.tools.builtin.knowledge.parser import DocumentParser
            import os
            import tempfile

            # 获取上传的文件
            form = await req.form()
            file = form.get("file")
            target_path = form.get("path", "")
            
            if not file:
                return JSONResponse({"error": "没有上传文件"}, status_code=400)

            # 保存到临时文件
            filename = file.filename or "upload"
            suffix = Path(filename).suffix
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    content = await file.read()
                    tmp.write(content)
                    tmp_path = Path(tmp.name)

                # 解析文档
                result = DocumentParser.parse(tmp_path)
                
                # 确定目标路径
                if not target_path:
                    stem = Path(filename).stem
                    # 清理文件名
                    stem = re.sub(r'[^\w\u4e00-\u9fff-]', '-', stem)
                    stem = re.sub(r'-+', '-', stem).strip('-').lower()
                    target_path = f"uploads/{stem}.md"
                
                # 确保 .md 后缀
                if not target_path.endswith(".md"):
                    target_path += ".md"

                # 保存到知识库
                full_path = (KNOWLEDGE_DIR / target_path).resolve()
                if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
                    return JSONResponse({"error": "路径不合法"}, status_code=400)
                
                full_path.parent.mkdir(parents=True, exist_ok=True)
                
                # 添加元数据头
                parsed_content = result["content"]
                header = f"---\nsource: {filename}\nformat: {result['format']}\nsize: {result['size']}\nparsed_at: {result['parsed_at']}\n---\n\n"
                final_content = header + parsed_content
                
                tmp_out = full_path.with_suffix(".tmp")
                tmp_out.write_text(final_content, encoding="utf-8")
                os.replace(tmp_out, full_path)

                # 更新索引
                index_path = KNOWLEDGE_DIR / "index.md"
                entry = f"- [{result['title']}]({target_path})"
                if index_path.exists():
                    idx = index_path.read_text(encoding="utf-8")
                    if target_path not in idx:
                        idx = idx.rstrip() + f"\n{entry}\n"
                        tmp_idx = index_path.with_suffix(".tmp")
                        tmp_idx.write_text(idx, encoding="utf-8")
                        os.replace(tmp_idx, index_path)
                else:
                    tmp_idx = index_path.with_suffix(".tmp")
                    tmp_idx.write_text(f"# 知识库索引\n\n{entry}\n", encoding="utf-8")
                    os.replace(tmp_idx, index_path)

                return {
                    "status": "ok",
                    "path": target_path,
                    "title": result["title"],
                    "format": result["format"],
                    "content": parsed_content,
                    "meta": result["meta"],
                }
            except Exception as e:
                return JSONResponse({"error": f"解析失败: {e}"}, status_code=500)
            finally:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)

    def _setup_mcp_routes(self):
        """MCP API."""

        # ── MCP API ──

        @self.app.get("/api/mcp")
        async def list_mcp():
            """列出 MCP 服务器."""
            from scout.tools.mcp import mcp_manager
            return {"servers": mcp_manager.list_servers()}

        @self.app.post("/api/mcp")
        async def add_mcp(req: dict):
            """添加 MCP 服务器."""
            from scout.tools.mcp import mcp_manager
            name = req.get("name", "unnamed")
            command = req.get("command")
            args = req.get("args", [])
            url = req.get("url")
            success = await mcp_manager.add_server(name, command=command, args=args, url=url)
            if success:
                return {"status": "ok", "message": f"MCP 服务器 {name} 已连接"}
            return JSONResponse({"error": f"连接 MCP 服务器 {name} 失败"}, status_code=400)

        @self.app.delete("/api/mcp/{server_name}")
        async def remove_mcp(server_name: str):
            """移除 MCP 服务器."""
            from scout.tools.mcp import mcp_manager
            await mcp_manager.remove_server(server_name)
            return {"status": "ok"}

    def _setup_agent_routes(self):
        """多 Agent API."""

        # ── 多 Agent API ──

        @self.app.get("/api/agents")
        async def list_agents():
            """列出所有 Agent — 返回当前运行实例的真实状态."""
            agents = []
            if self._agent:
                agents.append({
                    "id": "default",
                    "type": self._agent.__class__.__name__,
                    "status": "active",
                    "model": getattr(getattr(self._agent, "llm", None), "model", "unknown") or "unknown",
                    "provider": getattr(getattr(self._agent, "llm", None), "provider", "") or "",
                })
            return {"agents": agents}

        @self.app.get("/api/agents/bindings")
        async def list_bindings():
            """列出路由绑定."""
            return {"bindings": []}

    def _setup_webhook_routes(self):
        """Webhook API."""

        # ── Webhook API ──

        @self.app.get("/api/webhooks")
        async def list_webhooks():
            """列出所有 Webhook."""
            return {"webhooks": self._get_webhooks()}

        @self.app.post("/api/webhooks")
        async def create_webhook(req: Request):
            """创建 Webhook — 返回带 token 的 URL."""
            import secrets as _secrets
            body = await req.json()
            name = body.get("name", "unnamed")
            task = body.get("task", "")
            if not task:
                return JSONResponse({"error": "task 不能为空"}, status_code=400)
            token = _secrets.token_urlsafe(24)
            webhook = {
                "id": token,
                "name": name,
                "task": task,
                "url": f"http://localhost:{self.port}/api/webhook/{token}",
                "created_at": datetime.now().isoformat(),
                "call_count": 0,
                "last_called": "",
            }
            self._save_webhook(webhook)
            return {"status": "ok", "webhook": webhook}

        @self.app.delete("/api/webhooks/{token}")
        async def delete_webhook(token: str):
            """删除 Webhook."""
            self._delete_webhook(token)
            return {"status": "ok"}

        @self.app.post("/api/webhook/{token}")
        async def trigger_webhook(token: str, req: Request):
            """Webhook 触发 — 执行关联任务."""
            webhook = self._find_webhook(token)
            if not webhook:
                return JSONResponse({"error": "Webhook 不存在"}, status_code=404)
            # 更新调用计数
            webhook["call_count"] = webhook.get("call_count", 0) + 1
            webhook["last_called"] = datetime.now().isoformat()
            self._save_webhook(webhook)

            # 提取可选的附加参数
            try:
                body = await req.json()
                extra = body.get("message", "")
            except Exception:
                extra = ""

            task = webhook.get("task", "")
            if extra:
                task = f"{task}\n\n[Webhook 附加数据]\n{extra}"

            # 异步执行任务（不阻塞 webhook 响应）
            # P0: 优先走 AutomationRunner（策略门控 + 运行留痕 + 结果验证）
            runner = self._get_automation_runner()
            if runner:
                asyncio.create_task(runner.run_webhook_task(task, webhook.get("name", "")))
                return {"status": "accepted", "message": "任务已提交执行（无人值守模式）", "webhook": webhook["name"]}
            if self._agent:
                import copy
                session = Session(id=str(uuid.uuid4()))
                agent_copy = copy.copy(self._agent)
                agent_copy.callbacks = NullCallbacks()
                asyncio.create_task(agent_copy.run_conversation(task, session))
                return {"status": "accepted", "message": "任务已提交执行", "webhook": webhook["name"]}
            return JSONResponse({"error": "Agent 未配置"}, status_code=500)

    def _setup_automation_routes(self):
        """P0/P1 自动化与自进化 API（2026-08-13）.

        覆盖：
        - 触发器 CRUD + 手动触发（/api/triggers）
        - 运行记录与统计（/api/runs）
        - 无人值守策略（/api/automation/policy）
        - 周期性自省（/api/introspection）
        - 技能导入（agentskills.io）与 Record&Replay（/api/skills/import、/api/skills/record）
        - Memories 治理配置（/api/memories-config）
        - 分层指令链查看（/api/instructions）
        """

        # ── 触发器 ──

        @self.app.get("/api/triggers")
        async def list_triggers():
            runner = self._get_automation_runner()
            if not runner:
                return {"triggers": [], "note": "Agent 未就绪"}
            return {"triggers": [r.to_dict() for r in runner.trigger_mgr.list()]}

        @self.app.post("/api/triggers")
        async def create_trigger(req: Request):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            body = await req.json()
            task_template = body.get("task_template", "").strip()
            if not task_template:
                return JSONResponse({"error": "task_template 不能为空"}, status_code=400)
            ttype = body.get("type", "event")
            if ttype not in ("event", "cascade", "manual"):
                return JSONResponse({"error": "type 必须是 event/cascade/manual"}, status_code=400)
            if ttype == "event" and not body.get("event_name"):
                return JSONResponse({"error": "event 类型需要 event_name"}, status_code=400)
            if ttype == "cascade" and not body.get("after_trigger"):
                return JSONResponse({"error": "cascade 类型需要 after_trigger（上游触发器id）"}, status_code=400)

            from scout.automation.triggers import TriggerRule
            import uuid as _uuid
            rule = TriggerRule(
                id=str(_uuid.uuid4())[:8],
                name=body.get("name", "unnamed"),
                type=ttype,
                task_template=task_template,
                event_name=body.get("event_name", "") or ("task.complete" if ttype == "cascade" else ""),
                event_filters=body.get("event_filters", {}),
                after_trigger=body.get("after_trigger", ""),
                verification=body.get("verification", []),
                enabled=body.get("enabled", True),
                cooldown_seconds=int(body.get("cooldown_seconds", 0)),
            )
            runner.trigger_mgr.add(rule)
            return {"status": "ok", "trigger": rule.to_dict()}

        @self.app.delete("/api/triggers/{rule_id}")
        async def delete_trigger(rule_id: str):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            ok = runner.trigger_mgr.remove(rule_id)
            return {"status": "ok" if ok else "not_found"}

        @self.app.post("/api/triggers/{rule_id}/toggle")
        async def toggle_trigger(rule_id: str):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            rule = runner.trigger_mgr.get(rule_id)
            if not rule:
                return JSONResponse({"error": "触发器不存在"}, status_code=404)
            runner.trigger_mgr.enable(rule_id, not rule.enabled)
            return {"status": "ok", "enabled": not rule.enabled}

        @self.app.post("/api/triggers/{rule_id}/fire")
        async def fire_trigger(rule_id: str, req: Request):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            try:
                body = await req.json()
            except Exception:
                body = {}
            result = await runner.trigger_mgr.fire_manual(rule_id, body.get("payload", {}))
            return result

        # ── 运行记录（stats 路由必须在 /{run_id} 之前注册）──

        @self.app.get("/api/runs/stats")
        async def runs_stats(days: int = 7):
            runner = self._get_automation_runner()
            if not runner:
                return {"error": "Agent 未就绪"}
            return runner.stats(days=days)

        @self.app.get("/api/runs")
        async def list_runs(limit: int = 50, source: str = ""):
            runner = self._get_automation_runner()
            if not runner:
                return {"runs": []}
            return {"runs": runner.run_store.list(limit=limit, source=source)}

        @self.app.get("/api/runs/{run_id}")
        async def get_run(run_id: str):
            runner = self._get_automation_runner()
            if not runner:
                return JSONResponse({"error": "Agent 未就绪"}, status_code=503)
            run = runner.run_store.get(run_id)
            if not run:
                return JSONResponse({"error": "运行记录不存在"}, status_code=404)
            return run

        # ── 无人值守策略 ──

        @self.app.get("/api/automation/policy")
        async def get_automation_policy():
            runner = self._get_automation_runner()
            pm = runner.policy_mgr if runner else None
            if not pm:
                from scout.security.automation_policy import AutomationPolicyManager
                pm = AutomationPolicyManager()
            policy = pm.get_policy()
            return {
                "effective": policy.to_dict(),
                "has_org_policy": pm._org is not None,
                "has_user_policy": pm._user is not None,
            }

        @self.app.post("/api/automation/policy")
        async def set_automation_policy(req: Request):
            runner = self._get_automation_runner()
            from scout.security.automation_policy import AutomationPolicyManager, AutomationPolicy
            pm = runner.policy_mgr if runner else AutomationPolicyManager()
            body = await req.json()
            policy = pm.get_policy()
            if body.get("approval_policy"):
                if body["approval_policy"] not in ("auto", "writes", "prompt", "never"):
                    return JSONResponse({"error": "approval_policy 必须是 auto/writes/prompt/never"}, status_code=400)
                policy.approval_policy = body["approval_policy"]
            for key in ("allowed_tools", "denied_tools", "allowed_shell_patterns"):
                if isinstance(body.get(key), list):
                    setattr(policy, key, [str(v) for v in body[key]])
            if "notify_on_danger" in body:
                policy.notify_on_danger = bool(body["notify_on_danger"])
            if "max_steps" in body:
                policy.max_steps = max(1, int(body["max_steps"]))
            pm.save_user_policy(policy)
            return {"status": "ok", "policy": policy.to_dict()}

        @self.app.get("/api/automation/status")
        async def automation_status():
            runner = self._get_automation_runner()
            if not runner:
                return {"ready": False, "note": "Agent 未就绪"}
            return {
                "ready": True,
                "triggers": len(runner.trigger_mgr.list()),
                "policy": runner.policy_mgr.get_policy().to_dict(),
                "runs_7d": runner.stats(days=7).get("total", 0),
            }

        # ── 周期性自省 ──

        @self.app.get("/api/introspection/status")
        async def introspection_status():
            if not self._agent or not getattr(self._agent, "introspection", None):
                return {"enabled": False}
            return {"enabled": True, **self._agent.introspection.get_status()}

        @self.app.post("/api/introspection/run")
        async def introspection_run():
            if not self._agent or not getattr(self._agent, "introspection", None):
                return JSONResponse({"error": "自省模块未启用"}, status_code=503)
            report = await self._agent.introspection.run()
            return report

        # ── 技能导入（agentskills.io）与 Record & Replay ──

        @self.app.post("/api/skills/install-from-url")
        async def install_skill_from_url(req: Request):
            """一键安装技能 — 从 GitHub/Gitee 克隆含 SKILL.md 的技能仓库到 $SCOUT_DATA_DIR/skills/.

            Body: {"url": "https://github.com/user/repo", "branch": "main"}
            安全措施：
            - 仅接受 github.com / gitee.com 仓库 URL
            - git clone --depth 1 浅克隆到临时目录
            - 校验必须包含 SKILL.md 才算技能
            - 不执行克隆内容中的任何代码
            - 克隆完成后临时目录自动清理
            """
            body = await req.json()
            url = (body.get("url") or "").strip()
            branch = (body.get("branch") or "").strip() or None
            if not url:
                return JSONResponse({"error": "url 不能为空"}, status_code=400)

            # 安全白名单：仅允许代码托管平台的仓库 URL
            allowed_hosts = ("github.com", "gitee.com", "gitlab.com")
            if not any(h in url for h in allowed_hosts):
                return JSONResponse({"error": "仅支持 GitHub / Gitee / GitLab 仓库 URL"}, status_code=400)

            # URL 归一化：把 /blob/xxx.md、/tree/main/子目录 等页面 URL 还原为可克隆的仓库根
            # 场景：搜索结果常是仓库内文件页（如 /blob/main/README.md），git clone 不能克隆单文件
            url = self._normalize_repo_url(url)

            # 二次校验：归一化后必须是"仓库根"（owner/repo 或 owner/repo.git），拒绝其他形态
            import re as _re2
            if not _re2.match(r"^https?://(?:github\.com|gitee\.com|gitlab\.com)/[^/]+/[^/]+(?:\.git)?/?$", url):
                return JSONResponse({"error": "无法识别的仓库地址，请选择 GitHub/Gitee 仓库根页面"}, status_code=400)

            import tempfile, shutil, subprocess, os, signal, urllib.request
            tmp_dir = tempfile.mkdtemp(prefix="scout_skill_")
            try:
                # ── 双通道下载 ──
                # GitHub：github.com 主站在本机不稳定（TCP 卡死），但 codeload.github.com
                #   （tarball 下载通道）稳定。因此 GitHub 仓库走 codeload tar 包，绕开主站。
                # Gitee/GitLab：主站可达，直接用 git clone（浅克隆）。
                repo_fetched = False
                if "github.com" in url:
                    repo_fetched = self._fetch_github_tarball(url, tmp_dir)
                else:
                    cmd = ["git", "clone", "--depth", "1"]
                    if branch:
                        cmd += ["--branch", branch]
                    cmd += [url, tmp_dir]
                    try:
                        _nowin = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45, start_new_session=True, **_nowin)
                    except subprocess.TimeoutExpired as _te:
                        try:
                            os.killpg(os.getpgid(_te.pid), signal.SIGKILL)
                        except Exception:
                            pass
                        return JSONResponse({"error": "克隆超时（45s）。请检查网络后重试"}, status_code=400)
                    if result.returncode != 0:
                        return JSONResponse({"error": f"克隆失败: {result.stderr.strip()[:200]}"}, status_code=400)
                    repo_fetched = True

                if not repo_fetched:
                    return JSONResponse({"error": "仓库下载失败：本机网络无法连接 GitHub，可稍后重试或改用 Gitee 仓库"}, status_code=400)

                # 校验 SKILL.md 是否存在
                skill_md = Path(tmp_dir) / "SKILL.md"
                # 也支持子目录形式（repo 根就直接是技能）
                if not skill_md.exists():
                    # 递归找 SKILL.md（深度 <= 2）
                    found = list(Path(tmp_dir).glob("*/SKILL.md"))[:1]
                    if not found:
                        found = list(Path(tmp_dir).glob("*/*/SKILL.md"))[:1]
                    if not found:
                        return JSONResponse({"error": "该仓库不是有效技能仓库（未找到 SKILL.md）"}, status_code=400)
                    skill_md = found[0]
                    tmp_dir_repo = skill_md.parent  # 技能所在子目录
                else:
                    tmp_dir_repo = tmp_dir

                # 用 SkillManager 导入
                if not self._agent or not getattr(self._agent, "skill_mgr", None):
                    return JSONResponse({"error": "技能系统未启用"}, status_code=503)
                imported = self._agent.skill_mgr.import_agentskills_dir(tmp_dir_repo, scope="user")

                if imported <= 0:
                    return JSONResponse({"error": "导入失败：未识别到有效 SKILL.md 技能"}, status_code=400)

                return {"status": "ok", "imported": imported, "message": f"成功安装 {imported} 个技能"}
            except subprocess.TimeoutExpired:
                return JSONResponse({"error": "克隆超时（>45s），仓库可能过大或网络较慢，可稍后重试"}, status_code=400)
            except Exception as e:
                logger.error(f"一键安装技能失败: {e}")
                return JSONResponse({"error": f"安装失败: {str(e)}"}, status_code=500)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        @self.app.post("/api/skills/search-web")
        async def search_web_skills(req: Request):
            """搜索全网可复用的 Skill / 插件（生成前的现成方案推荐）.

            Body: {"query": "文档翻译", "top_k": 10}
            Returns: {"results": [SkillCandidate...]}
            """
            body = await req.json()
            query = (body.get("query") or "").strip()
            top_k = int(body.get("top_k") or 10)
            if not query:
                return JSONResponse({"error": "query 不能为空"}, status_code=400)
            from scout.engine.skill_search import get_skill_search, is_search_configured
            if not is_search_configured():
                return JSONResponse(
                    {"error": "未配置搜索引擎（SearXNG）。请在 设置 → 工具 中填写 SearXNG 实例地址后重试。"},
                    status_code=400,
                )
            try:
                results = await get_skill_search().search(query, top_k=top_k)
                return {"results": [c.to_dict() for c in results], "count": len(results)}
            except Exception as e:
                logger.error(f"搜索全网技能失败: {e}")
                return JSONResponse({"error": f"搜索失败: {e}"}, status_code=500)

        @self.app.post("/api/skills/import")
        async def import_skills(req: Request):
            """从外部 agentskills.io 兼容目录批量导入技能."""
            if not self._agent or not getattr(self._agent, "skill_mgr", None):
                return JSONResponse({"error": "技能系统未启用"}, status_code=503)
            body = await req.json()
            src_dir = body.get("dir", "").strip()
            if not src_dir:
                return JSONResponse({"error": "dir 不能为空"}, status_code=400)
            scope = body.get("scope", "user")
            count = self._agent.skill_mgr.import_agentskills_dir(src_dir, scope=scope)
            return {"status": "ok", "imported": count}

        @self.app.post("/api/skills/record")
        async def record_skill(req: Request):
            """Record & Replay — 从已有会话中起草可复用技能."""
            if not self._agent or not getattr(self._agent, "workflow_distiller", None):
                return JSONResponse({"error": "技能蒸馏未启用"}, status_code=503)
            body = await req.json()
            session_id = body.get("session_id", "").strip()
            if not session_id:
                return JSONResponse({"error": "session_id 不能为空"}, status_code=400)
            # 从会话存储加载消息
            messages = []
            store = getattr(self._agent, "session_store", None)
            if store:
                try:
                    session = store.load_session(session_id)
                    if session:
                        messages = [
                            {"role": m.role.value, "content": m.content}
                            for m in session.messages
                            if m.role.value in ("user", "assistant")
                        ]
                except Exception as e:
                    return JSONResponse({"error": f"会话加载失败: {e}"}, status_code=500)
            if not messages:
                return JSONResponse({"error": "会话不存在或无可用消息"}, status_code=404)
            result = await self._agent.workflow_distiller.record_from_session(messages)
            return result

        # ── Memories 治理配置 ──

        @self.app.get("/api/memories-config")
        async def get_memories_config():
            from scout.memory.governance import MemoriesConfig
            return MemoriesConfig.load().to_dict()

        @self.app.post("/api/memories-config")
        async def set_memories_config(req: Request):
            from scout.memory.governance import MemoriesConfig
            cfg = MemoriesConfig.load()
            body = await req.json()
            for key in cfg.to_dict():
                if key in body:
                    setattr(cfg, key, body[key])
            cfg.save()
            # 刷新 agent 的注入闸门
            if self._agent and getattr(self._agent, "memory_gate", None):
                from scout.memory.governance import GenerationGate
                self._agent.memory_gate = GenerationGate(cfg)
            return {"status": "ok", "config": cfg.to_dict()}

        # ── 分层指令链 ──

        @self.app.get("/api/instructions")
        async def get_instructions():
            chain = getattr(self._agent, "_instruction_chain", None) if self._agent else None
            if not chain:
                return {"loaded": False, "sources": []}
            return {
                "loaded": True,
                "sources": [{"scope": s.scope, "path": s.path, "chars": len(s.content)} for s in chain.sources],
                "stopped_at_limit": chain.stopped_at_limit,
            }

    def _setup_trace_routes(self):
        """观测时间线聚合 API（2026-08-13）."""

        # ── 观测时间线：按会话聚合 trace 列表 ──

        @self.app.get("/api/traces/by-session")
        async def traces_by_session(limit: int = 30):
            """按 session 聚合最近的 trace（观测页左栏列表用）."""
            if not self._agent or not self._agent.observability:
                return {"sessions": []}
            obs = self._agent.observability
            conn = obs._get_conn()
            rows = conn.execute(
                """SELECT session_id,
                          COUNT(*) as trace_count,
                          MIN(user_message) as first_message,
                          MAX(start_time) as last_time,
                          COALESCE(SUM(total_tokens), 0) as tokens,
                          COALESCE(SUM(total_cost), 0.0) as cost,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successes
                   FROM traces
                   GROUP BY session_id
                   ORDER BY last_time DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return {"sessions": [dict(r) for r in rows]}

        @self.app.get("/api/traces/session/{session_id}")
        async def traces_of_session(session_id: str):
            """某会话的全部 trace（观测页时间线用）."""
            if not self._agent or not self._agent.observability:
                return {"traces": []}
            obs = self._agent.observability
            conn = obs._get_conn()
            rows = conn.execute(
                "SELECT id FROM traces WHERE session_id = ? ORDER BY start_time ASC",
                (session_id,),
            ).fetchall()
            traces = []
            for r in rows:
                t = obs.get_trace(r["id"])
                if t:
                    traces.append(t.to_dict())
            return {"traces": traces}

    def _setup_plugin_routes(self):
        """插件 API."""

        # ── 插件 API ──

        @self.app.get("/api/plugins")
        async def list_plugins():
            """列出插件（统一使用 scout.plugins 正式版管理器，与 /api/plugins/ 保持同一数据源）."""
            from scout.plugins.manager import get_plugin_manager
            pm = get_plugin_manager()
            plugins = []
            for p in pm.list_plugins():
                item = dict(p)
                # 兼容 index.html / monitor.html 期望的字段
                item["source"] = "user_dir"
                item["loaded"] = True
                item["error"] = None
                plugins.append(item)
            return {"plugins": plugins}

        @self.app.post("/api/plugins/ai-generate")
        async def ai_generate_plugin(req: Request):
            """AI 生成插件代码."""
            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)

            data = await req.json()
            requirement = data.get("requirement", "").strip()

            if not requirement:
                return JSONResponse({"error": "请输入插件需求描述"}, status_code=400)

            # 读取插件规范文档
            spec_path = Path(__file__).parent.parent.parent / "docs" / "plugin-spec.md"
            if not spec_path.exists():
                # 尝试其他路径
                spec_path = Path(__file__).parent.parent / "docs" / "plugin-spec.md"
            
            spec_content = ""
            if spec_path.exists():
                with open(spec_path, 'r', encoding='utf-8') as f:
                    spec_content = f.read()
            else:
                # 使用简要规范
                spec_content = """
# Scout Agent 插件开发规范

## 基本结构
- 插件目录：$SCOUT_DATA_DIR/plugins/your_plugin_name/
- 主文件：__init__.py
- 配置文件：config.json（可选）

## 插件类模板
```python
from scout.plugins import Plugin, EventType
import logging

logger = logging.getLogger(__name__)

class YourPluginName(Plugin):
    name = "your_plugin_name"
    version = "1.0.0"
    author = "Your Name"
    description = "插件描述"
    priority = 100  # 0-200，数字越小优先级越高
    
    async def on_event(self, event):
        # event.event_type: EventType.BEFORE_CHAT, EventType.AFTER_CHAT, etc.
        # event.data: 事件数据
        # 返回 True 表示阻止后续插件，False 表示继续
        return False
```

## 常见模式
1. 关键词触发：检测 message 中的关键词，设置 event.data["direct_response"] 并返回 True
2. 消息过滤：修改 event.data["message"]
3. 工具监控：监听 BEFORE_TOOL/AFTER_TOOL 事件
"""

            # 构建 prompt
            prompt = f"""你是一个 Scout Agent 插件开发专家。请根据用户需求生成符合规范的插件代码。

## 用户需求
{requirement}

## 插件规范文档
{spec_content}

## 要求
1. 生成完整的插件代码（__init__.py 文件内容）
2. 代码必须符合规范文档中的结构
3. 包含必要的 import 语句
4. 包含适当的日志记录
5. 插件名称使用小写字母和下划线
6. 类名使用大驼峰命名法
7. 包含清晰的注释说明

## 返回格式
请只返回以下 JSON 格式，不要包含其他内容：
```json
{{
    "plugin_name": "插件名称（小写加下划线）",
    "code": "完整的插件代码（包含所有 import 和类定义）"
}}
```
"""

            try:
                import copy
                agent_copy = copy.copy(self._agent)
                agent_copy.callbacks = NullCallbacks()
                
                session = Session(id=str(uuid.uuid4()))
                result = await agent_copy.run_conversation(prompt, session)
                
                # 解析返回的 JSON
                import re
                json_match = re.search(r'```json\s*(.*?)\s*```', result["response"], re.DOTALL)
                if not json_match:
                    # 尝试直接解析
                    try:
                        result_data = json.loads(result["response"], strict=False)
                    except:
                        return JSONResponse({"error": "AI 返回格式错误"}, status_code=500)
                else:
                    json_str = json_match.group(1)
                    # 清理控制字符（保留换行和制表符）
                    json_str = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', json_str)
                    result_data = json.loads(json_str, strict=False)
                
                return {
                    "plugin_name": result_data.get("plugin_name", "generated_plugin"),
                    "code": result_data.get("code", "")
                }
                
            except Exception as e:
                logger.error(f"AI 生成插件失败: {e}")
                return JSONResponse({"error": f"生成失败: {str(e)}"}, status_code=500)

    def _setup_gateway_routes(self):
        """Gateway 状态 API."""

        # ── Gateway 状态 API ──

        @self.app.get("/api/status")
        async def get_status():
            """获取系统状态."""
            status = {
                "running": True,
                "tools": len(ToolRegistry.all_tools()),
                "agents": 1 if self._agent else 0,
                "adapters": [],
            }
            if self._agent:
                if self._agent.memory_store:
                    status["memories"] = len(self._agent.memory_store.list_recent(limit=1000))
                if self._agent.session_store:
                    status["sessions"] = len(self._agent.session_store.list_sessions(limit=1000))
                if self._agent.bus:
                    status["events"] = len(self._agent.bus.get_history(limit=1000))
                if self._agent.security:
                    status["security"] = True
                if self._agent.skill_mgr:
                    status["skills"] = len(self._agent.skill_mgr.list_skills())
                # 本地离线 embedding 模型状态（供前端展示）
                emb = getattr(self._agent, "_embedding_provider", None)
                if emb is not None and hasattr(emb, "model_info"):
                    status["embedding"] = emb.model_info
            return status

    def _setup_channel_routes(self):
        """渠道管理 API."""

        # ── 渠道管理 API ──

        @self.app.get("/api/channels")
        async def list_channels():
            """列出所有渠道."""
            channels = self._channel_manager.list_channels()
            return {"channels": channels}

        @self.app.post("/api/channels")
        async def add_channel(req: Request):
            """添加渠道."""
            from scout.adapters.platforms.feishu import FeishuAdapter
            from scout.adapters.platforms.wechat import WeChatAdapter
            from scout.adapters.platforms.telegram import TelegramAdapter
            from scout.adapters.platforms.dingtalk import DingTalkAdapter
            from scout.adapters.platforms.discord import DiscordAdapter
            from scout.adapters.platforms.slack import SlackAdapter
            from scout.adapters.platforms.qq import QQAdapter
            from scout.adapters.platforms.wecom_bot import WecomBotAdapter
            from scout.adapters.platforms.wechatmp import WechatMPAdapter
            from scout.adapters.platforms.wechatcom import WechatComAdapter
            from scout.adapters.platforms.wechat_kf import WechatKfAdapter
            from scout.adapters.platforms.weixin import WeixinAdapter

            data = await req.json()
            name = data.get("name")
            channel_type = data.get("type")
            config = data.get("config", {})

            if not name or not channel_type:
                return JSONResponse({"error": "缺少必要参数"}, status_code=400)

            # 根据类型创建适配器
            adapter_map = {
                "feishu": FeishuAdapter,
                "wechat": WeChatAdapter,
                "telegram": TelegramAdapter,
                "dingtalk": DingTalkAdapter,
                "discord": DiscordAdapter,
                "slack": SlackAdapter,
                "qq": QQAdapter,
                "wecom_bot": WecomBotAdapter,
                "wechatmp": WechatMPAdapter,
                "wechatcom": WechatComAdapter,
                "wechat_kf": WechatKfAdapter,
                "weixin": WeixinAdapter,
            }

            adapter_class = adapter_map.get(channel_type)
            if not adapter_class:
                return JSONResponse({"error": f"不支持的渠道类型: {channel_type}"}, status_code=400)

            try:
                # 将 name 添加到 config 中
                config["name"] = name
                adapter = adapter_class(config)
                self._channel_manager.register(name, adapter)
                self._channel_manager.save_config()
                return {"status": "ok", "channel": name}
            except Exception as e:
                logger.error(f"添加渠道失败: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.delete("/api/channels/{name}")
        async def delete_channel(name: str):
            """删除渠道."""
            if self._channel_manager.unregister(name):
                self._channel_manager.save_config()
                return {"status": "ok", "channel": name}
            else:
                return JSONResponse({"error": f"渠道不存在: {name}"}, status_code=404)

        @self.app.post("/api/channels/{name}/start")
        async def start_channel(name: str):
            """启动渠道."""
            # 设置 Agent 处理函数
            if self._agent:
                async def handle_message(message):
                    session = Session(id=f"channel_{name}_{message.sender}")
                    response = await self._agent.chat(message.content, session)
                    return response

                self._channel_manager.set_agent_handler(handle_message)

            success = await self._channel_manager.start_channel(name)
            if success:
                return {"status": "ok", "channel": name, "running": True}
            else:
                return JSONResponse({"error": "启动失败"}, status_code=500)

        @self.app.post("/api/channels/{name}/stop")
        async def stop_channel(name: str):
            """停止渠道."""
            success = await self._channel_manager.stop_channel(name)
            if success:
                return {"status": "ok", "channel": name, "running": False}
            else:
                return JSONResponse({"error": "停止失败"}, status_code=500)

        # ── 平台 Webhook 回调 ──
        # 微信/公众号/企业微信/飞书/QQ 的服务器回调统一在这里挂载，
        # 回调收到的消息经适配器解析后入队，由 ChannelManager 消费并交由 Agent 处理。
        # 2026-08-27: 此前平台回调从未挂载，webhook 模式（receive_webhook）形同虚设。

        @self.app.get("/wechatmp/webhook")
        async def wechatmp_webhook_verify(request: Request):
            adapter = self._channel_manager.get_adapter("wechatmp")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatmp 渠道未注册"}, status_code=404)
            result = await adapter.receive_webhook(dict(request.query_params), b"")
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/wechatmp/webhook")
        async def wechatmp_webhook_post(request: Request):
            adapter = self._channel_manager.get_adapter("wechatmp")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatmp 渠道未注册"}, status_code=404)
            body = await request.body()
            result = await adapter.receive_webhook(dict(request.query_params), body)
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.get("/wechatcom/webhook")
        async def wechatcom_webhook_verify(request: Request):
            adapter = self._channel_manager.get_adapter("wechatcom")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatcom 渠道未注册"}, status_code=404)
            result = await adapter.receive_webhook(dict(request.query_params), b"")
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/wechatcom/webhook")
        async def wechatcom_webhook_post(request: Request):
            adapter = self._channel_manager.get_adapter("wechatcom")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatcom 渠道未注册"}, status_code=404)
            body = await request.body()
            result = await adapter.receive_webhook(dict(request.query_params), body)
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.get("/wechat/webhook")
        async def wechat_webhook_verify(request: Request):
            adapter = self._channel_manager.get_adapter("wechat")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechat 渠道未注册"}, status_code=404)
            result = await adapter.receive_webhook(dict(request.query_params), b"")
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/wechat/webhook")
        async def wechat_webhook_post(request: Request):
            adapter = self._channel_manager.get_adapter("wechat")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechat 渠道未注册"}, status_code=404)
            body = await request.body()
            result = await adapter.receive_webhook(dict(request.query_params), body)
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/feishu/webhook")
        async def feishu_webhook(request: Request):
            adapter = self._channel_manager.get_adapter("feishu")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "feishu 渠道未注册"}, status_code=404)
            body = await request.json()
            result = await adapter.receive_webhook(body)
            return JSONResponse(result)

        @self.app.post("/qq/webhook")
        async def qq_webhook(request: Request):
            adapter = self._channel_manager.get_adapter("qq")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "qq 渠道未注册"}, status_code=404)
            if hasattr(adapter, "_verify_webhook") and not adapter._verify_webhook(dict(request.headers), await request.body()):
                return JSONResponse({"error": "signature check failed"}, status_code=403)
            body = await request.json()
            result = await adapter.receive_webhook(body)
            return JSONResponse(result)

    def _setup_chat_routes(self):
        """OpenAPI 兼容 API."""

        # ── OpenAPI 兼容 API ──

        @self.app.get("/v1/models")
        async def list_models():
            return {
                "object": "list",
                "data": [
                    {"id": "scout", "object": "model", "owned_by": "scout"},
                    {"id": "scout/default", "object": "model", "owned_by": "scout"},
                ],
            }

        @self.app.post("/v1/chat/completions")
        async def chat_completions(req: ChatRequest):
            """OpenAI 兼容 chat completions."""
            # 提取最后一条用户消息
            user_msg = ""
            for msg in reversed(req.messages):
                if msg.get("role") == "user":
                    user_msg = msg.get("content", "")
                    break

            if not user_msg:
                return JSONResponse({"error": "No user message found"}, status_code=400)

            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)

            # 获取或创建会话
            session_id = str(uuid.uuid4())
            session = Session(id=session_id)

            # 为每个请求创建独立回调（不修改共享 agent 的状态）
            import copy
            agent_copy = copy.copy(self.agent)
            agent_copy.callbacks = NullCallbacks()

            # 运行 Agent
            result = await agent_copy.run_conversation(
                user_msg, session, 
            )

            # 构建 OpenAI 兼容响应
            return ChatResponse(
                id=f"chatcmpl-{session_id}",
                created=int(datetime.now().timestamp()),
                model=req.model,
                choices=[ChatChoice(
                    message={"role": "assistant", "content": result["response"]},
                    finish_reason="stop",
                )],
            )

        # ── SSE 流式 ──

        @self.app.post("/api/chat")
        async def chat(req: ChatRequest):
            """聊天 API — 返回 SSE 流."""
            user_msg = ""
            for msg in reversed(req.messages):
                if msg.get("role") == "user":
                    user_msg = msg.get("content", "")
                    break

            if not user_msg:
                return JSONResponse({"error": "No user message"}, status_code=400)

            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)

            session_id = str(uuid.uuid4())
            session = Session(id=session_id)

            callbacks = WebCallbacks()
            import copy
            agent_copy = copy.copy(self.agent)
            # 主 agent 事件打 main 标签，与子代理(sub)区分编排过程
            from scout.core.callbacks import TaggedCallbacks
            agent_copy.callbacks = TaggedCallbacks(callbacks, agent_role="main", agent_name="主代理")
            # 让 delegate/parallel 拿到当前请求 agent（callbacks 已包装）
            from scout.tools.registry import ToolRegistry
            _prev_main_agent = getattr(ToolRegistry, "_main_agent", None)
            ToolRegistry._main_agent = agent_copy

            async def event_stream():
                # 启动 Agent 任务
                agent_task = asyncio.create_task(
                    agent_copy.run_conversation(user_msg, session)
                )
                while not agent_task.done():
                    try:
                        event = await asyncio.wait_for(callbacks.events.get(), timeout=0.1)
                        yield {"event": "message", "data": json.dumps(event, ensure_ascii=False)}
                    except asyncio.TimeoutError:
                        continue

                # 获取最终结果
                try:
                    result = await agent_task
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "done",
                            "data": {"response": result["response"], "steps": result["steps"]},
                            "timestamp": datetime.now().isoformat(),
                        }, ensure_ascii=False),
                    }
                except Exception as e:
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "error",
                            "data": {"error": str(e)},
                            "timestamp": datetime.now().isoformat(),
                        }, ensure_ascii=False),
                    }
                finally:
                    # 恢复主 Agent 引用（防止并发请求串扰）
                    try:
                        ToolRegistry._main_agent = _prev_main_agent
                    except Exception:
                        pass

            return EventSourceResponse(event_stream())

    def _setup_tool_routes(self):
        """工具列表 API."""

        # ── 工具列表 ──

        @self.app.get("/api/tools")
        async def list_tools():
            tools = ToolRegistry.all_tools()
            return {
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                        "annotations": t.annotations.model_dump(),
                    }
                    for t in tools.values()
                ]
            }

    def _setup_observability_routes(self):
        """可观测性 API."""

        @self.app.get("/api/traces")
        async def list_traces(limit: int = 20):
            """列出最近的追踪."""
            if not self._agent or not self._agent.observability:
                return {"traces": []}
            traces = self._agent.observability.list_recent_traces(limit=limit)
            return {"traces": traces}

        @self.app.get("/api/traces/{trace_id}")
        async def get_trace(trace_id: str):
            """获取追踪详情."""
            if not self._agent or not self._agent.observability:
                return JSONResponse({"error": "可观测性未启用"}, status_code=404)
            trace = self._agent.observability.get_trace(trace_id)
            if not trace:
                return JSONResponse({"error": "追踪不存在"}, status_code=404)
            return trace.to_dict()

        @self.app.get("/api/observability/stats")
        async def get_observability_stats(hours: int = 24):
            """获取可观测性统计."""
            if not self._agent or not self._agent.observability:
                return {"stats": {}}
            stats = self._agent.observability.get_stats(hours=hours)
            return {"stats": stats}

    def _setup_goal_routes(self):
        """目标管理 API."""

        @self.app.get("/api/goals")
        async def list_goals():
            """列出活跃目标."""
            if not self._agent or not self._agent.goal_manager:
                return {"goals": []}
            goals = self._agent.goal_manager.list_active_goals()
            return {
                "goals": [
                    {
                        "id": g.id,
                        "title": g.title,
                        "description": g.description,
                        "status": g.status,
                        "progress": g.overall_progress,
                        "tasks_count": len(g.tasks),
                        "completed_tasks": g.completed_tasks,
                        "created_at": g.created_at.isoformat(),
                    }
                    for g in goals
                ]
            }

        @self.app.get("/api/goals/{goal_id}")
        async def get_goal(goal_id: str):
            """获取目标详情."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=404)
            goal = self._agent.goal_manager.get_goal(goal_id)
            if not goal:
                return JSONResponse({"error": "目标不存在"}, status_code=404)
            return {
                "id": goal.id,
                "title": goal.title,
                "description": goal.description,
                "status": goal.status,
                "progress": goal.overall_progress,
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description,
                        "status": t.status,
                        "progress": t.progress,
                        "created_at": t.created_at.isoformat(),
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                    }
                    for t in goal.tasks
                ],
                "created_at": goal.created_at.isoformat(),
            }

        @self.app.post("/api/goals")
        async def create_goal(req: Request):
            """创建新目标."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            title = body.get("title")
            description = body.get("description", "")
            if not title:
                return JSONResponse({"error": "标题不能为空"}, status_code=400)
            goal = self._agent.goal_manager.create_goal(title, description)
            return {"status": "ok", "goal_id": goal.id}

        @self.app.post("/api/goals/{goal_id}/tasks")
        async def add_task(goal_id: str, req: Request):
            """为目标添加任务."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            title = body.get("title")
            description = body.get("description", "")
            if not title:
                return JSONResponse({"error": "标题不能为空"}, status_code=400)
            task = self._agent.goal_manager.add_task(goal_id, title, description)
            if not task:
                return JSONResponse({"error": "目标不存在"}, status_code=404)
            return {"status": "ok", "task_id": task.id}

        @self.app.put("/api/tasks/{task_id}")
        async def update_task(task_id: str, req: Request):
            """更新任务进度."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            progress = body.get("progress")
            status = body.get("status")
            if progress is not None:
                self._agent.goal_manager.update_task_progress(task_id, progress, status)
            return {"status": "ok"}

        @self.app.put("/api/goals/{goal_id}")
        async def update_goal(goal_id: str, req: Request):
            """更新目标状态."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            status = body.get("status")
            if status and status in ("active", "completed", "paused", "abandoned"):
                self._agent.goal_manager.update_goal_status(goal_id, status)
            return {"status": "ok"}

        @self.app.delete("/api/goals/{goal_id}")
        async def delete_goal(goal_id: str):
            """删除目标及其所有任务."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            self._agent.goal_manager.delete_goal(goal_id)
            return {"status": "ok"}

        @self.app.delete("/api/tasks/{task_id}")
        async def delete_task(task_id: str):
            """删除任务."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            self._agent.goal_manager.delete_task(task_id)
            return {"status": "ok"}

    def _setup_checkpoint_routes(self):
        """Checkpoint 管理 API."""

        @self.app.get("/api/checkpoints")
        async def list_checkpoints():
            """列出所有 checkpoints."""
            if not self._agent or not self._agent.checkpoint_manager:
                return {"checkpoints": []}
            checkpoints = self._agent.checkpoint_manager.list_checkpoints()
            return {"checkpoints": checkpoints}

        @self.app.get("/api/checkpoints/{session_id}")
        async def get_checkpoint(session_id: str):
            """获取指定会话的 checkpoint."""
            if not self._agent or not self._agent.checkpoint_manager:
                return JSONResponse({"error": "Checkpoint 系统未启用"}, status_code=400)
            checkpoint = self._agent.checkpoint_manager.load_checkpoint(session_id)
            if not checkpoint:
                return JSONResponse({"error": "Checkpoint 不存在"}, status_code=404)
            return checkpoint.to_dict()

        @self.app.delete("/api/checkpoints/{session_id}")
        async def delete_checkpoint(session_id: str):
            """删除指定会话的 checkpoint."""
            if not self._agent or not self._agent.checkpoint_manager:
                return JSONResponse({"error": "Checkpoint 系统未启用"}, status_code=400)
            deleted = self._agent.checkpoint_manager.delete_checkpoint(session_id)
            if not deleted:
                return JSONResponse({"error": "Checkpoint 不存在"}, status_code=404)
            return {"status": "ok", "message": "Checkpoint 已删除"}

        @self.app.post("/api/sessions/{session_id}/resume")
        async def resume_session(session_id: str):
            """从 checkpoint 恢复会话."""
            if not self._agent:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=400)
            if not self._agent.checkpoint_manager:
                return JSONResponse({"error": "Checkpoint 系统未启用"}, status_code=400)
            
            result = await self._agent.resume_from_checkpoint(session_id)
            if not result:
                return JSONResponse({"error": "Checkpoint 不存在"}, status_code=404)
            return result

    def _setup_a2a_routes(self):
        """A2A (Agent-to-Agent) 协议 API."""
        from scout.a2a.server import A2AServer
        from scout.a2a.types import TaskSendRequest, A2AMessage, TextPart, TaskStatus
        
        # 初始化 A2A Server
        self._a2a_server = A2AServer(self._agent) if self._agent else None
        
        @self.app.get("/.well-known/agent.json")
        async def get_agent_card():
            """获取 Agent Card - A2A 协议标准端点."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            card = self._a2a_server.get_agent_card()
            return card.model_dump()
        
        @self.app.post("/a2a/tasks/send")
        async def send_task(request: TaskSendRequest):
            """接收并执行 A2A 任务."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            response = await self._a2a_server.handle_task(request)
            return response.model_dump()
        
        @self.app.get("/a2a/tasks/{task_id}")
        async def get_task(task_id: str):
            """获取任务状态."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            task = self._a2a_server.get_task(task_id)
            if not task:
                return JSONResponse({"error": "任务不存在"}, status_code=404)
            return task.model_dump()
        
        @self.app.get("/a2a/tasks")
        async def list_tasks():
            """列出所有任务."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            tasks = self._a2a_server.list_tasks()
            return {"tasks": [t.model_dump() for t in tasks]}
        
        # A2A 客户端管理 API
        @self.app.get("/api/a2a/agents")
        async def list_a2a_agents():
            """列出已注册的远程 A2A agents."""
            if not self._agent or not self._agent.a2a_manager:
                return {"agents": []}
            agents = self._agent.a2a_manager.list_agents()
            return {"agents": agents}
        
        @self.app.post("/api/a2a/agents")
        async def add_a2a_agent(req: Request):
            """注册远程 A2A agent."""
            if not self._agent or not self._agent.a2a_manager:
                return JSONResponse({"error": "A2A 未启用"}, status_code=400)
            
            body = await req.json()
            name = body.get("name")
            url = body.get("url")
            
            if not name or not url:
                return JSONResponse({"error": "缺少 name 或 url"}, status_code=400)
            
            # SSRF 缓解：scheme/主机名校验 + 私有地址拦截（a2a/client.check_url_ssrf）
            try:
                parsed = urlparse(url)
            except Exception:
                parsed = None
            if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname:
                return JSONResponse({"error": "仅支持 http/https 且包含主机名的 URL"}, status_code=400)
            
            try:
                client = self._agent.a2a_manager.add_agent(name, url)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return {"status": "ok", "agent": {"name": name, "url": url}}
        
        @self.app.delete("/api/a2a/agents/{name}")
        async def remove_a2a_agent(name: str):
            """移除远程 A2A agent."""
            if not self._agent or not self._agent.a2a_manager:
                return JSONResponse({"error": "A2A 未启用"}, status_code=400)
            
            removed = self._agent.a2a_manager.remove_agent(name)
            if not removed:
                return JSONResponse({"error": "Agent 不存在"}, status_code=404)
            return {"status": "ok"}
        
        @self.app.post("/api/a2a/send")
        async def send_to_a2a_agent(req: Request):
            """向远程 A2A agent 发送任务."""
            if not self._agent or not self._agent.a2a_manager:
                return JSONResponse({"error": "A2A 未启用"}, status_code=400)
            
            body = await req.json()
            agent_name = body.get("agent")
            message = body.get("message")
            
            if not agent_name or not message:
                return JSONResponse({"error": "缺少 agent 或 message"}, status_code=400)
            
            client = self._agent.a2a_manager.get_client(agent_name)
            if not client:
                return JSONResponse({"error": "Agent 不存在"}, status_code=404)

            # 防滥用：任务消息长度上限
            if len(message) > 100_000:
                return JSONResponse({"error": "任务消息过长（上限 100KB）"}, status_code=400)

            try:
                task = await client.send_task(message)
                return {
                    "status": "ok",
                    "task_id": task.id,
                    "task_status": task.status.state,
                    "response": task.messages[-1].parts[0].text if task.messages and task.messages[-1].role == "agent" else None
                }
            except Exception as e:
                return JSONResponse({"error": f"发送失败: {str(e)}"}, status_code=500)

    def _setup_voice_routes(self):
        """语音 API — ASR / TTS / 语音对话（voice 模块接线入口）."""
        import tempfile
        from pathlib import Path as _Path

        # ── 语音能力查询 ──

        @self.app.get("/api/voice/capabilities")
        async def voice_capabilities():
            return {"status": "ok", "capabilities": self._voice_handler.get_capabilities()}

        # ── 语音识别：上传音频 → 文本 ──

        @self.app.post("/api/voice/asr")
        async def voice_asr(request: Request):
            form = await request.form()
            audio = form.get("audio") or form.get("file")
            if not audio:
                return JSONResponse({"error": "缺少音频文件 (audio)"}, status_code=400)
            try:
                data = await audio.read()
            except Exception:
                return JSONResponse({"error": "读取音频失败"}, status_code=400)
            if not data:
                return JSONResponse({"error": "音频内容为空"}, status_code=400)

            suffix = _Path(audio.filename or "audio.webm").suffix or ".webm"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                language = (form.get("language") or None) if form.get("language") else None
                text = await self._voice_handler.speech_to_text(tmp_path, language=language)
            except RuntimeError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:  # noqa: BLE001
                logger.error(f"ASR 失败: {e}")
                return JSONResponse({"error": f"语音识别失败: {e}"}, status_code=500)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return {"status": "ok", "text": text}

        # ── 语音合成：文本 → 音频 ──

        @self.app.post("/api/voice/tts")
        async def voice_tts(request: Request):
            body = await request.json()
            text = (body.get("text") or "").strip()
            if not text:
                return JSONResponse({"error": "缺少文本 (text)"}, status_code=400)
            if len(text) > 4000:
                return JSONResponse({"error": "文本过长，请控制在 4000 字符以内"}, status_code=400)

            response_format = (body.get("response_format") or "mp3").lower()
            try:
                audio_path = await self._voice_handler.text_to_speech(
                    text,
                    voice=body.get("voice"),
                    response_format=response_format,
                )
            except RuntimeError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:  # noqa: BLE001
                logger.error(f"TTS 失败: {e}")
                return JSONResponse({"error": f"语音合成失败: {e}"}, status_code=500)

            try:
                audio_bytes = _Path(audio_path).read_bytes()
            finally:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
            media_type = "audio/mpeg" if response_format == "mp3" else f"audio/{response_format}"
            return Response(content=audio_bytes, media_type=media_type)

        # ── 语音对话：音频 → 回复音频 ──

        @self.app.post("/api/voice/chat")
        async def voice_chat(request: Request):
            form = await request.form()
            audio = form.get("audio") or form.get("file")
            if not audio:
                return JSONResponse({"error": "缺少音频文件 (audio)"}, status_code=400)
            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)
            try:
                data = await audio.read()
            except Exception:
                return JSONResponse({"error": "读取音频失败"}, status_code=400)
            if not data:
                return JSONResponse({"error": "音频内容为空"}, status_code=400)

            suffix = _Path(audio.filename or "audio.webm").suffix or ".webm"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                language = (form.get("language") or None) if form.get("language") else None

                # 1. ASR：音频 → 文本
                text = await self._voice_handler.speech_to_text(tmp_path, language=language)

                # 2. 会话 → Agent 回复
                import copy
                session_id = str(uuid.uuid4())
                session = Session(id=session_id)
                agent_copy = copy.copy(self.agent)
                agent_copy.callbacks = NullCallbacks()
                result = await agent_copy.run_conversation(text, session)
                reply_text = result.get("response", "")

                # 3. TTS：回复文本 → 音频
                response_format = (form.get("response_format") or "mp3").lower()
                audio_path = await self._voice_handler.text_to_speech(
                    reply_text,
                    voice=(form.get("voice") or None) if form.get("voice") else None,
                    response_format=response_format,
                )
            except RuntimeError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:  # noqa: BLE001
                logger.error(f"语音对话失败: {e}")
                return JSONResponse({"error": f"语音对话失败: {e}"}, status_code=500)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            audio_bytes = _Path(audio_path).read_bytes()
            try:
                os.unlink(audio_path)
            except OSError:
                pass
            media_type = "audio/mpeg" if response_format == "mp3" else f"audio/{response_format}"
            return Response(content=audio_bytes, media_type=media_type)

    def _setup_websocket_endpoint(self):
        """WebSocket 端点."""

        # ── WebSocket ──

        @self.app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            # ── WebSocket 认证（安全修复 2026-08-09；登录认证开关 2026-08-21）──
            # 与 HTTP 中间件一致：登录认证开关关闭（默认）时全部放行；
            # 开启时要求 token（未设置凭证时仅放行本地回环，避免服务暴露在 0.0.0.0 时被外部无鉴权连入）。
            from scout.security.auth import AuthManager, verify_token
            _ws_auth_required = False
            try:
                from scout.config.manager import ConfigManager
                _ws_auth_required = bool(getattr(ConfigManager().load(), "auth_enabled", False))
            except Exception:
                _ws_auth_required = True
            if _ws_auth_required:
                _am = AuthManager()
                if _am.has_credentials():
                    _token = ws.query_params.get("token") or ws.query_params.get("access_token") or ""
                    if not _token or not verify_token(_token):
                        await ws.close(code=4401, reason="未授权")
                        return
                else:
                    _client_host = (ws.client.host if ws.client else "") or ""
                    if _client_host not in ("127.0.0.1", "::1", "localhost"):
                        await ws.close(code=4401, reason="未授权")
                        return
            await ws.accept()

            # 支持通过 query param 指定已有 session
            # ★ 2026-08-29：session_store 不依赖 self._agent——exe 启动时 agent 尚未
            # 重建（create_web_app() 不传 agent），此前导致带 sid 也永远"找不到"→
            # 每次启动都误报"会话已丢失"并新建会话（历史明明还在）。
            sid_param = ws.query_params.get("session_id")
            restored = True
            _sstore = self._session_store()
            if sid_param and _sstore:
                existing = _sstore.load_session(sid_param)
                if existing:
                    session = existing
                    restored = True
                else:
                    # ★ 修复 2026-08-27：sid 不存在时一律新建空会话，不再"复用最近会话"。
                    # 此前复用最近会话会导致：服务重启/数据丢失后，前端带旧 sid 重连，
                    # 后端悄悄把另一个会话顶上来 → 用户看到"历史错乱/消失/变成别人的对话"。
                    # 现在：新建空会话并在 session_init 标注 lost，前端可感知并明确提示。
                    session = Session(id=str(uuid.uuid4()))
                    restored = False
                    # ★ 修复 2026-08-29：新建会话立即落库。此前仅内存对象不落库，
                    # 前端 session_init 后 loadSession(新sid) 必然 404 → 误弹"会话不存在"提示。
                    try:
                        await _sstore.async_create(session.id, agent_id=session.agent_id)
                    except Exception:
                        logger.warning("新建会话落库失败（sid 不存在分支）", exc_info=True)
            elif ws.query_params.get("new") == "1":
                # ★ 2026-08-29：用户主动点"新对话"→ 强制新建，不恢复历史。
                session = Session(id=str(uuid.uuid4()))
                try:
                    await _sstore.async_create(session.id, agent_id=session.agent_id)
                except Exception:
                    logger.warning("新建会话落库失败（new=1 分支）", exc_info=True)
            else:
                # ★ 2026-08-29：无 sid（首启/缓存丢失）：有历史会话 → 恢复最近一个，
                # 不再每次启动都新建空会话导致堆积；无任何历史 → 才新建并落库。
                restored = True
                session = None
                try:
                    recent = _sstore.list_sessions(limit=1)
                    if recent:
                        session = _sstore.load_session(recent[0]["id"])
                except Exception:
                    logger.warning("恢复最近会话失败（无 sid 分支）", exc_info=True)
                if session is None:
                    session = Session(id=str(uuid.uuid4()))
                    try:
                        await _sstore.async_create(session.id, agent_id=session.agent_id)
                    except Exception:
                        logger.warning("新建会话落库失败（无 sid 分支）", exc_info=True)

            # 通知前端当前 session_id（restored=false 表示请求的旧会话已不存在，已新建）
            await ws.send_json({
                "type": "session_init",
                "data": {"session_id": session.id, "restored": restored},
            })

            # ★ 断裂点修复 2: 注册 WebSocket 连接到广播池
            self._active_ws_connections.add(ws)
            # 2026-08-11: pending 消息队列 — 解决 listen_cancel 并发消费 WebSocket 消息导致
            # 后续 chat 消息被丢弃（第2条消息卡死）的问题。listen_cancel 收到的非控制消息存入队列，
            # 外层循环优先从队列取，保证消息不丢失。
            import asyncio as _asyncio
            _pending_msgs: _asyncio.Queue = _asyncio.Queue()
            try:
                while True:
                    # 优先处理 pending 队列（listen_cancel 缓存的消息）
                    if not _pending_msgs.empty():
                        try:
                            data = _pending_msgs.get_nowait()
                        except _asyncio.QueueEmpty:
                            data = await ws.receive_json()
                    else:
                        data = await ws.receive_json()

                    # 处理取消信号
                    if data.get("type") == "cancel":
                        if self._agent:
                            self._agent.cancel()
                        await ws.send_json({"type": "cancelled", "data": {"message": "已停止生成"}})
                        continue

                    # 处理心跳 ping — 立即回 pong（保活，检测静默断连）
                    if data.get("type") == "ping":
                        await ws.send_json({"type": "pong", "data": {}})
                        continue

                    # 处理 Human-in-the-Loop 确认响应
                    if data.get("type") == "confirm_response":
                        request_id = data.get("request_id")
                        approved = data.get("approved", False)
                        if request_id and request_id in self._pending_confirmations:
                            future = self._pending_confirmations.pop(request_id)
                            if not future.done():
                                future.set_result(approved)
                        continue

                    user_msg = data.get("content", "")
                    ws_attachments = data.get("attachments", [])

                    if not user_msg.strip() and not ws_attachments:
                        continue

                    # 处理附件 — 保存到临时文件
                    attachment_info = []
                    for att in ws_attachments:
                        att_name = att.get("name", "unknown")
                        att_type = att.get("type", "")
                        att_data = att.get("data", "")
                        if att_data and att_data.startswith("data:"):
                            # 解析 base64 data URL
                            header, _, b64data = att_data.partition(",")
                            import base64
                            try:
                                file_bytes = base64.b64decode(b64data)
                                # 保存到临时目录
                                import tempfile, os
                                tmp_dir = os.path.join(tempfile.gettempdir(), "scout_uploads")
                                os.makedirs(tmp_dir, exist_ok=True)
                                file_path = os.path.join(tmp_dir, f"{uuid.uuid4().hex[:8]}_{att_name}")
                                with open(file_path, "wb") as f:
                                    f.write(file_bytes)
                                attachment_info.append({
                                    "name": att_name,
                                    "type": att_type,
                                    "size": att.get("size", 0),
                                    "path": file_path,
                                })
                            except Exception as e:
                                logger.warning(f"Failed to save attachment {att_name}: {e}")

                    # 如果有附件，附加到消息中
                    if attachment_info:
                        att_summary = "\n\n[附件]\n" + "\n".join(
                            f"- {a['name']} ({a['type']}, {a['size']} bytes) → {a['path']}"
                            for a in attachment_info
                        )
                        user_msg = user_msg + att_summary if user_msg.strip() else att_summary

                    if not user_msg.strip():
                        continue

                    if not self._agent:
                        await ws.send_json({"type": "error", "data": {"error": "请先在设置中配置 API Key"}})
                        continue

                    # 重新加载 session（可能被 PUT/DELETE API 修改过）
                    if self._agent.session_store:
                        fresh = self._agent.session_store.load_session(session.id)
                        if fresh:
                            session = fresh

                    # 编辑后发送：截断到编辑起点
                    edit_from = data.get("edit_from")
                    if edit_from is not None and self._agent.session_store:
                        # 保护：截断点必须落在有效范围内（防止异常值把整个会话清空）
                        edit_from = max(0, min(int(edit_from), len(session.messages)))
                        if edit_from < len(session.messages):
                            deleted_msgs = session.messages[edit_from:]
                            # 截断保护：先归档将被删除的消息（可事后恢复/审计）
                            try:
                                self._agent.session_store.archive_messages(
                                    session.id, deleted_msgs, reason="edit_truncate"
                                )
                            except Exception as _arch_err:
                                import logging
                                logging.getLogger(__name__).warning(f"归档失败: {_arch_err}")
                            # 清理被截断消息的记忆
                            if self._agent.memory_store:
                                for msg in deleted_msgs:
                                    if msg.content and len(msg.content) > 5:
                                        self._agent.memory_store.delete_by_content(msg.content)
                            session.messages = session.messages[:edit_from]
                            session.status = "idle"
                            self._agent.session_store.save_session(session)

                    # 为每个请求创建独立 Agent 副本
                    import copy
                    agent_copy = copy.copy(self._agent)
                    callbacks = WebCallbacks(ws)
                    # 主 agent 事件打 main 标签，与子代理(sub)区分编排过程
                    from scout.core.callbacks import TaggedCallbacks
                    agent_copy.callbacks = TaggedCallbacks(callbacks, agent_role="main", agent_name="主代理")
                    # 让 delegate/parallel 子代理拿到"当前请求"的 agent（callbacks 已包装），
                    # 否则 _main_agent 指向原始 agent（NullCallbacks）→ 子代理事件丢失
                    from scout.tools.registry import ToolRegistry
                    _prev_main_agent = getattr(ToolRegistry, "_main_agent", None)
                    ToolRegistry._main_agent = agent_copy

                    # ── 聊天模型选择：按消息覆盖模型（不改全局配置，2026-08-13）──
                    _req_model = str(data.get("model") or "").strip()
                    if _req_model:
                        try:
                            _override_llm = self._get_chat_llm(_req_model)
                            if _override_llm:
                                agent_copy.llm = _override_llm
                                # 用户显式选择的模型优先于双模型（thinker/executor）
                        except Exception as _model_err:
                            logger.warning(f"聊天模型切换失败({_req_model}): {_model_err}")

                    # 用流式对话 + 事件队列并发推送
                    _turn_ws_start = time.time()
                    async def run_stream():
                        async for delta in agent_copy.stream_conversation(user_msg, session, attachments=attachment_info or None):
                            try:
                                # 推送流式文本
                                if delta.text:
                                    await ws.send_json({"type": "stream_delta", "data": {"text": delta.text}})
                                # 推送猜测问题
                                if delta.suggestions:
                                    await ws.send_json({"type": "suggestions", "data": {"items": delta.suggestions}})
                                # 推送队列中剩余事件（fallback）
                                while not callbacks.events.empty():
                                    event = callbacks.events.get_nowait()
                                    await ws.send_json(event)
                            except (RuntimeError, WebSocketDisconnect):
                                return  # WebSocket 已断开，停止推送
                            if delta.done:
                                # 发 done，继续循环等 suggestions（suggestions 在 done 之后 yield）
                                steps = len([m for m in session.messages if m.role == Role.ASSISTANT])
                                # ── 本次回合 token/缓存/耗时统计（重试直到查到记录，容忍 record 落库延迟）──
                                turn_stats = None
                                for _retry in range(4):
                                    _s = self._collect_ws_usage(session.id, _turn_ws_start)
                                    if _s.get("calls", 0) > 0:
                                        turn_stats = _s
                                        break
                                    await asyncio.sleep(0.4)
                                await ws.send_json({"type": "done", "data": {"steps": steps, "usage": turn_stats or self._collect_ws_usage(session.id, _turn_ws_start)}})

                    # 并发：agent 流式输出 + 监听 cancel 消息
                    stream_task = asyncio.create_task(run_stream())
                    cancel_task = None
                    try:
                        # 同时监听 WebSocket 消息（只处理 cancel）
                        async def listen_cancel():
                            while not stream_task.done():
                                try:
                                    msg = await asyncio.wait_for(ws.receive_json(), timeout=0.5)
                                    if msg.get("type") == "cancel":
                                        agent_copy.cancel()
                                        await ws.send_json({"type": "cancelled", "data": {"message": "已停止生成"}})
                                        break
                                    elif msg.get("type") == "ping":
                                        # 生成期间外层循环被 stream_task 阻塞，心跳在这里回应
                                        await ws.send_json({"type": "pong", "data": {}})
                                    elif msg.get("type") == "confirm_response":
                                        # HITL 确认响应同样需处理，避免卡住
                                        request_id = msg.get("request_id")
                                        approved = msg.get("approved", False)
                                        if request_id and request_id in self._pending_confirmations:
                                            future = self._pending_confirmations.pop(request_id)
                                            if not future.done():
                                                future.set_result(approved)
                                    else:
                                        # 2026-08-11: 非控制消息（chat 等）缓存到队列，避免被丢弃
                                        await _pending_msgs.put(msg)
                                except asyncio.TimeoutError:
                                    continue
                                except (RuntimeError, WebSocketDisconnect):
                                    break

                        cancel_task = asyncio.create_task(listen_cancel())
                        await stream_task
                    except Exception as e:
                        # ★ 记忆兜底：异常退出时同样尝试沉淀本回合记忆。
                        # 消息已由 stream_conversation 逐条落库（数据不丢），
                        # 这里仅补记忆抽取，避免"LLM 中途报错 → 本回合无任何记忆"。
                        try:
                            if getattr(agent_copy, "memory_extractor", None):
                                await agent_copy._maybe_extract_session_memory(session)
                        except Exception as _mem_guard:
                            logger.debug(f"异常路径记忆抽取失败(可忽略): {_mem_guard}")
                        try:
                            await ws.send_json({"type": "error", "data": {"error": str(e)}})
                        except (RuntimeError, WebSocketDisconnect):
                            # WebSocket 已断开，无法发送错误消息
                            logger.debug(f"Cannot send error to client (connection closed): {e}")
                    finally:
                        if cancel_task and not cancel_task.done():
                            cancel_task.cancel()
                        # 恢复主 Agent 引用（防止并发请求串扰）
                        try:
                            from scout.tools.registry import ToolRegistry
                            ToolRegistry._main_agent = _prev_main_agent
                        except Exception:
                            pass
                        # ★ 兜底持久化：无论正常完成/报错/取消，都将会话写入数据库。
                        # 修复"刷新后历史消失"——此前会话卡在 acting 状态只存了 checkpoint，
                        # 数据库 sessions 表无记录，刷新后侧边栏看不到任何历史。
                        try:
                            if self._agent is not None and self._agent.session_store is not None:
                                if session.messages:
                                    if session.status in ("", "idle"):
                                        session.status = "done"
                                    self._agent.session_store.save_session(session)
                                    logger.info(f"[PERSIST] 兜底保存会话 {session.id[:8]} ({len(session.messages)} 条消息)")
                        except Exception as _persist_err:
                            logger.warning(f"[PERSIST] 兜底保存失败: {_persist_err}")

            except WebSocketDisconnect:
                # ★ 断裂点修复 2: 断开时从广播池移除
                self._active_ws_connections.discard(ws)
                pass
            except RuntimeError:
                # WebSocket 被客户端异常关闭（连接状态已断开）— 从广播池移除，避免连接泄漏
                self._active_ws_connections.discard(ws)
            except Exception as e:
                # 兜底：单个连接的任何异常都不允许向上冒泡拖垮整个 uvicorn 进程
                try:
                    await ws.send_json({"type": "error", "data": {"error": f"连接异常: {e}"}})
                except Exception as send_err:
                    logger.debug(f"Cannot send error to client: {send_err}")
