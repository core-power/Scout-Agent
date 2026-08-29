"""分布式追踪 — 借鉴 OpenTelemetry 的 Span 模型.

追踪 Agent 执行链路：
- 对话级 Span（整体耗时）
- 工具调用 Span（每个工具执行）
- LLM 调用 Span（每次 API 请求）
- 子代理 Span（委派任务）

用于性能分析和故障排查。
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator


@dataclass
class Span:
    """追踪 Span — 表示一个操作单元."""
    trace_id: str
    span_id: str
    parent_id: str | None
    operation: str
    start_time: float
    end_time: float | None = None
    status: str = "running"  # running / success / error
    tags: dict[str, Any] = field(default_factory=dict)
    logs: list[dict] = field(default_factory=list)
    children: list[Span] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        """耗时（毫秒）."""
        if self.end_time is None:
            return (time.time() - self.start_time) * 1000
        return (self.end_time - self.start_time) * 1000

    def add_log(self, message: str, level: str = "info", **kwargs):
        """添加日志."""
        self.logs.append({
            "timestamp": time.time(),
            "message": message,
            "level": level,
            **kwargs,
        })

    def set_tag(self, key: str, value: Any):
        """设置标签."""
        self.tags[key] = value

    def finish(self, status: str = "success"):
        """结束 Span."""
        self.end_time = time.time()
        self.status = status

    def to_dict(self) -> dict:
        """序列化为字典."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "operation": self.operation,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "tags": self.tags,
            "logs": self.logs,
            "children": [c.to_dict() for c in self.children],
        }


class TracingManager:
    """追踪管理器 — 管理多个 Trace."""

    def __init__(self, max_traces: int = 1000):
        self.max_traces = max_traces
        self._traces: dict[str, Span] = {}  # trace_id -> root span
        self._active_spans: dict[str, Span] = {}  # span_id -> span

    def start_trace(self, operation: str, **tags) -> Span:
        """启动新的 Trace."""
        trace_id = str(uuid.uuid4())[:8]
        span_id = str(uuid.uuid4())[:8]
        
        span = Span(
            trace_id=trace_id,
            span_id=span_id,
            parent_id=None,
            operation=operation,
            start_time=time.time(),
            tags=tags,
        )
        
        self._traces[trace_id] = span
        self._active_spans[span_id] = span
        
        # 清理旧 trace
        if len(self._traces) > self.max_traces:
            oldest = sorted(self._traces.values(), key=lambda s: s.start_time)
            for old_span in oldest[:len(self._traces) - self.max_traces]:
                self._traces.pop(old_span.trace_id, None)
                self._active_spans.pop(old_span.span_id, None)
        
        return span

    def start_span(self, parent: Span, operation: str, **tags) -> Span:
        """在父 Span 下创建子 Span."""
        span_id = str(uuid.uuid4())[:8]
        
        span = Span(
            trace_id=parent.trace_id,
            span_id=span_id,
            parent_id=parent.span_id,
            operation=operation,
            start_time=time.time(),
            tags=tags,
        )
        
        parent.children.append(span)
        self._active_spans[span_id] = span
        
        return span

    def finish_span(self, span: Span, status: str = "success"):
        """结束 Span."""
        span.finish(status)
        self._active_spans.pop(span.span_id, None)

    @asynccontextmanager
    async def trace(self, operation: str, **tags) -> AsyncIterator[Span]:
        """追踪上下文管理器 — 自动开始和结束."""
        span = self.start_trace(operation, **tags)
        try:
            yield span
            self.finish_span(span, "success")
        except Exception as e:
            span.set_tag("error", str(e))
            self.finish_span(span, "error")
            raise

    @asynccontextmanager
    async def span(self, parent: Span, operation: str, **tags) -> AsyncIterator[Span]:
        """子 Span 上下文管理器."""
        child = self.start_span(parent, operation, **tags)
        try:
            yield child
            self.finish_span(child, "success")
        except Exception as e:
            child.set_tag("error", str(e))
            self.finish_span(child, "error")
            raise

    def get_trace(self, trace_id: str) -> dict | None:
        """获取 Trace 详情."""
        span = self._traces.get(trace_id)
        return span.to_dict() if span else None

    def list_traces(self, limit: int = 50) -> list[dict]:
        """列出最近的 Trace."""
        traces = sorted(
            self._traces.values(),
            key=lambda s: s.start_time,
            reverse=True,
        )[:limit]
        
        return [
            {
                "trace_id": t.trace_id,
                "operation": t.operation,
                "start_time": datetime.fromtimestamp(t.start_time).isoformat(),
                "duration_ms": t.duration_ms,
                "status": t.status,
                "children_count": len(t.children),
            }
            for t in traces
        ]

    def stats(self) -> dict:
        """追踪统计."""
        if not self._traces:
            return {"total_traces": 0}
        
        durations = [t.duration_ms for t in self._traces.values() if t.end_time]
        
        return {
            "total_traces": len(self._traces),
            "active_traces": len([t for t in self._traces.values() if t.end_time is None]),
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
            "max_duration_ms": max(durations) if durations else 0,
            "min_duration_ms": min(durations) if durations else 0,
        }


# 全局追踪管理器
_tracing_manager = TracingManager()


def get_tracing_manager() -> TracingManager:
    """获取全局追踪管理器."""
    return _tracing_manager
