"""存储抽象层 — 支持 SQLite / PostgreSQL / Redis 多后端.

Phase 1 P0: 将 SQLite 替换为 PostgreSQL，引入 Redis 缓存层。
"""

from scout.storage.base import StorageBackend, CacheBackend
from scout.storage.postgres import PostgresStorage
from scout.storage.redis_cache import RedisCache
from scout.storage.sqlite import SQLiteStorage

__all__ = [
    "StorageBackend",
    "CacheBackend",
    "PostgresStorage",
    "RedisCache",
    "SQLiteStorage",
]
