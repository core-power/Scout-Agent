"""多渠道适配器基类 — 统一 IM 平台接入接口.

支持平台:
- 钉钉 (DingTalk)
- 飞书 (Feishu/Lark)
- Discord
- Telegram
- Slack
- 微信 (WeChat)

每个平台实现此接口，通过 ChannelManager 统一管理。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any


@dataclass
class PlatformMessage:
    """平台消息 — 统一消息格式."""
    platform: str  # dingtalk / feishu / discord / telegram / slack / wechat
    channel_id: str  # 频道/群组 ID
    user_id: str  # 发送者 ID
    user_name: str  # 发送者名称
    content: str  # 消息内容
    message_id: str  # 消息 ID
    timestamp: float  # 时间戳
    attachments: list[dict] | None = None  # 附件列表
    reply_to: str | None = None  # 回复的消息 ID
    metadata: dict[str, Any] | None = None  # 平台特有元数据


@dataclass
class PlatformResponse:
    """平台响应 — 统一回复格式."""
    success: bool
    message_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None


class ChannelAdapter(ABC):
    """渠道适配器抽象基类."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.platform = config.get("platform", "unknown")
        self.channel_id = config.get("channel_id", "")
        self.enabled = config.get("enabled", True)

        # 消息处理回调（可选，直接回调模式；ChannelManager 走队列消费）
        self._message_handler: Callable[[PlatformMessage], Coroutine] | None = None
        # 消息队列 — ChannelManager 通过 listen() 消费
        self._incoming_queue: asyncio.Queue[PlatformMessage] | None = None

    # ── 生命周期：ChannelManager 统一调用 start/stop/listen ──
    # 2026-08-20 新增默认实现：此前 ChannelManager 依赖 start/stop/listen，
    # 但基类接口是 connect/disconnect，导致所有平台 `/api/channels/{name}/start`
    # 直接 AttributeError。默认实现桥接两者，新方言适配器无需改造即可接入。

    def _ensure_queue(self) -> None:
        if self._incoming_queue is None:
            self._incoming_queue = asyncio.Queue()

    async def start(self) -> bool:
        """启动适配器 — 默认实现：连接平台."""
        self._ensure_queue()
        return await self.connect()

    async def stop(self) -> None:
        """停止适配器 — 默认实现：断开连接."""
        if getattr(self, "_connected", False):
            await self.disconnect()

    async def listen(self):
        """消息循环 — 默认实现：从内部队列读取.

        新方言适配器收到消息后调用 `_handle_incoming()` 入队，这里逐条产出；
        ChannelManager 的 `_run_channel` 消费。
        """
        self._ensure_queue()
        while True:
            try:
                yield await self._incoming_queue.get()
            except asyncio.CancelledError:
                return

    @abstractmethod
    async def connect(self) -> bool:
        """连接到平台."""
        ...

    @abstractmethod
    async def disconnect(self):
        """断开连接."""
        ...

    @abstractmethod
    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: str | None = None,
        **kwargs,
    ) -> PlatformResponse:
        """发送消息."""
        ...

    @abstractmethod
    async def send_file(
        self,
        channel_id: str,
        file_path: str,
        caption: str = "",
        **kwargs,
    ) -> PlatformResponse:
        """发送文件."""
        ...

    def set_message_handler(self, handler: Callable[[PlatformMessage], Coroutine]):
        """设置消息处理回调 — 当收到消息时调用."""
        self._message_handler = handler

    async def _handle_incoming(self, message: PlatformMessage):
        """处理收到的消息 — 由子类调用.

        2026-08-20 修复：此前仅在有 `_message_handler` 回调时才处理，而
        `set_message_handler` 全项目无调用者，导致新方言适配器收到的消息
        被静默丢弃。现改为无条件入队（供 ChannelManager 的 listen() 消费），
        并保留回调兼容。
        """
        self._ensure_queue()
        await self._incoming_queue.put(message)
        if self._message_handler:
            try:
                await self._message_handler(message)
            except Exception as e:
                print(f"[{self.platform}] 消息处理错误: {e}")

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """健康检查."""
        ...

    def get_info(self) -> dict[str, Any]:
        """获取适配器信息."""
        return {
            "platform": self.platform,
            "channel_id": self.channel_id,
            "enabled": self.enabled,
            "connected": hasattr(self, "_connected") and self._connected,
        }

