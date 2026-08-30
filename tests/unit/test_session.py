"""会话存储测试."""

import pytest
from datetime import datetime
from scout.core.types import Session, Message


class TestSessionStore:
    """会话存储测试."""
    
    @pytest.mark.unit
    def test_session_save_and_load(self, tmp_path):
        """测试保存和加载会话."""
        from scout.session.store import SessionStore
        
        store = SessionStore(db_path=tmp_path / "sessions.db")
        
        # 创建会话
        session = Session(
            id="test-session-1",
            agent_id="default",
            messages=[
                Message(role="user", content="你好"),
                Message(role="assistant", content="你好！有什么可以帮助你的吗？")
            ]
        )
        
        # 保存会话
        store.save_session(session)
        
        # 加载会话
        loaded = store.load_session("test-session-1")
        
        assert loaded is not None
        assert loaded.id == "test-session-1"
        assert len(loaded.messages) == 2
        assert loaded.messages[0].content == "你好"
        assert loaded.messages[1].content == "你好！有什么可以帮助你的吗？"
    
    @pytest.mark.unit
    def test_session_list(self, tmp_path):
        """测试列出会话."""
        from scout.session.store import SessionStore
        
        store = SessionStore(db_path=tmp_path / "sessions.db")
        
        # 创建多个会话
        for i in range(1, 4):
            session = Session(
                id=f"session-{i}",
                agent_id="default",
                messages=[Message(role="user", content=f"消息 {i}")]
            )
            store.save_session(session)
        
        # 列出会话
        sessions = store.list_sessions(limit=10)
        
        assert len(sessions) == 3
        session_ids = [s["id"] for s in sessions]
        assert "session-1" in session_ids
        assert "session-2" in session_ids
        assert "session-3" in session_ids
    
    @pytest.mark.unit
    def test_session_rename(self, tmp_path):
        """测试重命名会话."""
        from scout.session.store import SessionStore
        
        store = SessionStore(db_path=tmp_path / "sessions.db")
        
        # 创建会话
        session = Session(id="test-rename")
        store.save_session(session)
        
        # 重命名
        store.rename_session("test-rename", "新标题")
        
        # 验证
        sessions = store.list_sessions(limit=10)
        renamed = [s for s in sessions if s["id"] == "test-rename"]
        assert len(renamed) == 1
        assert renamed[0]["title"] == "新标题"
    
    @pytest.mark.unit
    def test_session_delete(self, tmp_path):
        """测试删除会话."""
        from scout.session.store import SessionStore
        
        store = SessionStore(db_path=tmp_path / "sessions.db")
        
        # 创建会话
        session = Session(id="to-delete")
        store.save_session(session)
        
        # 删除会话
        store.delete_session("to-delete")
        
        # 验证删除
        loaded = store.load_session("to-delete")
        assert loaded is None
    
    @pytest.mark.unit
    def test_session_search(self, tmp_path):
        """测试搜索会话消息."""
        from scout.session.store import SessionStore
        
        store = SessionStore(db_path=tmp_path / "sessions.db")
        
        # 创建带消息的会话
        session = Session(
            id="search-test",
            messages=[
                Message(role="user", content="Python 异步编程"),
                Message(role="assistant", content="asyncio 是 Python 的异步 I/O 库"),
                Message(role="user", content="如何使用？")
            ]
        )
        store.save_session(session)
        
        # 搜索
        results = store.search_messages("asyncio")
        
        assert len(results) > 0
        assert any("asyncio" in r["content"] for r in results)
