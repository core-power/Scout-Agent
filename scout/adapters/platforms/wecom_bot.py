"""企业微信群机器人适配器 — 基于 Webhook."""

from __future__ import annotations

import hashlib
import hmac
import base64
import logging
import time
from typing import Any

import httpx

from scout.adapters.platforms.base import ChannelAdapter, PlatformResponse

logger = logging.getLogger(__name__)


class WecomBotAdapter(ChannelAdapter):
    """企业微信群机器人适配器.
    
    通过 Webhook 发送消息到企业微信群:
    - 支持文本、Markdown、图片等消息类型
    - 支持签名验证 (可选)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "wecom_bot"
        self._connected = False
        self._webhook_url = config.get("webhook_url", "")
        self._webhook_key = config.get("webhook_key", "")  # 签名密钥

    async def connect(self) -> bool:
        """连接 (验证配置)."""
        if not self._webhook_url:
            logger.error("企业微信群 Webhook URL 未配置")
            return False
        
        self._connected = True
        logger.info("企业微信群机器人已配置")
        return True

    async def disconnect(self):
        """断开连接."""
        self._connected = False

    def _generate_sign(self) -> tuple[str, str]:
        """生成签名."""
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self._webhook_key}"
        
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        
        sign = base64.b64encode(hmac_code).decode("utf-8")
        return timestamp, sign

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送企业微信群消息."""
        if not self._webhook_url:
            return PlatformResponse(success=False, error="Webhook URL 未配置")
        
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                url = self._webhook_url
                
                # 如果有签名密钥，添加签名
                if self._webhook_key:
                    timestamp, sign = self._generate_sign()
                    url += f"&timestamp={timestamp}&sign={sign}"
                
                # 消息类型
                msg_type = kwargs.get("msg_type", "text")
                
                if msg_type == "markdown":
                    payload = {
                        "msgtype": "markdown",
                        "markdown": {"content": content},
                    }
                else:
                    payload = {
                        "msgtype": "text",
                        "text": {
                            "content": content,
                            "mentioned_list": kwargs.get("mentioned_list", []),
                            "mentioned_mobile_list": kwargs.get("mentioned_mobile_list", []),
                        },
                    }
                
                resp = await client.post(url, json=payload)
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
        """发送文件 (企业微信群 Webhook 不支持直接发送文件)."""
        return PlatformResponse(
            success=False,
            error="企业微信群 Webhook 不支持直接发送文件"
        )

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "wecom_bot",
            "connected": self._connected,
            "webhook_configured": bool(self._webhook_url),
            "sign_enabled": bool(self._webhook_key),
        }
