"""LLM 调用监控 — 记录每次模型调用的 token 消耗和缓存命中."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# ── 成本估算参考单价（元/百万 token） ──
# 键为模型名前缀（按最长前缀匹配）; 值为 (输入价, 输出价, 缓存命中输入折扣系数)
# 说明: 参考价随厂商调整可能变动，仅用于成本估算展示，可在下方自行修改。
_MODEL_PRICES: dict[str, tuple[float, float, float]] = {
    "qwen-max": (2.4, 9.6, 0.1),
    "qwen-plus": (0.4, 1.2, 0.1),
    "qwen-turbo": (0.15, 0.6, 0.1),
    "qwen-long": (0.2, 0.4, 0.1),
    "deepseek-chat": (1.0, 2.0, 0.1),
    "deepseek-reasoner": (2.0, 8.0, 0.1),
    "gpt-4o-mini": (1.1, 4.3, 0.5),
    "gpt-4o": (18.0, 72.0, 0.5),
}
_DEFAULT_PRICE: tuple[float, float, float] = (2.0, 8.0, 0.1)


def _match_price(model: str) -> tuple[float, float, float]:
    """按模型名前缀匹配参考单价，未知名模型用默认值."""
    for prefix, price in sorted(_MODEL_PRICES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if model.startswith(prefix):
            return price
    return _DEFAULT_PRICE


def estimate_cost(
    model: str, prompt_tokens: int, cached_tokens: int, completion_tokens: int,
) -> tuple[float, float]:
    """估算一次调用的实际成本与缓存节省金额（元）.

    返回 (实际成本, 缓存节省)。缓存命中的输入 token 按折扣系数计价。
    """
    in_price, out_price, cache_discount = _match_price(model)
    cached = min(cached_tokens, prompt_tokens)
    fresh = max(prompt_tokens - cached, 0)
    real_cost = (fresh * in_price + cached * in_price * cache_discount) / 1_000_000 \
        + completion_tokens * out_price / 1_000_000
    saved = cached * in_price * (1 - cache_discount) / 1_000_000
    return real_cost, saved


class LLMUsageTracker:
    """LLM 调用监控器 — 记录 token 消耗到 SQLite."""

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            db_path = Path.home() / ".scout" / "usage.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        # WAL 模式：并发安全 + 崩溃可恢复（持久化到 DB 文件，后续连接自动继承）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                cache_hit INTEGER DEFAULT 0,
                latency_ms INTEGER DEFAULT 0,
                role TEXT DEFAULT '',
                session_id TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON llm_usage(timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_model ON llm_usage(model)
        """)
        conn.commit()
        conn.close()

    def record(
        self,
        model: str,
        provider: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cached_tokens: int = 0,
        cache_hit: bool = False,
        latency_ms: int = 0,
        role: str = "",
        session_id: str = "",
    ):
        """记录一次 LLM 调用."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """INSERT INTO llm_usage
               (timestamp, model, provider, prompt_tokens, completion_tokens,
                total_tokens, cached_tokens, cache_hit, latency_ms, role, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                model, provider,
                prompt_tokens, completion_tokens,
                total_tokens or (prompt_tokens + completion_tokens),
                cached_tokens,
                1 if cache_hit else 0,
                latency_ms, role, session_id,
            ),
        )
        conn.commit()
        conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows

    def get_summary(self, period: str = "day") -> dict:
        """获取指定时间范围的统计.

        period: day, week, month, year
        """
        now = datetime.now()
        if period == "day":
            start = now - timedelta(days=1)
        elif period == "week":
            start = now - timedelta(days=7)
        elif period == "month":
            start = now - timedelta(days=30)
        elif period == "year":
            start = now - timedelta(days=365)
        else:
            start = now - timedelta(days=1)

        rows = self._query(
            """SELECT model, provider,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(total_tokens) as total_tokens,
                      SUM(cached_tokens) as cached_tokens,
                      SUM(cache_hit) as cache_hits,
                      COUNT(*) as call_count,
                      AVG(latency_ms) as avg_latency
               FROM llm_usage
               WHERE timestamp >= ?
               GROUP BY model
               ORDER BY total_tokens DESC""",
            (start.isoformat(),),
        )
        total = self._query(
            """SELECT
                SUM(prompt_tokens) as prompt_tokens,
                SUM(completion_tokens) as completion_tokens,
                SUM(total_tokens) as total_tokens,
                SUM(cached_tokens) as cached_tokens,
                SUM(cache_hit) as cache_hits,
                COUNT(*) as call_count,
                AVG(latency_ms) as avg_latency
               FROM llm_usage WHERE timestamp >= ?""",
            (start.isoformat(),),
        )
        total_dict = total[0] if total else {}
        prompt = total_dict.get("prompt_tokens") or 0
        cached = total_dict.get("cached_tokens") or 0
        total_dict["cache_hit_rate"] = round(cached / prompt, 4) if prompt else 0.0

        # 成本估算（参考价，缓存命中按折扣计价）
        for row in rows:
            cost, saved = estimate_cost(
                row.get("model", ""),
                row.get("prompt_tokens") or 0,
                row.get("cached_tokens") or 0,
                row.get("completion_tokens") or 0,
            )
            row["estimated_cost_cny"] = round(cost, 6)
            row["estimated_saved_cny"] = round(saved, 6)
        if total_dict.get("call_count"):
            # 按各模型行加权汇总（比用默认价更准确）
            total_cost = sum(r.get("estimated_cost_cny", 0.0) for r in rows)
            total_saved = sum(r.get("estimated_saved_cny", 0.0) for r in rows)
            total_dict["estimated_cost_cny"] = round(total_cost, 6)
            total_dict["estimated_saved_cny"] = round(total_saved, 6)
        else:
            total_dict["estimated_cost_cny"] = 0.0
            total_dict["estimated_saved_cny"] = 0.0

        return {
            "period": period,
            "start": start.isoformat(),
            "end": now.isoformat(),
            "total": total_dict,
            "models": rows,
        }

    def get_daily(self, days: int = 30) -> list[dict]:
        """获取每日 token 消耗趋势."""
        rows = self._query(
            """SELECT DATE(timestamp) as date,
                      SUM(prompt_tokens) as prompt_tokens,
                      SUM(completion_tokens) as completion_tokens,
                      SUM(total_tokens) as total_tokens,
                      SUM(cached_tokens) as cached_tokens,
                      SUM(cache_hit) as cache_hits,
                      COUNT(*) as call_count
               FROM llm_usage
               WHERE timestamp >= DATE('now', ?)
               GROUP BY DATE(timestamp)
               ORDER BY date DESC""",
            (f"-{days} days",),
        )
        return rows

    def get_recent(self, limit: int = 20) -> list[dict]:
        """获取最近的调用记录."""
        return self._query(
            """SELECT * FROM llm_usage
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        )


# ── 全局单例 ──
token_tracker = LLMUsageTracker()
