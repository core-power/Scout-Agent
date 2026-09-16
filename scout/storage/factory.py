"""存储后端工厂 — 根据配置自动选择存储后端.

支持通过环境变量或配置文件切换 SQLite / PostgreSQL / Redis。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from scout.storage.base import CacheBackend, StorageBackend
from scout.storage.postgres import PostgresStorage
from scout.storage.redis_cache import RedisCache
from scout.storage.sqlite import SQLiteStorage

logger = logging.getLogger("scout.storage.factory")

# 全局单例（仅默认配置）
_storage: StorageBackend | None = None
_cache: CacheBackend | None = None
# 显式指定 db_path 的独立实例（避免与全局单例冲突）
_storage_by_path: dict[str, StorageBackend] = {}


def get_storage_backend(
    backend: str | None = None,
    **kwargs: Any,
) -> StorageBackend:
    """获取关系型存储后端.

    Args:
        backend: 后端类型 ("sqlite" / "postgres")，默认从环境变量读取
        **kwargs: 传递给存储后端的额外参数

    Returns:
        StorageBackend 实例
    """
    global _storage

    db_path = kwargs.get("db_path")

    # 显式指定 db_path → 独立实例（测试/临时库），不做全局单例
    if db_path:
        key = str(db_path)
        if key in _storage_by_path:
            return _storage_by_path[key]
        inst = SQLiteStorage(db_path=key)
        _storage_by_path[key] = inst
        logger.info(f"存储后端: SQLite (独立实例 path={key})")
        return inst

    backend = backend or os.getenv("SCOUT_STORAGE_BACKEND", "sqlite")

    # SPI 每次动态解析（插件可装卸，不受全局单例短路）
    if backend == "spi":
        # 插件 SPI（2026-08-27）：backend="spi" 时使用插件注册的存储实现。
        # 插件侧 provide("storage") 返回可调用对象（类或工厂），按 impl(**kwargs) 实例化。
        from scout.plugins.spi import SPI_KIND_STORAGE, get_provider

        impl = get_provider(SPI_KIND_STORAGE)
        if impl is None:
            raise ValueError(
                "存储 SPI 未注册：backend='spi' 但无插件提供 'storage' 实现。"
                "请加载声明 provides=['storage'] 的插件，或改用内置后端。"
            )
        return impl(**kwargs) if callable(impl) else impl

    if _storage is not None:
        return _storage

    if backend == "postgres":
        dsn = kwargs.get("dsn") or os.getenv(
            "SCOUT_PG_DSN",
            "postgresql://scout:scout@localhost:5432/scout",
        )
        read_dsn = kwargs.get("read_dsn") or os.getenv("SCOUT_PG_READ_DSN")
        min_size = int(kwargs.get("min_size") or os.getenv("SCOUT_PG_MIN_SIZE", "5"))
        max_size = int(kwargs.get("max_size") or os.getenv("SCOUT_PG_MAX_SIZE", "20"))

        _storage = PostgresStorage(
            dsn=dsn,
            read_dsn=read_dsn,
            min_size=min_size,
            max_size=max_size,
        )
        logger.info(f"存储后端: PostgreSQL (dsn={dsn[:50]}...)")

    elif backend == "sqlite":
        db_path = kwargs.get("db_path") or os.getenv(
            "SCOUT_SQLITE_PATH",
            str(_SCOUT_DATA_DIR / "sessions.db"),
        )
        _storage = SQLiteStorage(db_path=db_path)
        logger.info(f"存储后端: SQLite (path={db_path})")

    else:
        raise ValueError(f"不支持的存储后端: {backend}")

    return _storage


def get_cache_backend(
    backend: str | None = None,
    **kwargs: Any,
) -> CacheBackend | None:
    """获取缓存后端.

    Args:
        backend: 后端类型 ("redis" / "none")，默认从环境变量读取
        **kwargs: 传递给缓存后端的额外参数

    Returns:
        CacheBackend 实例，如果 backend="none" 则返回 None
    """
    global _cache

    backend = backend or os.getenv("SCOUT_CACHE_BACKEND", "none")

    # SPI 每次动态解析（插件可装卸，不受全局单例短路）
    if backend == "spi":
        # 插件 SPI（2026-08-27）：插件提供 cache 实现（实现 CacheBackend 接口）。
        from scout.plugins.spi import SPI_KIND_CACHE, get_provider

        impl = get_provider(SPI_KIND_CACHE)
        if impl is None:
            raise ValueError(
                "缓存 SPI 未注册：backend='spi' 但无插件提供 'cache' 实现。"
                "请加载声明 provides=['cache'] 的插件，或改用内置后端。"
            )
        return impl(**kwargs) if callable(impl) else impl

    if _cache is not None:
        return _cache

    if backend == "redis":
        url = kwargs.get("url") or os.getenv(
            "SCOUT_REDIS_URL", "redis://localhost:6379"
        )
        prefix = kwargs.get("prefix") or os.getenv("SCOUT_REDIS_PREFIX", "scout:")
        db = int(kwargs.get("db") or os.getenv("SCOUT_REDIS_DB", "0"))

        # Sentinel 支持
        sentinel_hosts_str = os.getenv("SCOUT_REDIS_SENTINEL_HOSTS")
        sentinel_master = os.getenv("SCOUT_REDIS_SENTINEL_MASTER", "mymaster")

        if sentinel_hosts_str:
            # 格式: "host1:port1,host2:port2"
            sentinel_hosts = []
            for host_port in sentinel_hosts_str.split(","):
                host, port = host_port.strip().split(":")
                sentinel_hosts.append((host, int(port)))

            _cache = RedisCache(
                sentinel_hosts=sentinel_hosts,
                sentinel_master=sentinel_master,
                prefix=prefix,
                db=db,
            )
            logger.info(f"缓存后端: Redis Sentinel (master={sentinel_master})")
        else:
            _cache = RedisCache(url=url, prefix=prefix, db=db)
            logger.info(f"缓存后端: Redis (url={url})")

    elif backend == "none":
        logger.info("缓存后端: 未启用")
        return None

    else:
        raise ValueError(f"不支持的缓存后端: {backend}")

    return _cache


def reset_backends():
    """重置全局单例（用于测试）."""
    global _storage, _cache, _storage_by_path
    _storage = None
    _cache = None
    _storage_by_path = {}


async def init_backends():
    """初始化所有后端连接."""
    storage = get_storage_backend()
    await storage.connect()

    cache = get_cache_backend()
    if cache:
        await cache.connect()


async def close_backends():
    """关闭所有后端连接."""
    global _storage, _cache

    if _storage:
        await _storage.disconnect()
        _storage = None

    if _cache:
        await _cache.disconnect()
        _cache = None
