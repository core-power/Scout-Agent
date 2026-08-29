"""SQLite 存储后端实现（向后兼容）.

保留 SQLite 支持用于开发和测试环境。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from scout.storage.base import StorageBackend

logger = logging.getLogger("scout.storage.sqlite")


class SQLiteStorage(StorageBackend):
    """SQLite 存储后端 — 用于开发和测试."""

    def __init__(self, db_path: str | Path = "data/scout.db"):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        """建立连接."""
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        # WAL 模式
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        logger.info(f"SQLite 连接已建立: {self._db_path}")

    async def disconnect(self) -> None:
        """关闭连接."""
        if self._conn:
            self._conn.close()
            logger.info("SQLite 连接已关闭")

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        """执行写操作."""
        assert self._conn, "SQLite 未连接"
        if params:
            self._conn.execute(sql, params)
        else:
            self._conn.execute(sql)
        self._conn.commit()

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """批量执行写操作."""
        assert self._conn, "SQLite 未连接"
        self._conn.executemany(sql, params_list)
        self._conn.commit()

    async def fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        """查询单行."""
        assert self._conn, "SQLite 未连接"
        if params:
            row = self._conn.execute(sql, params).fetchone()
        else:
            row = self._conn.execute(sql).fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        """查询多行."""
        assert self._conn, "SQLite 未连接"
        if params:
            rows = self._conn.execute(sql, params).fetchall()
        else:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    async def execute_script(self, script: str) -> None:
        """执行多条 SQL 语句."""
        assert self._conn, "SQLite 未连接"
        self._conn.executescript(script)
        self._conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["SQLiteStorage"]:
        """事务上下文管理器.

        返回一个不自动提交的事务内操作封装，保证「多条写语句要么全部成功、要么全部回滚」，
        避免全量重写（如 DELETE + INSERT）在进程中断时被部分提交导致数据丢失。
        """
        assert self._conn, "SQLite 未连接"
        try:
            self._conn.execute("BEGIN TRANSACTION")
            yield _TransactionStorage(self._conn)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise


class _TransactionStorage(StorageBackend):
    """事务内操作封装 — 不自动提交，供 SQLiteStorage.transaction() 使用."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        if params:
            self._conn.execute(sql, params)
        else:
            self._conn.execute(sql)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        self._conn.executemany(sql, params_list)

    async def fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        if params:
            row = self._conn.execute(sql, params).fetchone()
        else:
            row = self._conn.execute(sql).fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        if params:
            rows = self._conn.execute(sql, params).fetchall()
        else:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    async def execute_script(self, script: str) -> None:
        self._conn.executescript(script)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[StorageBackend]:
        raise RuntimeError("不支持嵌套事务")
        yield self  # 满足类型检查
