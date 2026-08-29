"""QQ 适配器 — 基于 QQ 官方 Bot API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse

logger = logging.getLogger(__name__)


class QQAdapter(ChannelAdapter):
    """QQ Bot 适配器.
    
    支持 QQ 官方 Bot API (https://bot.q.qq.com/wiki/)
    - 消息收发
    - 群聊/私聊
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "qq"
        self._connected = False
        self._app_id = config.get("app_id", "")
        self._app_secret = config.get("app_secret", "")
        self._token = config.get("token", "")
        self._sandbox = config.get("sandbox", False)
        self._message_queue: asyncio.Queue = asyncio.Queue()
        
        # API 基础 URL
        self._api_base = "https://sandbox.api.sgroup.qq.com" if self._sandbox else "https://api.sgroup.qq.com"

    async def connect(self) -> bool:
        """连接 QQ Bot API."""
        if not self._app_id or not self._token:
            logger.error("QQ App ID 或 Token 未配置")
            return False
        
        try:
            # 验证 Token
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self._api_base}/users/@me",
                    headers=self._get_headers(),
                )
                if resp.status_code == 200:
                    self._connected = True
                    logger.info("QQ Bot 已连接")
                    return True
                else:
                    logger.error(f"QQ Bot 认证失败: {resp.status_code}")
                    return False
        except Exception as e:
            logger.error(f"QQ Bot 连接失败: {e}")
            return False

    def _get_headers(self) -> dict:
        """获取请求头."""
        return {
            "Authorization": f"Bot {self._app_id}.{self._token}",
            "Content-Type": "application/json",
        }

    async def disconnect(self):
        """断开连接."""
        self._connected = False

    def _verify_webhook(self, headers: dict, body: bytes) -> bool:
        """QQ 开放平台 Webhook 签名校验.

        QQ 回调使用请求头 `X-Token`（开放平台配置的 Secret）鉴权，
        未配置 Secret 时不做校验。
        """
        secret = self.config.get("webhook_secret", "")
        if not secret:
            return True
        token = headers.get("x-token", "")
        return hmac.compare_digest(token, secret)

    async def receive_webhook(self, data: dict) -> dict:
        """接收 QQ 开放平台 Webhook 事件.

        QQ Bot 开放平台 Webhook 事件格式:
        {
          "t": 1720000000000,          // 毫秒时间戳
          "id": "...",                 // 事件 ID
          "type": "GROUP_AT_MESSAGE_CREATE" | "C2C_MESSAGE_CREATE" | ...,
          "d": {
            "msg_id": "...",
            "content": "...",          // CQ 码文本，需去除 at
            "author": {"id": "...", "username": "..."},
            "group_openid": "...",     // 群聊时为频道 ID
            "channel_type": "GROUP" | "C2C"
          }
        }
        """
        event_type = data.get("type", "")
        d = data.get("d") or {}

        # 仅处理文本消息事件
        if event_type not in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            return {"status": "ok"}

        content = (d.get("content") or "").strip()
        if not content:
            return {"status": "ok"}

        # 去除 @机器人 前缀（CQ 码格式 <@!openid>）
        import re
        content = re.sub(r"<@!\w+>\s*", "", content).strip()

        author = d.get("author") or {}
        channel_type = d.get("channel_type", "GROUP")
        is_group = channel_type != "C2C"

        # 群聊用 group_openid 作频道，私聊用 author.id
        channel_id = d.get("group_openid") or author.get("id") or ""

        platform_msg = PlatformMessage(
            platform="qq",
            channel_id=channel_id,
            user_id=author.get("id") or "",
            user_name=author.get("username") or "",
            content=content,
            message_id=d.get("msg_id") or "",
            timestamp=float((data.get("t") or 0)) / 1000.0,
            metadata={"is_group": is_group, "event_type": event_type, "msg_id": d.get("msg_id")},
        )
        await self._handle_incoming(platform_msg)
        return {"status": "ok"}

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送 QQ 消息."""
        if not self._connected:
            return PlatformResponse(success=False, error="QQ Bot 未连接")
        
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                payload = {"content": content}
                
                # 回复消息
                if reply_to:
                    payload["msg_id"] = reply_to
                
                # 判断是群聊还是私聊
                is_group = kwargs.get("is_group", True)
                if is_group:
                    url = f"{self._api_base}/channels/{channel_id}/messages"
                else:
                    url = f"{self._api_base}/users/{channel_id}/messages"
                
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                
                if resp.status_code in (200, 201):
                    data = resp.json()
                    return PlatformResponse(success=True, message_id=data.get("id"))
                else:
                    return PlatformResponse(success=False, error=f"HTTP {resp.status_code}")
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        caption: str = "",
        **kwargs,
    ) -> PlatformResponse:
        """发送文件 (QQ Bot API 暂不支持直接发送文件)."""
        return PlatformResponse(
            success=False,
            error="QQ Bot API 暂不支持直接发送文件"
        )

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "qq",
            "connected": self._connected,
            "sandbox": self._sandbox,
            "app_configured": bool(self._app_id),
        }
