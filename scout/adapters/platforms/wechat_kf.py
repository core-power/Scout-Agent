"""微信客服适配器 — 基于微信客服 API."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse

logger = logging.getLogger(__name__)


class WechatKfAdapter(ChannelAdapter):
    """微信客服适配器.
    
    支持微信客服消息:
    - 客户咨询消息
    - 客服主动回复
    - 消息记录同步
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "wechat_kf"
        self._connected = False
        self._app_id = config.get("app_id", "")
        self._app_secret = config.get("app_secret", "")
        self._kf_account = config.get("kf_account", "")
        self._access_token = ""
        self._token_expires = 0
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._polling = False
        self._cursor = ""

    async def connect(self) -> bool:
        """连接微信客服."""
        if not self._app_id or not self._app_secret:
            logger.error("微信客服 App ID 或 Secret 未配置")
            return False
        
        try:
            await self._refresh_access_token()
            self._connected = True
            logger.info("微信客服已连接")
            
            # 启动消息轮询
            if not self._polling:
                self._polling = True
                asyncio.create_task(self._poll_messages())
            
            return True
        except Exception as e:
            logger.error(f"微信客服连接失败: {e}")
            return False

    async def _refresh_access_token(self):
        """刷新 Access Token."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.weixin.qq.com/cgi-bin/token",
                params={
                    "grant_type": "client_credential",
                    "appid": self._app_id,
                    "secret": self._app_secret,
                },
            )
            data = resp.json()
            
            if "access_token" in data:
                self._access_token = data["access_token"]
                self._token_expires = time.time() + data.get("expires_in", 7200)
            else:
                raise Exception(f"获取 Access Token 失败: {data.get('errmsg')}")

    async def _ensure_token(self):
        """确保 Token 有效."""
        if time.time() >= self._token_expires - 300:
            await self._refresh_access_token()

    async def _poll_messages(self):
        """轮询客服消息."""
        while self._polling and self._connected:
            try:
                await self._ensure_token()
                await self._fetch_messages()
            except Exception as e:
                logger.error(f"轮询消息失败: {e}")
            
            await asyncio.sleep(5)  # 每 5 秒轮询一次

    async def _fetch_messages(self):
        """获取新消息."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.weixin.qq.com/customservice/kf/syncmsg?access_token={self._access_token}",
                json={
                    "cursor": self._cursor,
                    "token": 0,
                    "begin_time": int(time.time()) - 3600,  # 最近 1 小时
                    "end_time": int(time.time()),
                },
            )
            
            data = resp.json()
            if data.get("errcode") == 0:
                msg_list = data.get("msg_list", [])
                for msg in msg_list:
                    if msg.get("msgtype") == "text":
                        platform_msg = PlatformMessage(
                            platform="wechat_kf",
                            channel_id=msg.get("openid", ""),
                            user_id=msg.get("openid", ""),
                            user_name=msg.get("openid", ""),
                            content=msg.get("text", {}).get("content", ""),
                            message_id=str(msg.get("msgid", "")),
                            timestamp=msg.get("time", 0),
                        )
                        await self._handle_incoming(platform_msg)
                
                # 更新游标
                if msg_list:
                    self._cursor = msg_list[-1].get("msgid", self._cursor)

    async def disconnect(self):
        """断开连接."""
        self._polling = False
        self._connected = False

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送客服消息."""
        if not self._connected:
            return PlatformResponse(success=False, error="微信客服未连接")
        
        try:
            await self._ensure_token()
            
            async with httpx.AsyncClient(timeout=10) as client:
                payload = {
                    "touser": channel_id,
                    "msgtype": "text",
                    "text": {"content": content},
                }
                
                if self._kf_account:
                    payload["customservice"] = {"kf_account": self._kf_account}
                
                resp = await client.post(
                    f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={self._access_token}",
                    json=payload,
                )
                
                data = resp.json()
                if data.get("errcode") == 0:
                    return PlatformResponse(success=True)
                else:
                    return PlatformResponse(
                        success=False,
                        error=f"errcode={data.get('errcode')}, errmsg={data.get('errmsg')}"
                    )
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        caption: str = "",
        **kwargs,
    ) -> PlatformResponse:
        """发送文件 (微信客服暂不支持)."""
        return PlatformResponse(
            success=False,
            error="微信客服暂不支持直接发送文件"
        )

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "wechat_kf",
            "connected": self._connected,
            "polling": self._polling,
            "token_valid": time.time() < self._token_expires,
            "app_configured": bool(self._app_id),
        }
