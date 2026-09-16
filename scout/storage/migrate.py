"""数据库迁移脚本 — SQLite → PostgreSQL.

用法:
    python -m scout.storage.migrate --from sqlite --to postgres

功能:
1. 读取 SQLite 数据
2. 创建 PostgreSQL 表结构
3. 迁移数据
4. 验证数据完整性
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scout.storage.migrate")


# PostgreSQL 表结构定义
PG_SCHEMA = """
-- 会话表
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT DEFAULT 'default',
    status TEXT DEFAULT 'idle',
    parent_id TEXT,
    lineage_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    extra JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);

-- 消息表
CREATE TABLE IF NOT EXISTS messages (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT,
    sender TEXT DEFAULT '',
    source TEXT DEFAULT '',
    reasoning TEXT,
    metadata JSONB DEFAULT '{}',
    timestamp TEXT,
    seq INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);

-- 消息归档表
CREATE TABLE IF NOT EXISTS messages_archive (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    sender TEXT DEFAULT '',
    source TEXT DEFAULT '',
    reasoning TEXT,
    metadata JSONB DEFAULT '{}',
    timestamp TEXT,
    seq INTEGER,
    archived_at TEXT,
    archive_reason TEXT DEFAULT 'edit_truncate'
);

CREATE INDEX IF NOT EXISTS idx_archive_session ON messages_archive(session_id);

-- 全文搜索（PostgreSQL 原生 tsvector）
ALTER TABLE messages ADD COLUMN IF NOT EXISTS search_vector tsvector;
CREATE INDEX IF NOT EXISTS idx_messages_search ON messages USING GIN(search_vector);

-- 自动更新搜索向量的触发器
CREATE OR REPLACE FUNCTION update_search_vector() RETURNS TRIGGER AS $$
BEGIN
    NEW.search_vector := to_tsvector('simple', COALESCE(NEW.content, ''));
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_search ON messages;
CREATE TRIGGER trg_update_search
    BEFORE INSERT OR UPDATE ON messages
    FOR EACH ROW EXECUTE FUNCTION update_search_vector();

-- 用量追踪表
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


async def migrate_sqlite_to_postgres(
    sqlite_path: str,
    pg_dsn: str,
    dry_run: bool = False,
):
    """从 SQLite 迁移到 PostgreSQL."""
    import asyncpg

    logger.info(f"源: SQLite ({sqlite_path})")
    logger.info(f"目标: PostgreSQL ({pg_dsn[:50]}...)")

    if dry_run:
        logger.info("[DRY RUN] 不执行实际迁移")

    # 1. 连接 SQLite
    sqlite_path = Path(sqlite_path).expanduser()
    if not sqlite_path.exists():
        logger.error(f"SQLite 文件不存在: {sqlite_path}")
        return

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    sqlite_conn.row_factory = sqlite3.Row

    # 2. 连接 PostgreSQL
    pg_conn = await asyncpg.connect(pg_dsn)

    try:
        # 3. 创建 PostgreSQL 表结构
        logger.info("创建 PostgreSQL 表结构...")
        if not dry_run:
            await pg_conn.execute(PG_SCHEMA)

        # 4. 迁移 sessions
        sessions = sqlite_conn.execute("SELECT * FROM sessions").fetchall()
        logger.info(f"迁移 {len(sessions)} 个会话...")

        if not dry_run:
            for s in sessions:
                extra = s["extra"] if s["extra"] else "{}"
                if isinstance(extra, str):
                    extra = json.loads(extra) if extra else {}
                await pg_conn.execute(
                    """INSERT INTO sessions (id, agent_id, status, parent_id, lineage_id, title, created_at, updated_at, extra)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                       ON CONFLICT (id) DO NOTHING""",
                    s["id"], s["agent_id"], s["status"], s["parent_id"],
                    s["lineage_id"] or "", s["title"] or "",
                    s["created_at"], s["updated_at"], json.dumps(extra),
                )

        # 5. 迁移 messages
        messages = sqlite_conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
        logger.info(f"迁移 {len(messages)} 条消息...")

        if not dry_run:
            batch_size = 100
            for i in range(0, len(messages), batch_size):
                batch = messages[i:i + batch_size]
                for m in batch:
                    metadata = m["metadata"] if m["metadata"] else "{}"
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata) if metadata else {}
                    await pg_conn.execute(
                        """INSERT INTO messages (session_id, role, content, sender, source, reasoning, metadata, timestamp, seq)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                        m["session_id"], m["role"], m["content"],
                        m["sender"] or "", m["source"] or "",
                        m["reasoning"], json.dumps(metadata),
                        m["timestamp"], m["seq"],
                    )

        # 6. 迁移 messages_archive（如果存在）
        try:
            archives = sqlite_conn.execute("SELECT * FROM messages_archive").fetchall()
            logger.info(f"迁移 {len(archives)} 条归档消息...")

            if not dry_run:
                for a in archives:
                    metadata = a["metadata"] if a["metadata"] else "{}"
                    if isinstance(metadata, str):
                        metadata = json.loads(metadata) if metadata else {}
                    await pg_conn.execute(
                        """INSERT INTO messages_archive (session_id, role, content, sender, source, reasoning, metadata, timestamp, seq, archived_at, archive_reason)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
                        a["session_id"], a["role"], a["content"],
                        a["sender"] or "", a["source"] or "",
                        a["reasoning"], json.dumps(metadata),
                        a["timestamp"], a["seq"],
                        a["archived_at"], a["archive_reason"] or "edit_truncate",
                    )
        except sqlite3.OperationalError:
            logger.info("messages_archive 表不存在，跳过")

        # 7. 验证
        if not dry_run:
            pg_session_count = await pg_conn.fetchval("SELECT COUNT(*) FROM sessions")
            pg_message_count = await pg_conn.fetchval("SELECT COUNT(*) FROM messages")
            logger.info(f"验证: PostgreSQL sessions={pg_session_count}, messages={pg_message_count}")
            logger.info(f"验证: SQLite sessions={len(sessions)}, messages={len(messages)}")

            if pg_session_count == len(sessions) and pg_message_count == len(messages):
                logger.info("✅ 迁移成功！数据完整性验证通过")
            else:
                logger.warning("⚠️ 数据数量不匹配，请检查")

        logger.info("迁移完成！")

    finally:
        sqlite_conn.close()
        await pg_conn.close()


