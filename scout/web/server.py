"""Scout Web Server — FastAPI + 静态文件 + WebSocket.

启动: scout --web --port 8848
"""

from __future__ import annotations

import asyncio
import logging
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    """未初始化凭证阶段仍放行的路径（登录引导 / 外部 webhook / 静态资源）.

    /api/files/download 不在白名单内：无凭证时同样返回 401，
    避免默认暴露下下载用户目录文件。
    """
    return (
        path.startswith("/api/auth")
        or path.startswith("/api/webhook")
        or path.startswith("/static")
        or path.startswith("/.well-known")
    )


def create_web_app(agent=None) -> FastAPI:
    """创建 FastAPI 应用并挂载所有路由."""
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # 启动时：立即清理一次旧日志 + 启动后台清理循环
        cleanup_task = asyncio.create_task(_log_cleanup_loop())
        watcher_task = None
        try:
            # 启动文件系统监听器（主动感知）
            try:
                from scout.bus.hub import bus as event_bus
                from scout.automation.watcher import get_watcher
                watcher_task = asyncio.create_task(get_watcher(bus=event_bus).start())
            except Exception as e:
                logging.getLogger(__name__).warning(f"文件监听器启动失败: {e}")

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
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except (asyncio.CancelledError, Exception):
                    pass

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
        version="1.0.0.0",
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
            # webhook 放行（token 在路径中）
            if path.startswith("/api/webhook"):
                return await call_next(request)
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

    # 挂载 Web 适配器（API 路由）
    WebAdapter(app, agent)
    
    # 挂载插件 API 路由
    try:
        from scout.plugins.api import router as plugin_router
        app.include_router(plugin_router, prefix="/api", tags=["plugins"])
    except Exception as e:
        import logging
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
        import logging
        logging.getLogger(__name__).warning(f"版本管理 API 加载失败: {e}")
    
    # 挂载模型监控 API 路由
    try:
        from scout.web.api.usage import router as usage_router
        app.include_router(usage_router)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"模型监控 API 加载失败: {e}")

    # 挂载通知管理 API 路由（跨渠道推送偏好 / 历史 / 测试）
    try:
        from scout.notify.api import router as notify_router
        app.include_router(notify_router, tags=["notify"])
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"通知管理 API 加载失败: {e}")

    # 挂载文件监听管理 API 路由（主动感知）
    try:
        from scout.automation.watcher_api import router as watcher_router
        app.include_router(watcher_router, tags=["watcher"])
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"文件监听管理 API 加载失败: {e}")

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

    # /watcher 返回文件监听页面（目录监听/事件流）
    @app.get("/watcher")
    async def watcher_page():
        return FileResponse(os.path.join(static_dir, "watcher.html"), headers=_nocache)

    # /webhooks 返回 Webhook 管理页面
    @app.get("/webhooks")
    async def webhooks_page():
        return FileResponse(os.path.join(static_dir, "webhooks.html"), headers=_nocache)

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
        return {"status": "healthy", "version": "1.0.0.0"}

    return app
