"""Gateway 控制面模块.

- Gateway: 控制面（借鉴 OpenClaw 单一控制面设计），原 scout/gateway.py，
  2026-08-03 迁入 control.py（此前被同名包遮蔽，从未被真正导入）。
- TracingManager / UsageTracker: 追踪与用量统计子系统。
"""

from scout.gateway.control import Gateway
from scout.gateway.tracing import TracingManager
from scout.gateway.usage import UsageTracker

__all__ = ["Gateway", "TracingManager", "UsageTracker"]
