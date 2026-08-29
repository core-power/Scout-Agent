"""子代理委派 — 借鉴 Hermes 的 delegate_task 设计.

Agent 可以将子任务委派给隔离子代理执行，获取结果后继续主流程。

修复记录 (2026-08-01):
- 子代理继承主 Agent 的工具（排除委派类工具防止无限递归）
- 新增委派深度限制（max_delegate_depth），防止无限嵌套
- 子代理优先使用 executor 模型（省钱）
- 子代理不覆盖 ToolRegistry._main_agent

接线记录 (2026-08-21):
- 提取 build_sub_agent() 共享函数，delegate_task / parallel_delegate / collaborate_task 复用
- 子代理注册进全局 AgentRouter（scout.multiagent.runtime），委派期间可被
  MultiAgentCoordinator / AgentMessenger 按 ID 获取，执行完毕自动注销
"""

from __future__ import annotations

import uuid
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.callbacks import TaggedCallbacks
from scout.core.types import Observation, Session
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# 子代理不应拥有的工具（防止无限递归委派 / 循环协作）
DELEGATE_TOOLS = {"delegate_task", "parallel_delegate", "collaborate_task"}

# 子代理注册到全局 router 的 ID 前缀
_ROUTER_PREFIX = "delegate:"


def build_sub_agent(
    agent: Any,
    *,
    task_context: str = "",
    sub_name: str = "子代理",
    delegation_id: str | None = None,
    current_depth: int = 0,
    max_depth: int = 2,
    max_turns: int = 6,
    system_prompt: str | None = None,
    router_suffix: str = "",
) -> Any:
    """构建隔离子代理（delegate_task / parallel_delegate / collaborate_task 共用）.

    - 继承主 Agent 的工具与安全配置（HITL / auto_approve / automation_policy）
    - 注册进全局 AgentRouter，委派期间可被 MultiAgentCoordinator / AgentMessenger
      按 ID 获取；调用方负责在结束后 unregister（防止泄漏）

    Args:
        system_prompt: 自定义子代理提示词（默认使用精简通用提示词）
        router_suffix: 并行批次内多个子代理共用一个 delegation_id 时，
                       用后缀区分注册 ID（如第 i 个子代理传 str(i)）

    Returns:
        已注册的子 Agent 实例（router_id = f"delegate:{delegation_id}[:suffix]"）
    """
    from scout.engine.agent import Agent
    from scout.multiagent.runtime import get_router

    sub_system_prompt = system_prompt or (
        "You are a focused sub-agent. Complete the assigned task efficiently using available tools.\n"
        "Use tools (shell, web_search, read_file, etc.) to gather information and produce results.\n"
        "Be concise and return a clear, actionable result.\n"
        "Respond in the same language as the task description.\n"
    )
    if task_context:
        sub_system_prompt += f"\nAdditional context:\n{task_context}\n"

    _delegation_id = delegation_id or f"dl_{uuid.uuid4().hex[:8]}"
    _router_id = f"{_ROUTER_PREFIX}{_delegation_id}"
    if router_suffix:
        _router_id = f"{_router_id}:{router_suffix}"

    sub_agent = Agent(
        llm=agent.llm,
        system_prompt=sub_system_prompt,
        max_turns=max_turns,
        temperature=0.3,  # 子代理用低温度，更确定性
        deep_thinking=False,  # 子代理不需要深度思考
        enable_persistence=False,
        enable_memory=False,
        # ── 安全继承：与主 Agent 一致，子代理危险操作同样经过 HITL 用户确认 ──
        enable_security=agent.enable_security,
        auto_approve=agent.security.auto_approve if agent.security else False,
        enable_hitl=agent.enable_hitl,
        hitl_tools=list(agent.hitl_tools) if agent.hitl_tools else None,
        enable_skills=False,
        enable_workspace=False,
        enable_bus=False,
        enable_context=False,
        # 子代理事件打上 sub 标签，前端区分「编排」(main) 与「执行」(sub)
        callbacks=TaggedCallbacks(
            agent.callbacks,
            agent_role="sub",
            agent_name=sub_name,
            delegation_id=_delegation_id,
        ),
        # ── 委派控制 ──
        delegate_depth=current_depth + 1,
        max_delegate_depth=max_depth,
        exclude_tools=DELEGATE_TOOLS,
        register_as_main=False,  # 不覆盖主 Agent 引用
    )
    # 继承自动化策略：自动化运行时子代理同样跳过 HITL，由 AutomationPolicy 门控
    sub_agent.automation_policy = getattr(agent, "automation_policy", None)
    # 注册进全局 router（coordinator / messenger 可访问）
    get_router().register_agent(_router_id, sub_agent)
    return sub_agent


