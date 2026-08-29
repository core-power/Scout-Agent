"""Redis 缓存后端实现.

支持 Redis Sentinel 高可用模式。
用于会话缓存、限流计数、分布式锁。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

try:
    import redis.asyncio as aioredis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

from scout.storage.base import CacheBackend

logger = logging.getLogger("scout.storage.redis")


class RedisCache(CacheBackend):
    """Redis 缓存后端 — 支持 Sentinel 高可用."""

    def __init__(
        self,
        url: str | None = None,
        sentinel_hosts: list[tuple[str, int]] | None = None,
        sentinel_master: str = "mymaster",
        password: str | None = None,
        db: int = 0,
        prefix: str = "scout:",
    ):
        if not HAS_REDIS:
            raise ImportError("redis 未安装，请运行: pip install redis")

        self._url = url
        self._sentinel_hosts = sentinel_hosts
        self._sentinel_master = sentinel_master
        self._password = password
        self._db = db
        self._prefix = prefix

        self._client: aioredis.Redis | None = None
        self._pubsub: Any = None

    def _make_key(self, key: str) -> str:
        """添加前缀."""
        return f"{self._prefix}{key}"

    async def connect(self) -> None:
        """建立 Redis 连接."""
        if self._sentinel_hosts:
            # Sentinel 模式
            from redis.asyncio.sentinel import Sentinel

            sentinel = Sentinel(
                self._sentinel_hosts,
                password=self._password,
            )
            self._client = sentinel.master_for(
                self._sentinel_master,
                db=self._db,
            )
            logger.info(f"Redis Sentinel 连接已建立: master={self._sentinel_master}")
        else:
            # 单机模式
            url = self._url or "redis://localhost:6379"
            self._client = aioredis.from_url(
                url,
                db=self._db,
                decode_responses=True,
            )
            logger.info(f"Redis 连接已建立: {url}")

    async def disconnect(self) -> None:
        """关闭连接."""
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._client:
            await self._client.close()
            logger.info("Redis 连接已关闭")

    async def get(self, key: str) -> str | None:
        """获取缓存值."""
        assert self._client, "Redis 未连接"
        return await self._client.get(self._make_key(key))

    async def set(self, key: str, value: str, ttl: int | None = None) -> None:
        """设置缓存值."""
        assert self._client, "Redis 未连接"
        if ttl:
            await self._client.setex(self._make_key(key), ttl, value)
        else:
            await self._client.set(self._make_key(key), value)

    async def delete(self, key: str) -> None:
        """删除缓存."""
        assert self._client, "Redis 未连接"
        await self._client.delete(self._make_key(key))

    async def exists(self, key: str) -> bool:
        """检查键是否存在."""
        assert self._client, "Redis 未连接"
        return bool(await self._client.exists(self._make_key(key)))

    async def expire(self, key: str, seconds: int) -> None:
        """设置过期时间."""
        assert self._client, "Redis 未连接"
        await self._client.expire(self._make_key(key), seconds)

    async def incr(self, key: str, amount: int = 1) -> int:
        """原子递增."""
        assert self._client, "Redis 未连接"
        return await self._client.incrby(self._make_key(key), amount)

    async def hset(self, name: str, key: str, value: str) -> None:
        """Hash 设置."""
        assert self._client, "Redis 未连接"
        await self._client.hset(self._make_key(name), key, value)

    async def hget(self, name: str, key: str) -> str | None:
        """Hash 获取."""
        assert self._client, "Redis 未连接"
        return await self._client.hget(self._make_key(name), key)

    async def hgetall(self, name: str) -> dict[str, str]:
        """Hash 获取全部."""
        assert self._client, "Redis 未连接"
        return await self._client.hgetall(self._make_key(name))

    async def publish(self, channel: str, message: str) -> None:
        """发布消息."""
        assert self._client, "Redis 未连接"
        await self._client.publish(self._make_key(channel), message)

    async def subscribe(self, channel: str, callback: Callable) -> None:
        """订阅消息."""
        assert self._client, "Redis 未连接"
        if not self._pubsub:
            self._pubsub = self._client.pubsub()

        await self._pubsub.subscribe(self._make_key(channel))

        async def listener():
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    await callback(message["data"])

        asyncio.create_task(listener())

    # ========== 分布式锁 ==========

    async def acquire_lock(
        self,
        key: str,
        ttl: int = 30,
        retry_delay: float = 0.1,
        max_retries: int = 10,
    ) -> str | None:
        """获取分布式锁.

        Args:
            key: 锁名称
            ttl: 锁过期时间（秒）
            retry_delay: 重试间隔（秒）
            max_retries: 最大重试次数

        Returns:
            锁 token（用于释放），失败返回 None
        """
        assert self._client, "Redis 未连接"
        import uuid

        lock_key = self._make_key(f"lock:{key}")
        token = str(uuid.uuid4())

        for _ in range(max_retries):
            acquired = await self._client.set(
                lock_key, token, nx=True, ex=ttl
            )
            if acquired:
                return token
            await asyncio.sleep(retry_delay)

        return None

    async def release_lock(self, key: str, token: str) -> bool:
        """释放分布式锁.

        Args:
            key: 锁名称
            token: 获取锁时返回的 token

        Returns:
            是否成功释放
        """
        assert self._client, "Redis 未连接"
        lock_key = self._make_key(f"lock:{key}")

        # Lua 脚本确保原子性：只有 token 匹配才删除
        lua_script = """
        if redis.call("get", KEYS[1]) == ARGV[1] then
            return redis.call("del", KEYS[1])
        else
            return 0
        end
        """
        result = await self._client.eval(lua_script, 1, lock_key, token)
        return bool(result)

    # ========== 限流器 ==========

    async def rate_limit(
        self,
        key: str,
        limit: int,
        window: int,
    ) -> tuple[bool, int]:
        """滑动窗口限流.

        Args:
            key: 限流键
            limit: 窗口内最大请求数
            window: 窗口大小（秒）

        Returns:
            (是否允许, 剩余配额)
        """
        assert self._client, "Redis 未连接"
        rate_key = self._make_key(f"rate:{key}")

        pipe = self._client.pipeline()
        pipe.incr(rate_key)
        pipe.expire(rate_key, window)
        results = await pipe.execute()

        current = results[0]
        allowed = current <= limit
        remaining = max(0, limit - current)

        return allowed, remaining
