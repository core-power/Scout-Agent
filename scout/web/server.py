"""Scout Web Server — FastAPI + 静态文件 + WebSocket.

启动: scout --web --port 8848
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scout.adapters.web import WebAdapter
from scout.security.auth import AuthManager

# 日志清理后台任务配置
_LOG_CLEAN_INTERVAL = 24 * 3600  # 每天检查一次（只要保留超过30天的就会被清掉）
_LOG_RETENTION_DAYS = 30


async def _log_cleanup_loop() -> None:
    """后台日志清理循环 — 每天检查并删除超过保留天数的旧日志."""
    from scout.core.log_config import cleanup_logs

    logger = logging.getLogger("scout.log_cleanup")
    while True:
        try:
            removed = cleanup_logs(retention_days=_LOG_RETENTION_DAYS)
            if removed:
                logger.info(f"日志清理: 已删除 {removed} 个超过 {_LOG_RETENTION_DAYS} 天的旧日志")
        except Exception as e:
            logger.warning(f"日志清理异常: {e}")
        await asyncio.sleep(_LOG_CLEAN_INTERVAL)


def _get_allowed_origins() -> list[str]:
    """从环境变量、配置或默认值获取允许的 CORS 来源.

    优先级：环境变量 SCOUT_CORS_ORIGINS > 配置文件 cors_origins > 默认本地地址。
    """
    # 1. 优先从环境变量读取
    env_origins = os.environ.get("SCOUT_CORS_ORIGINS", "").strip()
    if env_origins:
        return [o.strip() for o in env_origins.split(",") if o.strip()]
    # 2. 从配置文件读取
    try:
        from scout.config import ConfigManager
        cfg = ConfigManager().load()
        cfg_origins = getattr(cfg, "cors_origins", None) or []
        if cfg_origins:
            return [o.strip() for o in cfg_origins if o.strip()]
    except Exception as e:
        logging.getLogger(__name__).debug("读取 CORS 配置失败，使用默认本地地址: %s", e)
    # 3. 默认允许本地开发地址
    return [
        "http://localhost:8848",
        "http://127.0.0.1:8848",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def _is_initialization_whitelist(path: str) -> bool:
    """未初始化凭证阶段仍放行的路径（登录引导 / 静态资源）.

    /api/files/download 不在白名单内：无凭证时同样返回 401，
    避免默认暴露下下载用户目录文件。
    ★ 2026-09-25：外部 webhook 放行已随 Webhook 管理功能移除。
    """
    return (
        path.startswith("/api/auth")
        or path.startswith("/static")
        or path.startswith("/.well-known")
    )


def create_web_app(agent=None) -> FastAPI:
    """创建 FastAPI 应用并挂载所有路由."""
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # 启动时：立即清理一次旧日志 + 启动后台清理循环
        cleanup_task = asyncio.create_task(_log_cleanup_loop())
        try:
            # ★ 2026-09-25：文件监听器（FileWatcher）已按需求移除 —— Windows 桌面
            # 场景用不上，且启动即拉起 watchdog 监听增加常驻开销。相关模块
            # （automation/watcher.py、watcher_api.py、watcher.html）已删除，
            # 需要时从 git 历史恢复。

            # ★ 2026-09-01 修复：星夜凝萃调度器在此（事件循环就绪后）补启动。
            # init_starlight 在同步上下文调用时无事件循环，create_task 失败，
            # 导致定时蒸馏协程从未运行（"no running event loop" / never awaited）。
            # ★ 2026-09-25：启动补偿（maybe_catch_up）改由 start_scheduler()
            # 成功后自行挂载 —— 此处 agent 未就绪时 get_starlight() 为 None，
            # 单独挂载会落空（实测）。
            try:
                from scout.automation.starlight import get_starlight
                _sl = get_starlight()
                if _sl is not None:
                    _sl.start_scheduler()
            except Exception as e:
                logging.getLogger(__name__).warning(f"星夜凝萃调度器补启动失败: {e}")

            # 启动时立即清理一次
            from scout.core.log_config import cleanup_logs
            try:
                removed = cleanup_logs(retention_days=_LOG_RETENTION_DAYS)
                if removed:
                    logging.getLogger("scout.log_cleanup").info(
                        f"启动清理: 已删除 {removed} 个超过 {_LOG_RETENTION_DAYS} 天的旧日志"
                    )
            except Exception as e:
                logging.getLogger("scout.log_cleanup").warning("启动日志清理失败: %s", e)
            yield
        finally:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except (asyncio.CancelledError, Exception):
                pass

            # ★ 2026-09-14：退出兜底落盘 —— 把内存中的活跃会话写盘。此前只在
            # 回合收尾落盘（且工具中途不落库），正常关闭时「未收尾回合」或
            # 「距上次节流落盘 <5s 的增量」会丢失（用户反馈「重启后最新对话
            # 消息丢失」）。走线程池执行，避免同步全量重写阻塞关闭流程。
            #
            # ★ 2026-09-20：改用独立 threading.Thread，而非 asyncio.to_thread。
            # uvicorn 关闭时默认事件循环 executor 已 shutdown，to_thread 抛
            # "RuntimeError: cannot schedule new futures after shutdown"（实测日志）。
            # 独立线程不依赖事件循环，干净收尾；join 限时等待，超时不阻塞退出。
            try:
                import threading as _th

                from scout.session.store import get_session_store

                _box = {"n": -1, "err": None}
                _done = _th.Event()

                def _flush_worker():
                    try:
                        _box["n"] = get_session_store().flush_active()
                    except Exception as _fe:  # noqa: BLE001
                        _box["err"] = _fe
                    finally:
                        _done.set()

                _t = _th.Thread(target=_flush_worker, name="scout-exit-flush", daemon=True)
                _t.start()
                if not _done.wait(timeout=8):
                    logging.getLogger(__name__).warning("退出 flush 超时（>8s），跳过")
                if _box["err"] is not None:
                    logging.getLogger(__name__).warning("退出 flush 失败: %s", _box["err"], exc_info=True)
                elif _box["n"] > 0:
                    logging.getLogger(__name__).info("退出前已落盘 %d 个活跃会话", _box["n"])
            except Exception:
                logging.getLogger(__name__).warning("退出 flush 失败（不影响关闭）", exc_info=True)

    # 交互式 API 文档默认关闭（避免泄露 API 结构），可通过配置 web_docs
    # 或环境变量 SCOUT_ENABLE_DOCS=1 开启。
    _docs_enabled = os.environ.get("SCOUT_ENABLE_DOCS", "").lower() in ("1", "true", "yes")
    if not _docs_enabled:
        try:
            from scout.config.manager import ConfigManager
            _docs_enabled = bool(ConfigManager().load().web_docs)
        except Exception as e:
            logging.getLogger(__name__).debug("读取 web_docs 配置失败: %s", e)
    app = FastAPI(
        title="Scout Agent",
        version="1.0.0.5",
        lifespan=_lifespan,
        docs_url="/docs" if _docs_enabled else None,
        redoc_url="/redoc" if _docs_enabled else None,
        openapi_url="/openapi.json" if _docs_enabled else None,
    )

    # CORS — 从配置读取允许的域名，不再使用 "*"
    allowed_origins = _get_allowed_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 全局认证中间件（安全修复 2026-08-09）──
    # 保护所有 /api/* 管理接口，未认证返回 401。
    # 白名单排除：登录接口、外部 webhook、健康检查、静态资源。
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from scout.security.auth import verify_token

    # 注意：/api/auth/* 统一按前缀放行，无需精确匹配。
    # 已移除 "/api/newsfeed" 幽灵豁免（该路由不存在，避免未来误开放）。

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            # 静态资源、页面、公开路径放行
            if path.startswith("/static") or path.startswith("/.well-known"):
                return await call_next(request)
            # 仅保护敏感 API 前缀：/api、/v1、/a2a
            is_protected = (
                path.startswith("/api")
                or path.startswith("/v1")
                or path.startswith("/a2a")
                or path == "/ws"
            )
            if not is_protected:
                return await call_next(request)
            # ★ 2026-09-25：webhook 放行已随 Webhook 管理功能移除。
            # auth 白名单放行（统一按 /api/auth 前缀）
            if path.startswith("/api/auth"):
                return await call_next(request)
            # 登录认证开关（默认关闭）：关闭时所有受保护接口放行。
            # 配置实时读取（无缓存），开关在设置页「登录认证」中控制。
            try:
                from scout.config.manager import ConfigManager
                _cfg = ConfigManager().load()
                if not getattr(_cfg, "auth_enabled", False):
                    return await call_next(request)
            except Exception as e:
                logging.getLogger(__name__).warning("读取登录认证配置失败，按默认鉴权处理: %s", e)
            # 未设置凭证 → 仅放行初始化引导接口（登录引导 / 外部 webhook / 静态资源）。
            # 其余 API 一律 401：防止默认配置下服务暴露在 0.0.0.0 时整个 API 面（含
            # 插件上传、A2A 任务、会话读取、配置修改）无鉴权可访问。
            auth_mgr = AuthManager()
            if not auth_mgr.has_credentials():
                if _is_initialization_whitelist(path):
                    return await call_next(request)
                # 本地回环访问：未初始化凭证时仅放行只读 GET 与初始化引导接口，
                # 写操作（配置修改/插件上传/A2A 任务等）一律 401——
                # 防止默认配置下本地恶意进程在首次初始化前无鉴权越权操作。
                client_host = (request.client.host if request.client else "") or ""
                if client_host in ("127.0.0.1", "::1", "localhost") and (
                    request.method == "GET" or _is_initialization_whitelist(path)
                ):
                    return await call_next(request)
                return JSONResponse(
                    {"error": "未初始化登录凭证，请先通过 /api/auth/login 完成初始化"},
                    status_code=401,
                )
            # 校验 Authorization header
            auth = request.headers.get("authorization", "")
            token = ""
            if auth.startswith("Bearer "):
                token = auth[7:]
            elif auth.startswith("bearer "):
                token = auth[7:]
            else:
                # 兼容 query param token
                token = request.query_params.get("token", request.query_params.get("access_token", ""))
            if token and verify_token(token):
                return await call_next(request)
            return JSONResponse({"error": "未授权访问，请先登录"}, status_code=401)

    app.add_middleware(AuthMiddleware)

    # ── 响应压缩（2026-09-25 Windows 首屏性能）──
    # /chat 首屏 13 个资源合计 1258 KB，实测 gzip 后 344 KB（−73%），此前全站裸传。
    # 注册在 AuthMiddleware 之后 = 位于最外层，连 401 JSON 也一并压缩。
    # 安全性已核对 starlette 1.7：GZipResponder 对每个分片做 Z_SYNC_FLUSH（不缓冲
    # 整个响应），且 DEFAULT_EXCLUDED_CONTENT_TYPES 已含 text/event-stream ——
    # /api/chat/stream 的 EventSourceResponse 与 /ws 均不会被压缩或延迟。
    from starlette.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # 挂载 Web 适配器（API 路由）
    _web_adapter = WebAdapter(app, agent)
    # ★ 2026-09-23：暴露到 app.state —— 桌面版 create_web_app() 不传 agent，
    #   真实 agent 由 adapter 在配置加载后 rebuild 出来。运行时接口（如
    #   /api/context/stats）靠它拿到真实 agent，否则全程按 None 降级。
    try:
        app.state.web_adapter = _web_adapter
    except Exception:  # noqa: BLE001
        pass
    
    # 挂载插件 API 路由
    try:
        from scout.plugins.api import router as plugin_router
        app.include_router(plugin_router, prefix="/api", tags=["plugins"])
    except Exception as e:
        logging.getLogger(__name__).warning(f"插件 API 加载失败: {e}")
    
    # 挂载系统监控 API 路由
    try:
        from scout.web.api.system import router as system_router
        app.include_router(system_router, tags=["system"])
    except Exception as e:
        import traceback
        logging.getLogger("scout.web").error(f"系统监控 API 加载失败: {e}\n{traceback.format_exc()}")
    
    # 挂载版本管理 API 路由
    try:
        from scout.web.api.version import router as version_router
        app.include_router(version_router, tags=["version"])
    except Exception as e:
        logging.getLogger(__name__).warning(f"版本管理 API 加载失败: {e}")
    
    # 挂载模型监控 API 路由
    try:
        from scout.web.api.usage import router as usage_router
        app.include_router(usage_router)
    except Exception as e:
        logging.getLogger(__name__).warning(f"模型监控 API 加载失败: {e}")

    # 挂载通知管理 API 路由（跨渠道推送偏好 / 历史 / 测试）
    try:
        from scout.notify.api import router as notify_router
        app.include_router(notify_router, tags=["notify"])
    except Exception as e:
        logging.getLogger(__name__).warning(f"通知管理 API 加载失败: {e}")

    # ★ 2026-09-25：文件监听管理 API（watcher_api）已随功能移除。

    # 挂载文件系统浏览 API（文件树/读取/保存，2026-08-30）
    try:
        from scout.web.api.fs import router as fs_router
        app.include_router(fs_router, tags=["fs"])
    except Exception as e:
        logging.getLogger(__name__).warning(f"文件系统 API 加载失败: {e}")

    # ── 上下文占用统计（2026-09-22）──────────────────────────────────
    # 输入框的「上下文环」此前用前端 DOM 文本做粗估（cjk/1.4 + 单词/0.75），
    # 与后端真实口径（ContextManager.estimate_tokens / API 回传 prompt_tokens）
    # 差 2 倍以上，显示值没有参考价值。这里统一由后端出数，并给出分项占比，
    # 供点击展开查看「系统提示 / 摘要 / 用户 / 助手 / 工具输出」各占多少。
    @app.get("/api/context/stats")
    async def context_stats(
        request: Request,
        session_id: str = "",
        provider: str = "",
        model: str = "",
    ):
        """上下文圆环统计 — 2026-09-23 精准化改造.

        修复的精度缺口（此前显示值系统性偏低）：
        ① 系统提示只在有会话消息时才计入 → 新会话显示 ~0（实际每请求都带系统提示）
        ② 工具 schema（每次请求必发，可达数万 token）完全没计入
        ③ 实测值（prompt_tokens）观测点之后新增的消息不补差 → 低估最后一轮增量
        ④ limit 恒为 128000，不跟随所选模型的真实窗口（32k 模型占用低估 4 倍）

        ★ 关键前提（2026-09-23 夜）：闭包里的 ``agent`` 在桌面版**恒为 None** ——
        desktop/launcher.py 调的是 create_web_app()（不传 agent），真实 agent 由
        WebAdapter 在配置加载后 rebuild 出来并挂在 adapter._agent 上。此前本接口
        全程按 agent=None 走：读不到 context_mgr / session_store / 活跃会话，
        has_session 永远 false、used 恒等于「系统提示+工具定义」，对话再长也不动。
        现在运行时解析真实 agent（adapter → ToolRegistry._main_agent 兜底）。
        """
        try:
            from scout.context.manager import estimate_tokens
        except Exception:  # noqa: BLE001
            estimate_tokens = None

        agent_rt = agent
        if agent_rt is None:
            _ad = getattr(getattr(request, "app", None), "state", None)
            _ad = getattr(_ad, "web_adapter", None) if _ad is not None else None
            agent_rt = getattr(_ad, "_agent", None) or None
        if agent_rt is None:
            try:
                from scout.tools.registry import ToolRegistry
                agent_rt = getattr(ToolRegistry, "_main_agent", None) or None
            except Exception:  # noqa: BLE001
                agent_rt = None

        sid = (session_id or "").strip()
        cm = getattr(agent_rt, "context_mgr", None)
        store = getattr(agent_rt, "session_store", None)

        # ── limit 分母：用户手动覆盖 > 模型标注/名称推断 > 环境变量 > 默认 128000 ──
        # ★ 2026-09-24：不再把 cm.max_tokens（**压缩治理阈值**）当分母。
        #   它是"到多少 token 触发自动压缩"的策略值（默认 32768），与模型上下文
        #   窗口是两回事——用它当分母会让 128k 窗口的模型显示 33k，占用率虚高 4 倍
        #   （用户实测：真实 8% 显示成 31%）。未收录模型宁可用保守默认值。
        # ★ 2026-09-24（2）：用户在设置里手填的窗口长度优先级最高（按 provider:model
        #   记忆），未收录模型/自建端点不必再被 128000 硬套。
        limit = 0
        try:
            from scout.adapters.web.routes.config import (
                capability_key,
                resolve_model_context_length,
            )
            _cfg = None
            try:
                from scout.config.manager import ConfigManager
                _cfg = ConfigManager().load()
            except Exception:  # noqa: BLE001
                _cfg = None
            if not provider and _cfg is not None:
                provider = _cfg.provider or ""
            if not model and _cfg is not None:
                model = _cfg.model or ""
            if model and _cfg is not None:
                _over = getattr(_cfg, "model_context_overrides", None) or {}
                try:
                    limit = int(_over.get(capability_key(provider, model)) or 0)
                    limit_source = "user"
                except (TypeError, ValueError):
                    limit = 0
            if limit <= 0 and model:
                limit = int(resolve_model_context_length(provider, model) or 0)
                limit_source = "model"
        except Exception:  # noqa: BLE001
            limit = 0
        if limit <= 0:
            try:
                limit = int(os.getenv("SCOUT_CONTEXT_MAX_TOKENS", "0") or 0)
                limit_source = "env"
            except Exception:  # noqa: BLE001
                limit = 0
        if limit <= 0:
            limit = 128000
            limit_source = "default"

        # ★ 2026-09-23 实时性：生成期间消息只 append 进内存 session（落盘要到
        # 回合收尾），磁盘 load 拿到的是旧版本 → 整轮生成中数值不动。
        # 优先读 agent 的活跃会话注册表（内存版，含实时工具输出），无活跃才读磁盘。
        session = None
        active_sessions = getattr(agent_rt, "_active_sessions", None) or {}
        if sid and sid in active_sessions:
            session = active_sessions[sid]
        if session is None and store and sid:
            try:
                session = await asyncio.to_thread(store.load_session, sid)
            except TypeError:
                try:
                    session = store.load_session(sid)
                except Exception:  # noqa: BLE001
                    session = None
            except Exception:  # noqa: BLE001
                session = None

        # API 真实回传值优先；没有就退回本地估算
        real = 0
        obs_meta = {}
        if cm and sid:
            try:
                real = int(cm.real_prompt_tokens(sid) or 0)
                obs_meta = cm.real_prompt_meta(sid) or {}
            except Exception:  # noqa: BLE001
                real = 0

        buckets = {
            "system": {"label": "系统提示", "tokens": 0, "count": 0},
            "tools": {"label": "工具定义", "tokens": 0, "count": 0},
            "summary": {"label": "压缩摘要", "tokens": 0, "count": 0},
            "user": {"label": "用户消息", "tokens": 0, "count": 0},
            "assistant": {"label": "助手回复", "tokens": 0, "count": 0},
            "tool": {"label": "工具输出", "tokens": 0, "count": 0},
        }

        def _tok(text: str) -> int:
            if not text:
                return 0
            if estimate_tokens is not None:
                return int(estimate_tokens(text))
            return (len(text) + 3) // 4

        # ① 系统提示：每次请求必发，无论有无会话消息都要计入（修 ~0 显示）
        sys_prompt = str(getattr(agent_rt, "system_prompt", "") or "")
        if sys_prompt:
            buckets["system"]["tokens"] += _tok(sys_prompt)
            buckets["system"]["count"] += 1

        # ② 工具 schema：每次请求必发（修复此前完全漏计的数万 token）
        try:
            from scout.tools.registry import ToolRegistry
            tools_json = json.dumps(
                ToolRegistry.schemas(compact=True), ensure_ascii=False
            )
            if tools_json and tools_json != "[]":
                buckets["tools"]["tokens"] += _tok(tools_json)
                buckets["tools"]["count"] += len(ToolRegistry.all_tools())
        except Exception:  # noqa: BLE001
            pass

        # ③ 消息：按「观测前 / 观测后」分开估算——
        #    实测值(prompt_tokens)覆盖观测点之前的全部内容（系统提示+工具+历史），
        #    观测点之后的增量（最后一轮回复/工具输出）按估算补进显示值。
        #    切片对齐 raw session.messages（观测点记录的就是它的长度；
        #    build_llm_view 会裁剪工具输出，索引对不上）。
        obs_msg_count = obs_meta.get("msg_count") if isinstance(obs_meta, dict) else None
        has_obs = real > 0 and isinstance(obs_msg_count, int)

        def _bucket_key(m) -> str:
            content = getattr(m, "content", "") or ""
            role = getattr(getattr(m, "role", None), "value", str(getattr(m, "role", "")))
            role = str(role).lower()
            if (content or "").startswith("[对话摘要]"):
                return "summary"
            if role in ("system", "role.system"):
                return "system"
            if role in ("user", "role.user"):
                return "user"
            if role in ("tool", "role.tool"):
                return "tool"
            return "assistant"

        # post_buckets：观测后增量（按面值进分项）；pre_role：观测前历史按角色
        # 的估算值（实测值摊回时保持归属近似正确）
        post_buckets = {k: 0 for k in buckets}
        pre_role = {k: 0 for k in buckets}
        pre_msgs_est = 0

        if session is not None:
            raw_msgs = list(getattr(session, "messages", []) or [])
            if has_obs:
                cut = max(0, min(obs_msg_count, len(raw_msgs)))
                for m in raw_msgs[cut:]:
                    post_buckets[_bucket_key(m)] += _tok(getattr(m, "content", "") or "")
                for m in raw_msgs[:cut]:
                    t = _tok(getattr(m, "content", "") or "")
                    pre_role[_bucket_key(m)] += t
                    pre_msgs_est += t
            else:
                # 无实测：用 LLM 视图全量估算（视图含压缩摘要与工具输出裁剪，
                # 才是真正发给 LLM 的内容；原始全量会远高于实际占用）
                msgs = []
                if cm is not None:
                    try:
                        msgs = list(cm.build_llm_view(session) or [])
                    except Exception:  # noqa: BLE001 — 视图构建失败退回原始消息
                        msgs = []
                if not msgs:
                    msgs = raw_msgs
                for m in msgs:
                    buckets[_bucket_key(m)]["tokens"] += _tok(getattr(m, "content", "") or "")
                    buckets[_bucket_key(m)]["count"] += 1

        # 本地全量估算参考值（estimated 字段）：观测点路径下 buckets 只装了
        # 系统提示+工具定义，需补上观测前历史与观测后增量才是完整估算
        est_total = sum(b["tokens"] for b in buckets.values())
        if has_obs and session is not None:
            est_total += pre_msgs_est + sum(post_buckets.values())

        if real > 0:
            source = "real"
            # 实测值摊回：base = 系统提示 + 工具定义 + 观测前历史（都在实测里），
            # 按各自估算占比把 real 摊到分项；观测后增量按面值叠加。
            # 保证「分项之和 == 显示总量」且归属近似正确。
            base_est = buckets["system"]["tokens"] + buckets["tools"]["tokens"] + pre_msgs_est
            if has_obs and session is not None:
                used = real + sum(post_buckets.values())
                scale = real / base_est if base_est > 0 else 1.0
                for key, b in buckets.items():
                    b["tokens"] = int(round(
                        (buckets[key]["tokens"] + pre_role[key]) * scale
                    )) + post_buckets[key]
            else:
                # 有实测但无观测点元数据（旧会话）：显示实测值，分项按全量估算
                # 比例归一（无法补差，保持旧行为）
                used = real
                scale = real / est_total if est_total > 0 else 1.0
                for _key, b in buckets.items():
                    b["tokens"] = int(round(b["tokens"] * scale))
        else:
            used = est_total
            source = "estimate"

        breakdown = []
        for key, b in buckets.items():
            breakdown.append(
                {
                    "key": key,
                    "label": b["label"],
                    "tokens": int(b["tokens"]),
                    "count": int(b["count"]),
                    "ratio": round(b["tokens"] / used, 4) if used > 0 else 0.0,
                }
            )

        return {
            "session_id": sid,
            "used": int(used),
            "estimated": int(est_total),
            "real_observed": int(real),
            "limit": int(limit),
            "limit_source": limit_source,
            "ratio": round(used / limit, 4) if limit > 0 else 0.0,
            "source": source,
            "has_session": session is not None,
            "breakdown": breakdown,
        }

    # 静态文件目录
    static_dir = os.path.join(os.path.dirname(__file__), "static")

    # /chat 和 / 都返回聊天页面（no-cache：页面迭代频繁，避免移动端缓存旧版 JS）
    _nocache = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/chat")
    async def chat_page():
        return FileResponse(os.path.join(static_dir, "index.html"), headers=_nocache)

    # /usage 返回模型监控页面
    @app.get("/usage")
    async def usage_page():
        return FileResponse(os.path.join(static_dir, "usage.html"), headers=_nocache)

    # /plugins 返回插件管理页面
    @app.get("/plugins")
    async def plugins_page():
        return FileResponse(os.path.join(static_dir, "plugins.html"), headers=_nocache)

    # /plugin-builder 返回插件构建器页面
    @app.get("/plugin-builder")
    async def plugin_builder_page():
        return FileResponse(os.path.join(static_dir, "plugin-builder.html"), headers=_nocache)

    # /plugin-config 返回插件配置编辑页面
    @app.get("/plugin-config")
    async def plugin_config_page():
        return FileResponse(os.path.join(static_dir, "plugin-config.html"), headers=_nocache)

    # /monitor 返回系统监控页面
    @app.get("/monitor")
    async def monitor_page():
        return FileResponse(os.path.join(static_dir, "monitor.html"), headers=_nocache)

    # /automation 返回自动化中心页面（触发器/运行历史/策略/日报，2026-08-13）
    @app.get("/automation")
    async def automation_page():
        return FileResponse(os.path.join(static_dir, "automation.html"), headers=_nocache)

    # /observe 返回运行观测时间线页面（2026-08-13）
    @app.get("/observe")
    async def observe_page():
        return FileResponse(os.path.join(static_dir, "observe.html"), headers=_nocache)

    # /notify 返回通知中心页面（跨渠道推送偏好/历史/测试）
    @app.get("/notify")
    async def notify_page():
        return FileResponse(os.path.join(static_dir, "notify.html"), headers=_nocache)

    # ★ 2026-09-25：/watcher、/webhooks 页面已随功能移除。

    # /events 返回事件总线观测页面（事件流 + DLQ）
    @app.get("/events")
    async def events_page():
        return FileResponse(os.path.join(static_dir, "events.html"), headers=_nocache)

    # 挂载静态资源（css/js 等）
    if os.path.exists(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # PWA: Service Worker（带 Service-Worker-Allowed 头，作用域覆盖全站）
    @app.get("/sw.js")
    async def sw_js():
        return FileResponse(
            os.path.join(static_dir, "sw.js"),
            headers={"Content-Type": "text/javascript", "Service-Worker-Allowed": "/",
                     "Cache-Control": "no-cache"},
        )

    # PWA: Web App Manifest（与 /static/manifest.json 等价，路径更简洁）
    @app.get("/manifest.json")
    async def manifest_json():
        return FileResponse(
            os.path.join(static_dir, "manifest.json"),
            headers={"Content-Type": "application/manifest+json", "Cache-Control": "no-cache"},
        )

    # 根路径重定向到 /chat
    @app.get("/")
    async def root_page():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/chat")

    # 健康检查端点（用于 Docker）
    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "version": "1.0.0.5"}

    return app
