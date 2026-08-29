"""Slack 适配器 — 基于 Slack Bolt SDK."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse

logger = logging.getLogger(__name__)


class SlackAdapter(ChannelAdapter):
    """Slack Bot 适配器.
    
    支持两种模式:
    1. Web API 模式 — 使用 Bot Token 发送消息
    2. Socket Mode — 实时双向通信 (需要 slack_bolt SDK)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "slack"
        self._connected = False
        
        # Bot Token (xoxb-...)
        self._bot_token = config.get("bot_token", "")
        # App Token for Socket Mode (xapp-...)
        self._app_token = config.get("app_token", "")
        # Signing Secret for HTTP 验证
        self._signing_secret = config.get("signing_secret", "")
        
        self._app = None
        self._use_socket_mode = bool(self._app_token)

    async def connect(self) -> bool:
        """连接 Slack."""
        if not self._bot_token:
            logger.error("Slack Bot Token 未配置")
            return False
        
        try:
            if self._use_socket_mode:
                await self._start_socket_mode()
            else:
                # 验证 Token 有效性
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        "https://slack.com/api/auth.test",
                        headers={"Authorization": f"Bearer {self._bot_token}"}
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        logger.error(f"Slack Token 无效: {data.get('error')}")
                        return False
            
            self._connected = True
            logger.info(f"Slack 适配器已连接 (模式: {'Socket' if self._use_socket_mode else 'Web API'})")
            return True
        except Exception as e:
            logger.error(f"Slack 连接失败: {e}")
            return False

    async def _start_socket_mode(self):
        """启动 Socket Mode."""
        try:
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            
            self._app = AsyncApp(token=self._bot_token, signing_secret=self._signing_secret)
            
            # 注册消息处理器
            @self._app.event("message")
            async def handle_message(event, say):
                platform_msg = PlatformMessage(
                    platform="slack",
                    channel_id=event.get("channel", ""),
                    user_id=event.get("user", ""),
                    user_name=event.get("user", ""),
                    content=event.get("text", ""),
                    message_id=event.get("ts", ""),
                    timestamp=float(event.get("ts", 0)),
                )
                await self._handle_incoming(platform_msg)
            
            # 启动 Socket Mode Handler
            handler = AsyncSocketModeHandler(self._app, self._app_token)
            asyncio.create_task(handler.start_async())
        except ImportError:
            logger.warning("slack_bolt 未安装，降级到 Web API 模式")
            self._use_socket_mode = False

    async def disconnect(self):
        """断开连接."""
        if self._app:
            # Socket Mode handler 没有官方的 stop 方法
            pass
        self._connected = False

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送 Slack 消息."""
        if not self._bot_token:
            return PlatformResponse(success=False, error="Bot Token 未配置")
        
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                payload = {
                    "channel": channel_id,
                    "text": content,
                }
                
                # 如果需要回复 (thread)
                if reply_to:
                    payload["thread_ts"] = reply_to
                
                resp = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {self._bot_token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                
                data = resp.json()
                if data.get("ok"):
                    return PlatformResponse(
                        success=True,
                        message_id=data.get("message", {}).get("ts")
                    )
                else:
                    return PlatformResponse(success=False, error=data.get("error"))
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        caption: str = "",
        **kwargs,
    ) -> PlatformResponse:
        """发送文件."""
        if not self._bot_token:
            return PlatformResponse(success=False, error="Bot Token 未配置")
        
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                with open(file_path, "rb") as f:
                    resp = await client.post(
                        "https://slack.com/api/files.upload",
                        headers={"Authorization": f"Bearer {self._bot_token}"},
                        data={
                            "channels": channel_id,
                            "initial_comment": caption,
                        },
                        files={"file": f},
                    )
                
                data = resp.json()
                if data.get("ok"):
                    return PlatformResponse(success=True, message_id=data.get("file", {}).get("id"))
                else:
                    return PlatformResponse(success=False, error=data.get("error"))
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "slack",
            "connected": self._connected,
            "mode": "socket" if self._use_socket_mode else "web_api",
            "bot_token_configured": bool(self._bot_token),
            "app_token_configured": bool(self._app_token),
        }
