"""NATS JetStream 事件总线实现.

替代内存 EventBus，支持：
- 消息持久化（JetStream）
- 多消费者组
- DLQ（死信队列）语义保留
- on_error 回调机制保留
- 历史消息持久化到 NATS
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Coroutine

try:
    import nats
    from nats.aio.client import Client as NATSClient
    from nats.js.api import RetentionPolicy, StorageType
    HAS_NATS = True
except ImportError:
    HAS_NATS = False

from scout.bus.hub import DeadLetterEntry

logger = logging.getLogger("scout.bus.nats")

# NATS 主题前缀
SUBJECT_PREFIX = "scout.events"
DLQ_SUBJECT = "scout.dlq"
HISTORY_STREAM = "SCOUT_EVENTS"


class NATSEventBus:
    """NATS JetStream 事件总线 — 持久化 + 多副本.

    保留与原 EventBus 相同的 API 语义：
    - on() / off() 订阅/取消
    - emit() 发布
    - on_error() 错误回调
    - get_history() 历史查询
    - get_dlq() / clear_dlq() 死信队列管理
    """

    def __init__(
        self,
        servers: list[str] | None = None,
        max_history: int = 100,
        max_dlq: int = 50,
    ):
        if not HAS_NATS:
            raise ImportError("nats-py 未安装，请运行: pip install nats-py")

        self._servers = servers or ["nats://localhost:4222"]
        self._max_history = max_history
        self._max_dlq = max_dlq

        self._client: NATSClient | None = None
        self._js: Any = None  # JetStream context
        self._subscriptions: dict[str, list[Any]] = {}  # subject -> [sub]
        self._handlers: dict[str, list[Callable[..., Coroutine]]] = {}
        self._error_callbacks: list[Callable[[DeadLetterEntry], Any]] = []

        # 本地缓存（降级/快速查询用）
        self._history: list[dict] = []
        self._dlq: list[DeadLetterEntry] = []

    async def connect(self) -> None:
        """连接 NATS 并初始化 JetStream."""
        self._client = await nats.connect(
            servers=self._servers,
            reconnect_time_wait=2,
            max_reconnect_attempts=-1,  # 无限重连
        )
        self._js = self._client.jetstream()

        # 确保 Stream 存在
        try:
            await self._js.add_stream(
                name=HISTORY_STREAM,
                subjects=[f"{SUBJECT_PREFIX}.>"],
                retention=RetentionPolicy.LIMITS,
                storage=StorageType.FILE,
                max_msgs=self._max_history * 10,
            )
        except Exception:
            pass  # Stream 已存在

        # 确保 DLQ Stream 存在
        try:
            await self._js.add_stream(
                name="SCOUT_DLQ",
                subjects=[f"{DLQ_SUBJECT}.>"],
                retention=RetentionPolicy.LIMITS,
                storage=StorageType.FILE,
                max_msgs=self._max_dlq * 10,
            )
        except Exception:
            pass

        logger.info(f"NATS JetStream 已连接: {self._servers}")

    async def disconnect(self) -> None:
        """断开连接."""
        for subs in self._subscriptions.values():
            for sub in subs:
                try:
                    await sub.unsubscribe()
                except Exception:
                    pass

        if self._client:
            await self._client.close()
            logger.info("NATS 连接已关闭")

    def on(self, event_type: str, handler: Callable[..., Coroutine]):
        """订阅事件 — 注册本地 handler + NATS 订阅."""
        subject = f"{SUBJECT_PREFIX}.{event_type}"

        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

        # 异步创建 NATS 订阅
        asyncio.create_task(self._subscribe(subject, event_type))

    async def _subscribe(self, subject: str, event_type: str):
        """创建 NATS 订阅."""
        assert self._js, "NATS 未连接"

        async def _handler(msg):
            try:
                data = json.loads(msg.data.decode())
                event = data.get("event", {})

                handlers = self._handlers.get(event_type, [])
                for h in handlers:
                    try:
                        await h(event)
                    except Exception as e:
                        dlq_entry = DeadLetterEntry(
                            event=event, handler=h, error=e
                        )
                        self._dlq.append(dlq_entry)
                        if len(self._dlq) > self._max_dlq:
                            self._dlq = self._dlq[-self._max_dlq:]

                        logger.warning(
                            f"事件处理失败: {event_type} → "
                            f"{dlq_entry.handler_name}: {e}"
                        )

                        # 发布到 DLQ 主题
                        try:
                            await self._js.publish(
                                f"{DLQ_SUBJECT}.{event_type}",
                                json.dumps(dlq_entry.to_dict()).encode(),
                            )
                        except Exception:
                            pass

                        # 触发错误回调
                        for cb in self._error_callbacks:
                            try:
                                if asyncio.iscoroutinefunction(cb):
                                    await cb(dlq_entry)
                                else:
                                    cb(dlq_entry)
                            except Exception:
                                pass

                await msg.ack()
            except Exception as e:
                logger.error(f"NATS 消息处理异常: {e}")
                await msg.nak()

        sub = await self._js.subscribe(subject, cb=_handler)

        if subject not in self._subscriptions:
            self._subscriptions[subject] = []
        self._subscriptions[subject].append(sub)

    def off(self, event_type: str, handler: Callable[..., Coroutine]):
        """取消订阅."""
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h != handler
            ]

    def on_error(self, callback: Callable[[DeadLetterEntry], Any]):
        """注册错误回调."""
        self._error_callbacks.append(callback)

    async def emit(self, event_type: str, data: dict[str, Any] | None = None):
        """发布事件 — 通过 NATS JetStream 持久化."""
        assert self._js, "NATS 未连接"

        event = {
            "type": event_type,
            "data": data or {},
            "timestamp": datetime.now().isoformat(),
        }

        # 本地历史缓存
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        # 发布到 NATS
        subject = f"{SUBJECT_PREFIX}.{event_type}"
        payload = json.dumps({"event": event}).encode()

        try:
            await self._js.publish(subject, payload)
        except Exception as e:
            logger.error(f"NATS 发布失败: {subject}: {e}")
            raise

    def get_history(
        self, event_type: str | None = None, limit: int = 20
    ) -> list[dict]:
        """获取事件历史（本地缓存）."""
        events = self._history
        if event_type:
            events = [e for e in events if e["type"] == event_type]
        return events[-limit:]

    def get_dlq(self, limit: int = 20) -> list[dict]:
        """获取死信队列."""
        entries = self._dlq[-limit:]
        return [e.to_dict() for e in entries]

    def clear_dlq(self) -> int:
        """清空死信队列."""
        count = len(self._dlq)
        self._dlq.clear()
        return count

    @property
    def dlq_size(self) -> int:
        return len(self._dlq)

    async def health_check(self) -> bool:
        """健康检查."""
        try:
            if self._client and self._client.is_connected:
                await self._client.flush(timeout=2)
                return True
            return False
        except Exception:
            return False
