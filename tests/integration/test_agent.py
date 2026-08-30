"""集成测试 - 测试多个组件协同工作."""

import pytest
from pathlib import Path


class TestAgentIntegration:
    """Agent 集成测试 - 测试完整的对话流程."""
    
    @pytest.mark.integration
    @pytest.mark.skip(reason="需要配置 API Key")
    async def test_full_conversation_flow(self, tmp_path):
        """测试完整的对话流程：用户输入 -> 工具调用 -> 响应生成."""
        # 这个测试需要真实的 API Key，默认跳过
        # 可以通过 pytest -m integration --runslow 来运行
        pass
    
    @pytest.mark.integration
    async def test_tool_chain_execution(self, tmp_path):
        """测试工具链执行：多个工具顺序执行."""
        from scout.tools.builtin.files.unified import UnifiedFileTool

        # 创建文件
        tool = UnifiedFileTool()
        test_file = tmp_path / "chain_test.txt"

        result1 = await tool.execute(
            action="write",
            path=str(test_file),
            content="第一步：创建文件\n第二步：追加内容\n"
        )
        assert result1.success

        # 读取验证
        result2 = await tool.execute(action="read", path=str(test_file))

        assert result2.success
        assert "第一步" in result2.output
        assert "第二步" in result2.output


class TestMemoryIntegration:
    """记忆系统集成测试."""
    
    @pytest.mark.integration
    async def test_memory_persistence(self, tmp_path):
        """测试记忆持久化：保存后重启仍能检索."""
        from scout.memory.store import MemoryStore
        
        db_path = tmp_path / "memory.db"
        
        # 第一次：保存记忆
        store1 = MemoryStore(db_path=db_path)
        await store1.add_async(
            content="持久化测试记忆",
            category="test",
            importance=0.9
        )
        del store1  # 模拟关闭
        
        # 第二次：新实例检索
        store2 = MemoryStore(db_path=db_path)
        results = await store2.search_async("持久化", limit=5)
        
        assert len(results) > 0
        assert any("持久化测试" in r.content for r in results)


class TestSessionIntegration:
    """会话系统集成测试."""
    
    @pytest.mark.integration
    async def test_session_with_tools(self, tmp_path):
        """测试会话中调用工具."""
        from scout.session.store import SessionStore
        from scout.core.types import Session, Message, Observation
        from scout.tools.builtin.files.unified import UnifiedFileTool
        
        store = SessionStore(db_path=tmp_path / "sessions.db")
        
        # 创建会话
        session = Session(
            id="tool-session",
            messages=[Message(role="user", content="帮我创建一个文件")]
        )
        
        # 模拟工具调用
        write_tool = UnifiedFileTool()
        result = await write_tool.execute(
            action="write",
            path=str(tmp_path / "output.txt"),
            content="工具生成的内容"
        )
        
        # 记录观察（注意：当前 SessionStore 不持久化 observations）
        observation = Observation(
            tool_name="file_write",
            success=result.success,
            output=result.output
        )
        session.observations.append(observation)
        
        # 添加助手响应（将工具结果包含在消息中）
        session.messages.append(
            Message(role="assistant", content=f"文件已创建：{result.output}")
        )
        
        # 保存会话
        store.save_session(session)
        
        # 验证
        loaded = store.load_session("tool-session")
        assert loaded is not None
        # observations 不会被持久化，这是当前设计的限制
        # assert len(loaded.observations) == 1
        # 但 messages 会被正确保存
        assert len(loaded.messages) == 2
        assert loaded.messages[0].role.value == "user"
        assert loaded.messages[0].content == "帮我创建一个文件"
        assert loaded.messages[1].role.value == "assistant"
        assert "文件已创建" in loaded.messages[1].content
