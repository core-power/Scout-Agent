"""运行记录 — 无人值守任务的全生命周期留痕与结构化统计.

对标 Agent Harness 的 Verification Layer 证据链要求：
每次自动化运行（cron/webhook/事件/级联）记录完整执行档案，
支持事后审计「AI 在我睡觉时干了什么」。

存储: $SCOUT_DATA_DIR/runs.db (SQLite)，保留最近 1000 条。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from scout.storage.schema import ensure_schema
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = _SCOUT_DATA_DIR / "runs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    trigger_id TEXT DEFAULT '',
    task TEXT NOT NULL,
    status TEXT DEFAULT 'running',
    started_at REAL NOT NULL,
    finished_at REAL,
    steps INTEGER DEFAULT 0,
    tool_calls INTEGER DEFAULT 0,
    session_id TEXT DEFAULT '',
    response_summary TEXT DEFAULT '',
    verification TEXT DEFAULT '',
    events TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""


class RunStore:
    """运行记录存储."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else _DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._conn() as conn:
                conn.executescript(_SCHEMA)
                ensure_schema(conn)
        except Exception as e:
            logger.warning(f"runs.db 初始化失败: {e}")

    # ── 写入 ──

    def start_run(
        self,
        source: str,
        task: str,
        trigger_id: str = "",
        session_id: str = "",
    ) -> str:
        """开始一次运行，返回 run_id."""
        run_id = str(uuid.uuid4())[:12]
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO runs (run_id, source, trigger_id, task, status, started_at, session_id) "
                    "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                    (run_id, source, trigger_id, task[:2000], time.time(), session_id),
                )
        except Exception as e:
            logger.warning(f"运行记录写入失败: {e}")
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        steps: int = 0,
        tool_calls: int = 0,
        response_summary: str = "",
        verification: dict | None = None,
        events: list | None = None,
    ) -> None:
        """结束运行并写入结果.

        注意：events 为 None 时保留运行期间已追加的事件流（append_event），
        传空列表才显式清空。
        """
        try:
            with self._conn() as conn:
                if events is None:
                    # 保留已追加的事件流
                    row = conn.execute(
                        "SELECT events FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                    events_json = row["events"] if row and row["events"] else ""
                else:
                    events_json = json.dumps(events, ensure_ascii=False)
                conn.execute(
                    "UPDATE runs SET status=?, finished_at=?, steps=?, tool_calls=?, "
                    "response_summary=?, verification=?, events=? WHERE run_id=?",
                    (
                        status, time.time(), steps, tool_calls,
                        response_summary[:3000],
                        json.dumps(verification, ensure_ascii=False) if verification else "",
                        events_json,
                        run_id,
                    ),
                )
        except Exception as e:
            logger.warning(f"运行记录更新失败: {e}")

    def append_event(self, run_id: str, event: dict) -> None:
        """追加执行事件（工具调用/审批拒绝/验证结果等）."""
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT events FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if not row:
                    return
                events = json.loads(row["events"]) if row["events"] else []
                events.append({"ts": time.time(), **event})
                events = events[-200:]  # 上限
                conn.execute(
                    "UPDATE runs SET events=? WHERE run_id=?",
                    (json.dumps(events, ensure_ascii=False), run_id),
                )
        except Exception as e:
            logger.debug(f"事件追加失败: {e}")

    # ── 查询 ──

    def get(self, run_id: str) -> dict | None:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                return self._row_to_dict(row) if row else None
        except Exception:
            return None

    def list(self, limit: int = 50, source: str = "") -> list[dict]:
        try:
            with self._conn() as conn:
                if source:
                    rows = conn.execute(
                        "SELECT * FROM runs WHERE source=? ORDER BY started_at DESC LIMIT ?",
                        (source, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
                    ).fetchall()
                return [self._row_to_dict(r) for r in rows]
        except Exception:
            return []

    def stats(self, days: int = 7) -> dict[str, Any]:
        """结构化统计 — 按来源分组的成功率、平均步数、失败原因."""
        since = time.time() - days * 86400
        result: dict[str, Any] = {"days": days, "total": 0, "by_source": {}}
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT source, status, COUNT(*) as n, "
                    "AVG(steps) as avg_steps, AVG(CASE WHEN finished_at > 0 THEN finished_at - started_at END) as avg_duration "
                    "FROM runs WHERE started_at >= ? GROUP BY source, status",
                    (since,),
                ).fetchall()
                for r in rows:
                    src = r["source"]
                    if src not in result["by_source"]:
                        result["by_source"][src] = {
                            "total": 0, "success": 0, "failed": 0,
                            "verification_failed": 0, "running": 0,
                            "avg_steps": 0, "avg_duration_sec": 0,
                        }
                    bucket = result["by_source"][src]
                    n = r["n"]
                    bucket["total"] += n
                    result["total"] += n
                    if r["status"] in bucket:
                        bucket[r["status"]] += n
                    bucket["avg_steps"] = round(r["avg_steps"] or 0, 1)
                    bucket["avg_duration_sec"] = round(r["avg_duration"] or 0, 1)
                # 计算成功率
                for bucket in result["by_source"].values():
                    done = bucket["total"] - bucket["running"]
                    bucket["success_rate"] = round(bucket["success"] / done, 3) if done else None
        except Exception as e:
            result["error"] = str(e)
        return result

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("verification", "events"):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except Exception:
                    pass
        return d

    def cleanup(self, keep: int = 1000) -> int:
        """清理旧记录."""
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "DELETE FROM runs WHERE run_id NOT IN "
                    "(SELECT run_id FROM runs ORDER BY started_at DESC LIMIT ?)",
                    (keep,),
                )
                return cur.rowcount
        except Exception:
            return 0
