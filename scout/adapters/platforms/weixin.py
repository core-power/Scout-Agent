"""个人微信适配器 — 通过 Wechaty 或 itchat 接入."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse

logger = logging.getLogger(__name__)


class WeixinAdapter(ChannelAdapter):
    """个人微信适配器.
    
    支持两种模式:
    1. Wechaty 模式 — 使用 Wechaty Puppet (推荐，稳定)
    2. itchat 模式 — 使用 itchat Web 协议 (不稳定，可能被封)
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "weixin"
        self._connected = False
        self._mode = config.get("mode", "wechaty")  # wechaty / itchat
        self._wechaty_token = config.get("wechaty_token", "")
        self._wechaty_puppet = config.get("wechaty_puppet", "padlocal")
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._bot = None

    async def connect(self) -> bool:
        """连接个人微信."""
        if self._mode == "wechaty":
            return await self._connect_wechaty()
        elif self._mode == "itchat":
            return await self._connect_itchat()
        else:
            logger.error(f"不支持的模式: {self._mode}")
            return False

    async def _connect_wechaty(self) -> bool:
        """使用 Wechaty 连接."""
        if not self._wechaty_token:
            logger.error("Wechaty Token 未配置")
            return False
        
        try:
            from wechaty import Wechaty, WechatyOptions
            
            options = WechatyOptions(
                token=self._wechaty_token,
                puppet=self._wechaty_puppet,
            )
            
            self._bot = Wechaty(options)
            
            # 注册事件处理器
            @self._bot.on("login")
            async def on_login(user):
                logger.info(f"微信登录成功: {user}")
                self._connected = True
            
            @self._bot.on("message")
            async def on_message(msg):
                if msg.is_self():
                    return
                
                contact = msg.talker()
                room = msg.room()
                
                platform_msg = PlatformMessage(
                    platform="weixin",
                    channel_id=room.id if room else contact.id,
                    user_id=contact.id,
                    user_name=contact.name or contact.id,
                    content=msg.text(),
                    message_id=msg.id,
                    timestamp=msg.date().timestamp(),
                    metadata={
                        "is_room": bool(room),
                        "room_topic": await room.topic() if room else None,
                    },
                )
                
                await self._handle_incoming(platform_msg)
            
            # 后台启动
            asyncio.create_task(self._bot.start())
            
            # 等待登录
            for _ in range(50):  # 最多等待 5 秒
                if self._connected:
                    return True
                await asyncio.sleep(0.1)
            
            return self._connected
        except ImportError:
            logger.error("wechaty 未安装，请运行: pip install wechaty")
            return False
        except Exception as e:
            logger.error(f"Wechaty 连接失败: {e}")
            return False

    async def _connect_itchat(self) -> bool:
        """使用 itchat 连接."""
        try:
            import itchat
            from itchat.content import TEXT
            
            # 注册消息处理器
            @itchat.msg_register(TEXT)
            def on_message(msg):
                platform_msg = PlatformMessage(
                    platform="weixin",
                    channel_id=msg["FromUserName"],
                    user_id=msg["FromUserName"],
                    user_name=msg["ActualNickName"] if msg.get("ActualNickName") else msg["FromUserName"],
                    content=msg["Text"],
                    message_id=msg["MsgId"],
                    timestamp=msg["CreateTime"],
                )
                asyncio.create_task(self._handle_incoming(platform_msg))
            
            # 后台登录
            asyncio.create_task(asyncio.to_thread(itchat.auto_login, hotReload=True))
            
            self._connected = True
            logger.info("itchat 已连接")
            return True
        except ImportError:
            logger.error("itchat 未安装，请运行: pip install itchat-uos")
            return False
        except Exception as e:
            logger.error(f"itchat 连接失败: {e}")
            return False

    async def disconnect(self):
        """断开连接."""
        if self._mode == "wechaty" and self._bot:
            await self._bot.stop()
        elif self._mode == "itchat":
            try:
                import itchat
                itchat.logout()
            except Exception:
                pass
        self._connected = False

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送微信消息."""
        if not self._connected:
            return PlatformResponse(success=False, error="微信未连接")
        
        try:
            if self._mode == "wechaty":
                return await self._send_wechaty(channel_id, content)
            elif self._mode == "itchat":
                return await self._send_itchat(channel_id, content)
            else:
                return PlatformResponse(success=False, error="未知模式")
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def _send_wechaty(self, channel_id: str, content: str) -> PlatformResponse:
        """通过 Wechaty 发送消息."""
        if not self._bot:
            return PlatformResponse(success=False, error="Wechaty Bot 未初始化")
        
        # 判断是群聊还是私聊
        if channel_id.startswith("@@"):
            # 群聊
            room = await self._bot.Room.find(channel_id)
            if room:
                await room.say(content)
                return PlatformResponse(success=True)
            else:
                return PlatformResponse(success=False, error="群聊不存在")
        else:
            # 私聊
            contact = await self._bot.Contact.find(channel_id)
            if contact:
                await contact.say(content)
                return PlatformResponse(success=True)
            else:
                return PlatformResponse(success=False, error="联系人不存在")

    async def _send_itchat(self, channel_id: str, content: str) -> PlatformResponse:
        """通过 itchat 发送消息."""
        try:
            import itchat
            
            # 判断是群聊还是私聊
            if channel_id.startswith("@@"):
                # 群聊
                itchat.send(content, toUserName=channel_id)
            else:
                # 私聊
                itchat.send(content, toUserName=channel_id)
            
            return PlatformResponse(success=True)
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
        if not self._connected:
            return PlatformResponse(success=False, error="微信未连接")
        
        try:
            if self._mode == "wechaty":
                return await self._send_file_wechaty(channel_id, file_path)
            elif self._mode == "itchat":
                return await self._send_file_itchat(channel_id, file_path)
            else:
                return PlatformResponse(success=False, error="未知模式")
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def _send_file_wechaty(self, channel_id: str, file_path: str) -> PlatformResponse:
        """通过 Wechaty 发送文件."""
        if not self._bot:
            return PlatformResponse(success=False, error="Wechaty Bot 未初始化")
        
        from wechaty import FileBox
        
        file_box = FileBox.from_file(file_path)
        
        if channel_id.startswith("@@"):
            room = await self._bot.Room.find(channel_id)
            if room:
                await room.say(file_box)
                return PlatformResponse(success=True)
        else:
            contact = await self._bot.Contact.find(channel_id)
            if contact:
                await contact.say(file_box)
                return PlatformResponse(success=True)
        
        return PlatformResponse(success=False, error="目标不存在")

    async def _send_file_itchat(self, channel_id: str, file_path: str) -> PlatformResponse:
        """通过 itchat 发送文件."""
        try:
            import itchat
            itchat.send_file(file_path, toUserName=channel_id)
            return PlatformResponse(success=True)
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "weixin",
            "connected": self._connected,
            "mode": self._mode,
            "wechaty_configured": bool(self._wechaty_token),
        }
