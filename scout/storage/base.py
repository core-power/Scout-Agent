"""存储后端抽象基类.

定义统一的存储接口，支持 SQLite / PostgreSQL / Redis 等多后端。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


class StorageBackend(ABC):
    """关系型存储后端抽象基类."""

    @abstractmethod
    async def connect(self) -> None:
        """建立连接/连接池."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """关闭连接/连接池."""
        ...

    @abstractmethod
    async def execute(self, sql: str, params: tuple | None = None) -> None:
        """执行写操作 (INSERT/UPDATE/DELETE)."""
        ...

    @abstractmethod
    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """批量执行写操作."""
        ...

    @abstractmethod
    async def fetchone(self, sql: str, params: tuple | None = None) -> dict | None:
        """查询单行."""
        ...

    @abstractmethod
    async def fetchall(self, sql: str, params: tuple | None = None) -> list[dict]:
        """查询多行."""
        ...

    @abstractmethod
    async def execute_script(self, script: str) -> None:
        """执行多条 SQL 语句（用于初始化/迁移）."""
        ...

    @abstractmethod
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["StorageBackend"]:
        """事务上下文管理器."""
        ...

    async def health_check(self) -> bool:
        """健康检查."""
        try:
            await self.fetchone("SELECT 1")
            return True
        except Exception:
            return False


class CacheBackend(ABC):
    """缓存后端抽象基类."""

    @abstractmethod
    async def connect(self) -> None:
        """建立连接."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """关闭连接."""
        ...

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """获取缓存值."""
        ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """设置缓存值."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """删除缓存."""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """检查键是否存在."""
        ...

    @abstractmethod
    async def expire(self, key: str, seconds: int) -> None:
        """设置过期时间."""
        ...

    @abstractmethod
    async def incr(self, key: str, amount: int = 1) -> int:
        """原子递增."""
        ...

    @abstractmethod
    async def hset(self, name: str, key: str, value: str) -> None:
        """Hash 设置."""
        ...

    @abstractmethod
    async def hget(self, name: str, key: str) -> str | None:
        """Hash 获取."""
        ...

    @abstractmethod
    async def hgetall(self, name: str) -> dict[str, str]:
        """Hash 获取全部."""
        ...

    @abstractmethod
    async def publish(self, channel: str, message: str) -> None:
        """发布消息."""
        ...

    @abstractmethod
    async def subscribe(self, channel: str, callback: Any) -> None:
        """订阅消息."""
        ...

    async def health_check(self) -> bool:
        """健康检查."""
        try:
            await self.set("__health__", "ok", ttl=10)
            val = await self.get("__health__")
            return val == "ok"
        except Exception:
            return False
