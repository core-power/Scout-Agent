"""统一 Schema 版本管理与增量迁移框架.

设计目标（升级能力）：
- 每个 SQLite 数据库文件用 ``PRAGMA user_version`` 记录自身 schema 版本；
- 所有建表/加列/改表操作集中在 ``MIGRATIONS`` 注册表中按版本号组织；
- 新库建表（DDL 已是最新结构）后调用 :func:`ensure_schema` 将版本置为
  ``SCHEMA_VERSION``；旧库则按序执行缺失版本的增量迁移（幂等）。

未来升级流程（给开发者）：
1. 修改对应模块的建表 DDL（新库直接获得新结构）；
2. 在 ``MIGRATIONS`` 注册新版本迁移函数（旧库增量补丁，函数内用
   :func:`column_exists` / :func:`table_exists` 保证幂等）；
3. 递增 ``SCHEMA_VERSION``；
4. 启动时各模块 ``_init_db`` 会自动调用 :func:`ensure_schema` 完成升级，
   无需人工干预。

约定：
- 迁移函数签名: ``def migrate(conn: sqlite3.Connection) -> None``；
- 迁移函数必须幂等（重复执行不报错）；
- 同一版本号可包含多个数据库文件的补丁，各自用表名判断是否生效。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

logger = logging.getLogger(__name__)

# 当前所有数据库文件的 schema 版本（每有结构变更 +1）
SCHEMA_VERSION = 1

# 版本号 -> 迁移函数（幂等）
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


def register_migration(version: int) -> Callable:
    """装饰器：注册 ``version`` 版本的增量迁移函数."""

    def decorator(fn: Callable[[sqlite3.Connection], None]) -> Callable:
        MIGRATIONS[version] = fn
        return fn

    return decorator


def get_schema_version(conn: sqlite3.Connection) -> int:
    """读取当前 schema 版本（``PRAGMA user_version``）."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """写入 schema 版本并提交."""
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """判断表是否存在."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """判断表中是否存在某列."""
    if not table_exists(conn, table):
        return False
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return column in cols


def run_migrations(conn: sqlite3.Connection, target: int | None = None) -> int:
    """把数据库从当前版本增量迁移到 ``target``（默认最新），返回执行的迁移数.

    幂等：重复调用安全；新库版本已达标时直接返回 0。
    """
    target = target or SCHEMA_VERSION
    current = get_schema_version(conn)
    if current >= target:
        return 0

    applied = 0
    for version in range(current + 1, target + 1):
        fn = MIGRATIONS.get(version)
        if fn is None:
            # 未注册的迁移版本：不标记 user_version、不跳过，
            # 升级停在此版本——未来补录该版本迁移后会自动续跑。
            logger.warning(
                "schema 迁移 v%d 未注册，升级停止于此版本（等待补录后自动续跑）", version
            )
            break
        try:
            fn(conn)
            logger.info("schema 迁移 v%d 完成", version)
        except sqlite3.Error:
            logger.exception("schema 迁移 v%d 失败", version)
            raise
        set_schema_version(conn, version)
        applied += 1
    return applied


def ensure_schema(conn: sqlite3.Connection) -> int:
    """建表后调用：把 schema 升级到 ``SCHEMA_VERSION``，返回执行的迁移数."""
    return run_migrations(conn)


# ────────────────────────────────────────────────────────────────────────
# v1 迁移（2026-08-30 建立版本化机制时收编的历史补丁）
# ────────────────────────────────────────────────────────────────────────
@register_migration(1)
def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v1：补齐历史遗留缺失列（旧库增量升级，幂等）."""
    # traces.error（observability.db 旧库补列）
    if not column_exists(conn, "traces", "error"):
        try:
            conn.execute("ALTER TABLE traces ADD COLUMN error TEXT")
        except sqlite3.OperationalError:
            pass  # 表不存在等情况，忽略
    # memories.embedding（memory.db 旧库补列）
    if not column_exists(conn, "memories", "embedding"):
        try:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN embedding BLOB DEFAULT NULL"
            )
        except sqlite3.OperationalError:
            pass
