"""Observability — 全链路追踪与可观测性.

借鉴 LangSmith/Langfuse 的设计，为 Scout Agent 提供：
- 分布式追踪（Trace/Span 模型）
- Token 消耗统计
- 延迟监控
- 工具调用成功率
- 可视化仪表板数据 API

设计原则：
- 轻量级：SQLite 存储，无需外部依赖
- 零侵入：通过回调和装饰器集成，不改动核心逻辑
- 实时查询：支持 REST API 暴露指标
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from scout.storage.schema import ensure_schema

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """追踪跨度 — 一次操作的完整记录."""
    id: str
    trace_id: str
    parent_id: str | None
    name: str
    span_type: str  # "llm" | "tool" | "conversation" | "reflection" | "goal"
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: int = 0
    status: str = "running"  # running | success | error
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "type": self.span_type,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "input": self.input_data,
            "output": self.output_data,
            "metadata": self.metadata,
            "error": self.error,
        }


@dataclass
class Trace:
    """追踪 — 一次完整对话的根跨度."""
    id: str
    session_id: str
    user_message: str
    start_time: datetime
    end_time: datetime | None = None
    total_duration_ms: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    status: str = "running"
    error: str | None = None
    spans: list[Span] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "user_message": self.user_message,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "total_duration_ms": self.total_duration_ms,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "status": self.status,
            "error": self.error,
            "spans": [s.to_dict() for s in self.spans],
        }


class ObservabilityTracker:
    """可观测性追踪器 — SQLite 持久化.

    用法：
        tracker = ObservabilityTracker()
        with tracker.trace_conversation(session_id, user_message) as trace:
            with tracker.span(trace.id, "llm", "thinker") as span:
                span.input_data = {"messages": [...]}
                # ... 执行 LLM 调用
                span.output_data = {"content": "...", "tokens": 100}
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(
            db_path
            if db_path is not None
            else str(_SCOUT_DATA_DIR / "observability.db")
        ).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS traces (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_message TEXT,
                start_time TEXT,
                end_time TEXT,
                total_duration_ms INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                total_cost REAL DEFAULT 0.0,
                status TEXT DEFAULT 'running',
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS spans (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                parent_id TEXT,
                name TEXT NOT NULL,
                span_type TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                duration_ms INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                input_data TEXT,
                output_data TEXT,
                metadata TEXT,
                error TEXT,
                FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_id) REFERENCES spans(id)
            );

            CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
        """)
        # 统一 schema 版本管理：自动执行缺失版本的增量迁移（幂等）
        ensure_schema(conn)
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id);
            CREATE INDEX IF NOT EXISTS idx_traces_time ON traces(start_time DESC);
        """)
        conn.commit()

    def start_trace(
        self,
        session_id: str,
        user_message: str,
    ) -> Trace:
        """开始追踪一次完整对话."""
        trace_id = str(uuid4())
        start_time = datetime.now()

        trace = Trace(
            id=trace_id,
            session_id=session_id,
            user_message=user_message,
            start_time=start_time,
        )

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO traces (id, session_id, user_message, start_time, status)
               VALUES (?, ?, ?, ?, ?)""",
            (trace.id, trace.session_id, trace.user_message,
             trace.start_time.isoformat(), trace.status),
        )
        conn.commit()
        return trace

    def end_trace(self, trace: Trace, status: str = "success", error: str | None = None) -> None:
        """结束追踪，聚合统计.

        2026-08-28 修复：
        - 支持 error 状态：异常路径也必须调用 end_trace，避免 trace 永远 running
        - error 时把该 trace 下所有仍 running 的 span 一并标记 error（防 UI 永久挂起）
        """
        trace.status = status
        trace.end_time = datetime.now()
        trace.total_duration_ms = int(
            (trace.end_time - trace.start_time).total_seconds() * 1000
        )
        if error and trace.error is None:
            trace.error = error
        # 聚合 token 和 cost
        conn = self._get_conn()
        span_rows = conn.execute(
            "SELECT output_data FROM spans WHERE trace_id = ?",
            (trace.id,),
        ).fetchall()
        for row in span_rows:
            if row["output_data"]:
                try:
                    output = json.loads(row["output_data"])
                    # 优先从 usage 嵌套字典获取 token，其次从顶层 tokens 字段
                    usage = output.get("usage", {})
                    if usage:
                        trace.total_tokens += usage.get("total_tokens", 0)
                    else:
                        trace.total_tokens += output.get("tokens", 0)
                    trace.total_cost += output.get("cost", 0.0)
                except Exception:
                    pass

        conn.execute(
            """UPDATE traces SET end_time = ?, total_duration_ms = ?,
               total_tokens = ?, total_cost = ?, status = ?, error = ?
               WHERE id = ?""",
            (trace.end_time.isoformat(), trace.total_duration_ms,
             trace.total_tokens, trace.total_cost, trace.status, error, trace.id),
        )
        if status == "error":
            # 异常结束：把仍 running 的 span 一并标记 error，避免 UI 永久显示 running
            now = datetime.now()
            conn.execute(
                """UPDATE spans SET end_time = ?, status = 'error',
                   duration_ms = ?,
                   error = COALESCE(error, ?)
                   WHERE trace_id = ? AND status = 'running'""",
                (
                    now.isoformat(),
                    max(0, int((now - trace.start_time).total_seconds() * 1000)),
                    (error or "trace ended with error"),
                    trace.id,
                ),
            )
        conn.commit()

    def start_span(
        self,
        trace_id: str,
        span_type: str,
        name: str,
        parent_id: str | None = None,
    ) -> Span:
        """开始追踪一个操作跨度."""
        span_id = str(uuid4())
        start_time = datetime.now()

        span = Span(
            id=span_id,
            trace_id=trace_id,
            parent_id=parent_id,
            name=name,
            span_type=span_type,
            start_time=start_time,
        )

        conn = self._get_conn()
        conn.execute(
            """INSERT INTO spans (id, trace_id, parent_id, name, span_type, start_time, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (span.id, span.trace_id, span.parent_id, span.name,
             span.span_type, span.start_time.isoformat(), span.status),
        )
        conn.commit()
        return span

    def end_span(self, span: Span) -> None:
        """结束跨度."""
        span.end_time = datetime.now()
        span.duration_ms = int(
            (span.end_time - span.start_time).total_seconds() * 1000
        )
        conn = self._get_conn()
        conn.execute(
            """UPDATE spans SET end_time = ?, duration_ms = ?, status = ?,
               input_data = ?, output_data = ?, metadata = ?, error = ?
               WHERE id = ?""",
            (span.end_time.isoformat(), span.duration_ms, span.status,
             json.dumps(span.input_data, ensure_ascii=False),
             json.dumps(span.output_data, ensure_ascii=False),
             json.dumps(span.metadata, ensure_ascii=False),
             span.error, span.id),
        )
        conn.commit()

    def get_trace(self, trace_id: str) -> Trace | None:
        """获取追踪详情."""
        conn = self._get_conn()
        trace_row = conn.execute(
            "SELECT * FROM traces WHERE id = ?", (trace_id,)
        ).fetchone()
        if not trace_row:
            return None

        trace = Trace(
            id=trace_row["id"],
            session_id=trace_row["session_id"],
            user_message=trace_row["user_message"],
            start_time=datetime.fromisoformat(trace_row["start_time"]),
            end_time=datetime.fromisoformat(trace_row["end_time"]) if trace_row["end_time"] else None,
            total_duration_ms=trace_row["total_duration_ms"],
            total_tokens=trace_row["total_tokens"],
            total_cost=trace_row["total_cost"],
            status=trace_row["status"],
        )

        span_rows = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_time",
            (trace_id,),
        ).fetchall()

        for sr in span_rows:
            trace.spans.append(Span(
                id=sr["id"],
                trace_id=sr["trace_id"],
                parent_id=sr["parent_id"],
                name=sr["name"],
                span_type=sr["span_type"],
                start_time=datetime.fromisoformat(sr["start_time"]),
                end_time=datetime.fromisoformat(sr["end_time"]) if sr["end_time"] else None,
                duration_ms=sr["duration_ms"],
                status=sr["status"],
                input_data=json.loads(sr["input_data"]) if sr["input_data"] else {},
                output_data=json.loads(sr["output_data"]) if sr["output_data"] else {},
                metadata=json.loads(sr["metadata"]) if sr["metadata"] else {},
                error=sr["error"],
            ))

        return trace

    def list_recent_traces(self, limit: int = 20) -> list[dict]:
        """列出最近的追踪."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id, session_id, user_message, start_time, total_duration_ms,
                      total_tokens, total_cost, status
               FROM traces ORDER BY start_time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self, hours: int = 24) -> dict:
        """获取统计指标."""
        conn = self._get_conn()
        datetime.now().isoformat()

        # 总追踪数
        total_traces = conn.execute(
            "SELECT COUNT(*) as cnt FROM traces WHERE start_time >= datetime('now', ?)",
            (f"-{hours} hours",),
        ).fetchone()["cnt"]

        # 成功率
        success_traces = conn.execute(
            """SELECT COUNT(*) as cnt FROM traces
               WHERE start_time >= datetime('now', ?) AND status = 'success'""",
            (f"-{hours} hours",),
        ).fetchone()["cnt"]

        # 总 token 和成本
        token_stats = conn.execute(
            """SELECT COALESCE(SUM(total_tokens), 0) as tokens,
                      COALESCE(SUM(total_cost), 0.0) as cost
               FROM traces WHERE start_time >= datetime('now', ?)""",
            (f"-{hours} hours",),
        ).fetchone()

        # 工具调用统计
        tool_stats = conn.execute(
            """SELECT name, COUNT(*) as calls,
                      AVG(duration_ms) as avg_duration,
                      SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successes
               FROM spans
               WHERE span_type = 'tool' AND start_time >= datetime('now', ?)
               GROUP BY name ORDER BY calls DESC LIMIT 10""",
            (f"-{hours} hours",),
        ).fetchall()

        return {
            "period_hours": hours,
            "total_traces": total_traces,
            "success_rate": success_traces / total_traces if total_traces > 0 else 0.0,
            "total_tokens": token_stats["tokens"],
            "total_cost": token_stats["cost"],
            "top_tools": [dict(r) for r in tool_stats],
        }
