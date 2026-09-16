"""钉钉适配器 — 支持机器人 Webhook 和 Stream 模式."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from scout.adapters.platforms.base import ChannelAdapter, PlatformResponse
from scout.core.types import Message, Role

logger = logging.getLogger(__name__)


class DingTalkAdapter(ChannelAdapter):
    """钉钉机器人适配器.
    
    支持两种模式:
    1. Webhook 模式 — 简单的消息推送
    2. Stream 模式 — 实时双向通信 (需要 dingtalk_stream SDK)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "dingtalk"
        self._connected = False
        
        # Webhook 配置
        self._webhook_url = config.get("webhook_url", "")
        self._webhook_secret = config.get("webhook_secret", "")
        
        # Stream 模式配置
        self._app_key = config.get("app_key", "")
        self._app_secret = config.get("app_secret", "")
        self._use_stream = config.get("use_stream", False)
        
        self._stream_client = None
        self._message_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        """连接钉钉."""
        try:
            if self._use_stream and self._app_key and self._app_secret:
                # Stream 模式
                await self._start_stream()
            else:
                # Webhook 模式 — 验证连通性
                if not self._webhook_url:
                    logger.error("钉钉 Webhook URL 未配置")
                    return False
            
            self._connected = True
            logger.info(f"钉钉适配器已连接 (模式: {'Stream' if self._use_stream else 'Webhook'})")
            return True
        except Exception as e:
            logger.error(f"钉钉连接失败: {e}")
            return False

    async def _start_stream(self):
        """启动 Stream 模式."""
        try:
            import dingtalk_stream
            from dingtalk_stream import AckMessage
            
            credential = dingtalk_stream.Credential(self._app_key, self._app_secret)
            client = dingtalk_stream.DingTalkStreamClient(credential)
            
            # 注册消息处理器
            def on_message(message):
                asyncio.create_task(self._handle_stream_message(message))
                return AckMessage.STATUS_OK, "OK"
            
            client.register_callback_handler(
                dingtalk_stream.ChatbotMessage.TOPIC,
                dingtalk_stream.ChatbotHandler(on_message)
            )
            
            # 后台运行
            asyncio.create_task(asyncio.to_thread(client.start_forever))
            self._stream_client = client
        except ImportError:
            logger.warning("dingtalk_stream 未安装，降级到 Webhook 模式")
            self._use_stream = False

    async def _handle_stream_message(self, message):
        """处理 Stream 消息并转入消息队列.

        参考 CowAgent dingtalk_channel 的消息解析：
        ChatbotMessage 含 text.content / senderStaffId / conversationId / msgId
        """
        try:
            data = getattr(message, "data", message)
            # 忽略自己发出的消息，避免回环
            my_msg = getattr(data, "my_msg", False)
            if isinstance(data, dict):
                my_msg = bool(data.get("my_msg", False) or data.get("isInAtList") is None and False)
            if my_msg:
                return

            # 提取文本（兼容 dict 与对象两种形态）
            if isinstance(data, dict):
                text_obj = data.get("text") or {}
                text = text_obj.get("content", "") if isinstance(text_obj, dict) else str(text_obj)
                sender = data.get("senderStaffId", "") or data.get("sender_staff_id", "")
                conversation = data.get("conversationId", "") or data.get("conversation_id", "")
                msg_id = data.get("msgId", "") or data.get("msg_id", "")
            else:
                text_obj = getattr(data, "text", None)
                if isinstance(text_obj, dict):
                    text = text_obj.get("content", "")
                elif text_obj is not None:
                    text = str(text_obj)
                else:
                    text = ""
                sender = getattr(data, "sender_staff_id", "") or ""
                conversation = getattr(data, "conversation_id", "") or ""
                msg_id = getattr(data, "msg_id", "") or ""

            if text:
                await self._message_queue.put(Message(
                    role=Role.USER,
                    content=text,
                    sender=sender,
                    session_id=conversation or sender or msg_id,
                    message_id=msg_id,
                    source="dingtalk",
                ))
        except Exception as e:
            logger.error(f"钉钉 Stream 消息解析失败: {e}")

    async def listen(self):
        """从消息队列读取收到的消息."""
        while True:
            msg = await self._message_queue.get()
            yield msg

    async def disconnect(self):
        """断开连接."""
        if self._stream_client:
            try:
                stop = getattr(self._stream_client, "stop_forever", None) or getattr(self._stream_client, "stop", None)
                if stop:
                    stop()
            except Exception as e:
                logger.debug(f"钉钉 Stream 停止失败: {e}")
        self._connected = False

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送钉钉消息."""
        if not self._webhook_url:
            return PlatformResponse(success=False, error="Webhook URL 未配置")
        
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # 构建消息体
                payload = {
                    "msgtype": "text",
                    "text": {"content": content},
                }
                
                # 如果有 @ 需求
                at_mobiles = kwargs.get("at_mobiles", [])
                at_all = kwargs.get("at_all", False)
                if at_mobiles or at_all:
                    payload["at"] = {
                        "atMobiles": at_mobiles,
                        "isAtAll": at_all,
                    }
                
                resp = await client.post(self._webhook_url, json=payload)
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
        """发送文件 (钉钉 Webhook 不支持直接发送文件)."""
        return PlatformResponse(
            success=False,
            error="钉钉 Webhook 不支持直接发送文件，请使用钉钉开放平台 API"
        )

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "dingtalk",
            "connected": self._connected,
            "mode": "stream" if self._use_stream else "webhook",
            "webhook_configured": bool(self._webhook_url),
            "stream_configured": bool(self._app_key and self._app_secret),
        }
