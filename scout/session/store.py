"""会话存储 — 支持 SQLite / PostgreSQL + Redis 缓存.

重构: 使用 StorageBackend 抽象层 + CacheBackend 缓存层。
保留原有 API 兼容性（同步），新增 async API。

架构:
- 持久化: StorageBackend (SQLite / PostgreSQL)
- 缓存: CacheBackend (Redis) — 热会话缓存
- 搜索: PostgreSQL tsvector / SQLite FTS5
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from scout.storage.base import CacheBackend, StorageBackend

logger = logging.getLogger("scout.session")

# ========== 常量 ==========

MAX_MESSAGES_IN_CONTEXT = 200  # 上下文窗口最大消息数
ARCHIVE_ON_EDIT = True  # 编辑时是否归档被截断的消息

# ========== SQL 模板（参数化占位符，运行时替换） ==========

_SQL_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT DEFAULT 'default',
    status TEXT DEFAULT 'idle',
    parent_id TEXT,
    lineage_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    extra TEXT DEFAULT '{}'
)
"""

_SQL_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    sender TEXT DEFAULT '',
    source TEXT DEFAULT '',
    reasoning TEXT,
    metadata TEXT DEFAULT '{}',
    timestamp TEXT,
    seq INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
)
"""

_SQL_CREATE_ARCHIVE = """
CREATE TABLE IF NOT EXISTS messages_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    sender TEXT DEFAULT '',
    source TEXT DEFAULT '',
    reasoning TEXT,
    metadata TEXT DEFAULT '{}',
    timestamp TEXT,
    seq INTEGER,
    archived_at TEXT,
    archive_reason TEXT DEFAULT 'edit_truncate'
)
"""


def _p(n: int) -> str:
    """生成参数化占位符: _p(1) → '$1' (PG) 或 '?' (SQLite)."""
    return f"${n}"


class SessionStore:
    """会话存储 — 支持多后端 + Redis 缓存.

    持久化层通过 StorageBackend 抽象，支持 SQLite / PostgreSQL。
    缓存层通过 CacheBackend 抽象，支持 Redis。

    缓存策略:
    - 会话元数据: TTL 30 分钟
    - 消息列表: TTL 10 分钟
    - 会话列表: TTL 5 分钟
    - 写入时失效 (write-through invalidation)
    """

    def __init__(
        self,
        db_path: str | None = None,
        storage: StorageBackend | None = None,
        cache: CacheBackend | None = None,
    ):
        self._storage = storage
        self._cache = cache
        # 串行化对共享后端连接的写入（多线程 + 多事件循环下均安全），
        # 避免全量重写（DELETE+INSERT）并发交错导致消息丢失
        self._lock = threading.Lock()
        self._is_sqlite = False
        self._db_path = db_path

        # 缓存 TTL（秒）
        self._session_ttl = 1800  # 30 分钟
        self._messages_ttl = 600  # 10 分钟
        self._list_ttl = 300  # 5 分钟

    # ========== 初始化 ==========

    async def _ensure_storage(self) -> StorageBackend:
        """确保存储后端已初始化."""
        if self._storage is not None:
            return self._storage

        # 延迟导入避免循环依赖
        from scout.storage.factory import get_cache_backend, get_storage_backend

        if self._db_path:
            # 显式指定 db_path → 独立 SQLite 实例（测试/临时库）
            self._storage = get_storage_backend(backend="sqlite", db_path=str(self._db_path))
        else:
            self._storage = get_storage_backend()
        if self._cache is None:
            self._cache = get_cache_backend()

        # 检测是否为 SQLite
        from scout.storage.sqlite import SQLiteStorage
        self._is_sqlite = isinstance(self._storage, SQLiteStorage)

        await self._storage.connect()
        if self._cache:
            try:
                await self._cache.connect()
            except Exception as e:
                logger.warning(f"缓存后端连接失败，降级为无缓存: {e}")
                self._cache = None

        await self._init_schema()
        return self._storage

    async def _init_schema(self) -> None:
        """初始化数据库表结构."""
        assert self._storage is not None

        if self._is_sqlite:
            # SQLite: 使用 ? 占位符, executescript
            script = _SQL_CREATE_SESSIONS + ";" + _SQL_CREATE_MESSAGES + ";" + _SQL_CREATE_ARCHIVE
            # FTS5
            script += """;
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content, session_id, content='messages', content_rowid='id'
            )"""
            script += """;
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content, session_id)
                VALUES (new.id, new.content, new.session_id);
            END"""
            await self._storage.execute_script(script)
        else:
            # PostgreSQL: 使用 $N 占位符
            pg_script = """
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
            """
            await self._storage.execute_script(pg_script)

    # ========== 缓存辅助方法 ==========

    def _cache_key(self, *parts: str) -> str:
        """生成缓存键."""
        return ":".join(parts)

    async def _cache_get_json(self, key: str) -> Any | None:
        """从缓存获取 JSON 数据."""
        if not self._cache:
            return None
        try:
            raw = await self._cache.get(key)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"缓存读取失败: {key}: {e}")
        return None

    async def _cache_set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        """将 JSON 数据写入缓存."""
        if not self._cache:
            return
        try:
            await self._cache.set(key, json.dumps(value, ensure_ascii=False), ttl=ttl)
        except Exception as e:
            logger.debug(f"缓存写入失败: {key}: {e}")

    async def _cache_delete(self, *keys: str) -> None:
        """删除缓存键."""
        if not self._cache:
            return
        for key in keys:
            try:
                await self._cache.delete(key)
            except Exception:
                pass

    async def _invalidate_session_cache(self, session_id: str) -> None:
        """失效会话相关缓存."""
        await self._cache_delete(
            self._cache_key("session", session_id),
            self._cache_key("messages", session_id),
            self._cache_key("messages_full", session_id),
            self._cache_key("session_list", "all"),
        )

    # ========== 会话 CRUD (Async) ==========

    async def async_create(
        self,
        session_id: str,
        agent_id: str = "default",
        parent_id: str | None = None,
        lineage_id: str = "",
        title: str = "",
        extra: dict | None = None,
    ) -> None:
        """创建新会话."""
        db = await self._ensure_storage()
        now = datetime.now().isoformat(timespec="seconds")
        extra_json = json.dumps(extra or {}, ensure_ascii=False)

        await db.execute(
            "INSERT INTO sessions (id, agent_id, status, parent_id, lineage_id, title, created_at, updated_at, extra) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            (session_id, agent_id, "idle", parent_id or "", lineage_id, title, now, now, extra_json),
        )

        # 缓存会话元数据
        await self._cache_set_json(
            self._cache_key("session", session_id),
            {
                "id": session_id, "agent_id": agent_id, "status": "idle",
                "parent_id": parent_id or "", "lineage_id": lineage_id,
                "title": title, "created_at": now, "updated_at": now,
                "extra": extra or {},
            },
            ttl=self._session_ttl,
        )
        # 失效列表缓存
        await self._cache_delete(self._cache_key("session_list", "all"))

    async def async_fork(
        self,
        session_id: str,
        new_session_id: str,
        up_to_seq: int | None = None,
        title: str | None = None,
        extra: dict | None = None,
    ) -> dict | None:
        """从现有会话 fork 出一个新分支会话.

        Args:
            session_id: 源会话 ID
            new_session_id: 新分支会话 ID
            up_to_seq: 复制到源会话的第 up_to_seq 条消息为止
                       （None = 复制全部消息），用于"从某处回退再分叉"
            title: 新会话标题（默认基于源会话标题 + " (fork)"）
            extra: 新会话 extra 字段

        Returns:
            新会话元数据；源会话不存在时返回 None

        实现：
        - 新会话 parent_id = 源会话 id
        - lineage_id = 源会话的 lineage_id（若空则为源会话 id），记录分叉链
        - 复制源会话消息（role/content/sender/source/reasoning/metadata 全量保留）
        """
        db = await self._ensure_storage()
        parent = await self.async_get(session_id)
        if not parent:
            return None

        new_title = title or (f"{parent.get('title') or '会话'} (fork)")
        lineage = parent.get("lineage_id") or session_id
        await self.async_create(
            new_session_id,
            agent_id=parent.get("agent_id", "default"),
            parent_id=session_id,
            lineage_id=lineage,
            title=new_title,
            extra=extra or {"forked_from": session_id, "fork_of": parent.get("title") or ""},
        )

        # 复制源会话消息
        rows = await db.fetchall(
            "SELECT role, content, sender, source, reasoning, metadata, timestamp, seq "
            "FROM messages WHERE session_id = $1 ORDER BY seq ASC",
            (session_id,),
        )
        copied = 0
        now = datetime.now().isoformat(timespec="seconds")
        for row in rows:
            r = dict(row)
            seq = r["seq"]
            if up_to_seq is not None and seq > up_to_seq:
                continue
            await db.execute(
                "INSERT INTO messages (session_id, role, content, sender, source, reasoning, metadata, timestamp, seq) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                (
                    new_session_id, r["role"], r["content"], r.get("sender") or "",
                    r.get("source") or "", r.get("reasoning") or "",
                    r.get("metadata") if isinstance(r.get("metadata"), str) else json.dumps(r.get("metadata") or {}, ensure_ascii=False),
                    r.get("timestamp") or now, seq,
                ),
            )
            copied += 1

        # 失效缓存
        await self._cache_delete(
            self._cache_key("messages", new_session_id),
            self._cache_key("messages_full", new_session_id),
            self._cache_key("session", new_session_id),
            self._cache_key("session_list", "all"),
        )

        return await self.async_get(new_session_id)

    async def async_get(self, session_id: str) -> dict | None:
        """获取会话元数据."""
        # 先查缓存
        cached = await self._cache_get_json(self._cache_key("session", session_id))
        if cached is not None:
            return cached

        db = await self._ensure_storage()
        row = await db.fetchone(
            "SELECT * FROM sessions WHERE id = $1", (session_id,)
        )
        if not row:
            return None

        # 解析 extra 字段
        result = dict(row)
        if isinstance(result.get("extra"), str):
            try:
                result["extra"] = json.loads(result["extra"])
            except (json.JSONDecodeError, TypeError):
                result["extra"] = {}

        # 写入缓存
        await self._cache_set_json(
            self._cache_key("session", session_id), result, ttl=self._session_ttl
        )
        return result

    async def async_update_status(self, session_id: str, status: str) -> None:
        """更新会话状态."""
        db = await self._ensure_storage()
        now = datetime.now().isoformat(timespec="seconds")
        await db.execute(
            "UPDATE sessions SET status = $1, updated_at = $2 WHERE id = $3",
            (status, now, session_id),
        )
        await self._cache_delete(self._cache_key("session", session_id))

    async def async_update_title(self, session_id: str, title: str) -> None:
        """更新会话标题."""
        db = await self._ensure_storage()
        now = datetime.now().isoformat(timespec="seconds")
        await db.execute(
            "UPDATE sessions SET title = $1, updated_at = $2 WHERE id = $3",
            (title, now, session_id),
        )
        await self._cache_delete(self._cache_key("session", session_id))

    async def async_update_extra(self, session_id: str, extra: dict) -> None:
        """更新会话 extra 字段."""
        db = await self._ensure_storage()
        now = datetime.now().isoformat(timespec="seconds")
        extra_json = json.dumps(extra, ensure_ascii=False)
        await db.execute(
            "UPDATE sessions SET extra = $1, updated_at = $2 WHERE id = $3",
            (extra_json, now, session_id),
        )
        await self._cache_delete(self._cache_key("session", session_id))

    async def async_delete(self, session_id: str) -> bool:
        """删除会话及其所有消息."""
        db = await self._ensure_storage()
        session = await self.async_get(session_id)
        if not session:
            return False

        await db.execute("DELETE FROM messages WHERE session_id = $1", (session_id,))
        await db.execute("DELETE FROM sessions WHERE id = $1", (session_id,))

        await self._invalidate_session_cache(session_id)
        return True

    async def async_list(
        self, agent_id: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """列出会话."""
        # 尝试缓存（仅首页无过滤时）
        cache_key = self._cache_key("session_list", "all")
        if agent_id is None and offset == 0 and limit <= 50:
            cached = await self._cache_get_json(cache_key)
            if cached is not None:
                return cached[:limit]

        db = await self._ensure_storage()
        if agent_id:
            rows = await db.fetchall(
                "SELECT * FROM sessions WHERE agent_id = $1 "
                "ORDER BY updated_at DESC LIMIT $2 OFFSET $3",
                (agent_id, limit, offset),
            )
        else:
            rows = await db.fetchall(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT $1 OFFSET $2",
                (limit, offset),
            )

        results = []
        for row in rows:
            r = dict(row)
            if isinstance(r.get("extra"), str):
                try:
                    r["extra"] = json.loads(r["extra"])
                except (json.JSONDecodeError, TypeError):
                    r["extra"] = {}
            results.append(r)

        # 缓存列表（仅首页无过滤时）
        if agent_id is None and offset == 0:
            await self._cache_set_json(cache_key, results, ttl=self._list_ttl)

        return results

    # ========== 消息操作 (Async) ==========

    async def async_append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        sender: str = "",
        source: str = "",
        reasoning: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """追加一条消息到会话."""
        with self._lock:
            db = await self._ensure_storage()
            now = datetime.now().isoformat(timespec="seconds")
            meta_json = json.dumps(metadata or {}, ensure_ascii=False)

            # 获取当前最大 seq
            row = await db.fetchone(
                "SELECT MAX(seq) as max_seq FROM messages WHERE session_id = $1",
                (session_id,),
            )
            next_seq = (row["max_seq"] or 0) + 1 if row else 1

            await db.execute(
                "INSERT INTO messages (session_id, role, content, sender, source, reasoning, metadata, timestamp, seq) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                (session_id, role, content, sender, source, reasoning or "", meta_json, now, next_seq),
            )

            # 更新会话时间
            await db.execute(
                "UPDATE sessions SET updated_at = $1 WHERE id = $2", (now, session_id)
            )

            # 失效消息缓存
            await self._cache_delete(
                self._cache_key("messages", session_id),
                self._cache_key("messages_full", session_id),
                self._cache_key("session", session_id),
            )

    async def async_get_messages(
        self,
        session_id: str,
        limit: int = MAX_MESSAGES_IN_CONTEXT,
        include_internal: bool = False,
    ) -> list[dict]:
        """获取会话消息列表（用于构建 LLM 上下文）."""
        cache_key = self._cache_key(
            "messages" if not include_internal else "messages_full",
            session_id,
        )

        # 尝试缓存（默认参数时）
        if limit == MAX_MESSAGES_IN_CONTEXT:
            cached = await self._cache_get_json(cache_key)
            if cached is not None:
                return cached

        db = await self._ensure_storage()

        if include_internal:
            rows = await db.fetchall(
                "SELECT role, content, sender, source, reasoning, metadata, timestamp, seq "
                "FROM messages WHERE session_id = $1 ORDER BY seq ASC",
                (session_id,),
            )
        else:
            rows = await db.fetchall(
                "SELECT role, content, sender, source, reasoning, metadata, timestamp, seq "
                "FROM messages WHERE session_id = $1 AND source != 'internal' ORDER BY seq ASC",
                (session_id,),
            )

        messages = []
        for row in rows:
            r = dict(row)
            # 解析 metadata
            if isinstance(r.get("metadata"), str):
                try:
                    r["metadata"] = json.loads(r["metadata"])
                except (json.JSONDecodeError, TypeError):
                    r["metadata"] = {}
            messages.append(r)

        # 截断到 limit
        if len(messages) > limit:
            messages = messages[-limit:]

        # 写入缓存
        if limit == MAX_MESSAGES_IN_CONTEXT:
            await self._cache_set_json(cache_key, messages, ttl=self._messages_ttl)

        return messages

    async def async_get_all_messages(self, session_id: str) -> list[dict]:
        """获取会话所有消息（含 internal，用于 UI 展示）."""
        return await self.async_get_messages(session_id, limit=999999, include_internal=True)

    async def async_replace_messages_from(
        self,
        session_id: str,
        from_index: int,
        new_messages: list[dict],
    ) -> dict:
        """从指定索引位置替换消息（编辑-截断功能）.

        被移除的消息归档到 messages_archive 表。
        """
        db = await self._ensure_storage()
        now = datetime.now().isoformat(timespec="seconds")

        # 获取当前所有消息
        current = await self.async_get_all_messages(session_id)

        if from_index < 0:
            from_index = max(0, len(current) + from_index)
        if from_index >= len(current):
            return {"kept": len(current), "removed": 0, "added": 0}

        kept = current[:from_index]
        removed = current[from_index:]

        # 归档被移除的消息
        if removed and ARCHIVE_ON_EDIT:
            for msg in removed:
                meta = msg.get("metadata", {})
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                meta_json = json.dumps(meta, ensure_ascii=False)
                await db.execute(
                    "INSERT INTO messages_archive "
                    "(session_id, role, content, sender, source, reasoning, metadata, timestamp, seq, archived_at, archive_reason) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                    (
                        session_id, msg.get("role", ""), msg.get("content", ""),
                        msg.get("sender", ""), msg.get("source", ""),
                        msg.get("reasoning", ""), meta_json,
                        msg.get("timestamp", ""), msg.get("seq", 0),
                        now, "edit_truncate",
                    ),
                )

        # 删除 from_index 之后的消息
        if removed:
            min_seq = removed[0].get("seq", 0)
            await db.execute(
                "DELETE FROM messages WHERE session_id = $1 AND seq >= $2",
                (session_id, min_seq),
            )

        # 插入新消息
        next_seq = (kept[-1].get("seq", 0) + 1) if kept else 1
        added = 0
        for msg in new_messages:
            meta = msg.get("metadata", {})
            meta_json = json.dumps(meta, ensure_ascii=False) if isinstance(meta, dict) else meta
            await db.execute(
                "INSERT INTO messages (session_id, role, content, sender, source, reasoning, metadata, timestamp, seq) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                (
                    session_id, msg.get("role", ""), msg.get("content", ""),
                    msg.get("sender", ""), msg.get("source", ""),
                    msg.get("reasoning", ""), meta_json,
                    msg.get("timestamp", now), next_seq,
                ),
            )
            next_seq += 1
            added += 1

        # 更新会话时间
        await db.execute(
            "UPDATE sessions SET updated_at = $1 WHERE id = $2", (now, session_id)
        )

        await self._invalidate_session_cache(session_id)

        return {"kept": len(kept), "removed": len(removed), "added": added}

    async def async_get_archive(self, session_id: str, limit: int = 100) -> list[dict]:
        """获取会话归档消息."""
        db = await self._ensure_storage()
        rows = await db.fetchall(
            "SELECT * FROM messages_archive WHERE session_id = $1 "
            "ORDER BY archived_at DESC, seq ASC LIMIT $2",
            (session_id, limit),
        )
        results = []
        for row in rows:
            r = dict(row)
            if isinstance(r.get("metadata"), str):
                try:
                    r["metadata"] = json.loads(r["metadata"])
                except (json.JSONDecodeError, TypeError):
                    r["metadata"] = {}
            results.append(r)
        return results

    # ========== 搜索 (Async) ==========

    async def async_search(self, keyword: str, limit: int = 20) -> list[dict]:
        """全文搜索消息内容."""
        db = await self._ensure_storage()
        from scout.storage.sqlite import SQLiteStorage

        if isinstance(db, SQLiteStorage):
            # SQLite FTS5
            rows = await db.fetchall(
                "SELECT m.session_id, m.role, m.content, m.timestamp, "
                "s.title as session_title "
                "FROM messages_fts fts "
                "JOIN messages m ON m.id = fts.rowid "
                "JOIN sessions s ON s.id = m.session_id "
                "WHERE messages_fts MATCH $1 "
                "ORDER BY m.timestamp DESC LIMIT $2",
                (keyword, limit),
            )
        else:
            # PostgreSQL tsvector
            rows = await db.fetchall(
                "SELECT m.session_id, m.role, m.content, m.timestamp, "
                "s.title as session_title "
                "FROM messages m "
                "JOIN sessions s ON s.id = m.session_id "
                "WHERE m.search_vector @@ plainto_tsquery('simple', $1) "
                "ORDER BY m.timestamp DESC LIMIT $2",
                (keyword, limit),
            )
        return [dict(r) for r in rows]

    # ========== 统计 (Async) ==========

    async def async_stats(self) -> dict:
        """获取存储统计信息."""
        db = await self._ensure_storage()
        from scout.storage.sqlite import SQLiteStorage

        session_count_row = await db.fetchone("SELECT COUNT(*) as cnt FROM sessions")
        session_count = session_count_row["cnt"] if session_count_row else 0

        message_count_row = await db.fetchone("SELECT COUNT(*) as cnt FROM messages")
        message_count = message_count_row["cnt"] if message_count_row else 0

        # 归档数量
        try:
            archive_count_row = await db.fetchone("SELECT COUNT(*) as cnt FROM messages_archive")
            archive_count = archive_count_row["cnt"] if archive_count_row else 0
        except Exception:
            archive_count = 0

        # 存储大小
        storage_size = "N/A"
        if isinstance(db, SQLiteStorage) and self._db_path:
            db_file = Path(self._db_path).expanduser()
            if db_file.exists():
                size_bytes = db_file.stat().st_size
                if size_bytes < 1024 * 1024:
                    storage_size = f"{size_bytes / 1024:.1f} KB"
                else:
                    storage_size = f"{size_bytes / (1024 * 1024):.1f} MB"

        return {
            "session_count": session_count,
            "message_count": message_count,
            "archive_count": archive_count,
            "storage_size": storage_size,
            "backend": type(db).__name__,
            "cache_enabled": self._cache is not None,
        }

    # ========== 会话全量读写 (agent 内存态为准) ==========
    # 这些方法是 agent.py / web.py / starlight.py 的既有调用契约。
    # save_session: 全量重写消息（agent 在内存中维护 session.messages，保存时整体落库）

    async def async_save_session(self, session: Any) -> None:
        """全量保存会话（消息全量重写，seq 按列表顺序重排）."""
        with self._lock:
            db = await self._ensure_storage()
            now = datetime.now().isoformat(timespec="seconds")

            # 检查会话是否存在
            existing = await db.fetchone(
                "SELECT id FROM sessions WHERE id = $1", (session.id,)
            )
            extra_json = json.dumps(session.extra or {}, ensure_ascii=False)
            if existing:
                await db.execute(
                    "UPDATE sessions SET status = $1, title = $2, extra = $3, "
                    "updated_at = $4 WHERE id = $5",
                    (session.status, session.extra.get("title", ""), extra_json, now, session.id),
                )
            else:
                await db.execute(
                    "INSERT INTO sessions (id, agent_id, status, parent_id, lineage_id, title, created_at, updated_at, extra) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                    (
                        session.id, session.agent_id, session.status,
                        session.parent_id or "", session.lineage_id or "",
                        session.extra.get("title", ""), now, now, extra_json,
                    ),
                )

            # 全量重写消息：删除旧消息前，先把「将被移除的旧消息」归档到 messages_archive。
            # 原因：session.messages 是 agent 内存态，编辑截断/上下文治理/恢复不完整时
            # 可能只包含部分消息；若不归档直接 DELETE，未被包含的历史会被永久删除且无法找回。
            old_rows = await db.fetchall(
                "SELECT * FROM messages WHERE session_id = $1 ORDER BY seq ASC",
                (session.id,),
            )
            if old_rows:
                new_sigs = set()
                for m in session.messages:
                    role = m.role.value if hasattr(m.role, "value") else str(m.role)
                    new_sigs.add((role, m.content or ""))
                removed = [
                    r for r in old_rows
                    if (r["role"], r["content"] or "") not in new_sigs
                ]
                if removed:
                    await self._archive_raw_rows(
                        db, session.id, removed,
                        reason="save_sync", now=now,
                    )

            rows = []
            for seq, m in enumerate(session.messages):
                meta_json = json.dumps(m.metadata or {}, ensure_ascii=False)
                ts = m.timestamp.isoformat() if isinstance(m.timestamp, datetime) else str(m.timestamp or now)
                rows.append((
                    session.id,
                    m.role.value if hasattr(m.role, "value") else str(m.role),
                    m.content or "", m.sender or "", m.source or "",
                    m.reasoning or "", meta_json, ts, seq,
                ))

            async with db.transaction():
                await db.execute("DELETE FROM messages WHERE session_id = $1", (session.id,))
                if rows:
                    await db.executemany(
                        "INSERT INTO messages (session_id, role, content, sender, source, reasoning, metadata, timestamp, seq) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                        rows,
                    )

            await self._invalidate_session_cache(session.id)

    async def async_load_session(self, session_id: str) -> Any | None:
        """加载会话（含全部消息，按 seq 排序）."""
        from scout.core.types import Message, Role, Session

        db = await self._ensure_storage()
        row = await db.fetchone(
            "SELECT * FROM sessions WHERE id = $1", (session_id,)
        )
        if not row:
            return None

        msgs_rows = await db.fetchall(
            "SELECT * FROM messages WHERE session_id = $1 ORDER BY seq ASC",
            (session_id,),
        )
        messages: list[Message] = []
        for r in msgs_rows:
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            try:
                role = Role(r.get("role", "user"))
            except ValueError:
                role = Role.USER
            ts = r.get("timestamp") or ""
            try:
                ts_dt = datetime.fromisoformat(ts) if ts else datetime.now()
            except (ValueError, TypeError):
                ts_dt = datetime.now()
            messages.append(Message(
                role=role,
                content=r.get("content") or "",
                sender=r.get("sender") or "",
                source=r.get("source") or "",
                reasoning=r.get("reasoning"),
                metadata=meta,
                timestamp=ts_dt,
            ))

        extra = row.get("extra") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (json.JSONDecodeError, TypeError):
                extra = {}
        return Session(
            id=row["id"],
            agent_id=row.get("agent_id") or "default",
            messages=messages,
            status=row.get("status") or "idle",
            parent_id=row.get("parent_id"),
            lineage_id=row.get("lineage_id") or "",
            extra=extra,
        )

    async def async_list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """列出会话元数据（含首条消息预览）."""
        db = await self._ensure_storage()
        rows = await db.fetchall(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT $1 OFFSET $2",
            (limit, offset),
        )
        results = []
        for row in rows:
            r = dict(row)
            if isinstance(r.get("extra"), str):
                try:
                    r["extra"] = json.loads(r["extra"])
                except (json.JSONDecodeError, TypeError):
                    r["extra"] = {}
            # 补充首条消息预览（剥离 runtime_context 系统注入）
            try:
                first = await db.fetchone(
                    "SELECT content FROM messages WHERE session_id = $1 AND role = 'user' "
                    "ORDER BY seq ASC LIMIT 1",
                    (row["id"],),
                )
                if first and first.get("content"):
                    import re as _re
                    _c = first["content"]
                    _c = _re.sub(r"<runtime_context>[\s\S]*?</runtime_context>", "", _c)
                    _c = _re.sub(r"<memories>[\s\S]*?</memories>", "", _c)
                    _c = _re.sub(r"<skills>[\s\S]*?</skills>", "", _c)
                    r["preview"] = _c.strip()[:100]
            except Exception:
                r["preview"] = ""
            results.append(r)
        return results

    async def async_rename_session(self, session_id: str, title: str) -> None:
        """重命名会话标题."""
        db = await self._ensure_storage()
        now = datetime.now().isoformat(timespec="seconds")
        await db.execute(
            "UPDATE sessions SET title = $1, updated_at = $2 WHERE id = $3",
            (title, now, session_id),
        )
        await self._invalidate_session_cache(session_id)

    async def async_delete_session(self, session_id: str) -> bool:
        """删除会话及其消息."""
        db = await self._ensure_storage()
        row = await db.fetchone("SELECT id FROM sessions WHERE id = $1", (session_id,))
        if not row:
            return False
        await db.execute("DELETE FROM messages WHERE session_id = $1", (session_id,))
        await db.execute("DELETE FROM sessions WHERE id = $1", (session_id,))
        await self._invalidate_session_cache(session_id)
        return True

    async def async_search_messages(self, keyword: str, limit: int = 20) -> list[dict]:
        """跨会话全文搜索消息（SQLite 用 LIKE，PG 用 ILIKE）."""
        db = await self._ensure_storage()
        from scout.storage.sqlite import SQLiteStorage

        if isinstance(db, SQLiteStorage):
            rows = await db.fetchall(
                "SELECT m.session_id, m.role, m.content, m.timestamp, "
                "s.title as session_title "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.content LIKE $1 "
                "ORDER BY m.timestamp DESC LIMIT $2",
                (f"%{keyword}%", limit),
            )
        else:
            rows = await db.fetchall(
                "SELECT m.session_id, m.role, m.content, m.timestamp, "
                "s.title as session_title "
                "FROM messages m JOIN sessions s ON s.id = m.session_id "
                "WHERE m.content ILIKE $1 "
                "ORDER BY m.timestamp DESC LIMIT $2",
                (f"%{keyword}%", limit),
            )
        results = []
        for r in rows:
            d = dict(r)
            if d.get("content") and len(d["content"]) > 200:
                d["content"] = d["content"][:200] + "..."
            results.append(d)
        return results

    async def async_archive_messages(
        self, session_id: str, messages: list[Any], reason: str = "edit_truncate"
    ) -> int:
        """归档被截断/删除的消息（写入 messages_archive）."""
        db = await self._ensure_storage()
        now = datetime.now().isoformat(timespec="seconds")
        archived = 0
        for idx, m in enumerate(messages):
            meta_json = json.dumps(m.metadata or {}, ensure_ascii=False)
            ts = m.timestamp.isoformat() if isinstance(m.timestamp, datetime) else str(m.timestamp or now)
            await db.execute(
                "INSERT INTO messages_archive "
                "(session_id, role, content, sender, source, reasoning, metadata, timestamp, seq, archived_at, archive_reason) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                (
                    session_id,
                    m.role.value if hasattr(m.role, "value") else str(m.role),
                    m.content or "", m.sender or "", m.source or "",
                    m.reasoning or "", meta_json, ts, idx, now, reason,
                ),
            )
            archived += 1
        return archived

    async def _archive_raw_rows(
        self, db: StorageBackend, session_id: str,
        rows: list[dict], reason: str, now: str,
    ) -> int:
        """把已从 messages 表查出的旧行归档到 messages_archive（去重后写入）."""
        archived = 0
        for r in rows:
            # 去重：同一会话 + role + content 已归档过则跳过，避免重复归档
            dup = await db.fetchone(
                "SELECT 1 FROM messages_archive WHERE session_id = $1 AND role = $2 "
                "AND content = $3 LIMIT 1",
                (session_id, r["role"], r["content"] or ""),
            )
            if dup:
                continue
            await db.execute(
                "INSERT INTO messages_archive "
                "(session_id, role, content, sender, source, reasoning, metadata, timestamp, seq, archived_at, archive_reason) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                (
                    session_id,
                    r["role"], r["content"] or "", r["sender"] or "", r["source"] or "",
                    r["reasoning"] or "", r["metadata"] or "{}", r["timestamp"] or "",
                    r["seq"] or 0, now, reason,
                ),
            )
            archived += 1
        return archived

    # ========== 同步兼容层 ==========
    # 为保持向后兼容，提供同步方法（内部使用 asyncio）

    def save_session(self, session: Any) -> None:
        """全量保存会话（同步）."""
        self._run_async(self.async_save_session(session))

    def load_session(self, session_id: str) -> Any | None:
        """加载会话（同步）."""
        return self._run_async(self.async_load_session(session_id))

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """列出会话（同步）."""
        return self._run_async(self.async_list_sessions(limit, offset))

    def rename_session(self, session_id: str, title: str) -> None:
        """重命名会话（同步）."""
        self._run_async(self.async_rename_session(session_id, title))

    def delete_session(self, session_id: str) -> bool:
        """删除会话（同步）."""
        return self._run_async(self.async_delete_session(session_id))

    def search_messages(self, keyword: str, limit: int = 20) -> list[dict]:
        """跨会话搜索消息（同步）."""
        return self._run_async(self.async_search_messages(keyword, limit))

    def archive_messages(self, session_id: str, messages: list[Any], reason: str = "edit_truncate") -> int:
        """归档消息（同步）."""
        return self._run_async(self.async_archive_messages(session_id, messages, reason))

    def _run_async(self, coro):
        """在同步上下文中运行异步协程."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # 已有事件循环在运行 — 创建新线程执行
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=30)
        else:
            return asyncio.run(coro)

    def create(self, session_id: str, agent_id: str = "default",
               parent_id: str | None = None, lineage_id: str = "",
               title: str = "", extra: dict | None = None) -> None:
        """创建新会话（同步）."""
        self._run_async(self.async_create(session_id, agent_id, parent_id, lineage_id, title, extra))

    def fork(self, session_id: str, new_session_id: str,
             up_to_seq: int | None = None, title: str | None = None,
             extra: dict | None = None) -> dict | None:
        """从现有会话 fork 出分支（同步）."""
        return self._run_async(self.async_fork(
            session_id, new_session_id, up_to_seq, title, extra
        ))

    def get(self, session_id: str) -> dict | None:
        """获取会话元数据（同步）."""
        return self._run_async(self.async_get(session_id))

    def update_status(self, session_id: str, status: str) -> None:
        """更新会话状态（同步）."""
        self._run_async(self.async_update_status(session_id, status))

    def update_title(self, session_id: str, title: str) -> None:
        """更新会话标题（同步）."""
        self._run_async(self.async_update_title(session_id, title))

    def update_extra(self, session_id: str, extra: dict) -> None:
        """更新会话 extra 字段（同步）."""
        self._run_async(self.async_update_extra(session_id, extra))

    def delete(self, session_id: str) -> bool:
        """删除会话（同步）."""
        return self._run_async(self.async_delete(session_id))

    def list(self, agent_id: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """列出会话（同步）."""
        return self._run_async(self.async_list(agent_id, limit, offset))

    def append_message(self, session_id: str, role: str, content: str,
                       sender: str = "", source: str = "",
                       reasoning: str | None = None, metadata: dict | None = None) -> None:
        """追加消息（同步）."""
        self._run_async(self.async_append_message(
            session_id, role, content, sender, source, reasoning, metadata
        ))

    def get_messages(self, session_id: str, limit: int = MAX_MESSAGES_IN_CONTEXT,
                     include_internal: bool = False) -> list[dict]:
        """获取消息列表（同步）."""
        return self._run_async(self.async_get_messages(session_id, limit, include_internal))

    def get_all_messages(self, session_id: str) -> list[dict]:
        """获取所有消息（同步）."""
        return self._run_async(self.async_get_all_messages(session_id))

    def replace_messages_from(self, session_id: str, from_index: int,
                               new_messages: list[dict]) -> dict:
        """从指定位置替换消息（同步）."""
        return self._run_async(self.async_replace_messages_from(
            session_id, from_index, new_messages
        ))

    def get_archive(self, session_id: str, limit: int = 100) -> list[dict]:
        """获取归档消息（同步）."""
        return self._run_async(self.async_get_archive(session_id, limit))

    def search(self, keyword: str, limit: int = 20) -> list[dict]:
        """全文搜索（同步）."""
        return self._run_async(self.async_search(keyword, limit))

    def stats(self) -> dict:
        """获取统计信息（同步）."""
        return self._run_async(self.async_stats())


def get_session_store(backend: str | None = None, **kwargs) -> "SessionStore":
    """会话存储工厂（2026-08-27）— 支持插件 SPI 替换.

    backend 优先级：显式参数 > 环境变量 SCOUT_SESSION_STORE。
    backend="spi" 时从插件取 session 实现（未注册则报错提示加载对应插件）。
    """
    backend = backend or os.getenv("SCOUT_SESSION_STORE", "")
    if backend == "spi":
        from scout.plugins.spi import SPI_KIND_SESSION, get_provider

        impl = get_provider(SPI_KIND_SESSION)
        if impl is None:
            raise ValueError(
                "会话存储 SPI 未注册：backend='spi' 但无插件提供 'session' 实现。"
                "请加载声明 provides=['session'] 的插件，或改用内置后端。"
            )
        return impl(**kwargs) if callable(impl) else impl
    return SessionStore(**kwargs)