async def migrate_usage_jsonl_to_postgres(
    jsonl_path: str,
    pg_dsn: str,
    dry_run: bool = False,
):
    """迁移用量记录从 JSONL 到 PostgreSQL."""
    import asyncpg

    jsonl_path = Path(jsonl_path)
    if not jsonl_path.exists():
        logger.info(f"用量记录文件不存在: {jsonl_path}，跳过")
        return

    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))

    logger.info(f"迁移 {len(records)} 条用量记录...")

    if dry_run:
        return

    pg_conn = await asyncpg.connect(pg_dsn)
    try:
        for r in records:
            await pg_conn.execute(
                """INSERT INTO usage_records (timestamp, model, role, prompt_tokens, completion_tokens, total_tokens, cost_usd, session_id, tool_calls, latency_ms)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                r["timestamp"], r["model"], r["role"],
                r["prompt_tokens"], r["completion_tokens"],
                r["total_tokens"], r["cost_usd"],
                r.get("session_id", ""), r.get("tool_calls", 0),
                r.get("latency_ms", 0),
            )
        logger.info(f"✅ 用量记录迁移完成: {len(records)} 条")
    finally:
        await pg_conn.close()


def main():
    parser = argparse.ArgumentParser(description="Scout Agent 数据迁移工具")
    parser.add_argument("--from", dest="source", default="sqlite", choices=["sqlite"])
    parser.add_argument("--to", dest="target", default="postgres", choices=["postgres"])
    parser.add_argument("--sqlite-path", default=None, help="SQLite 数据库路径（默认 <盘符>:\\.scout\\sessions.db）")
    parser.add_argument("--usage-path", default="data/usage/usage_history.jsonl")
    parser.add_argument("--pg-dsn", default="postgresql://scout:scout@localhost:5432/scout")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-usage", action="store_true", help="跳过用量数据迁移")

    args = parser.parse_args()

    sqlite_path = args.sqlite_path or str(_SCOUT_DATA_DIR / "sessions.db")

    asyncio.run(migrate_sqlite_to_postgres(
        sqlite_path=sqlite_path,
        pg_dsn=args.pg_dsn,
        dry_run=args.dry_run,
    ))

    if not args.skip_usage:
        asyncio.run(migrate_usage_jsonl_to_postgres(
            jsonl_path=args.usage_path,
            pg_dsn=args.pg_dsn,
            dry_run=args.dry_run,
        ))


if __name__ == "__main__":
    main()