def unregister_sub_agent(delegation_id: str, router_suffix: str = "") -> None:
    """从全局 router 注销子代理（委派结束，防止泄漏）."""
    from scout.multiagent.runtime import get_router

    _router_id = f"{_ROUTER_PREFIX}{delegation_id}"
    if router_suffix:
        _router_id = f"{_router_id}:{router_suffix}"
    get_router().unregister_agent(_router_id)


class DelegateTaskTool(ToolDefinition):
    """子代理委派工具 — 将子任务交给隔离子代理执行."""

    name = "delegate_task"
    description = (
        "将一个子任务委派给隔离子代理执行。子代理拥有独立的会话和工具（shell、搜索、文件等），"
        "执行完成后返回结果。适用于：复杂任务分解、多步调研、代码生成等需要工具链的子任务。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "委派给子代理的任务描述",
            },
            "context": {
                "type": "string",
                "description": "给子代理的上下文信息（可选）",
            },
        },
        "required": ["task"],
    }
    annotations = ToolAnnotations(read_only=False, open_world=False)

    async def execute(self, task: str, context: str = "") -> Observation:
        # 从工具注册表获取主 agent
        agent = getattr(ToolRegistry, "_main_agent", None)
        if not agent:
            return Observation(
                tool_name="delegate_task",
                success=False,
                output="无法委派：主 Agent 未注册",
            )

        # ── 深度限制检查 ──
        current_depth = getattr(agent, "delegate_depth", 0)
        max_depth = getattr(agent, "max_delegate_depth", 2)
        if current_depth >= max_depth:
            return Observation(
                tool_name="delegate_task",
                success=False,
                output=f"无法委派：已达到最大委派深度 ({max_depth})，请直接使用工具完成任务",
            )

        try:
            # 创建隔离子会话
            sub_session = Session(
                id=str(uuid.uuid4()),
                parent_id=None,
                lineage_id="delegated",
            )

            # 构建委派 prompt
            prompt = task
            if context:
                prompt = f"上下文: {context}\n\n任务: {task}"

            # 子代理名（前端展示）：任务摘要前 20 字
            _task_summary = (task or "").strip().replace("\n", " ")[:20]
            _sub_name = f"子代理-{_task_summary}" if _task_summary else "子代理"
            # 委派唯一 ID：每次 delegate_task 一个独立子代理卡片
            _delegation_id = f"dl_{uuid.uuid4().hex[:8]}"
            # 创建子 Agent — 继承工具但排除委派类工具，防止无限递归
            sub_agent = build_sub_agent(
                agent,
                task_context=context,
                sub_name=_sub_name,
                delegation_id=_delegation_id,
                current_depth=current_depth,
                max_depth=max_depth,
            )

            try:
                result = await sub_agent.run_conversation(prompt, sub_session)
                return Observation(
                    tool_name="delegate_task",
                    success=True,
                    output=f"子代理执行完成 (步数: {result['steps']}):\n{result['response'][:3000]}",
                )
            finally:
                unregister_sub_agent(_delegation_id)
        except Exception as e:
            return Observation(
                tool_name="delegate_task",
                success=False,
                output=f"子代理执行失败: {e}",
            )


# import 时自动注册
ToolRegistry.register(DelegateTaskTool())


def _register_collaborate() -> None:
    """注册协作编排工具（延迟导入，避免 collaborate 反向 import 本模块时未加载完）."""
    from scout.tools.builtin.delegate.collaborate import CollaborateTaskTool

    ToolRegistry.register(CollaborateTaskTool())


_register_collaborate()
