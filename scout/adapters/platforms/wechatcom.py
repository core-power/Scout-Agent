"""企业微信应用适配器 — 基于企业微信 API."""

from __future__ import annotations

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse

logger = logging.getLogger(__name__)


class WechatComAdapter(ChannelAdapter):
    """企业微信应用适配器.
    
    支持企业微信自建应用:
    - 消息收发
    - 通讯录管理
    - 应用菜单
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "wechatcom"
        self._connected = False
        self._corp_id = config.get("corp_id", "")
        self._corp_secret = config.get("corp_secret", "")
        self._agent_id = config.get("agent_id", "")
        self._token = config.get("token", "")
        self._aes_key = config.get("aes_key", "")
        self._access_token = ""
        self._token_expires = 0
        self._message_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        """连接企业微信."""
        if not self._corp_id or not self._corp_secret:
            logger.error("企业微信 Corp ID 或 Secret 未配置")
            return False
        
        try:
            await self._refresh_access_token()
            self._connected = True
            logger.info("企业微信应用已连接")
            return True
        except Exception as e:
            logger.error(f"企业微信连接失败: {e}")
            return False

    async def _refresh_access_token(self):
        """刷新 Access Token."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={
                    "corpid": self._corp_id,
                    "corpsecret": self._corp_secret,
                },
            )
            data = resp.json()
            
            if data.get("errcode") == 0:
                self._access_token = data["access_token"]
                self._token_expires = time.time() + data.get("expires_in", 7200)
            else:
                raise Exception(f"获取 Access Token 失败: {data.get('errmsg')}")

    async def _ensure_token(self):
        """确保 Token 有效."""
        if time.time() >= self._token_expires - 300:
            await self._refresh_access_token()

    async def disconnect(self):
        """断开连接."""
        self._connected = False

    async def receive_webhook(self, query: dict, raw_body: bytes) -> dict:
        """接收企业微信应用回调.

        - GET: 回调 URL 验证（msg_signature 校验 + echostr 解密返回）
        - POST: 消息回调（明文或 AES 加密 XML），解析后入队由 Agent 处理

        回复采用主动调用应用消息接口的方式，回调立即返回成功。
        """
        from scout.adapters.platforms.wecom_crypto import (
            decrypt_wecom_message,
            verify_signature,
        )

        timestamp = query.get("timestamp", "")
        nonce = query.get("nonce", "")
        echostr = query.get("echostr", "")

        # GET 验证模式
        if echostr:
            if self._token and not verify_signature(self._token, query.get("msg_signature", ""), timestamp, nonce):
                return {"code": 403, "body": "signature check failed", "content_type": "text/plain"}
            if self._aes_key:
                plain = decrypt_wecom_message(echostr, self._aes_key, self._corp_id)
                return {"code": 200, "body": plain, "content_type": "text/plain"}
            return {"code": 200, "body": echostr, "content_type": "text/plain"}

        # POST 消息回调
        if not raw_body:
            return {"code": 400, "body": "empty body", "content_type": "text/plain"}

        xml_text = raw_body.decode("utf-8", errors="replace")

        # 校验签名（若配置了 Token）
        if self._token:
            try:
                root0 = ET.fromstring(xml_text)
                enc0 = root0.findtext("Encrypt") or ""
            except ET.ParseError:
                enc0 = ""
            if not verify_signature(self._token, query.get("msg_signature", ""), timestamp, nonce, enc0):
                logger.warning("企业微信回调签名校验失败")
                return {"code": 403, "body": "signature check failed", "content_type": "text/plain"}

        # 加密模式解密
        if self._aes_key:
            try:
                root0 = ET.fromstring(xml_text)
                enc0 = root0.findtext("Encrypt") or ""
                if enc0:
                    xml_text = decrypt_wecom_message(enc0, self._aes_key, self._corp_id)
            except ET.ParseError:
                pass

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning(f"企业微信回调 XML 解析失败: {e}")
            return {"code": 200, "body": "success", "content_type": "text/plain"}

        msg_type = root.findtext("MsgType") or ""
        from_user = root.findtext("FromUserName") or ""
        msg_id = root.findtext("MsgId") or ""
        create_time = root.findtext("CreateTime") or "0"

        content = ""
        if msg_type == "text":
            content = root.findtext("Content") or ""
        elif msg_type == "event":
            event = root.findtext("Event") or ""
            content = f"[事件] {event}"

        if from_user and content:
            platform_msg = PlatformMessage(
                platform="wechatcom",
                channel_id=from_user,
                user_id=from_user,
                user_name=from_user,
                content=content,
                message_id=msg_id,
                timestamp=float(create_time or 0),
                metadata={"msg_type": msg_type, "msgid": msg_id},
            )
            await self._handle_incoming(platform_msg)

        return {"code": 200, "body": "success", "content_type": "text/plain"}

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送企业微信应用消息."""
        if not self._connected:
            return PlatformResponse(success=False, error="企业微信未连接")
        
        try:
            await self._ensure_token()
            
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={self._access_token}",
                    json={
                        "touser": channel_id,
                        "msgtype": "text",
                        "agentid": self._agent_id,
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
        """发送文件 (企业微信暂不支持)."""
        return PlatformResponse(
            success=False,
            error="企业微信暂不支持直接发送文件"
        )

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "wechatcom",
            "connected": self._connected,
            "token_valid": time.time() < self._token_expires,
            "corp_configured": bool(self._corp_id),
        }
