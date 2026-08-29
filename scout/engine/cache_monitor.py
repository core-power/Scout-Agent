"""P0a: 缓存命中率监控 — 埋点先行，积累基线数据.

职责:
1. 记录每次 LLM 调用的缓存命中率 (cached_tokens / prompt_tokens)
2. 按会话维度聚合统计
3. 提供基线报告（P0 改造前后的对比依据）
4. 低命中率告警

设计原则:
- 零侵入：不修改 LLM 调用链路，仅消费 tracker 已记录的数据
- 轻量：内存中维护滑动窗口，定期持久化到 SQLite
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("scout.engine.cache_monitor")


@dataclass
class SessionCacheStats:
    """单会话缓存统计."""
    session_id: str
    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_cached_tokens: int = 0
    total_completion_tokens: int = 0
    low_hit_count: int = 0  # 命中率 < 50% 的调用次数
    last_call_time: float = 0.0

    @property
    def hit_ratio(self) -> float:
        if self.total_prompt_tokens == 0:
            return 0.0
        return self.total_cached_tokens / self.total_prompt_tokens

    @property
    def cache_savings_ratio(self) -> float:
        """缓存节省比例（缓存命中部分按 20% 计费，节省 80%）."""
        return self.hit_ratio * 0.8

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "hit_ratio": round(self.hit_ratio, 4),
            "cache_savings_ratio": round(self.cache_savings_ratio, 4),
            "low_hit_count": self.low_hit_count,
        }


@dataclass
class GlobalCacheStats:
    """全局缓存统计（跨会话聚合）."""
    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_cached_tokens: int = 0
    total_completion_tokens: int = 0
    sessions_tracked: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def hit_ratio(self) -> float:
        if self.total_prompt_tokens == 0:
            return 0.0
        return self.total_cached_tokens / self.total_prompt_tokens

    @property
    def input_cost_ratio(self) -> float:
        """输入侧实际成本占比（缓存命中部分按 20% 计费）.

        1.0 = 无缓存（全价），0.2 = 全命中（2 折）
        """
        if self.total_prompt_tokens == 0:
            return 1.0
        uncached = self.total_prompt_tokens - self.total_cached_tokens
        effective_cost = uncached + self.total_cached_tokens * 0.2
        return effective_cost / self.total_prompt_tokens

    def to_dict(self) -> dict:
        uptime_hours = (time.time() - self.start_time) / 3600
        return {
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "hit_ratio": round(self.hit_ratio, 4),
            "input_cost_ratio": round(self.input_cost_ratio, 4),
            "sessions_tracked": self.sessions_tracked,
            "uptime_hours": round(uptime_hours, 1),
        }


class CacheMonitor:
    """缓存命中率监控器.

    用法:
        monitor = get_cache_monitor()
        monitor.record(session_id, prompt_tokens, cached_tokens, completion_tokens)
        report = monitor.get_report()
    """

    LOW_HIT_THRESHOLD = 0.5  # 命中率低于 50% 视为低命中

    def __init__(self):
        self._sessions: dict[str, SessionCacheStats] = {}
        self._global = GlobalCacheStats()

    def record(
        self,
        session_id: str,
        prompt_tokens: int,
        cached_tokens: int,
        completion_tokens: int = 0,
        model: str = "",
    ) -> None:
        """记录一次 LLM 调用的缓存数据."""
        # 更新会话级统计
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionCacheStats(session_id=session_id)
            self._global.sessions_tracked += 1

        stats = self._sessions[session_id]
        stats.total_calls += 1
        stats.total_prompt_tokens += prompt_tokens
        stats.total_cached_tokens += cached_tokens
        stats.total_completion_tokens += completion_tokens
        stats.last_call_time = time.time()

        hit_ratio = cached_tokens / prompt_tokens if prompt_tokens > 0 else 0
        if hit_ratio < self.LOW_HIT_THRESHOLD:
            stats.low_hit_count += 1

        # 更新全局统计
        self._global.total_calls += 1
        self._global.total_prompt_tokens += prompt_tokens
        self._global.total_cached_tokens += cached_tokens
        self._global.total_completion_tokens += completion_tokens

        # 日志（低频，避免刷屏）
        if self._global.total_calls % 50 == 0:
            logger.info(
                f"[CacheMonitor] 全局命中率: {self._global.hit_ratio:.1%} "
                f"({self._global.total_calls} calls, "
                f"input_cost_ratio: {self._global.input_cost_ratio:.1%})"
            )

    def get_session_stats(self, session_id: str) -> dict | None:
        """获取单会话的缓存统计."""
        stats = self._sessions.get(session_id)
        return stats.to_dict() if stats else None

    def get_report(self) -> dict:
        """获取全局缓存报告."""
        report = self._global.to_dict()

        # 按命中率排序的会话列表（最近活跃的）
        active_sessions = sorted(
            [s for s in self._sessions.values() if s.total_calls > 0],
            key=lambda s: s.last_call_time,
            reverse=True,
        )[:20]
        report["recent_sessions"] = [s.to_dict() for s in active_sessions]

        # 健康度评估
        hit_ratio = self._global.hit_ratio
        if hit_ratio >= 0.9:
            report["health"] = "excellent"
            report["health_msg"] = "缓存命中率 ≥90%，前缀静态化生效 ✅"
        elif hit_ratio >= 0.7:
            report["health"] = "good"
            report["health_msg"] = "缓存命中率 70-90%，有优化空间"
        elif hit_ratio >= 0.5:
            report["health"] = "fair"
            report["health_msg"] = "缓存命中率 50-70%，建议检查前缀稳定性"
        else:
            report["health"] = "poor"
            report["health_msg"] = "缓存命中率 <50%，前缀可能被动态内容破坏 ⚠️"

        return report

    def check_p0_threshold(self, target: float = 0.9) -> bool:
        """检查是否达到 P0 验收标准（命中率 ≥ target 持续稳定）."""
        return self._global.hit_ratio >= target and self._global.total_calls >= 100

    def reset(self) -> None:
        """重置所有统计（用于 P0 改造后的新基线）."""
        self._sessions.clear()
        self._global = GlobalCacheStats()
        logger.info("[CacheMonitor] 统计已重置")


# ── 全局单例 ──
_monitor: CacheMonitor | None = None


def get_cache_monitor() -> CacheMonitor:
    """获取全局 CacheMonitor 实例."""
    global _monitor
    if _monitor is None:
        _monitor = CacheMonitor()
    return _monitor
