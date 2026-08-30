"""升级能力：统一 schema 版本管理 + 增量迁移框架测试.

覆盖:
- 新库 ensure_schema 后 user_version 达到当前版本;
- 旧库（无版本标记）自动补列（traces.error / memories.embedding）;
- 幂等性: 重复执行不报错、不重复应用;
- 版本回退读取: user_version 高于代码版本时保持原值。
"""

from __future__ import annotations

import sqlite3

import pytest

from scout.storage.schema import (
    SCHEMA_VERSION,
    MIGRATIONS,
    column_exists,
    ensure_schema,
    get_schema_version,
    register_migration,
    run_migrations,
    table_exists,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def test_new_db_ensure_schema_sets_version(conn):
    """新库建表后 ensure_schema 应把版本置为当前版本."""
    conn.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY);")
    assert ensure_schema(conn) == SCHEMA_VERSION
    assert get_schema_version(conn) == SCHEMA_VERSION


def test_idempotent_repeat_run(conn):
    """重复调用 ensure_schema 应返回 0（已是最新版本）."""
    conn.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY);")
    ensure_schema(conn)
    assert ensure_schema(conn) == 0
    assert get_schema_version(conn) == SCHEMA_VERSION


def test_legacy_db_migrates_columns(conn):
    """旧库（版本 0）应自动补 traces.error / memories.embedding 列."""
    conn.executescript(
        """
        CREATE TABLE traces (id INTEGER PRIMARY KEY, start_time TEXT);
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT);
        """
    )
    assert ensure_schema(conn) == SCHEMA_VERSION
    assert column_exists(conn, "traces", "error")
    assert column_exists(conn, "memories", "embedding")


def test_migration_registry_populated():
    """版本化机制建立时至少应有一个历史迁移."""
    assert 1 in MIGRATIONS
    assert max(MIGRATIONS) == SCHEMA_VERSION


def test_register_migration_applies_once(conn):
    """新注册的迁移按序执行一次，且幂等."""
    calls = []
    target = SCHEMA_VERSION + 1

    @register_migration(target)
    def _fake_migrate(c: sqlite3.Connection) -> None:  # noqa: ARG001
        calls.append(1)

    try:
        # 从 v0 升到 target：历史迁移 + 新迁移各执行一次
        assert run_migrations(conn, target=target) == target
        assert len(calls) == 1
        # 版本已达 target，再次运行不再执行
        assert run_migrations(conn, target=target) == 0
        assert len(calls) == 1
    finally:
        MIGRATIONS.pop(target, None)


def test_unregistered_version_stops_progress(conn):
    """未注册的版本应停止推进（不标记），未来补录后自动续跑."""
    conn.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY);")
    # 只注册到 v1，目标 v3 中存在 v2 缺口
    assert run_migrations(conn, target=3) == 1
    # 停在 v1，不标记 v2/v3
    assert get_schema_version(conn) == 1
    # 补录 v2 后再次运行，自动续跑 v2→v3（v3 仍缺则停在 v3 之前）
    MIGRATIONS[2] = lambda c: None  # noqa: E731
    try:
        assert run_migrations(conn, target=3) == 1
        assert get_schema_version(conn) == 2
    finally:
        MIGRATIONS.pop(2, None)


def test_higher_user_version_not_downgraded(conn):
    """user_version 高于代码版本时不应回退."""
    conn.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY);")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    conn.commit()
    # 已高于目标版本：不执行迁移、不降低版本
    assert run_migrations(conn) == 0
    assert get_schema_version(conn) == SCHEMA_VERSION + 5


def test_migrate_target_older_than_current_noop(conn):
    conn.executescript("CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY);")
    ensure_schema(conn)
    assert run_migrations(conn, target=SCHEMA_VERSION - 1) == 0
    assert get_schema_version(conn) == SCHEMA_VERSION
