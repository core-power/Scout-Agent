"""协作编排工具 — 通过 MultiAgentCoordinator 完成「分解 → 并行执行 → 聚合」.

这是 scout/multiagent 协调架构（coordinator / router）的运行时接线点：
- 复用全局 AgentRouter（scout.multiagent.runtime），主 Agent 以 default 身份参与
- 子代理由 build_sub_agent 懒创建并注册，执行结束统一注销（防泄漏）
- 任务由 TaskDecomposer 用 LLM 自动分解，TaskExecutor 按依赖并行执行，
  ResultAggregator 汇总最终答案
"""

from __future__ import annotations

import uuid

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

from scout.multiagent.coordinator import MultiAgentCoordinator
from scout.multiagent.runtime import get_router

from scout.tools.builtin.delegate import build_sub_agent


class CollaborateTaskTool(ToolDefinition):
    """协作编排工具 — 自动分解任务并由多个子代理协作执行."""

    name = "collaborate_task"
    description = (
        "将大型复杂任务自动分解为多个子任务，交由多个子代理协作执行（并行），"
        "再聚合为最终答案。适合：跨领域大型调研、多步骤复杂任务、需要多个视角的任务。\n"
        "与 delegate_task / parallel_delegate 的区别：本工具自动完成「分解→执行→聚合」，"
        "无需手动拆分子任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "需要协作完成的复杂任务描述",
            },
            "auto_decompose": {
                "type": "boolean",
                "description": "是否自动分解为子任务（默认 true；false 时仅用一个子代理执行）",
            },
        },
        "required": ["task"],
    }
    annotations = ToolAnnotations(read_only=False, open_world=False)

    async def execute(self, task: str, auto_decompose: bool = True) -> Observation:
        agent = getattr(ToolRegistry, "_main_agent", None)
        if not agent:
            return Observation(
                tool_name="collaborate_task",
                success=False,
                output="无法协作：主 Agent 未注册",
            )

        router = get_router()
        # 主 Agent 以 default 身份参与协作（幂等注册）
        if router.get_agent("default") is None:
            router.register_agent("default", agent)

        # 记录本次协作 spawn 的子代理 ID，结束后统一注销
        _spawned_ids: list[str] = []

        def _factory(agent_id: str):
            sub = build_sub_agent(
                agent,
                sub_name=f"协作-{agent_id}",
                delegation_id=f"co_{uuid.uuid4().hex[:8]}",
                current_depth=getattr(agent, "delegate_depth", 0),
                max_depth=getattr(agent, "max_delegate_depth", 2),
                max_turns=6,
            )
            _spawned_ids.append(agent_id)
            return sub

        coordinator = MultiAgentCoordinator(router, agent.llm, agent_factory=_factory)
        try:
            result = await coordinator.coordinate(task, auto_decompose=auto_decompose)
            subtask_count = len(result["subtasks"])
            final = result["final_result"]
            return Observation(
                tool_name="collaborate_task",
                success=True,
                output=f"协作完成 ({subtask_count} 个子任务):\n{final[:3000]}",
            )
        except Exception as e:
            return Observation(
                tool_name="collaborate_task",
                success=False,
                output=f"协作执行失败: {e}",
            )
        finally:
            # 清理：注销本次 spawn 的子代理，防止泄漏
            for _aid in _spawned_ids:
                router.unregister_agent(_aid)
            for _rid in list(router._agents):
                if _rid.startswith("delegate:co_"):
                    router.unregister_agent(_rid)


# import 时自动注册（由 delegate/__init__.py 延迟导入）
ToolRegistry.register(CollaborateTaskTool())
