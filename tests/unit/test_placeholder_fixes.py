"""占位实现修复回归测试 — 2026-08-21.

覆盖：
1. MCP 工具由真实客户端注册（替换演示用假实现）
2. env_config keyring 模式的 list_all 索引
3. 记忆同步向量检索（_vector_search 不再恒空）
4. 钉钉 Stream 消息解析入队
5. 微信/飞书发送走真实 API（非 TODO 占位）
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest


class TestMCPRealImplementation:
    """MCP 真实实现注册与 API."""

    @pytest.mark.unit
    def test_mcp_tool_registered(self):
        """真实 mcp 工具已注册，假 mcp_tool 已移除."""
        import scout.tools.builtin.mcp  # noqa: F401 触发注册
        from scout.tools.registry import ToolRegistry

        names = set(ToolRegistry.all_tools())
        assert "mcp" in names
        assert "mcp_tool" not in names  # 假实现已移除

    @pytest.mark.unit
    def test_mcp_manager_api_complete(self):
        """mcp_manager 提供 add/remove/list/register API（web.py 依赖）. """
        from scout.tools.mcp import mcp_manager

        for method in ("add_server", "remove_server", "list_servers", "register_server", "get_server"):
            assert hasattr(mcp_manager, method), f"缺少 {method}"

    @pytest.mark.unit
    def test_mcp_tool_annotations_fields(self):
        """注解使用正确字段名（read_only 而非 readOnlyHint）. """
        from scout.tools.mcp import MCPTool

        assert MCPTool.annotations.read_only is True
        assert getattr(MCPTool.annotations, "read_only", None) is not None

    @pytest.mark.unit
    async def test_mcp_tool_unregistered_server_fails_gracefully(self):
        """调用未注册 server 返回失败 Observation，不抛异常."""
        from scout.tools.mcp import MCPTool

        tool = MCPTool()
        obs = await tool.execute(server_name="nonexistent", tool_name="any")
        assert obs.success is False
        assert "未注册" in obs.output


class TestEnvConfigKeyringIndex:
    """env_config keyring 模式的 key 索引."""

    @pytest.mark.unit
    def test_keyring_list_all_uses_index(self, monkeypatch):
        """keyring 模式 save 后 list_all 能列出（此前恒为空）."""
        # 内存 keyring mock
        store: dict[tuple, str] = {}

        class FakeKeyring:
            @staticmethod
            def set_password(service, username, password):
                store[(service, username)] = password

            @staticmethod
            def get_password(service, username):
                return store.get((service, username))

            @staticmethod
            def delete_password(service, username):
                store.pop((service, username), None)

        import sys

        monkeypatch.setitem(sys.modules, "keyring", FakeKeyring)
        monkeypatch.setattr("scout.tools.builtin.env_config._KEYRING_INDEX_SERVICE", "idx-test")
        monkeypatch.setattr("scout.tools.builtin.env_config._KEYRING_INDEX_USER", "keys")

        from scout.tools.builtin.env_config import EnvStore

        env = EnvStore()
        assert env._use_keyring is True  # 自动检测到 keyring
        assert env.save("TEST_KEY_A", "secret-a", description="desc a")
        assert env.save("TEST_KEY_B", "secret-b")

        keys = env.list_all()
        names = {k["key"] for k in keys}
        assert "TEST_KEY_A" in names
        assert "TEST_KEY_B" in names
        desc = next(k for k in keys if k["key"] == "TEST_KEY_A")
        assert desc["description"] == "desc a"

        assert env.get("TEST_KEY_A") == "secret-a"
        assert env.delete("TEST_KEY_A") is True
        names = {k["key"] for k in env.list_all()}
        assert "TEST_KEY_A" not in names
        assert "TEST_KEY_B" in names


class TestMemorySyncVectorSearch:
    """同步向量检索修复（此前 _vector_search 恒返回空）."""

    @pytest.mark.unit
    async def test_vector_search_sync_works(self, tmp_path):
        """同步 _vector_search 在注入 embedding provider 后能语义检索."""
        from scout.memory.store import MemoryStore

        store = MemoryStore(db_path=tmp_path / "mem.db")

        # 注入假 embedding provider
        class FakeProvider:
            async def embed(self, text: str) -> np.ndarray:
                # 简单词袋向量：query 含 "python" 则命中对应记忆
                vec = np.zeros(8, dtype=np.float32)
                if "python" in text:
                    vec[0] = 1.0
                if "报告" in text:
                    vec[1] = 1.0
                return vec

        store.set_embedding_provider(FakeProvider())

        # 添加带 embedding 的记忆
        await store.add_async(
            content="用户使用 python 开发",
            category="fact",
            importance=0.9,
        )
        # 手动生成并写入 embedding
        conn = store._get_conn()
        rows = conn.execute("SELECT id, content FROM memories WHERE embedding IS NULL").fetchall()
        for r in rows:
            vec = await FakeProvider().embed(r["content"])
            conn.execute(
                "UPDATE memories SET embedding=? WHERE id=?",
                (vec.astype(np.float32).tobytes(), r["id"]),
            )
        conn.commit()
        store._load_vector_index()
        assert store._vector_index is not None

        # 同步向量检索应返回结果
        results = store._vector_search("python", limit=5)
        assert len(results) >= 1
        assert any("python" in r.content for r in results)


class TestDingTalkStreamParsing:
    """钉钉 Stream 消息解析（此前为 pass 占位）."""

    @pytest.mark.unit
    async def test_stream_message_parsed(self):
        from scout.adapters.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter({})

        class FakeStreamMessage:
            data = {
                "text": {"content": "你好，测试"},
                "senderStaffId": "staff_1",
                "conversationId": "conv_1",
                "msgId": "msg_1",
            }

        await adapter._handle_stream_message(FakeStreamMessage())
        msg = await asyncio.wait_for(adapter._message_queue.get(), timeout=2)
        assert msg.content == "你好，测试"
        assert msg.sender == "staff_1"
        assert msg.session_id == "conv_1"
        assert msg.source == "dingtalk"

    @pytest.mark.unit
    async def test_stream_own_message_ignored(self):
        from scout.adapters.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter({})

        class OwnMessage:
            data = {
                "text": {"content": "自己发的"},
                "senderStaffId": "staff_1",
                "conversationId": "conv_1",
                "msgId": "msg_2",
                "my_msg": True,
            }

        await adapter._handle_stream_message(OwnMessage())
        assert adapter._message_queue.empty()


class TestWeChatRealSend:
    """微信发送走真实客服消息 API（此前为 TODO 占位）."""

    @pytest.mark.unit
    async def test_send_message_calls_custom_api(self, monkeypatch):
        from scout.adapters.platforms.wechat import WeChatAdapter

        adapter = WeChatAdapter({"app_id": "app1", "app_secret": "sec1"})
        calls = []

        async def fake_get(url, params=None, **kw):
            return type("R", (), {"json": lambda self: {"access_token": "tok1", "expires_in": 7200}})()

        async def fake_post(url, params=None, json=None, **kw):
            calls.append((url, params, json))
            return type("R", (), {"json": lambda self: {"errcode": 0}})()

        class _AsyncCtx:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, *a, **kw):
                return await fake_get(*a, **kw)

            async def post(self, *a, **kw):
                return await fake_post(*a, **kw)

        async def _aenter(self):
            return _AsyncCtx()

        async def _aexit(self, *a):
            return None

        fake_client = type(
            "C",
            (),
            {
                "__init__": lambda self, *a, **kw: None,
                "__aenter__": _aenter,
                "__aexit__": _aexit,
            },
        )
        monkeypatch.setattr("scout.adapters.platforms.wechat.httpx.AsyncClient", fake_client)
        ok = await adapter.send_message("user_openid", "测试消息")
        assert ok is True
        # 应调用真实微信 API
        assert any("api.weixin.qq.com" in c[0] for c in calls)
        post = calls[-1]
        assert post[2]["touser"] == "user_openid"
        assert post[2]["msgtype"] == "text"
        assert post[2]["text"]["content"] == "测试消息"

    @pytest.mark.unit
    async def test_send_message_without_creds_fails(self, monkeypatch):
        from scout.adapters.platforms.wechat import WeChatAdapter

        adapter = WeChatAdapter({})
        ok = await adapter.send_message("user", "hi")
        assert ok is False


class TestFeishuRealSendFile:
    """飞书 send_file 走真实上传+发送（此前为 TODO 占位）."""

    @pytest.mark.unit
    async def test_send_file_uploads_then_sends(self, tmp_path, monkeypatch):
        from scout.adapters.platforms.feishu import FeishuAdapter

        f = tmp_path / "test.txt"
        f.write_text("hello")
        adapter = FeishuAdapter({"app_id": "app1", "app_secret": "sec1"})
        adapter._tenant_access_token = "tok1"
        calls = []

        class _Ctx:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, url, headers=None, json=None, data=None, files=None):
                calls.append((url, headers, json, data, files))
                if "files" in url:
                    return type("R", (), {"json": lambda self: {"data": {"file_key": "fk1"}}})()
                return type("R", (), {"status_code": 200})()

        async def _aenter(self):
            return _Ctx()

        async def _aexit(self, *a):
            return None

        fake_client = type(
            "C",
            (),
            {
                "__init__": lambda self, *a, **kw: None,
                "__aenter__": _aenter,
                "__aexit__": _aexit,
            },
        )
        monkeypatch.setattr("scout.adapters.platforms.feishu.httpx.AsyncClient", fake_client)

        ok = await adapter.send_file("open_id_1", str(f))
        assert ok is True
        # 两次调用：上传 + 发送
        assert len(calls) == 2
        assert "im/v1/files" in calls[0][0]
        assert "im/v1/messages" in calls[1][0]
        assert calls[1][2]["msg_type"] == "file"
        assert '"file_key": "fk1"' in calls[1][2]["content"]
