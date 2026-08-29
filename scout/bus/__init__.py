"""Scout Agent 事件总线.

支持内存 EventBus（默认）和 NATS JetStream（生产环境）。
通过 SCOUT_BUS_BACKEND 环境变量切换后端。
"""

from scout.bus.hub import EventBus, bus, get_bus, reset_bus

__all__ = ["EventBus", "bus", "get_bus", "reset_bus"]
