"""Discord 适配器 — 基于 discord.py."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from typing import Any

import httpx

from scout.adapters.platforms.base import (
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_COUNT,
    ChannelAdapter,
    PlatformMessage,
    PlatformResponse,
    format_attachment_hints,
    save_inbound_attachment,
)

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

                platform_msg = await self._to_platform_message(message)
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

    async def _collect_attachments(self, message) -> tuple[list[dict], list[str]]:
        """把 discord.Message.attachments 下载到临时目录.

        直接用 httpx 拉取 Attachment.url（不依赖 discord.py 的读接口），
        返回 (附件元数据列表 {name,type,size,path}, 文本提示列表)。
        超限（>20MB / >5 个）的附件跳过并生成提示，单个失败不影响其余附件。
        """
        attachments: list[dict] = []
        notices: list[str] = []
        items = list(getattr(message, "attachments", None) or [])
        if not items:
            return attachments, notices

        async with httpx.AsyncClient(timeout=120) as client:
            for att in items:
                name = getattr(att, "filename", None) or "attachment"
                if len(attachments) >= ATTACHMENT_MAX_COUNT:
                    notices.append(
                        f"[附件: {name} 已跳过 — 单条消息最多 {ATTACHMENT_MAX_COUNT} 个附件]"
                    )
                    continue
                size = getattr(att, "size", 0) or 0
                if size > ATTACHMENT_MAX_BYTES:
                    notices.append(f"[附件: {name} 超过 20MB，已跳过]")
                    continue
                url = getattr(att, "url", None) or ""
                if not url:
                    notices.append(f"[附件: {name} 缺少下载地址，已跳过]")
                    continue
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        notices.append(f"[附件: {name} 下载失败，已跳过]")
                        continue
                    data = resp.content
                    mime = getattr(att, "content_type", None) or ""
                    if not mime:
                        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    path = save_inbound_attachment(name, data)
                    attachments.append(
                        {
                            "name": name,
                            "type": mime,
                            "size": size or len(data),
                            "path": path,
                        }
                    )
                except Exception:
                    notices.append(f"[附件: {name} 下载失败，已跳过]")
        return attachments, notices

    async def _to_platform_message(self, message) -> PlatformMessage:
        """把 discord.Message 转换为 PlatformMessage — 附件落盘并附到消息文本."""
        attachments, notices = await self._collect_attachments(message)
        content = message.content or ""
        hint = format_attachment_hints(attachments, notices)
        if hint:
            content = f"{content}\n\n{hint}" if content.strip() else hint
        return PlatformMessage(
            platform="discord",
            channel_id=str(message.channel.id),
            user_id=str(message.author.id),
            user_name=str(message.author),
            content=content,
            message_id=str(message.id),
            timestamp=message.created_at.timestamp(),
            attachments=attachments or None,
        )

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
