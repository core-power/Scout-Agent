"""共享状态管理 - Agent 间的数据共享和同步"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
import logging

logger = logging.getLogger(__name__)


@dataclass
class SharedData:
    """共享数据项"""
    key: str
    value: Any
    version: int = 0
    owner: str = ""  # 创建者 agent_id
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "version": self.version,
            "owner": self.owner,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "metadata": self.metadata,
        }


class SharedStateManager:
    """共享状态管理器 - 线程安全的共享数据存储"""
    
    def __init__(self, persistence_path: str | None = None):
        self._data: dict[str, SharedData] = {}
        self._lock = asyncio.Lock()  # 简化的全局锁
        self._subscribers: dict[str, list[Callable]] = {}  # key -> [callback]
        self._persistence_path = persistence_path
    
    async def get(self, key: str, default: Any = None) -> Any:
        """获取共享数据"""
        async with self._lock:
            data = self._data.get(key)
            if data is None:
                return default
            return data.value
    
    async def set(
        self,
        key: str,
        value: Any,
        owner: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """设置共享数据（返回新版本号）"""
        async with self._lock:
            if key in self._data:
                # 更新现有数据
                old_data = self._data[key]
                new_version = old_data.version + 1
                self._data[key] = SharedData(
                    key=key,
                    value=value,
                    version=new_version,
                    owner=owner or old_data.owner,
                    created_at=old_data.created_at,
                    updated_at=datetime.now(),
                    metadata=metadata or old_data.metadata,
                )
            else:
                # 创建新数据
                new_version = 1
                self._data[key] = SharedData(
                    key=key,
                    value=value,
                    version=new_version,
                    owner=owner,
                    metadata=metadata or {},
                )
            
            # 保存数据副本用于通知
            data_copy = self._data[key]
        
        # 在锁外触发订阅者（避免死锁）
        await self._notify_subscribers(key, data_copy)
        
        # 持久化
        if self._persistence_path:
            await self._persist()
        
        return new_version
    
    async def update(
        self,
        key: str,
        update_fn: Callable[[Any], Any],
        owner: str = "",
    ) -> tuple[int, Any]:
        """原子更新共享数据（返回版本号和更新后的值）"""
        async with self._lock:
            current = self._data.get(key)
            current_value = current.value if current else None
            
            # 执行更新函数
            new_value = update_fn(current_value)
            
            # 直接更新数据（不调用 set 避免死锁）
            if key in self._data:
                old_data = self._data[key]
                new_version = old_data.version + 1
                self._data[key] = SharedData(
                    key=key,
                    value=new_value,
                    version=new_version,
                    owner=owner or old_data.owner,
                    created_at=old_data.created_at,
                    updated_at=datetime.now(),
                    metadata=old_data.metadata,
                )
            else:
                new_version = 1
                self._data[key] = SharedData(
                    key=key,
                    value=new_value,
                    version=new_version,
                    owner=owner,
                    metadata={},
                )
            
            return new_version, new_value
    
    async def compare_and_swap(
        self,
        key: str,
        expected_version: int,
        new_value: Any,
        owner: str = "",
    ) -> bool:
        """CAS 操作 - 仅在版本匹配时更新"""
        async with self._lock:
            current = self._data.get(key)
            
            if current is None and expected_version == 0:
                # 直接更新（不调用 set 避免死锁）
                self._data[key] = SharedData(
                    key=key,
                    value=new_value,
                    version=1,
                    owner=owner,
                    metadata={},
                )
                return True
            elif current and current.version == expected_version:
                # 直接更新（不调用 set 避免死锁）
                new_version = current.version + 1
                self._data[key] = SharedData(
                    key=key,
                    value=new_value,
                    version=new_version,
                    owner=owner or current.owner,
                    created_at=current.created_at,
                    updated_at=datetime.now(),
                    metadata=current.metadata,
                )
                return True
            else:
                return False
    
    async def delete(self, key: str) -> bool:
        """删除共享数据"""
        deleted = False
        async with self._lock:
            if key in self._data:
                del self._data[key]
                deleted = True
        
        if deleted:
            # 在锁外通知订阅者（避免死锁）
            await self._notify_subscribers(key, None)
            
            if self._persistence_path:
                await self._persist()
        
        return deleted
    
    async def list_keys(self, prefix: str = "") -> list[str]:
        """列出所有键（可选前缀过滤）"""
        async with self._lock:
            if prefix:
                return [k for k in self._data.keys() if k.startswith(prefix)]
            else:
                return list(self._data.keys())
    
    async def get_metadata(self, key: str) -> dict | None:
        """获取数据元信息"""
        async with self._lock:
            data = self._data.get(key)
            if data:
                return data.to_dict()
            return None
    
    def subscribe(self, key: str, callback: Callable[[SharedData | None], None]):
        """订阅数据变化"""
        if key not in self._subscribers:
            self._subscribers[key] = []
        self._subscribers[key].append(callback)
    
    def unsubscribe(self, key: str, callback: Callable):
        """取消订阅"""
        if key in self._subscribers:
            self._subscribers[key] = [
                cb for cb in self._subscribers[key] if cb != callback
            ]
    
    async def _notify_subscribers(self, key: str, data: SharedData | None):
        """通知订阅者"""
        if key not in self._subscribers:
            return
        
        for callback in self._subscribers[key]:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)
            except Exception as e:
                logger.error(f"订阅者回调失败: {e}")
    
    async def _persist(self):
        """持久化到磁盘"""
        if not self._persistence_path:
            return
        
        try:
            data = {
                key: shared_data.to_dict()
                for key, shared_data in self._data.items()
            }
            
            with open(self._persistence_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"持久化失败: {e}")
    
    async def load(self):
        """从磁盘加载"""
        if not self._persistence_path:
            return
        
        try:
            with open(self._persistence_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            for key, item in data.items():
                self._data[key] = SharedData(
                    key=item["key"],
                    value=item["value"],
                    version=item["version"],
                    owner=item["owner"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                    updated_at=datetime.fromisoformat(item["updated_at"]),
                    metadata=item["metadata"],
                )
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"加载失败: {e}")
    
    async def snapshot(self) -> dict[str, Any]:
        """获取当前状态快照"""
        async with self._lock:
            return {
                key: data.to_dict()
                for key, data in self._data.items()
            }


class AgentWorkspace:
    """Agent 工作空间 - 每个 Agent 的私有状态 + 共享状态访问"""
    
    def __init__(
        self,
        agent_id: str,
        shared_state: SharedStateManager,
    ):
        self.agent_id = agent_id
        self.shared = shared_state
        self._local: dict[str, Any] = {}
    
    def set_local(self, key: str, value: Any):
        """设置本地状态"""
        self._local[key] = value
    
    def get_local(self, key: str, default: Any = None) -> Any:
        """获取本地状态"""
        return self._local.get(key, default)
    
    async def share(self, key: str, value: Any, metadata: dict | None = None):
        """将数据发布到共享空间"""
        await self.shared.set(key, value, owner=self.agent_id, metadata=metadata)
    
    async def read(self, key: str, default: Any = None) -> Any:
        """从共享空间读取"""
        return await self.shared.get(key, default)
    
    async def update_shared(
        self,
        key: str,
        update_fn: Callable[[Any], Any],
    ) -> tuple[int, Any]:
        """原子更新共享数据"""
        return await self.shared.update(key, update_fn, owner=self.agent_id)
