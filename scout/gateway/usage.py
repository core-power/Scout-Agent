"""用量追踪 — Token 消耗、API 调用、成本统计.

重构: 使用 StorageBackend 持久化 + CacheBackend 缓存。
- 持久化: StorageBackend (PostgreSQL / SQLite) 替代 JSONL 文件
- 缓存: CacheBackend (Redis) 缓存摘要和每日统计

追踪维度：
- LLM API 调用次数和 Token 消耗
- 工具调用次数和成功率
- 会话数和消息数
- 成本估算（基于模型定价）

用于成本控制和资源规划。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout.storage.base import CacheBackend, StorageBackend

logger = logging.getLogger("scout.gateway.usage")


@dataclass
class UsageRecord:
    """单条用量记录."""
    timestamp: float
    model: str
    role: str  # thinker / executor / main
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    session_id: str = ""
    tool_calls: int = 0
    latency_ms: float = 0


class UsageTracker:
    """用量追踪器 — 支持多后端持久化 + Redis 缓存.

    持久化层通过 StorageBackend 抽象，支持 SQLite / PostgreSQL。
    缓存层通过 CacheBackend 抽象，支持 Redis。

    缓存策略:
    - 摘要统计: TTL 60 秒（频繁读取，变化快）
    - 每日明细: TTL 5 分钟
    - 预算信息: TTL 10 分钟
    """

    # 模型定价（USD per 1K tokens）
    PRICING = {
        "gpt-4o": {"prompt": 0.005, "completion": 0.015},
        "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.0006},
        "gpt-4-turbo": {"prompt": 0.01, "completion": 0.03},
        "qwen-max": {"prompt": 0.002, "completion": 0.006},
        "qwen-plus": {"prompt": 0.0004, "completion": 0.0012},
        "qwen-turbo": {"prompt": 0.0002, "completion": 0.0006},
        "deepseek-chat": {"prompt": 0.00014, "completion": 0.00028},
        "deepseek-v4-flash-0731": {"prompt": 0.00014, "completion": 0.00028},
        "qwen3.7-max": {"prompt": 0.002, "completion": 0.006},
    }

    # 缓存 TTL（秒）
    _SUMMARY_TTL = 60
    _DAILY_TTL = 300
    _BUDGET_TTL = 600

    def __init__(
        self,
        data_dir: str | Path = "data/usage",
        storage: StorageBackend | None = None,
        cache: CacheBackend | None = None,
    ):
        self.data_dir = Path(data_dir)
        self._storage = storage
        self._cache = cache
        self._initialized = False

        # 内存缓存（降级/快速查询用）
        self._records: list[UsageRecord] = []
        self._daily_stats: dict[str, dict] = {}

    # ========== 初始化 ==========

    async def _ensure_initialized(self) -> None:
        """确保存储后端已初始化."""
        if self._initialized:
            return

        if self._storage is None:
            from scout.storage.factory import get_cache_backend, get_storage_backend
            self._storage = get_storage_backend()
            if self._cache is None:
                self._cache = get_cache_backend()

        await self._storage.connect()
        if self._cache:
            try:
                await self._cache.connect()
            except Exception as e:
                logger.warning(f"缓存后端连接失败，降级为无缓存: {e}")
                self._cache = None

        await self._init_schema()
        await self._load_history()
        self._initialized = True

    async def _init_schema(self) -> None:
        """初始化用量追踪表."""
        assert self._storage is not None
        from scout.storage.sqlite import SQLiteStorage

        if isinstance(self._storage, SQLiteStorage):
            script = """
            CREATE TABLE IF NOT EXISTS usage_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                model TEXT NOT NULL,
                role TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                session_id TEXT DEFAULT '',
                tool_calls INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_records(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_records(model);
            CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id);
            """
            await self._storage.execute_script(script)
        else:
            # PostgreSQL
            pg_script = """
            CREATE TABLE IF NOT EXISTS usage_records (
                id SERIAL PRIMARY KEY,
                timestamp DOUBLE PRECISION NOT NULL,
                model TEXT NOT NULL,
                role TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cost_usd DOUBLE PRECISION DEFAULT 0,
                session_id TEXT DEFAULT '',
                tool_calls INTEGER DEFAULT 0,
                latency_ms DOUBLE PRECISION DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON usage_records(timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_records(model);
            CREATE INDEX IF NOT EXISTS idx_usage_session ON usage_records(session_id);
            """
            await self._storage.execute_script(pg_script)

    async def _load_history(self) -> None:
        """从存储后端加载历史用量记录."""
        assert self._storage is not None

        try:
            rows = await self._storage.fetchall(
                "SELECT * FROM usage_records ORDER BY timestamp ASC"
            )
            self._records = []
            for row in rows:
                r = dict(row)
                self._records.append(UsageRecord(
                    timestamp=r["timestamp"],
                    model=r["model"],
                    role=r["role"],
                    prompt_tokens=r["prompt_tokens"],
                    completion_tokens=r["completion_tokens"],
                    total_tokens=r["total_tokens"],
                    cost_usd=r["cost_usd"],
                    session_id=r.get("session_id", ""),
                    tool_calls=r.get("tool_calls", 0),
                    latency_ms=r.get("latency_ms", 0),
                ))
            logger.info(f"加载 {len(self._records)} 条历史用量记录")
        except Exception as e:
            logger.warning(f"加载历史用量记录失败: {e}")
            # 降级: 尝试从 JSONL 文件加载
            self._load_jsonl_fallback()

    def _load_jsonl_fallback(self) -> None:
        """从 JSONL 文件加载（降级兼容）."""
        history_file = self.data_dir / "usage_history.jsonl"
        if not history_file.exists():
            return
        try:
            with open(history_file, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        self._records.append(UsageRecord(**data))
            logger.info(f"从 JSONL 降级加载 {len(self._records)} 条用量记录")
        except Exception:
            pass

    # ========== 缓存辅助方法 ==========

    def _cache_key(self, *parts: str) -> str:
        return "usage:" + ":".join(parts)

    async def _cache_get_json(self, key: str) -> Any | None:
        if not self._cache:
            return None
        try:
            raw = await self._cache.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    async def _cache_set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        if not self._cache:
            return
        try:
            await self._cache.set(key, json.dumps(value, ensure_ascii=False), ttl=ttl)
        except Exception:
            pass

    async def _cache_delete(self, *keys: str) -> None:
        if not self._cache:
            return
        for key in keys:
            try:
                await self._cache.delete(key)
            except Exception:
                pass

    async def _invalidate_summary_cache(self) -> None:
        """失效摘要和统计缓存."""
        await self._cache_delete(
            self._cache_key("summary", "7"),
            self._cache_key("summary", "30"),
            self._cache_key("daily", "7"),
            self._cache_key("daily", "30"),
            self._cache_key("budget"),
            self._cache_key("monthly_estimate"),
        )

    # ========== 核心方法 (Async) ==========

    async def async_record_llm_call(
        self,
        model: str,
        role: str,
        prompt_tokens: int,
        completion_tokens: int,
        session_id: str = "",
        tool_calls: int = 0,
        latency_ms: float = 0,
    ) -> UsageRecord:
        """记录 LLM API 调用."""
        await self._ensure_initialized()

        total_tokens = prompt_tokens + completion_tokens

        # 计算成本
        pricing = self.PRICING.get(model, {"prompt": 0.001, "completion": 0.002})
        cost = (
            prompt_tokens * pricing["prompt"] / 1000
            + completion_tokens * pricing["completion"] / 1000
        )

        record = UsageRecord(
            timestamp=time.time(),
            model=model,
            role=role,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            session_id=session_id,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
        )

        # 内存缓存
        self._records.append(record)

        # 持久化到存储后端
        assert self._storage is not None
        try:
            await self._storage.execute(
                "INSERT INTO usage_records "
                "(timestamp, model, role, prompt_tokens, completion_tokens, total_tokens, "
                "cost_usd, session_id, tool_calls, latency_ms) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                (
                    record.timestamp, record.model, record.role,
                    record.prompt_tokens, record.completion_tokens,
                    record.total_tokens, record.cost_usd,
                    record.session_id, record.tool_calls, record.latency_ms,
                ),
            )
        except Exception as e:
            logger.warning(f"用量记录持久化失败: {e}")
            # 降级: 写入 JSONL
            self._save_jsonl_fallback(record)

        # 更新每日统计（内存）
        self._update_daily_stats(record)

        # 失效缓存
        await self._invalidate_summary_cache()

        return record

    def _save_jsonl_fallback(self, record: UsageRecord) -> None:
        """JSONL 降级写入."""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            history_file = self.data_dir / "usage_history.jsonl"
            with open(history_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "timestamp": record.timestamp,
                    "model": record.model,
                    "role": record.role,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "total_tokens": record.total_tokens,
                    "cost_usd": record.cost_usd,
                    "session_id": record.session_id,
                    "tool_calls": record.tool_calls,
                    "latency_ms": record.latency_ms,
                }) + "\n")
        except Exception:
            pass

    def _update_daily_stats(self, record: UsageRecord) -> None:
        """更新每日统计（内存）."""
        date = datetime.fromtimestamp(record.timestamp).strftime("%Y-%m-%d")

        if date not in self._daily_stats:
            self._daily_stats[date] = {
                "api_calls": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "tool_calls": 0,
                "models": defaultdict(int),
            }

        stats = self._daily_stats[date]
        stats["api_calls"] += 1
        stats["total_tokens"] += record.total_tokens
        stats["total_cost"] += record.cost_usd
        stats["tool_calls"] += record.tool_calls
        stats["models"][record.model] += 1

    async def async_get_summary(self, days: int = 7) -> dict:
        """获取用量摘要（最近 N 天）."""
        await self._ensure_initialized()

        # 尝试缓存
        cache_key = self._cache_key("summary", str(days))
        cached = await self._cache_get_json(cache_key)
        if cached is not None:
            return cached

        cutoff = time.time() - days * 86400
        recent = [r for r in self._records if r.timestamp >= cutoff]

        if not recent:
            result = {
                "period_days": days,
                "api_calls": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "avg_tokens_per_call": 0,
                "tool_calls": 0,
            }
        else:
            total_tokens = sum(r.total_tokens for r in recent)
            total_cost = sum(r.cost_usd for r in recent)
            tool_calls = sum(r.tool_calls for r in recent)

            # 按模型分组
            by_model: dict[str, dict] = {}
            for r in recent:
                if r.model not in by_model:
                    by_model[r.model] = {"calls": 0, "tokens": 0, "cost": 0.0}
                by_model[r.model]["calls"] += 1
                by_model[r.model]["tokens"] += r.total_tokens
                by_model[r.model]["cost"] += r.cost_usd

            result = {
                "period_days": days,
                "api_calls": len(recent),
                "total_tokens": total_tokens,
                "total_cost_usd": round(total_cost, 4),
                "avg_tokens_per_call": total_tokens // len(recent) if recent else 0,
                "tool_calls": tool_calls,
                "by_model": by_model,
            }

        await self._cache_set_json(cache_key, result, ttl=self._SUMMARY_TTL)
        return result

    async def async_get_daily_breakdown(self, days: int = 7) -> list[dict]:
        """获取每日用量明细."""
        await self._ensure_initialized()

        # 尝试缓存
        cache_key = self._cache_key("daily", str(days))
        cached = await self._cache_get_json(cache_key)
        if cached is not None:
            return cached

        result = []
        for i in range(days):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            stats = self._daily_stats.get(date, {})

            result.append({
                "date": date,
                "api_calls": stats.get("api_calls", 0),
                "total_tokens": stats.get("total_tokens", 0),
                "total_cost_usd": round(stats.get("total_cost", 0.0), 4),
                "tool_calls": stats.get("tool_calls", 0),
                "models": dict(stats.get("models", {})),
            })

        await self._cache_set_json(cache_key, result, ttl=self._DAILY_TTL)
        return result

    async def async_get_model_usage(self, model: str, days: int = 30) -> dict:
        """获取特定模型的用量."""
        await self._ensure_initialized()

        cutoff = time.time() - days * 86400
        records = [r for r in self._records if r.model == model and r.timestamp >= cutoff]

        if not records:
            return {"model": model, "period_days": days, "calls": 0}

        return {
            "model": model,
            "period_days": days,
            "calls": len(records),
            "total_tokens": sum(r.total_tokens for r in records),
            "total_cost_usd": round(sum(r.cost_usd for r in records), 4),
            "avg_latency_ms": sum(r.latency_ms for r in records) / len(records),
        }

    async def async_estimate_monthly_cost(self) -> float:
        """估算月度成本（基于最近 7 天趋势）."""
        summary = await self.async_get_summary(days=7)
        daily_avg = summary["total_cost_usd"] / 7 if summary["api_calls"] > 0 else 0
        return round(daily_avg * 30, 2)

    async def async_set_budget(self, monthly_budget_usd: float) -> None:
        """设置月度预算."""
        # 持久化到文件（预算配置不需要数据库）
        self.data_dir.mkdir(parents=True, exist_ok=True)
        budget_file = self.data_dir / "budget.json"
        with open(budget_file, "w", encoding="utf-8") as f:
            json.dump({"monthly_budget_usd": monthly_budget_usd}, f)

        await self._cache_delete(self._cache_key("budget"))

    async def async_get_budget(self) -> dict:
        """获取预算信息."""
        # 尝试缓存
        cache_key = self._cache_key("budget")
        cached = await self._cache_get_json(cache_key)
        if cached is not None:
            return cached

        budget_file = self.data_dir / "budget.json"
        if not budget_file.exists():
            return {"monthly_budget_usd": None, "current_spend": 0.0, "remaining": None}

        with open(budget_file, encoding="utf-8") as f:
            data = json.load(f)

        monthly_budget = data.get("monthly_budget_usd")
        summary = await self.async_get_summary(days=30)
        current_spend = summary["total_cost_usd"]

        result = {
            "monthly_budget_usd": monthly_budget,
            "current_spend": current_spend,
            "remaining": monthly_budget - current_spend if monthly_budget else None,
            "usage_percent": (current_spend / monthly_budget * 100) if monthly_budget else None,
        }

        await self._cache_set_json(cache_key, result, ttl=self._BUDGET_TTL)
        return result

    async def async_check_budget_alert(self, threshold_percent: float = 80.0) -> bool:
        """检查是否触发预算警报."""
        budget_info = await self.async_get_budget()
        usage_percent = budget_info.get("usage_percent")
        return usage_percent is not None and usage_percent >= threshold_percent

    # ========== 同步兼容层 ==========

    def _run_async(self, coro):
        """在同步上下文中运行异步协程."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return asyncio.run(coro)

    def record_llm_call(
        self,
        model: str,
        role: str,
        prompt_tokens: int,
        completion_tokens: int,
        session_id: str = "",
        tool_calls: int = 0,
        latency_ms: float = 0,
    ) -> UsageRecord:
        """记录 LLM API 调用（同步）."""
        return self._run_async(self.async_record_llm_call(
            model, role, prompt_tokens, completion_tokens,
            session_id, tool_calls, latency_ms,
        ))

    def get_summary(self, days: int = 7) -> dict:
        """获取用量摘要（同步）."""
        return self._run_async(self.async_get_summary(days))

    def get_daily_breakdown(self, days: int = 7) -> list[dict]:
        """获取每日用量明细（同步）."""
        return self._run_async(self.async_get_daily_breakdown(days))

    def get_model_usage(self, model: str, days: int = 30) -> dict:
        """获取特定模型的用量（同步）."""
        return self._run_async(self.async_get_model_usage(model, days))

    def estimate_monthly_cost(self) -> float:
        """估算月度成本（同步）."""
        return self._run_async(self.async_estimate_monthly_cost())

    def set_budget(self, monthly_budget_usd: float) -> None:
        """设置月度预算（同步）."""
        self._run_async(self.async_set_budget(monthly_budget_usd))

    def get_budget(self) -> dict:
        """获取预算信息（同步）."""
        return self._run_async(self.async_get_budget())

    def check_budget_alert(self, threshold_percent: float = 80.0) -> bool:
        """检查是否触发预算警报（同步）."""
        return self._run_async(self.async_check_budget_alert(threshold_percent))


# 全局用量追踪器
_usage_tracker: UsageTracker | None = None


def get_usage_tracker() -> UsageTracker:
    """获取全局用量追踪器."""
    global _usage_tracker
    if _usage_tracker is None:
        _usage_tracker = UsageTracker()
    return _usage_tracker
