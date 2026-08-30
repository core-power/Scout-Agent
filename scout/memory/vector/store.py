"""向量记忆存储 — 基于 SQLite + numpy 的轻量级向量数据库.

实现:
- 向量嵌入存储与相似度检索（余弦相似度）
- 时间衰减 + 重要性评分（融合排序）
- 自动清理过期/低分记忆
- 与 MemoryStore 集成
- 支持动态向量维度（自动适配 ONNX / API / Hash 嵌入）
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class VectorMemory:
    """向量记忆条目."""
    id: str
    content: str
    embedding: np.ndarray
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5
    created_at: float = 0.0
    access_count: int = 0
    last_accessed: float = 0.0
    tags: list[str] = field(default_factory=list)
    ttl_days: int | None = None  # None = 永不过期


class VectorStore:
    """向量记忆存储 — 轻量级实现，无需外部向量数据库.
    
    使用 SQLite 存储元数据 + numpy 内存索引进行向量检索。
    适合 < 100K 条记忆的场景。
    
    支持的嵌入维度:
    - OpenAI text-embedding-3-small: 1536 维
    - Hash 嵌入: 768 维（可配置）
    """

    def __init__(
        self,
        db_path: str | Path = "data/vector_memory.db",
        embedding_dim: int = 512,
        max_memories: int = 10000,
    ):
        self.db_path = Path(db_path)
        self.embedding_dim = embedding_dim
        self.max_memories = max_memories
        
        # 内存索引
        self._vectors: np.ndarray | None = None  # (N, dim)
        self._ids: list[str] = []
        self._lock = threading.RLock()
        
        # 初始化数据库
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._load_index()

    def _init_db(self):
        """初始化 SQLite 表."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    metadata TEXT DEFAULT '{}',
                    importance REAL DEFAULT 0.5,
                    created_at REAL NOT NULL,
                    access_count INTEGER DEFAULT 0,
                    last_accessed REAL DEFAULT 0.0,
                    tags TEXT DEFAULT '[]',
                    ttl_days INTEGER DEFAULT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_importance ON memories(importance)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)")
            # 统一 schema 版本管理：自动执行缺失版本的增量迁移（幂等）
            from scout.storage.schema import ensure_schema
            ensure_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        # WAL 模式：并发安全 + 崩溃可恢复（幂等，每个连接设置一次）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _load_index(self):
        """从数据库加载向量索引到内存."""
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, embedding FROM memories ORDER BY created_at"
                ).fetchall()
            
            if not rows:
                self._vectors = np.zeros((0, self.embedding_dim), dtype=np.float32)
                self._ids = []
                return
            
            self._ids = [r[0] for r in rows]
            vectors = []
            for r in rows:
                vec = np.frombuffer(r[1], dtype=np.float32)
                # 自动适配维度变化（兼容旧数据）
                if len(vec) != self.embedding_dim:
                    # 截断或补零
                    if len(vec) > self.embedding_dim:
                        vec = vec[:self.embedding_dim]
                    else:
                        padded = np.zeros(self.embedding_dim, dtype=np.float32)
                        padded[:len(vec)] = vec
                        vec = padded
                vectors.append(vec)
            self._vectors = np.stack(vectors)

    def add(self, memory: VectorMemory) -> str:
        """添加一条向量记忆."""
        with self._lock:
            # 确保向量维度正确
            if memory.embedding.shape[0] != self.embedding_dim:
                # 尝试自动适配
                if memory.embedding.shape[0] > self.embedding_dim:
                    memory.embedding = memory.embedding[:self.embedding_dim]
                else:
                    padded = np.zeros(self.embedding_dim, dtype=np.float32)
                    padded[:memory.embedding.shape[0]] = memory.embedding
                    memory.embedding = padded
            
            # 写入数据库
            with self._connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO memories 
                       (id, content, embedding, metadata, importance, created_at, access_count, last_accessed, tags, ttl_days)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        memory.id,
                        memory.content,
                        memory.embedding.astype(np.float32).tobytes(),
                        json.dumps(memory.metadata, ensure_ascii=False),
                        memory.importance,
                        memory.created_at or time.time(),
                        memory.access_count,
                        memory.last_accessed,
                        json.dumps(memory.tags),
                        memory.ttl_days,
                    ),
                )
            
            # 更新内存索引
            if self._vectors is not None and len(self._vectors) > 0:
                self._vectors = np.vstack([self._vectors, memory.embedding.reshape(1, -1).astype(np.float32)])
            else:
                self._vectors = memory.embedding.reshape(1, -1).astype(np.float32)
            self._ids.append(memory.id)
            
            # 自动清理
            if len(self._ids) > self.max_memories:
                self._evict_low_score()
            
            return memory.id

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 10,
        min_score: float = 0.0,
        tags: list[str] | None = None,
        time_decay_factor: float = 0.001,
    ) -> list[dict[str, Any]]:
        """向量相似度检索 + 时间衰减 + 重要性加权.
        
        最终得分 = cosine_similarity * importance * time_decay
        
        Args:
            query_embedding: 查询向量
            top_k: 返回条数
            min_score: 最低相似度阈值
            tags: 按标签过滤
            time_decay_factor: 时间衰减系数（越大衰减越快）
        
        Returns:
            记忆列表，按得分降序
        """
        with self._lock:
            if self._vectors is None or len(self._vectors) == 0:
                return []
            
            # 1. 计算余弦相似度
            query = query_embedding.reshape(1, -1).astype(np.float32)
            
            # 维度适配
            if query.shape[1] != self._vectors.shape[1]:
                if query.shape[1] > self._vectors.shape[1]:
                    query = query[:, :self._vectors.shape[1]]
                else:
                    padded = np.zeros((1, self._vectors.shape[1]), dtype=np.float32)
                    padded[:, :query.shape[1]] = query
                    query = padded
            
            query_norm = np.linalg.norm(query)
            if query_norm == 0:
                return []
            
            vec_norms = np.linalg.norm(self._vectors, axis=1, keepdims=True)
            vec_norms = np.where(vec_norms == 0, 1, vec_norms)
            
            similarities = (self._vectors @ query.T).flatten() / (vec_norms.flatten() * query_norm)
            
            # 2. 过滤低分
            mask = similarities >= min_score
            if tags:
                # 需要从数据库获取标签信息
                pass  # 简化：标签过滤在后续步骤处理
            
            # 3. 获取候选记忆详情
            candidate_indices = np.where(mask)[0]
            if len(candidate_indices) == 0:
                return []
            
            # 4. 从数据库获取元数据并计算最终得分
            results = []
            now = time.time()
            
            with self._connect() as conn:
                for idx in candidate_indices:
                    mem_id = self._ids[idx]
                    row = conn.execute(
                        "SELECT content, metadata, importance, created_at, access_count, tags FROM memories WHERE id = ?",
                        (mem_id,),
                    ).fetchone()
                    if not row:
                        continue
                    
                    content, metadata_str, importance, created_at, access_count, tags_str = row
                    mem_tags = json.loads(tags_str) if tags_str else []
                    
                    # 标签过滤
                    if tags and not any(t in mem_tags for t in tags):
                        continue
                    
                    # 时间衰减: exp(-factor * age_hours)
                    age_hours = (now - created_at) / 3600
                    time_decay = math.exp(-time_decay_factor * age_hours)
                    
                    # 最终得分
                    final_score = float(similarities[idx]) * importance * time_decay
                    
                    results.append({
                        "id": mem_id,
                        "content": content,
                        "metadata": json.loads(metadata_str) if metadata_str else {},
                        "score": final_score,
                        "similarity": float(similarities[idx]),
                        "importance": importance,
                        "created_at": created_at,
                        "tags": mem_tags,
                    })
            
            # 5. 按最终得分排序
            results.sort(key=lambda x: x["score"], reverse=True)
            
            # 6. 更新访问计数
            top_results = results[:top_k]
            with self._connect() as conn:
                for r in top_results:
                    conn.execute(
                        "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                        (now, r["id"]),
                    )
            
            return top_results

    def delete(self, memory_id: str) -> bool:
        """删除一条记忆."""
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
                if cursor.rowcount == 0:
                    return False
            
            # 重建索引
            if memory_id in self._ids:
                idx = self._ids.index(memory_id)
                self._ids.pop(idx)
                if self._vectors is not None:
                    self._vectors = np.delete(self._vectors, idx, axis=0)
            
            return True

    def _evict_low_score(self):
        """清理低分记忆 — 保留 top max_memories 条."""
        with self._connect() as conn:
            # 删除重要性最低且最旧的记录
            excess = len(self._ids) - self.max_memories
            if excess <= 0:
                return
            
            rows = conn.execute(
                """SELECT id FROM memories 
                   ORDER BY importance ASC, created_at ASC 
                   LIMIT ?""",
                (excess,),
            ).fetchall()
            
            ids_to_delete = [r[0] for r in rows]
            if ids_to_delete:
                placeholders = ",".join("?" * len(ids_to_delete))
                conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids_to_delete)
            
            # 重建索引
            self._load_index()

    def cleanup_expired(self) -> int:
        """清理过期记忆."""
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                cursor = conn.execute(
                    """DELETE FROM memories 
                       WHERE ttl_days IS NOT NULL 
                       AND created_at + (ttl_days * 86400) < ?""",
                    (now,),
                )
                deleted = cursor.rowcount
            
            if deleted > 0:
                self._load_index()
            
            return deleted

    def stats(self) -> dict:
        """存储统计."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            avg_importance = conn.execute("SELECT AVG(importance) FROM memories").fetchone()[0] or 0
        
        return {
            "total_memories": total,
            "embedding_dim": self.embedding_dim,
            "avg_importance": round(avg_importance, 3),
            "max_memories": self.max_memories,
            "index_loaded": self._vectors is not None and len(self._vectors) > 0,
        }
