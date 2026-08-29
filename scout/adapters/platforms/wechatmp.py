"""微信公众号适配器 — 支持被动回复和主动推送."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse

logger = logging.getLogger(__name__)


class WechatMPAdapter(ChannelAdapter):
    """微信公众号适配器.
    
    支持两种模式:
    1. 被动回复 — 用户发消息后 5 秒内回复
    2. 主动推送 — 使用客服消息接口 (需要认证服务号)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "wechatmp"
        self._connected = False
        self._app_id = config.get("app_id", "")
        self._app_secret = config.get("app_secret", "")
        self._token = config.get("token", "")
        self._aes_key = config.get("aes_key", "")
        self._access_token = ""
        self._token_expires = 0
        self._message_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        """连接微信公众号."""
        if not self._app_id or not self._app_secret:
            logger.error("微信公众号 App ID 或 Secret 未配置")
            return False
        
        try:
            # 获取 Access Token
            await self._refresh_access_token()
            self._connected = True
            logger.info("微信公众号已连接")
            return True
        except Exception as e:
            logger.error(f"微信公众号连接失败: {e}")
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
        if time.time() >= self._token_expires - 300:  # 提前 5 分钟刷新
            await self._refresh_access_token()

    def verify_signature(self, signature: str, timestamp: str, nonce: str) -> bool:
        """验证微信服务器签名."""
        arr = sorted([self._token, timestamp, nonce])
        temp_str = "".join(arr)
        sign = hashlib.sha1(temp_str.encode("utf-8")).hexdigest()
        return sign == signature

    async def receive_webhook(self, query: dict, raw_body: bytes) -> dict:
        """接收微信公众号服务器回调.

        接入服务器配置后，微信会把用户消息推送到配置的回调 URL：
        - GET: 服务器地址验证（返回 echostr）
        - POST: 用户消息回调（XML，可能加密）

        消息经解析后通过 `_handle_incoming` 入队，由 ChannelManager
        的 `listen()` 消费并交由 Agent 处理；回复通过客服消息接口主动推送。
        """
        signature = query.get("signature", "")
        timestamp = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        echostr = query.get("echostr", "")

        # GET 验证模式
        if echostr:
            if not self._token or self.verify_signature(signature, timestamp, nonce):
                return {"code": 200, "body": echostr, "content_type": "text/plain"}
            return {"code": 403, "body": "signature check failed", "content_type": "text/plain"}

        # POST 消息回调
        if not raw_body:
            return {"code": 400, "body": "empty body", "content_type": "text/plain"}

        xml_text = raw_body.decode("utf-8", errors="replace")

        # 安全模式：body 为 <xml><Encrypt>...</Encrypt></xml>，需解密
        if self._aes_key:
            try:
                root = ET.fromstring(xml_text)
                enc = root.findtext("Encrypt") or ""
                if enc:
                    from scout.adapters.platforms.wecom_crypto import decrypt_wecom_message

                    xml_text = decrypt_wecom_message(enc, self._aes_key, self._app_id)
            except ET.ParseError:
                pass

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning(f"微信公众号回调 XML 解析失败: {e}")
            return {"code": 200, "body": "success", "content_type": "text/plain"}

        msg_type = root.findtext("MsgType") or ""
        from_user = root.findtext("FromUserName") or ""
        msg_id = root.findtext("MsgId") or ""
        create_time = root.findtext("CreateTime") or "0"

        # 仅处理文本消息（其他类型可扩展）
        content = ""
        if msg_type == "text":
            content = root.findtext("Content") or ""
        elif msg_type == "voice":
            content = root.findtext("Recognition") or ""
        elif msg_type == "event":
            event = root.findtext("Event") or ""
            content = f"[事件] {event}"

        if from_user and content:
            platform_msg = PlatformMessage(
                platform="wechatmp",
                channel_id=from_user,
                user_id=from_user,
                user_name=from_user,
                content=content,
                message_id=msg_id,
                timestamp=float(create_time or 0),
                metadata={"msg_type": msg_type, "msgid": msg_id},
            )
            await self._handle_incoming(platform_msg)

        # 异步处理，立即返回微信要求的成功响应
        return {"code": 200, "body": "success", "content_type": "text/plain"}

    async def disconnect(self):
        """断开连接."""
        self._connected = False

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送客服消息 (主动推送)."""
        if not self._connected:
            return PlatformResponse(success=False, error="微信公众号未连接")
        
        try:
            await self._ensure_token()
            
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={self._access_token}",
                    json={
                        "touser": channel_id,
                        "msgtype": "text",
                        "text": {"content": content},
                    },
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
        """发送文件 (微信公众号暂不支持)."""
        return PlatformResponse(
            success=False,
            error="微信公众号暂不支持直接发送文件"
        )

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "wechatmp",
            "connected": self._connected,
            "token_valid": time.time() < self._token_expires,
            "app_configured": bool(self._app_id),
        }
