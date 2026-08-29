"""基础设施健康检查 — PostgreSQL / Redis / NATS.

提供统一的健康检查接口，用于监控和告警。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger("scout.infra.health")


@dataclass
class ComponentHealth:
    """组件健康状态."""
    name: str
    status: str  # "healthy" / "degraded" / "unhealthy"
    latency_ms: float = 0
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class InfraHealth:
    """基础设施整体健康状态."""
    status: str  # "healthy" / "degraded" / "unhealthy"
    components: list[ComponentHealth] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "timestamp": self.timestamp,
            "components": [
                {
                    "name": c.name,
                    "status": c.status,
                    "latency_ms": round(c.latency_ms, 2),
                    "details": c.details,
                }
                for c in self.components
            ],
        }


class HealthChecker:
    """基础设施健康检查器."""

    def __init__(
        self,
        storage=None,
        cache=None,
        event_bus=None,
    ):
        self._storage = storage
        self._cache = cache
        self._event_bus = event_bus

    async def check_all(self) -> InfraHealth:
        """检查所有组件."""
        tasks = []

        if self._storage:
            tasks.append(self._check_storage())
        if self._cache:
            tasks.append(self._check_cache())
        if self._event_bus:
            tasks.append(self._check_event_bus())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        components = []
        for r in results:
            if isinstance(r, Exception):
                components.append(ComponentHealth(
                    name="unknown",
                    status="unhealthy",
                    details={"error": str(r)},
                ))
            elif isinstance(r, ComponentHealth):
                components.append(r)

        # 整体状态
        if all(c.status == "healthy" for c in components):
            overall = "healthy"
        elif any(c.status == "unhealthy" for c in components):
            overall = "unhealthy"
        else:
            overall = "degraded"

        return InfraHealth(status=overall, components=components)

    async def _check_storage(self) -> ComponentHealth:
        """检查存储后端."""
        import time
        start = time.monotonic()

        try:
            ok = await self._storage.health_check()
            latency = (time.monotonic() - start) * 1000

            if ok:
                return ComponentHealth(
                    name="storage",
                    status="healthy",
                    latency_ms=latency,
                    details={"type": type(self._storage).__name__},
                )
            else:
                return ComponentHealth(
                    name="storage",
                    status="unhealthy",
                    latency_ms=latency,
                    details={"error": "health_check returned False"},
                )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="storage",
                status="unhealthy",
                latency_ms=latency,
                details={"error": str(e)},
            )

    async def _check_cache(self) -> ComponentHealth:
        """检查缓存后端."""
        import time
        start = time.monotonic()

        try:
            ok = await self._cache.health_check()
            latency = (time.monotonic() - start) * 1000

            if ok:
                return ComponentHealth(
                    name="cache",
                    status="healthy",
                    latency_ms=latency,
                    details={"type": type(self._cache).__name__},
                )
            else:
                return ComponentHealth(
                    name="cache",
                    status="degraded",
                    latency_ms=latency,
                    details={"warning": "cache health_check returned False"},
                )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="cache",
                status="degraded",  # 缓存故障不致命
                latency_ms=latency,
                details={"error": str(e)},
            )

    async def _check_event_bus(self) -> ComponentHealth:
        """检查事件总线."""
        import time
        start = time.monotonic()

        try:
            if hasattr(self._event_bus, "health_check"):
                ok = await self._event_bus.health_check()
            else:
                # 内存 EventBus 始终健康
                ok = True

            latency = (time.monotonic() - start) * 1000

            if ok:
                return ComponentHealth(
                    name="event_bus",
                    status="healthy",
                    latency_ms=latency,
                    details={"type": type(self._event_bus).__name__},
                )
            else:
                return ComponentHealth(
                    name="event_bus",
                    status="unhealthy",
                    latency_ms=latency,
                    details={"error": "event_bus health_check returned False"},
                )
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            return ComponentHealth(
                name="event_bus",
                status="unhealthy",
                latency_ms=latency,
                details={"error": str(e)},
            )
