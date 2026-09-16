"""事件总线 — 发布/订阅 + 异步分发.

重构: 支持内存 EventBus（默认）和 NATS JetStream（生产环境）。
通过 SCOUT_BUS_BACKEND 环境变量切换:
- "memory" (默认): 内存 EventBus
- "nats": NATS JetStream 事件总线

借鉴 OpenClaw 的事件系统设计，所有模块通过事件总线松耦合通信。

优化 (2026-08-01):
- 添加 dead letter 队列 — 订阅者异常不再只打日志，而是进入 DLQ 供排查
- 添加告警回调机制 — 可注册 on_error 回调处理异常事件

优化 (2026-08-13):
- 支持 NATS JetStream 后端（通过工厂模式切换）
- 保留内存 EventBus 作为默认后端
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine

logger = logging.getLogger("scout.bus")


@dataclass
class DeadLetterEntry:
    """死信队列条目 — 记录处理失败的事件."""
    event: dict[str, Any]
    handler: Callable
    error: Exception
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def handler_name(self) -> str:
        return getattr(self.handler, "__name__", str(self.handler))

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "handler": self.handler_name,
            "error": str(self.error),
            "error_type": type(self.error).__name__,
            "timestamp": self.timestamp.isoformat(),
        }


class EventBus:
    """事件总线 — 发布/订阅 + 异步分发.

    支持同步和异步 handler，异常被捕获并记录到 DLQ。
    可通过 on_error() 注册回调处理异常事件（如告警）。

    用法:
        bus = EventBus()

        # 订阅
        @bus.on("notification")
        async def handle_notification(event):
            ...

        # 发布
        await bus.emit("notification", {"title": "任务完成", ...})

        # 注册错误回调
        bus.on_error(lambda dle: alert(dle))
    """

    def __init__(self, max_history: int = 100, max_dlq: int = 50):
        self._subscribers: dict[str, list[Callable[..., Coroutine]]] = {}
        self._history: deque[dict] = deque(maxlen=max_history)
        self._dlq: deque[DeadLetterEntry] = deque(maxlen=max_dlq)
        self._error_callbacks: list[Callable[[DeadLetterEntry], Any]] = []

    def on(self, event_type: str, handler: Callable[..., Coroutine]):
        """订阅事件 — 可作为装饰器使用."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        return handler  # 支持 @bus.on("type") 装饰器语法

    def off(self, event_type: str, handler: Callable[..., Coroutine]):
        """取消订阅."""
        if event_type in self._subscribers:
            self._subscribers[event_type] = [
                h for h in self._subscribers[event_type] if h != handler
            ]

    def on_error(self, callback: Callable[[DeadLetterEntry], Any]):
        """注册错误回调 — 当 handler 异常时触发.

        回调可以是同步或异步函数，接收 DeadLetterEntry 参数。
        """
        self._error_callbacks.append(callback)

    async def emit(self, event_type: str, data: dict[str, Any] | None = None):
        """发布事件 — 异步分发到所有订阅者."""
        event = {
            "type": event_type,
            "data": data or {},
            "timestamp": datetime.now().isoformat(),
        }
        self._history.append(event)

        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                # 记录到 DLQ
                dlq_entry = DeadLetterEntry(event=event, handler=handler, error=e)
                self._dlq.append(dlq_entry)
                logger.warning(
                    f"事件处理失败: {event_type} → "
                    f"{dlq_entry.handler_name}: {e}"
                )

                # 触发错误回调
                for callback in self._error_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(dlq_entry)
                        else:
                            callback(dlq_entry)
                    except Exception as cb_err:
                        logger.error(f"错误回调执行失败: {cb_err}")

    def get_history(
        self, event_type: str | None = None, limit: int = 20
    ) -> list[dict]:
        """获取事件历史."""
        events = list(self._history)
        if event_type:
            events = [e for e in events if e["type"] == event_type]
        return events[-limit:]

    def get_dlq(self, limit: int = 20) -> list[dict]:
        """获取死信队列."""
        entries = list(self._dlq)[-limit:]
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
        """健康检查 — 内存 EventBus 始终健康."""
        return True


# ========== 事件总线工厂 ==========

_bus_instance: EventBus | None = None


def get_bus() -> EventBus:
    """获取全局事件总线实例.

    根据 SCOUT_BUS_BACKEND 环境变量选择后端:
    - "memory" (默认): 内存 EventBus
    - "nats": NATS JetStream 事件总线
    """
    global _bus_instance

    if _bus_instance is not None:
        return _bus_instance

    backend = os.getenv("SCOUT_BUS_BACKEND", "memory")

    if backend == "nats":
        try:
            from scout.bus.nats_bus import NATSEventBus

            servers_str = os.getenv("SCOUT_NATS_SERVERS", "nats://localhost:4222")
            servers = [s.strip() for s in servers_str.split(",")]

            _bus_instance = NATSEventBus(servers=servers)
            logger.info(f"事件总线: NATS JetStream (servers={servers})")
        except ImportError:
            logger.warning("nats-py 未安装，降级为内存 EventBus")
            _bus_instance = EventBus()
    else:
        _bus_instance = EventBus()
        logger.info("事件总线: 内存 EventBus")

    return _bus_instance


def reset_bus():
    """重置全局事件总线实例（用于测试）."""
    global _bus_instance
    _bus_instance = None


# 全局默认实例（向后兼容）
bus = EventBus()
