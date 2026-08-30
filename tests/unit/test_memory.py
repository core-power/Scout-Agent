"""记忆存储测试."""

import pytest


class TestMemoryStore:
    """记忆存储测试."""
    
    @pytest.mark.unit
    async def test_memory_add(self, tmp_path):
        """测试添加记忆."""
        from scout.memory.store import MemoryStore
        
        store = MemoryStore(db_path=tmp_path / "memory.db")
        
        memory_id = await store.add_async(
            content="用户喜欢简洁的回复",
            category="preference",
            importance=0.8
        )
        
        assert memory_id is not None
    
    @pytest.mark.unit
    async def test_memory_search(self, tmp_path):
        """测试搜索记忆."""
        from scout.memory.store import MemoryStore
        
        store = MemoryStore(db_path=tmp_path / "memory.db")
        
        # 添加多个记忆
        await store.add_async(
            content="用户喜欢简洁的回复",
            category="preference",
            importance=0.8
        )
        
        await store.add_async(
            content="用户的项目使用 Python 3.11",
            category="fact",
            importance=0.9
        )
        
        await store.add_async(
            content="明天要完成报告",
            category="todo",
            importance=0.7
        )
        
        # 搜索记忆
        results = await store.search_async("简洁", limit=5)
        
        assert len(results) > 0
        assert any("简洁" in r.content for r in results)
    
    @pytest.mark.unit
    async def test_memory_list(self, tmp_path):
        """测试列出记忆."""
        from scout.memory.store import MemoryStore
        
        store = MemoryStore(db_path=tmp_path / "memory.db")
        
        # 添加记忆
        await store.add_async(content="记忆1", category="test")
        await store.add_async(content="记忆2", category="test")
        await store.add_async(content="记忆3", category="test")
        
        # 列出记忆
        memories = store.list_recent(limit=10)
        
        assert len(memories) == 3
    
    @pytest.mark.unit
    async def test_memory_delete(self, tmp_path):
        """测试删除记忆."""
        from scout.memory.store import MemoryStore
        
        store = MemoryStore(db_path=tmp_path / "memory.db")
        
        # 添加记忆
        memory_id = await store.add_async(content="要删除的记忆")
        
        # 删除记忆
        store.delete(memory_id)
        
        # 验证删除
        memories = store.list_recent(limit=10)
        assert not any(m.id == memory_id for m in memories)
