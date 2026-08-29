"""Discord 适配器 — 基于 discord.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse

logger = logging.getLogger(__name__)


class DiscordAdapter(ChannelAdapter):
    """Discord Bot 适配器.
    
    使用 discord.py 库实现完整的 Bot 功能:
    - 消息收发
    - 文件上传
    - 频道管理
    """

    def __init__(self, config: dict):
        super().__init__(config)
        self.platform = "discord"
        self._connected = False
        self._bot_token = config.get("bot_token", "")
        self._client = None
        self._message_queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        """连接 Discord."""
        if not self._bot_token:
            logger.error("Discord Bot Token 未配置")
            return False
        
        try:
            import discord
            from discord.ext import commands
            
            # 创建 Bot 实例
            intents = discord.Intents.default()
            intents.message_content = True
            intents.guilds = True
            
            self._client = commands.Bot(command_prefix="!", intents=intents)
            
            # 注册事件处理器
            @self._client.event
            async def on_ready():
                logger.info(f"Discord Bot 已登录: {self._client.user}")
                self._connected = True
            
            @self._client.event
            async def on_message(message):
                if message.author == self._client.user:
                    return
                
                # 转换为 PlatformMessage
                platform_msg = PlatformMessage(
                    platform="discord",
                    channel_id=str(message.channel.id),
                    user_id=str(message.author.id),
                    user_name=str(message.author),
                    content=message.content,
                    message_id=str(message.id),
                    timestamp=message.created_at.timestamp(),
                )
                
                await self._handle_incoming(platform_msg)
            
            # 后台启动
            asyncio.create_task(self._client.start(self._bot_token))
            
            # 等待连接就绪
            for _ in range(50):  # 最多等待 5 秒
                if self._connected:
                    return True
                await asyncio.sleep(0.1)
            
            return self._connected
        except ImportError:
            logger.error("discord.py 未安装，请运行: pip install discord.py")
            return False
        except Exception as e:
            logger.error(f"Discord 连接失败: {e}")
            return False

    async def disconnect(self):
        """断开连接."""
        if self._client:
            await self._client.close()
        self._connected = False

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送 Discord 消息."""
        if not self._client or not self._connected:
            return PlatformResponse(success=False, error="Discord 未连接")
        
        try:
            channel = self._client.get_channel(int(channel_id))
            if not channel:
                return PlatformResponse(success=False, error=f"频道 {channel_id} 不存在")
            
            # 如果需要回复
            reference = None
            if reply_to:
                try:
                    ref_msg = await channel.fetch_message(int(reply_to))
                    reference = ref_msg.to_reference()
                except Exception:
                    pass
            
            msg = await channel.send(content, reference=reference)
            return PlatformResponse(success=True, message_id=str(msg.id))
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
        if not self._client or not self._connected:
            return PlatformResponse(success=False, error="Discord 未连接")
        
        try:
            import discord
            
            channel = self._client.get_channel(int(channel_id))
            if not channel:
                return PlatformResponse(success=False, error=f"频道 {channel_id} 不存在")
            
            file = discord.File(file_path)
            msg = await channel.send(content=caption, file=file)
            return PlatformResponse(success=True, message_id=str(msg.id))
        except Exception as e:
            return PlatformResponse(success=False, error=str(e))

    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        return {
            "platform": "discord",
            "connected": self._connected,
            "bot_user": str(self._client.user) if self._client and self._client.user else None,
            "guilds_count": len(self._client.guilds) if self._client else 0,
        }
