"""测试多 Agent 协作系统"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from scout.multiagent import (
    AgentRouter,
    Binding,
    AgentMessenger,
    MultiAgentCoordinator,
    TaskDecomposer,
    TaskExecutor,
    ResultAggregator,
    Task,
    TaskStatus,
    WorkflowExecutor,
    WorkflowBuilder,
    Workflow,
    WorkflowStatus,
    SharedStateManager,
    SharedData,
    AgentWorkspace,
)
from scout.multiagent.runtime import get_router, get_messenger, get_shared_state
from scout.core.types import Message, Session


class TestMultiAgentRuntime:
    """运行时单例：router / messenger / shared_state 全局共享."""

    def test_singletons_are_shared(self):
        assert get_router() is get_router()
        assert get_messenger() is get_messenger()
        assert get_shared_state() is get_shared_state()
        assert isinstance(get_router(), AgentRouter)
        assert isinstance(get_messenger(), AgentMessenger)
        assert isinstance(get_shared_state(), SharedStateManager)

    @pytest.mark.asyncio
    async def test_shared_state_read_write(self):
        st = get_shared_state()
        await st.set("runtime-test.k", "v")
        assert await st.get("runtime-test.k") == "v"
        assert "runtime-test.k" in await st.list_keys()
        await st.delete("runtime-test.k")
        assert await st.get("runtime-test.k") is None


class TestMultiAgentCoordinator:
    """测试多 Agent 协调器"""
    
    @pytest.fixture
    def mock_llm(self):
        """创建 Mock LLM"""
        llm = Mock()
        response = Mock()
        response.content = '{"subtasks": [{"description": "子任务1", "agent_id": "agent1"}]}'
        llm.complete = AsyncMock(return_value=response)
        return llm
    
    @pytest.fixture
    def mock_router(self):
        """创建 Mock Router"""
        router = AgentRouter()
        
        # Mock Agent
        mock_agent = Mock()
        mock_agent.run_conversation = AsyncMock(return_value={"response": "测试结果"})
        router.register_agent("agent1", mock_agent)
        router.register_agent("default", mock_agent)
        
        return router
    
    @pytest.mark.asyncio
    async def test_task_decomposer(self, mock_llm):
        """测试任务分解器"""
        decomposer = TaskDecomposer()
        available_agents = [
            {"id": "agent1", "specialty": "通用"},
            {"id": "agent2", "specialty": "代码"},
        ]
        
        tasks = await decomposer.decompose("完成一个复杂任务", mock_llm, available_agents)
        
        assert len(tasks) == 1
        assert tasks[0].description == "子任务1"
        assert tasks[0].agent_id == "agent1"
        mock_llm.complete.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_task_executor(self, mock_router):
        """测试任务执行器"""
        executor = TaskExecutor(mock_router)
        
        task = Task(id="task1", description="测试任务", agent_id="agent1")
        result = await executor.execute(task)
        
        assert result == "测试结果"
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "测试结果"
    
    @pytest.mark.asyncio
    async def test_result_aggregator(self, mock_llm):
        """测试结果聚合器"""
        aggregator = ResultAggregator()
        
        subtasks = [
            Task(id="task1", description="子任务1", result="结果1"),
            Task(id="task2", description="子任务2", result="结果2"),
        ]
        
        response = Mock()
        response.content = "聚合后的最终答案"
        mock_llm.complete = AsyncMock(return_value=response)
        
        result = await aggregator.aggregate("原始任务", subtasks, mock_llm)
        
        assert result == "聚合后的最终答案"
    
    @pytest.mark.asyncio
    async def test_coordinator(self, mock_router, mock_llm):
        """测试协调器完整流程"""
        coordinator = MultiAgentCoordinator(mock_router, mock_llm)
        
        # Mock 分解结果
        decompose_response = Mock()
        decompose_response.content = '{"subtasks": [{"description": "执行任务", "agent_id": "agent1"}]}'
        
        # Mock 聚合结果
        aggregate_response = Mock()
        aggregate_response.content = "最终答案"
        
        mock_llm.complete = AsyncMock(side_effect=[decompose_response, aggregate_response])
        
        result = await coordinator.coordinate("测试任务", auto_decompose=True)
        
        assert "final_result" in result
        assert "subtasks" in result
        assert "execution_time" in result
        assert result["final_result"] == "最终答案"


class TestWorkflow:
    """测试工作流系统"""
    
    @pytest.fixture
    def mock_router(self):
        """创建 Mock Router"""
        router = AgentRouter()
        mock_agent = Mock()
        mock_agent.run_conversation = AsyncMock(return_value={"response": "工作流结果"})
        router.register_agent("agent1", mock_agent)
        router.register_agent("default", mock_agent)
        return router
    
    @pytest.fixture
    def mock_llm(self):
        """创建 Mock LLM"""
        return Mock()
    
    def test_workflow_builder_sequential(self):
        """测试顺序工作流构建"""
        tasks = [
            {"name": "步骤1", "description": "第一步"},
            {"name": "步骤2", "description": "第二步"},
            {"name": "步骤3", "description": "第三步"},
        ]
        
        workflow = WorkflowBuilder.sequential("顺序工作流", tasks)
        
        assert len(workflow.nodes) == 3
        nodes = list(workflow.nodes.values())
        assert len(nodes[0].dependencies) == 0
        assert len(nodes[1].dependencies) == 1
        assert len(nodes[2].dependencies) == 1
    
    def test_workflow_builder_parallel(self):
        """测试并行工作流构建"""
        tasks = [
            {"name": "任务A", "description": "并行任务A"},
            {"name": "任务B", "description": "并行任务B"},
        ]
        
        workflow = WorkflowBuilder.parallel("并行工作流", tasks)
        
        assert len(workflow.nodes) == 2
        for node in workflow.nodes.values():
            assert len(node.dependencies) == 0
    
    def test_workflow_builder_fan_out_fan_in(self):
        """测试扇出-扇入工作流构建"""
        parallel_tasks = [
            {"name": "并行1", "description": "并行任务1"},
            {"name": "并行2", "description": "并行任务2"},
        ]
        final_task = {"name": "汇总", "description": "汇总任务"}
        
        workflow = WorkflowBuilder.fan_out_fan_in("扇出扇入", parallel_tasks, final_task)
        
        assert len(workflow.nodes) == 3
        nodes = list(workflow.nodes.values())
        # 最后一个是汇总节点，依赖前两个
        assert len(nodes[-1].dependencies) == 2
    
    @pytest.mark.asyncio
    async def test_workflow_executor(self, mock_router, mock_llm):
        """测试工作流执行"""
        executor = WorkflowExecutor(mock_router, mock_llm)
        
        workflow = WorkflowBuilder.sequential("测试工作流", [
            {"name": "步骤1", "description": "第一步", "agent_id": "agent1"},
            {"name": "步骤2", "description": "第二步", "agent_id": "agent1"},
        ])
        
        result = await executor.execute(workflow)
        
        assert result.status == WorkflowStatus.COMPLETED
        assert all(
            node.status == WorkflowStatus.COMPLETED
            for node in result.nodes.values()
        )


class TestSharedState:
    """测试共享状态管理"""
    
    @pytest.mark.asyncio
    async def test_basic_operations(self):
        """测试基本读写操作"""
        manager = SharedStateManager()
        
        # 设置
        version = await manager.set("key1", "value1", owner="agent1")
        assert version == 1
        
        # 读取
        value = await manager.get("key1")
        assert value == "value1"
        
        # 更新
        version = await manager.set("key1", "value2", owner="agent1")
        assert version == 2
        
        value = await manager.get("key1")
        assert value == "value2"
    
    @pytest.mark.asyncio
    async def test_atomic_update(self):
        """测试原子更新"""
        manager = SharedStateManager()
        
        await manager.set("counter", 0)
        
        version, value = await manager.update("counter", lambda x: (x or 0) + 1)
        
        assert version == 2
        assert value == 1
    
    @pytest.mark.asyncio
    async def test_compare_and_swap(self):
        """测试 CAS 操作"""
        manager = SharedStateManager()
        
        await manager.set("key1", "value1")
        
        # 成功 CAS
        success = await manager.compare_and_swap("key1", 1, "value2")
        assert success
        
        # 失败 CAS（版本不匹配）
        success = await manager.compare_and_swap("key1", 1, "value3")
        assert not success
        
        value = await manager.get("key1")
        assert value == "value2"
    
    @pytest.mark.asyncio
    async def test_subscriber(self):
        """测试订阅机制"""
        manager = SharedStateManager()
        
        received_data = []
        
        async def callback(data):
            received_data.append(data)
        
        manager.subscribe("key1", callback)
        
        await manager.set("key1", "value1")
        await manager.set("key1", "value2")
        
        assert len(received_data) == 2
        assert received_data[0].value == "value1"
        assert received_data[1].value == "value2"
    
    @pytest.mark.asyncio
    async def test_agent_workspace(self):
        """测试 Agent 工作空间"""
        shared = SharedStateManager()
        workspace = AgentWorkspace("agent1", shared)
        
        # 本地状态
        workspace.set_local("local_key", "local_value")
        assert workspace.get_local("local_key") == "local_value"
        
        # 共享状态
        await workspace.share("shared_key", "shared_value")
        value = await workspace.read("shared_key")
        assert value == "shared_value"
        
        # 原子更新
        version, value = await workspace.update_shared(
            "counter",
            lambda x: (x or 0) + 10
        )
        assert value == 10


class TestIntegration:
    """集成测试"""
    
    @pytest.mark.asyncio
    async def test_multi_agent_with_shared_state(self):
        """测试多 Agent 使用共享状态协作"""
        # 创建共享状态
        shared = SharedStateManager()
        
        # 创建多个工作空间
        workspace1 = AgentWorkspace("agent1", shared)
        workspace2 = AgentWorkspace("agent2", shared)
        
        # Agent1 写入
        await workspace1.share("result", {"step": 1, "data": "初始数据"})
        
        # Agent2 读取并更新
        result = await workspace2.read("result")
        assert result["step"] == 1
        
        await workspace2.update_shared(
            "result",
            lambda x: {**x, "step": 2, "processed": True}
        )
        
        # Agent1 读取更新后的值
        updated = await workspace1.read("result")
        assert updated["step"] == 2
        assert updated["processed"] is True
    
    @pytest.mark.asyncio
    async def test_workflow_with_shared_state(self):
        """测试工作流结合共享状态"""
        shared = SharedStateManager()
        
        # 初始化共享状态
        await shared.set("progress", 0)
        
        async def task1():
            await shared.update("progress", lambda x: x + 33)
            return "完成阶段1"
        
        async def task2():
            await shared.update("progress", lambda x: x + 33)
            return "完成阶段2"
        
        async def task3():
            await shared.update("progress", lambda x: x + 34)
            return "完成阶段3"
        
        # 创建路由和执行器
        router = AgentRouter()
        llm = Mock()
        executor = WorkflowExecutor(router, llm)
        
        # 构建工作流
        workflow = Workflow(id="test", name="测试")
        node1 = workflow.add_node("任务1", task_fn=task1)
        node2 = workflow.add_node("任务2", task_fn=task2, dependencies=[node1.id])
        node3 = workflow.add_node("任务3", task_fn=task3, dependencies=[node2.id])
        
        # 执行
        result = await executor.execute(workflow)
        
        assert result.status == WorkflowStatus.COMPLETED
        
        # 检查进度
        progress = await shared.get("progress")
        assert progress == 100


class TestDelegateHITLInheritance:
    """子代理委派的安全继承：enable_hitl / auto_approve / hitl_tools / automation_policy 必须与主 Agent 一致."""

    @staticmethod
    def _make_main_agent(**overrides):
        """构造一个轻量主 Agent 桩，具备 delegate_task 需要的属性."""
        agent = Mock()
        agent.llm = Mock()
        agent.callbacks = Mock()
        agent.callbacks.on_confirm = AsyncMock(return_value=True)
        agent.delegate_depth = 0
        agent.max_delegate_depth = 2
        agent.enable_security = overrides.get("enable_security", True)
        agent.security = overrides.get("security", Mock(auto_approve=False))
        agent.enable_hitl = overrides.get("enable_hitl", True)
        agent.hitl_tools = overrides.get("hitl_tools", {"shell", "execute_code"})
        agent.automation_policy = overrides.get("automation_policy", None)
        return agent

    @pytest.mark.asyncio
    async def test_delegate_sub_agent_inherits_hitl(self, monkeypatch):
        """子代理继承主 Agent 的 HITL：enable_hitl/auto_approve/hitl_tools/enable_security."""
        from scout.tools.builtin.delegate import DelegateTaskTool
        from scout.tools.registry import ToolRegistry

        captured = []
        router_ids_seen = []

        async def fake_run_conversation(self, prompt, session):
            captured.append(self)
            # 执行期间：子代理应已注册进全局 router（coordinator / messenger 可访问）
            from scout.multiagent.runtime import get_router
            router_ids_seen.append(list(get_router()._agents.keys()))
            return {"response": "ok", "steps": 1}

        main = self._make_main_agent()
        monkeypatch.setattr(ToolRegistry, "_main_agent", main, raising=False)
        with patch("scout.engine.agent.Agent.run_conversation", fake_run_conversation):
            obs = await DelegateTaskTool().execute(task="调研天气")
        assert obs.success is True
        assert captured, "子代理未创建"
        sub = captured[0]
        assert sub.enable_hitl is True
        assert sub.enable_security is True
        assert sub.hitl_tools == {"shell", "execute_code"}
        assert sub.security.auto_approve is False
        assert sub.automation_policy is None
        # 子代理不应再拥有委派/协作工具（防止无限递归与循环协作）
        assert sub._exclude_tools == {"delegate_task", "parallel_delegate", "collaborate_task"}
        # 执行期间注册进 router，执行完毕注销（防泄漏）
        from scout.multiagent.runtime import get_router
        assert any("delegate:dl_" in rid for rid in router_ids_seen[0])
        assert all("delegate:dl_" not in rid for rid in get_router()._agents)

    @pytest.mark.asyncio
    async def test_delegate_inherits_auto_approve_and_policy(self, monkeypatch):
        """主 Agent auto_approve=True 或带 automation_policy 时子代理同样继承."""
        from scout.tools.builtin.delegate import DelegateTaskTool
        from scout.tools.registry import ToolRegistry

        captured = []

        async def fake_run_conversation(self, prompt, session):
            captured.append(self)
            return {"response": "ok", "steps": 1}

        main = self._make_main_agent(
            security=Mock(auto_approve=True),
            enable_hitl=False,
            automation_policy=Mock(),
        )
        monkeypatch.setattr(ToolRegistry, "_main_agent", main, raising=False)
        with patch("scout.engine.agent.Agent.run_conversation", fake_run_conversation):
            obs = await DelegateTaskTool().execute(task="跑脚本")
        assert obs.success is True
        sub = captured[0]
        assert sub.enable_hitl is False  # 主 Agent 关闭 HITL，子代理同样关闭
        assert sub.security.auto_approve is True
        assert sub.automation_policy is not None  # 自动化策略继承，不挂起确认

    @pytest.mark.asyncio
    async def test_parallel_sub_agents_inherit_hitl(self, monkeypatch):
        """并行委派的每个子代理同样继承 HITL 配置."""
        from scout.tools.builtin.parallel import ParallelDelegateTool
        from scout.tools.registry import ToolRegistry

        captured = []

        async def fake_run_conversation(self, prompt, session):
            captured.append(self)
            return {"response": "ok", "steps": 1}

        main = self._make_main_agent()
        monkeypatch.setattr(ToolRegistry, "_main_agent", main, raising=False)
        with patch("scout.engine.agent.Agent.run_conversation", fake_run_conversation):
            obs = await ParallelDelegateTool().execute(
                tasks=[{"task": "任务A", "label": "A"}, {"task": "任务B", "label": "B"}]
            )
        assert obs.success is True
        assert len(captured) == 2
        for sub in captured:
            assert sub.enable_hitl is True
            assert sub.enable_security is True
            assert sub.hitl_tools == {"shell", "execute_code"}
            assert sub.security.auto_approve is False


class TestCollaborateTask:
    """collaborate_task 工具端到端：coordinator 分解→并行执行→聚合→清理."""

    @staticmethod
    def _make_main_agent(llm):
        agent = Mock()
        agent.llm = llm
        agent.callbacks = Mock()
        agent.callbacks.on_confirm = AsyncMock(return_value=True)
        agent.delegate_depth = 0
        agent.max_delegate_depth = 2
        agent.enable_security = True
        agent.security = Mock(auto_approve=False)
        agent.enable_hitl = True
        agent.hitl_tools = {"shell", "execute_code"}
        agent.automation_policy = None
        return agent

    @pytest.mark.asyncio
    async def test_coordinate_end_to_end(self, monkeypatch):
        """完整链路：自动分解为 2 个子任务 → 懒建子代理并行执行 → 聚合."""
        from scout.tools.builtin.delegate.collaborate import CollaborateTaskTool
        from scout.tools.registry import ToolRegistry

        sub_calls = []
        captured_subagents = []

        async def fake_run_conversation(self, prompt, session):
            captured_subagents.append(self)
            sub_calls.append(prompt)
            return {"response": f"子任务结果 {len(sub_calls)}", "steps": 1}

        llm = Mock()
        import json
        decomposed = {
            "subtasks": [
                {"description": "调研市场", "agent_id": "sub-agent-1", "dependencies": []},
                {"description": "编写方案", "agent_id": "sub-agent-2", "dependencies": []},
            ]
        }
        llm.complete = AsyncMock(
            side_effect=lambda msgs: Mock(
                content=(
                    json.dumps(decomposed)
                    if "分解" in msgs[0]["content"]
                    else "综合后的最终答案"
                )
            )
        )

        main = self._make_main_agent(llm)
        router = get_router()
        monkeypatch.setattr(ToolRegistry, "_main_agent", main, raising=False)
        # 清空 router 历史注册，保证可复现
        for _rid in list(router._agents):
            router.unregister_agent(_rid)

        with patch("scout.engine.agent.Agent.run_conversation", fake_run_conversation):
            obs = await CollaborateTaskTool().execute(
                task="写一份市场分析报告", auto_decompose=True
            )

        assert obs.success is True, obs.output
        assert "协作完成 (2 个子任务)" in obs.output
        assert "综合后的最终答案" in obs.output
        assert len(sub_calls) == 2  # 两个子任务都执行了
        assert len(captured_subagents) == 2
        # 子代理安全继承
        for sub in captured_subagents:
            assert sub.enable_hitl is True
            assert sub.enable_security is True
        # 清理完成：router 中不再有本次协作的子代理
        assert not any(rid.startswith("delegate:co_") for rid in router._agents)
        assert not any(rid.startswith("sub-agent") for rid in router._agents)

    @pytest.mark.asyncio
    async def test_no_decompose_single_subagent(self, monkeypatch):
        """auto_decompose=False 时仅一个子代理执行."""
        from scout.tools.builtin.delegate.collaborate import CollaborateTaskTool
        from scout.tools.registry import ToolRegistry

        sub_calls = []

        async def fake_run_conversation(self, prompt, session):
            sub_calls.append(prompt)
            return {"response": "直接结果", "steps": 1}

        llm = Mock()
        llm.complete = AsyncMock(return_value=Mock(content="直接结果"))

        main = self._make_main_agent(llm)
        # 不分解时单任务由 default（主 Agent）执行，模拟其 async run_conversation
        main.run_conversation = AsyncMock(return_value={"response": "直接结果", "steps": 1})
        router = get_router()
        monkeypatch.setattr(ToolRegistry, "_main_agent", main, raising=False)
        for _rid in list(router._agents):
            router.unregister_agent(_rid)

        with patch("scout.engine.agent.Agent.run_conversation", fake_run_conversation):
            obs = await CollaborateTaskTool().execute(
                task="简单任务", auto_decompose=False
            )

        assert obs.success is True, obs.output
        assert len(sub_calls) == 0  # 不分解：由主 Agent 直接执行
        assert main.run_conversation.await_count == 1
        assert not any(rid.startswith("delegate:co_") for rid in router._agents)


class TestGatewayMultiAgentWiring:
    """Gateway 接线：route_message / collaborate 使 router / messenger 真正被使用."""

    def _make_gateway(self, monkeypatch):
        from scout.gateway.control import Gateway
        from scout.config import LLMConfig

        gw = Gateway(config=LLMConfig())  # api_key 空 → 不创建 default agent
        return gw

    def test_route_message_falls_back_to_default(self, monkeypatch):
        gw = self._make_gateway(monkeypatch)
        fake_agent = Mock()
        gw._agents["default"] = fake_agent
        gw.router.register_agent("default", fake_agent)
        result = gw.route_message("你好，帮我查个东西")
        assert result["routed_to"] == "default"
        assert result["agent_count"] >= 1
        assert isinstance(result["bindings"], list)

    @pytest.mark.asyncio
    async def test_collaborate_broadcasts_via_messenger(self, monkeypatch):
        gw = self._make_gateway(monkeypatch)
        fake_agent = Mock()
        fake_agent.run_conversation = AsyncMock(return_value={"response": "结果A", "steps": 1})
        gw._agents["default"] = fake_agent
        gw._agents["worker"] = Mock()
        gw._agents["worker"].run_conversation = AsyncMock(
            return_value={"response": "结果B", "steps": 1}
        )
        result = await gw.collaborate(["default", "worker"], "汇总数据")
        assert result["success"] is True
        assert len(result["results"]) == 2
        # 无可用 agent 时优雅失败
        result2 = await gw.collaborate(["ghost"], "无")
        assert result2["success"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
