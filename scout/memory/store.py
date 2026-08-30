"""记忆系统 — 长期记忆存储 + 向量语义检索 + 时间衰减 + 重要性评分.

借鉴 OpenClaw 的记忆时间衰减设计。
记忆分为：长期记忆（MEMORY.md）和动态记忆（每日文件）。

v2: 集成向量嵌入，支持混合检索（FTS5 文本 + 向量语义）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from scout.storage.schema import ensure_schema

logger = logging.getLogger(__name__)


class MemoryEntry:
    """一条记忆."""

    def __init__(
        self,
        id: int | None,
        content: str,
        category: str = "general",
        importance: float = 0.5,
        created_at: datetime | None = None,
        last_accessed: datetime | None = None,
        access_count: int = 0,
    ):
        self.id = id
        self.content = content
        self.category = category
        self.importance = importance  # 0.0 ~ 1.0
        self.created_at = created_at or datetime.now()
        self.last_accessed = last_accessed or datetime.now()
        self.access_count = access_count

    def decay_score(self) -> float:
        """时间衰减分数 — 越久未访问越低."""
        days = (datetime.now() - self.last_accessed).days
        decay = max(0.1, 1.0 - days * 0.05)  # 每天衰减 5%，最低 10%
        return self.importance * decay

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category,
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "last_accessed": self.last_accessed.isoformat(),
            "access_count": self.access_count,
            "decay_score": self.decay_score(),
        }


class MemoryStore:
    """记忆存储 — SQLite + FTS5 + 向量语义检索.

    支持两种检索模式:
    1. 纯文本检索 (FTS5 + LIKE) — 无需 embedding，开箱即用
    2. 混合检索 (向量语义 + FTS5) — 需要注入 embedding_provider

    当注入 embedding_provider 后，search() 会自动进行混合检索：
    - 向量语义检索（余弦相似度）
    - FTS5 文本检索
    - 结果融合排序（RRF, Reciprocal Rank Fusion）
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        embedding_provider: Any = None,
    ):
        self.db_path = Path(
            db_path if db_path is not None else str(_SCOUT_DATA_DIR / "memory.db")
        ).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()

        # 向量嵌入提供者（可选）
        self._embedding_provider = embedding_provider

        # 向量索引（内存中，用于快速检索）
        self._vector_index: np.ndarray | None = None  # (N, dim)
        self._vector_ids: list[int] = []  # 对应 memory id
        self._vector_lock = threading.RLock()

        self._init_db()
        self._load_vector_index()

    def set_embedding_provider(self, provider: Any) -> None:
        """注入/更新 embedding provider.

        Args:
            provider: 实现了 embed(text) -> np.ndarray 的对象
        """
        self._embedding_provider = provider
        # 重建向量索引
        self._rebuild_vector_index()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False
            )
            self._local.conn.row_factory = sqlite3.Row
            # WAL 模式：并发安全 + 崩溃可恢复
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                importance REAL DEFAULT 0.5,
                created_at TEXT,
                last_accessed TEXT,
                access_count INTEGER DEFAULT 0,
                embedding BLOB DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);

            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content,
                content='memories',
                content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content)
                    VALUES('delete', old.id, old.content);
            END;
        """)
        conn.commit()

        # 统一 schema 版本管理：自动执行缺失版本的增量迁移（幂等）
        ensure_schema(conn)

    def _load_vector_index(self) -> None:
        """从数据库加载向量索引到内存."""
        with self._vector_lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL"
            ).fetchall()

            if not rows:
                self._vector_index = None
                self._vector_ids = []
                return

            self._vector_ids = [r["id"] for r in rows]
            vectors = []
            for r in rows:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                vectors.append(vec)

            if vectors:
                self._vector_index = np.stack(vectors)
            else:
                self._vector_index = None

    def _rebuild_vector_index(self) -> None:
        """重建向量索引 — 为所有没有 embedding 的记忆生成向量."""
        if self._embedding_provider is None:
            return

        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, content FROM memories WHERE embedding IS NULL"
        ).fetchall()

        if not rows:
            # 所有记忆都已有 embedding，直接加载索引
            self._load_vector_index()
            return

        logger.info(f"为 {len(rows)} 条记忆生成向量嵌入...")

        # 批量生成 embedding
        texts = [r["content"] for r in rows]
        ids = [r["id"] for r in rows]

        try:
            # 尝试异步调用（如果在事件循环中）
            try:
                loop = asyncio.get_running_loop()
                # 在事件循环中，创建新线程执行异步函数
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        lambda: asyncio.run(self._embedding_provider.embed_batch(texts))
                    )
                    embeddings = future.result(timeout=60)
            except RuntimeError:
                # 没有事件循环，直接创建
                embeddings = asyncio.run(self._embedding_provider.embed_batch(texts))

            # 保存到数据库
            for mem_id, embedding in zip(ids, embeddings):
                conn.execute(
                    "UPDATE memories SET embedding = ? WHERE id = ?",
                    (embedding.astype(np.float32).tobytes(), mem_id),
                )
            conn.commit()

            logger.info(f"✅ 已为 {len(embeddings)} 条记忆生成向量嵌入")

        except Exception as e:
            logger.warning(f"批量生成 embedding 失败: {e}")

        # 重新加载索引
        self._load_vector_index()

    async def _embed_text(self, text: str) -> np.ndarray | None:
        """将文本转换为向量."""
        if self._embedding_provider is None:
            return None

        try:
            embedding = await self._embedding_provider.embed(text)
            return embedding
        except Exception as e:
            logger.warning(f"Embedding 生成失败: {e}")
            return None

    def add(
        self,
        content: str,
        category: str = "general",
        importance: float = 0.5,
        embedding: np.ndarray | None = None,
    ) -> int:
        """添加一条记忆.

        Args:
            content: 记忆内容
            category: 类别
            importance: 重要性 0.0~1.0
            embedding: 预计算的向量（可选，如果不提供且有 embedding_provider 会自动生成）

        安全（2026-08-13，对标 Hermes 记忆通道防护）：
        - 写入前做提示词注入/凭证窃取/隐形Unicode 扫描，block 级直接拒绝
        - API Key/Token/密码等自动脱敏（对标 Codex secret redaction）
        - 返回 -1 表示被安全策略拒绝
        """
        # ── 记忆安全扫描（可用 $SCOUT_DATA_DIR/memories.json 的 security_scan 开关）──
        try:
            from scout.memory.governance import MemoriesConfig
            from scout.memory.security_scan import scan_memory_content
            if MemoriesConfig.load().security_scan:
                scan = scan_memory_content(content, redact=True)
                if scan.blocked:
                    import logging
                    logging.getLogger(__name__).warning(
                        f"记忆写入被安全扫描拦截: {'; '.join(scan.issues)}"
                    )
                    return -1
                if scan.issues:
                    import logging
                    logging.getLogger(__name__).info(
                        f"记忆写入已脱敏/清洗: {'; '.join(scan.issues)}"
                    )
                content = scan.redacted_text
        except Exception:
            pass  # 扫描模块异常不阻塞正常写入

        conn = self._get_conn()
        now = datetime.now().isoformat()

        # 序列化 embedding
        embedding_blob = None
        if embedding is not None:
            embedding_blob = embedding.astype(np.float32).tobytes()

        cur = conn.execute(
            """INSERT INTO memories (content, category, importance, created_at, last_accessed, access_count, embedding)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (content, category, importance, now, now, embedding_blob),
        )
        conn.commit()
        mem_id = cur.lastrowid

        # 如果有 embedding，更新内存索引
        if embedding is not None:
            with self._vector_lock:
                if self._vector_index is not None and len(self._vector_index) > 0:
                    self._vector_index = np.vstack([
                        self._vector_index,
                        embedding.reshape(1, -1).astype(np.float32),
                    ])
                else:
                    self._vector_index = embedding.reshape(1, -1).astype(np.float32)
                if mem_id not in self._vector_ids:
                    self._vector_ids.append(mem_id)

        return mem_id

    async def add_async(
        self,
        content: str,
        category: str = "general",
        importance: float = 0.5,
    ) -> int:
        """异步添加记忆 — 自动生成 embedding."""
        embedding = await self._embed_text(content)
        return self.add(content, category, importance, embedding=embedding)

    def search(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        """搜索记忆 — 混合检索（向量语义 + FTS5 + LIKE）.

        当有 embedding_provider 时，使用 RRF (Reciprocal Rank Fusion) 融合两路结果。
        无 embedding 时，退化为纯文本检索。
        """
        # 尝试向量检索
        vector_results = self._vector_search(query, limit=limit * 2)

        # 文本检索
        text_results = self._text_search(query, limit=limit * 2)

        if vector_results and text_results:
            # RRF 融合排序
            results = self._rrf_fusion(vector_results, text_results, limit)
        elif vector_results:
            results = vector_results[:limit]
        elif text_results:
            results = text_results[:limit]
        else:
            return []

        # 访问计数：每次搜索对最终结果统一计一次
        self._bump_access_count(results)
        return results

    async def search_async(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        """异步搜索 — 先用 embedding 做语义检索，再融合文本检索."""
        # 生成查询向量
        query_embedding = await self._embed_text(query)

        if query_embedding is not None:
            # 有向量，做语义检索
            vector_results = self._vector_search_with_embedding(
                query_embedding, limit=limit * 2
            )
            text_results = self._text_search(query, limit=limit * 2)

            if vector_results and text_results:
                results = self._rrf_fusion(vector_results, text_results, limit)
            elif vector_results:
                results = vector_results[:limit]
            elif text_results:
                results = text_results[:limit]
            else:
                return []

            # 访问计数：每次搜索对最终结果统一计一次
            self._bump_access_count(results)
            return results
        else:
            # 无向量，退化为文本检索
            results = self._text_search(query, limit)
            self._bump_access_count(results)
            return results

    def _vector_search_with_embedding(
        self,
        query_embedding: np.ndarray,
        limit: int = 10,
        min_similarity: float = 0.3,
    ) -> list[MemoryEntry]:
        """用向量做语义检索."""
        with self._vector_lock:
            if self._vector_index is None or len(self._vector_index) == 0:
                return []

            # 计算余弦相似度
            query = query_embedding.reshape(1, -1).astype(np.float32)

            # 维度适配
            if query.shape[1] != self._vector_index.shape[1]:
                if query.shape[1] > self._vector_index.shape[1]:
                    query = query[:, :self._vector_index.shape[1]]
                else:
                    padded = np.zeros((1, self._vector_index.shape[1]), dtype=np.float32)
                    padded[:, :query.shape[1]] = query
                    query = padded

            query_norm = np.linalg.norm(query)
            if query_norm == 0:
                return []

            vec_norms = np.linalg.norm(self._vector_index, axis=1, keepdims=True)
            vec_norms = np.where(vec_norms == 0, 1, vec_norms)

            similarities = (self._vector_index @ query.T).flatten() / (
                vec_norms.flatten() * query_norm
            )

            # 过滤低相似度
            mask = similarities >= min_similarity
            candidate_indices = np.where(mask)[0]

            if len(candidate_indices) == 0:
                return []

            # 按相似度排序
            sorted_indices = candidate_indices[np.argsort(similarities[candidate_indices])[::-1]]
            sorted_indices = sorted_indices[:limit]

            # 获取记忆详情（按 id 去重，防御 _vector_ids 中出现重复 id）
            conn = self._get_conn()
            results = []
            seen_ids = set()
            for idx in sorted_indices:
                mem_id = self._vector_ids[idx]
                if mem_id in seen_ids:
                    continue
                seen_ids.add(mem_id)
                row = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (mem_id,)
                ).fetchone()
                if row:
                    entry = MemoryEntry(
                        id=row["id"],
                        content=row["content"],
                        category=row["category"],
                        importance=row["importance"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                        last_accessed=datetime.fromisoformat(row["last_accessed"]),
                        access_count=row["access_count"],
                    )
                    results.append(entry)

            return results

    def _vector_search(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        """同步向量检索 — 复用 backfill 模式在独立线程中生成查询向量.

        在事件循环内调用时通过 ThreadPoolExecutor 新建线程执行 asyncio.run，
        避免阻塞主循环；无事件循环时直接创建新 loop。
        """
        if self._embedding_provider is None:
            return []
        if self._vector_index is None or len(self._vector_index) == 0:
            return []
        try:
            import concurrent.futures

            try:
                asyncio.get_running_loop()
                # 已在事件循环中：新线程执行异步 embedding
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        lambda: asyncio.run(self._embedding_provider.embed(query))
                    )
                    query_embedding = future.result(timeout=60)
            except RuntimeError:
                # 无事件循环，直接创建
                query_embedding = asyncio.run(self._embedding_provider.embed(query))
        except Exception:
            return []
        if query_embedding is None:
            return []
        return self._vector_search_with_embedding(query_embedding, limit=limit)

    def _text_search(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        """FTS5 + LIKE 多策略中文搜索."""
        conn = self._get_conn()

        # 1. 先用 FTS5 搜索（用双引号包裹避免特殊字符语法错误）
        fts_query = '"' + query.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                """SELECT m.* FROM memories_fts fts
                   JOIN memories m ON m.id = fts.rowid
                   WHERE memories_fts MATCH ?
                   ORDER BY m.importance DESC, m.last_accessed DESC LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
        except Exception:
            rows = []

        # 2. FTS5 无结果时，用 LIKE 模糊搜索（中文友好，限制长度）
        if not rows and len(query) <= 100:
            try:
                like_pattern = f"%{query}%"
                rows = conn.execute(
                    """SELECT * FROM memories
                       WHERE content LIKE ? OR category LIKE ?
                       ORDER BY importance DESC, last_accessed DESC LIMIT ?""",
                    (like_pattern, like_pattern, limit),
                ).fetchall()
            except Exception:
                rows = []

        # 3. 拆词搜索 — 将 query 拆成 2 字片段，逐个 LIKE，取并集
        if not rows and 2 <= len(query) <= 100:
            keywords = [query[i:i+2] for i in range(max(1, len(query) - 2))]
            seen = set()
            for kw in keywords:
                try:
                    kw_rows = conn.execute(
                        """SELECT * FROM memories WHERE content LIKE ?
                           ORDER BY importance DESC LIMIT ?""",
                        (f"%{kw}%", limit * 2),
                    ).fetchall()
                    for r in kw_rows:
                        if r["id"] not in seen:
                            seen.add(r["id"])
                            rows.append(r)
                            if len(rows) >= limit:
                                break
                    if len(rows) >= limit:
                        break
                except Exception:
                    continue

        # 4. 如果还没结果，尝试单字搜索
        if not rows:
            for char in query[:20]:  # 限制搜索字符数
                if len(char.strip()) == 1:
                    try:
                        char_rows = conn.execute(
                            """SELECT * FROM memories WHERE content LIKE ?
                               ORDER BY importance DESC LIMIT ?""",
                            (f"%{char}%", limit),
                        ).fetchall()
                        if char_rows:
                            rows = char_rows
                            break
                    except Exception:
                        continue

        results = []
        seen_ids = set()
        for r in rows:
            if r["id"] in seen_ids:
                continue
            seen_ids.add(r["id"])
            entry = MemoryEntry(
                id=r["id"],
                content=r["content"],
                category=r["category"],
                importance=r["importance"],
                created_at=datetime.fromisoformat(r["created_at"]),
                last_accessed=datetime.fromisoformat(r["last_accessed"]),
                access_count=r["access_count"],
            )
            results.append(entry)

        # 访问计数统一在 search/search_async 入口执行（避免与 RRF 融合重复累加）
        return results

    def _rrf_fusion(
        self,
        vector_results: list[MemoryEntry],
        text_results: list[MemoryEntry],
        limit: int = 10,
        k: int = 60,
    ) -> list[MemoryEntry]:
        """RRF (Reciprocal Rank Fusion) 融合排序.

        RRF score = sum(1 / (k + rank_i))
        k 是平滑常数，通常取 60。
        """
        scores: dict[int, float] = {}
        entry_map: dict[int, MemoryEntry] = {}

        # 向量检索排名
        for rank, entry in enumerate(vector_results):
            if entry.id not in scores:
                scores[entry.id] = 0.0
                entry_map[entry.id] = entry
            scores[entry.id] += 1.0 / (k + rank + 1)

        # 文本检索排名
        for rank, entry in enumerate(text_results):
            if entry.id not in scores:
                scores[entry.id] = 0.0
                entry_map[entry.id] = entry
            scores[entry.id] += 1.0 / (k + rank + 1)

        # 按 RRF 分数排序（访问计数统一在 search/search_async 入口执行）
        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        return [entry_map[mem_id] for mem_id in sorted_ids[:limit]]

    def _bump_access_count(self, results: list[MemoryEntry]) -> None:
        """对搜索结果统一计一次访问（避免文本检索与 RRF 融合重复累加）."""

        if not results:
            return
        conn = self._get_conn()
        now = datetime.now().isoformat()
        for entry in results:
            conn.execute(
                "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 WHERE id = ?",
                (now, entry.id),
            )
        conn.commit()

    def list_recent(self, category: str | None = None, limit: int = 20) -> list[MemoryEntry]:
        """列出最近的记忆."""
        conn = self._get_conn()
        if category:
            rows = conn.execute(
                "SELECT * FROM memories WHERE category = ? ORDER BY created_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

        return [
            MemoryEntry(
                id=r["id"],
                content=r["content"],
                category=r["category"],
                importance=r["importance"],
                created_at=datetime.fromisoformat(r["created_at"]),
                last_accessed=datetime.fromisoformat(r["last_accessed"]),
                access_count=r["access_count"],
            )
            for r in rows
        ]

    def count(self) -> int:
        """记忆总条数（自省模块用）."""
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) as n FROM memories").fetchone()
        return int(row["n"]) if row else 0

    def list_oldest(self, limit: int = 50) -> list[MemoryEntry]:
        """列出最旧的记忆（自省合并用）."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM memories ORDER BY created_at ASC LIMIT ?", (limit,)
        ).fetchall()
        return [
            MemoryEntry(
                id=r["id"],
                content=r["content"],
                category=r["category"],
                importance=r["importance"],
                created_at=datetime.fromisoformat(r["created_at"]),
                last_accessed=datetime.fromisoformat(r["last_accessed"]),
                access_count=r["access_count"],
            )
            for r in rows
        ]

    def decay_cleanup(self, min_score: float = 0.05) -> int:
        """清理衰减过度的记忆 — 分数低于 min_score 的删除.

        Returns: 删除的条数
        """
        conn = self._get_conn()
        deleted = 0
        rows = conn.execute("SELECT * FROM memories").fetchall()
        for r in rows:
            entry = MemoryEntry(
                id=r["id"],
                content=r["content"],
                category=r["category"],
                importance=r["importance"],
                created_at=datetime.fromisoformat(r["created_at"]),
                last_accessed=datetime.fromisoformat(r["last_accessed"]),
                access_count=r["access_count"],
            )
            if entry.decay_score() < min_score:
                conn.execute("DELETE FROM memories WHERE id = ?", (entry.id,))
                deleted += 1
        conn.commit()

        # 重建向量索引
        if deleted > 0:
            self._load_vector_index()

        return deleted

    def delete(self, memory_id: int) -> None:
        conn = self._get_conn()
        # 主表删除由 AFTER DELETE 触发器（memories_ad）自动同步 FTS5 索引，
        # 此处不要再手动执行 FTS5 'delete' 命令，否则同一 rowid 二次删除
        # 会导致 FTS5 索引状态不一致（database disk image is malformed）。
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()

        # 更新向量索引（删除全部匹配项，避免重复 id 残留导致搜索结果重复/索引错位）
        with self._vector_lock:
            while memory_id in self._vector_ids:
                idx = self._vector_ids.index(memory_id)
                self._vector_ids.pop(idx)
                if self._vector_index is not None and idx < len(self._vector_index):
                    self._vector_index = np.delete(self._vector_index, idx, axis=0)

    def update(self, memory_id: int, content: str = None, category: str = None, importance: float = None) -> None:
        """更新记忆内容."""
        conn = self._get_conn()

        # 旧内容快照（content 变化时，FTS5 'delete' 命令需要旧值）
        old_row = None
        if content is not None:
            old_row = conn.execute(
                "SELECT content FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()

        fields = []
        params = []
        if content is not None:
            fields.append("content = ?")
            params.append(content)
            # 内容更新时，标记 embedding 需要重新生成
            fields.append("embedding = NULL")
        if category is not None:
            fields.append("category = ?")
            params.append(category)
        if importance is not None:
            fields.append("importance = ?")
            params.append(importance)
        if not fields:
            return
        fields.append("last_accessed = ?")
        params.append(datetime.now().isoformat())
        params.append(memory_id)
        conn.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id = ?", params)
        # 仅 content 变化时同步 FTS 索引（否则保留原索引，避免误删）
        if content is not None:
            if old_row:
                conn.execute(
                    "INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', ?, ?)",
                    (memory_id, old_row["content"]),
                )
            conn.execute(
                "INSERT INTO memories_fts (rowid, content) VALUES (?, ?)",
                (memory_id, content),
            )
        conn.commit()

        # 重建向量索引
        self._load_vector_index()

    def delete_by_content(self, content: str) -> int:
        """按内容模糊匹配删除记忆，返回删除条数."""
        # 限制搜索长度，避免 LIKE pattern too complex
        if len(content) > 100:
            return 0
        
        conn = self._get_conn()
        try:
            cur = conn.execute("DELETE FROM memories WHERE content LIKE ?", (f"%{content}%",))
            conn.commit()
        except Exception:
            return 0

        # 重建向量索引
        if cur.rowcount > 0:
            self._load_vector_index()

        return cur.rowcount

    def stats(self) -> dict:
        """存储统计."""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        with_embedding = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        avg_importance = conn.execute("SELECT AVG(importance) FROM memories").fetchone()[0] or 0

        return {
            "total_memories": total,
            "with_embedding": with_embedding,
            "embedding_coverage": f"{with_embedding}/{total}" if total > 0 else "0/0",
            "avg_importance": round(avg_importance, 3),
            "has_embedding_provider": self._embedding_provider is not None,
            "vector_index_loaded": self._vector_index is not None and len(self._vector_index) > 0,
        }


def get_memory_store(backend: str | None = None, **kwargs) -> "MemoryStore":
    """记忆存储工厂（2026-08-27）— 支持插件 SPI 替换.

    backend 优先级：显式参数 > 环境变量 SCOUT_MEMORY_STORE。
    backend="spi" 时从插件取 memory 实现（未注册则报错提示加载对应插件）。
    """
    backend = backend or os.getenv("SCOUT_MEMORY_STORE", "")
    if backend == "spi":
        from scout.plugins.spi import SPI_KIND_MEMORY, get_provider

        impl = get_provider(SPI_KIND_MEMORY)
        if impl is None:
            raise ValueError(
                "记忆存储 SPI 未注册：backend='spi' 但无插件提供 'memory' 实现。"
                "请加载声明 provides=['memory'] 的插件，或改用内置后端。"
            )
        return impl(**kwargs) if callable(impl) else impl
    return MemoryStore(**kwargs)
