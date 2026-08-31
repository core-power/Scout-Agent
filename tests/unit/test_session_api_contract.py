"""SessionStore API 契约回归测试 — 防止核心方法丢失/签名变更.

背景: 2026-08-13 发现 SessionStore 缺失 save_session/load_session 等一整套
方法，但 agent.py/web.py/starlight.py/旧测试全在调用（磁盘代码被覆盖导致）。
本测试固化这些方法的契约，保证未来任何重构都不会再静默丢失。

运行: pytest tests/unit/test_session_api_contract.py -v
"""

import pytest
from datetime import datetime

from scout.core.types import Session, Message, Role


class TestSessionApiContract:
    """会话存储必须提供的方法契约."""

    REQUIRED_METHODS = [
        # 全量读写
        "save_session", "load_session",
        # 列表/改名/删除
        "list_sessions", "rename_session", "delete_session",
        # 搜索/归档
        "search_messages", "archive_messages",
        # 追加/详情
        "append_message", "get_messages", "get_all_messages",
        # 异步版
        "async_save_session", "async_load_session",
        "async_list_sessions", "async_rename_session",
        "async_delete_session", "async_search_messages", "async_archive_messages",
    ]

    def test_required_methods_exist(self):
        """所有调用方依赖的方法必须存在."""
        from scout.session.store import SessionStore

        missing = [m for m in self.REQUIRED_METHODS if not hasattr(SessionStore, m)]
        assert not missing, f"SessionStore 缺失方法: {missing}"

    def test_default_path_points_to_appdata_scout(self, monkeypatch):
        """默认库路径必须指向 %APPDATA%\\Scout\\sessions.db（2026-08-31 主目录优先）."""
        import os
        from pathlib import Path

        from scout.config.paths import PROJECT_ROOT, get_data_dir
        from scout.storage import factory

        monkeypatch.delenv("SCOUT_SQLITE_PATH", raising=False)
        expected = str(get_data_dir() / "sessions.db")
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            if appdata:
                # Windows 首选 %APPDATA%\Scout（修复"更新丢配置"），而非盘符根 .scout
                assert expected == str(Path(appdata) / "Scout" / "sessions.db"), (
                    f"默认 SQLite 路径应为 %APPDATA%\\Scout\\sessions.db, 实际 {expected}"
                )
            else:
                # 无 APPDATA 时回退 <盘符>\.scout
                assert expected == str(Path(PROJECT_ROOT.anchor) / ".scout" / "sessions.db")
        else:
            assert ".scout" in expected, f"默认路径必须包含 .scout: {expected}"
        # 通过 factory 的默认逻辑验证
        backend = factory.get_storage_backend(backend="sqlite", db_path=None)
        assert str(backend._db_path) == expected, (
            f"默认 SQLite 路径应为 {expected}, 实际 {backend._db_path}"
        )

    @pytest.mark.asyncio
    async def test_save_load_roundtrip_async(self, tmp_path):
        """异步 save/load 往返，消息顺序保留."""
        from scout.session.store import SessionStore

        store = SessionStore(db_path=tmp_path / "contract.db")
        session = Session(id="c1", messages=[
            Message(role=Role.USER, content="你好 <runtime_context>..."),
            Message(role=Role.ASSISTANT, content="你好！"),
        ])
        await store.async_save_session(session)

        loaded = await store.async_load_session("c1")
        assert loaded is not None
        assert len(loaded.messages) == 2
        assert loaded.messages[0].role == Role.USER
        assert loaded.messages[0].content == "你好 <runtime_context>..."
        assert loaded.messages[1].content == "你好！"

    def test_rename_delete_list_sync(self, tmp_path):
        """同步 rename/delete/list."""
        from scout.session.store import SessionStore

        store = SessionStore(db_path=tmp_path / "contract2.db")
        s = Session(id="c2")
        store.save_session(s)
        store.rename_session("c2", "测试标题")
        sessions = store.list_sessions()
        assert sessions[0]["title"] == "测试标题"
        assert store.delete_session("c2") is True
        assert store.load_session("c2") is None

    def test_search_messages(self, tmp_path):
        """搜索消息."""
        from scout.session.store import SessionStore

        store = SessionStore(db_path=tmp_path / "contract3.db")
        s = Session(id="c3", messages=[
            Message(role=Role.USER, content="Python 异步编程"),
            Message(role=Role.ASSISTANT, content="asyncio 库"),
        ])
        store.save_session(s)
        results = store.search_messages("asyncio")
        assert len(results) > 0

    def test_archive_messages(self, tmp_path):
        """归档消息."""
        from scout.session.store import SessionStore

        store = SessionStore(db_path=tmp_path / "contract4.db")
        s = Session(id="c4", messages=[Message(role=Role.USER, content="将被归档")])
        store.save_session(s)
        n = store.archive_messages("c4", s.messages, reason="test")
        assert n == 1
        archived = store.get_archive("c4")
        assert len(archived) == 1