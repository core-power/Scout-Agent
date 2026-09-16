"""认证路由组（/api/auth/*：login/check/change-password/status/setup）.

W1 拆分（2026-09-14）：自 adapters/web.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""
import logging

logger = logging.getLogger("scout.adapters.web")


from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, Response
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from scout.security.auth import AuthManager, rotate_secret, verify_token

if TYPE_CHECKING:  # 仅为类型检查，运行时无循环依赖
    from scout.adapters.web.adapter import WebAdapter


class AuthRoutes:
    """认证路由组（/api/auth/*：login/check/change-password/status/setup）（mixin）."""

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
                except Exception as _re:
                    # ★ 2026-09-01：JWT 密钥轮换失败不应静默 —— 此时历史 token
                    # 仍然有效，属于安全降级，必须留痕供审计
                    logger.warning(f"密码已修改但 JWT 密钥轮换失败（历史 token 仍有效）: {_re}")
            config.auth_enabled = enabled
            self.config_mgr.save(config)
            return {
                "status": "ok",
                "enabled": enabled,
                "username": username if enabled else "",
            }
