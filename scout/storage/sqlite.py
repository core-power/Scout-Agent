"""SQLite 存储后端实现（向后兼容）.

保留 SQLite 支持用于开发和测试环境。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from scout.storage.base import StorageBackend

logger = logging.getLogger("scout.storage.sqlite")

# ★ 2026-08-29 修复：store.py 等上层模块使用了 Postgres 风格占位符 $1/$2，
# 而 sqlite3 只支持 ?（或 :name/@name/$name 命名参数，不含 $数字）。
# 此前所有 $N 查询/写入都会抛 "Binding N ('$N') is a named parameter..." 异常，
# 导致会话从未真正落库 → 每次启动历史全丢、前端反复弹"该对话不存在"。
# 这里统一把 $N 归一化为 ?（按出现顺序一一对应，语义完全等价）。
_PLACEHOLDER_RE = re.compile(r"\$\d+")


def _normalize_sql(sql: str) -> str:
    """将 Postgres 风格占位符 $1/$2 归一化为 sqlite3 的 ?."""
    if _PLACEHOLDER_RE.search(sql):
        return _PLACEHOLDER_RE.sub("?", sql)
    return sql


class _ReentrantLock:
    """可重入异步锁：同一 Task 可多次进入（计数），不同 Task 互斥.

    为什么不用 asyncio.Lock：它**不可重入**。SQLiteStorage 是底层存储原语，
    无法约束所有调用方 —— 若某调用方在持锁路径里再次调用本存储的加锁方法
    （典型：在 ``transaction()`` 内误用外层 ``db.execute`` 而非事务句柄 ``tx``），
    就会自死锁（实测曾因此让 SessionStore.save_session 永久挂起）。可重入锁
    让同任务重入直接放行，从根上消除这类死锁；跨任务仍严格互斥，保证单连接
    不被并发交错使用。

    注意：重入只防「挂起」，不保证事务原子性 —— 事务内仍应使用 ``tx`` 句柄
    （tx.* 不逐条 commit），用外层 db.* 会因每条 commit 而破坏原子性。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._count = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if self._owner is task and self._count > 0:
            self._count += 1
            return
        await self._lock.acquire()
        self._owner = task
        self._count = 1

    def release(self) -> None:
        if self._count <= 0:
            return
        self._count -= 1
        if self._count == 0:
            self._owner = None
            self._lock.release()

    async def __aenter__(self) -> "_ReentrantLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()


