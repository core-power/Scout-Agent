"""PostgreSQL 存储后端实现.

使用 asyncpg 实现异步 PostgreSQL 连接池。
支持主从复制（读写分离）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

from scout.storage.base import StorageBackend

logger = logging.getLogger("scout.storage.postgres")


class PostgresStorage(StorageBackend):
    """PostgreSQL 存储后端 — 支持主从复制."""

    def __init__(
        self,
        dsn: str,
        read_dsn: str | None = None,
        min_size: int = 5,
        max_size: int = 20,
        statement_cache_size: int = 0,  # 禁用 prepared statement 缓存（兼容 pgbouncer）
    ):
        if not HAS_ASYNCPG:
            raise ImportError("asyncpg 未安装，请运行: pip install asyncpg")

        self._dsn = dsn
        self._read_dsn = read_dsn or dsn
        self._min_size = min_size
        self._max_size = max_size
        self._statement_cache_size = statement_cache_size

        self._write_pool: asyncpg.Pool | None = None
        self._read_pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """创建读写连接池."""
        pool_kwargs = dict(
            min_size=self._min_size,
            max_size=self._max_size,
            statement_cache_size=self._statement_cache_size,
        )

        self._write_pool = await asyncpg.create_pool(self._dsn, **pool_kwargs)
        logger.info(f"写连接池已创建: min={self._min_size}, max={self._max_size}")

        if self._read_dsn != self._dsn:
            self._read_pool = await asyncpg.create_pool(self._read_dsn, **pool_kwargs)
            logger.info(f"读连接池已创建: min={self._min_size}, max={self._max_size}")
        else:
            self._read_pool = self._write_pool
            logger.info("读写共用同一连接池")

    async def disconnect(self) -> None:
        """关闭连接池."""
        if self._write_pool:
            await self._write_pool.close()
            logger.info("写连接池已关闭")

        if self._read_pool and self._read_pool != self._write_pool:
            await self._read_pool.close()
            logger.info("读连接池已关闭")

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        """执行写操作."""
        assert self._write_pool, "连接池未初始化，请先调用 connect()"
        async with self._write_pool.acquire() as conn:
            if params:
                await conn.execute(sql, *params)
            else:
                await conn.execute(sql)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """批量执行写操作."""
        assert self._write_pool, "连接池未初始化，请先调用 connect()"
        async with self._write_pool.acquire() as conn:
            await conn.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        """查询单行（使用读连接池）."""
        assert self._read_pool, "连接池未初始化，请先调用 connect()"
        async with self._read_pool.acquire() as conn:
            if params:
                row = await conn.fetchrow(sql, *params)
            else:
                row = await conn.fetchrow(sql)
            return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        """查询多行（使用读连接池）."""
        assert self._read_pool, "连接池未初始化，请先调用 connect()"
        async with self._read_pool.acquire() as conn:
            if params:
                rows = await conn.fetch(sql, *params)
            else:
                rows = await conn.fetch(sql)
            return [dict(r) for r in rows]

    async def execute_script(self, script: str) -> None:
        """执行多条 SQL 语句."""
        assert self._write_pool, "连接池未初始化，请先调用 connect()"
        async with self._write_pool.acquire() as conn:
            await conn.execute(script)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["PostgresStorage"]:
        """事务上下文管理器."""
        assert self._write_pool, "连接池未初始化，请先调用 connect()"
        async with self._write_pool.acquire() as conn:
            async with conn.transaction():
                # 创建一个临时的存储实例用于事务内操作
                tx_storage = _TransactionStorage(conn)
                yield tx_storage

    async def health_check(self) -> bool:
        """健康检查 — 分别检查读写连接."""
        try:
            # 检查写连接
            async with self._write_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")

            # 检查读连接
            if self._read_pool and self._read_pool != self._write_pool:
                async with self._read_pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")

            return True
        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            return False


class _TransactionStorage(StorageBackend):
    """事务内的存储操作封装."""

    def __init__(self, conn: "asyncpg.Connection"):
        self._conn = conn

    async def connect(self) -> None:
        pass  # 事务内不需要

    async def disconnect(self) -> None:
        pass  # 事务内不需要

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        if params:
            await self._conn.execute(sql, *params)
        else:
            await self._conn.execute(sql)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        await self._conn.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        if params:
            row = await self._conn.fetchrow(sql, *params)
        else:
            row = await self._conn.fetchrow(sql)
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        if params:
            rows = await self._conn.fetch(sql, *params)
        else:
            rows = await self._conn.fetch(sql)
        return [dict(r) for r in rows]

    async def execute_script(self, script: str) -> None:
        await self._conn.execute(script)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["StorageBackend"]:
        raise RuntimeError("不支持嵌套事务")
        yield self  # 满足类型检查