class SQLiteStorage(StorageBackend):
    """SQLite 存储后端 — 用于开发和测试.

    并发模型（2026-09-24 修复）：单个 sqlite3 连接以 ``check_same_thread=False``
    打开，此前所有 async 方法**直接在事件循环上跑同步 sqlite 调用**——既阻塞循环
    （磁盘 IO 期间整个 agent 卡住），又因无锁而在并发协程下交错使用同一连接、
    可能损坏游标/事务状态。现统一：① 阻塞调用经 ``asyncio.to_thread`` 卸到线程池；
    ② 用 ``asyncio.Lock`` 串行化对单连接的访问；③ 事务全程持锁，保证 BEGIN→…→COMMIT
    之间不被其他操作插入。
    """

    def __init__(self, db_path: str | Path = "data/scout.db"):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        # 串行化单连接访问；可重入（同任务），防「持锁路径再调本实例方法」自死锁
        self._lock = _ReentrantLock()

    async def _run(self, fn, *args):
        """在锁保护下把阻塞的 sqlite 调用丢到线程池执行."""
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # ── 同步内部实现（在线程池中运行，不直接暴露）──

    def _connect_sync(self) -> None:
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def _execute_sync(self, sql: str, params: tuple | None) -> None:
        if params:
            self._conn.execute(sql, params)
        else:
            self._conn.execute(sql)
        self._conn.commit()

    def _executemany_sync(self, sql: str, params_list: list[tuple]) -> None:
        self._conn.executemany(sql, params_list)
        self._conn.commit()

    def _fetchone_sync(self, sql: str, params: tuple | None):
        cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        row = cur.fetchone()
        return dict(row) if row else None

    def _fetchall_sync(self, sql: str, params: tuple | None):
        cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        return [dict(r) for r in cur.fetchall()]

    def _execute_script_sync(self, script: str) -> None:
        self._conn.executescript(script)
        self._conn.commit()

    async def connect(self) -> None:
        """建立连接."""
        await asyncio.to_thread(self._connect_sync)
        logger.info(f"SQLite 连接已建立: {self._db_path}")

    async def disconnect(self) -> None:
        """关闭连接."""
        if self._conn:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)
            logger.info("SQLite 连接已关闭")

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        """执行写操作."""
        assert self._conn, "SQLite 未连接"
        await self._run(self._execute_sync, _normalize_sql(sql), params)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """批量执行写操作."""
        assert self._conn, "SQLite 未连接"
        await self._run(self._executemany_sync, _normalize_sql(sql), params_list)

    async def fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        """查询单行."""
        assert self._conn, "SQLite 未连接"
        return await self._run(self._fetchone_sync, _normalize_sql(sql), params)

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        """查询多行."""
        assert self._conn, "SQLite 未连接"
        return await self._run(self._fetchall_sync, _normalize_sql(sql), params)

    async def execute_script(self, script: str) -> None:
        """执行多条 SQL 语句."""
        assert self._conn, "SQLite 未连接"
        await self._run(self._execute_script_sync, script)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["SQLiteStorage"]:
        """事务上下文管理器.

        返回一个不自动提交的事务内操作封装，保证「多条写语句要么全部成功、要么全部回滚」，
        避免全量重写（如 DELETE + INSERT）在进程中断时被部分提交导致数据丢失。

        ★ 全程持有 self._lock：BEGIN→（多条操作）→COMMIT/ROLLBACK 之间不允许其他
        协程插入对同一连接的操作；事务内各操作经 to_thread 执行但**不再重复取锁**
        （锁不可重入），由本上下文统一持有。
        """
        assert self._conn, "SQLite 未连接"
        async with self._lock:
            await asyncio.to_thread(self._conn.execute, "BEGIN TRANSACTION")
            try:
                yield _TransactionStorage(self._conn)
                await asyncio.to_thread(self._conn.commit)
            except Exception:
                await asyncio.to_thread(self._conn.rollback)
                raise


class _TransactionStorage(StorageBackend):
    """事务内操作封装 — 不自动提交，供 SQLiteStorage.transaction() 使用.

    调用方（SQLiteStorage.transaction）已持有连接锁，故本类各方法只把阻塞调用
    卸到线程池、**不再取锁**（asyncio.Lock 不可重入，重复取会死锁）。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    def _execute_sync(self, sql: str, params: tuple | None) -> None:
        if params:
            self._conn.execute(sql, params)
        else:
            self._conn.execute(sql)

    def _fetchone_sync(self, sql: str, params: tuple | None):
        cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        row = cur.fetchone()
        return dict(row) if row else None

    def _fetchall_sync(self, sql: str, params: tuple | None):
        cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        return [dict(r) for r in cur.fetchall()]

    async def execute(self, sql: str, params: tuple | None = None) -> None:
        await asyncio.to_thread(self._execute_sync, _normalize_sql(sql), params)

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        await asyncio.to_thread(self._conn.executemany, _normalize_sql(sql), params_list)

    async def fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        return await asyncio.to_thread(self._fetchone_sync, _normalize_sql(sql), params)

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        return await asyncio.to_thread(self._fetchall_sync, _normalize_sql(sql), params)

    async def execute_script(self, script: str) -> None:
        await asyncio.to_thread(self._conn.executescript, script)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[StorageBackend]:
        raise RuntimeError("不支持嵌套事务")
        yield self  # 满足类型检查
