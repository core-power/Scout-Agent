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

import json
import uuid
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.callbacks import TaggedCallbacks
from scout.core.types import Observation, Session
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# 子代理不应拥有的工具：
# - 委派类：防止无限递归委派 / 循环协作
# - ask_user（2026-09-23）：子代理不直接向用户弹澄清卡片（弹窗无 sub 上下文，
#   用户分不清是哪个代理在问）——歧义写进结论交回主代理，由主代理统一澄清
DELEGATE_TOOLS = {"delegate_task", "parallel_delegate", "collaborate_task", "ask_user"}

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
    # comm tools hint (2026-09-07): sub_report / shared_data
    sub_system_prompt += (
        "\nCommunication tools available to you:\n"
        "- sub_report(kind, content): report progress/finding/blocker to the "
        "main agent mid-run (1-4 per task, key findings only - your full "
        "transcript is discarded, but reports are kept).\n"
        "- shared_data(action, key, value): exchange data with sibling "
        "subagents of this delegation batch.\n"
        "Use sub_report before finishing if you discovered something the "
        "main agent needs; use shared_data for sibling handoff.\n"
    )

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
        enable_security=agent.enable_security,
        auto_approve=agent.security.auto_approve if agent.security else False,
        # 继承主 Agent 的权限模式（输入框开关），子代理执行标准一致
        permission_mode=getattr(agent.security, "permission_mode", "ask") if agent.security else "ask",
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
                # 2026-09-09：接线委派上下文（sub_report/shared_data 归属判定）
                from scout.multiagent.runtime import (
                    reset_current_delegation,
                    set_current_delegation,
                )

                _ctx_tok = set_current_delegation(_delegation_id, _sub_name)
                try:
                    result = await sub_agent.run_conversation(prompt, sub_session)
                finally:
                    reset_current_delegation(_ctx_tok)
                # comm digest: mid-run sub_report messages (2026-09-07)
                from scout.multiagent.broker import digest_reports
                from scout.multiagent.runtime import get_broker

                _dg = digest_reports(get_broker().drain(_delegation_id))
                if _dg:
                    result["response"] = _dg + "\n\n" + result["response"]
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


# ── subagent comm tools (2026-09-07): effective only inside delegated subagents



_KINDS = ("progress", "finding", "blocker")

def _delegation_context():
    """(delegation_id, sub_name) of the calling sub-agent.

    ★ 2026-09-09：改读 ContextVar —— 此前读 ToolRegistry._main_agent_holder
    （全代码库从未赋值）→ sub_report/shared_data 恒被判"不在子代理内"，
    子代理内部通讯整链路死亡。ContextVar 任务级隔离，并行委派互不串扰。
    """
    try:
        from scout.multiagent.runtime import current_delegation

        cur = current_delegation()
        if cur:
            return cur
    except Exception:  # noqa: BLE001
        pass
    # 兼容旧 holder 路径（如有外部接线）
    holder = getattr(ToolRegistry, "_main_agent_holder", None)
    agent = getattr(holder, "agent", None) if holder else None
    cb = getattr(agent, "callbacks", None)
    return (
        getattr(cb, "delegation_id", None),
        getattr(cb, "agent_name", None) or "sub",
    )


class SubReportTool(ToolDefinition):
    """Report mid-run status to the main agent (broker buffered)."""

    name = "sub_report"
    pure_read = True
    description = (
        "Report mid-run status to the main agent. kind=progress (stage done), "
        "finding (key fact the main agent WILL see even though your full "
        "transcript is discarded after the task), blocker (need a decision or "
        "missing input). Use sparingly: 1-4 per task."
    )
    parameters = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["progress", "finding", "blocker"],
            },
            "content": {
                "type": "string",
                "description": "One line. Max 160 chars kept.",
            },
        },
        "required": ["kind", "content"],
    }
    annotations = ToolAnnotations(read_only_hint=True)

    async def execute(self, kind: str = "progress", content: str = "") -> Observation:
        from scout.multiagent.broker import SubReport
        from scout.multiagent.runtime import get_broker

        delegation_id, sub_name = _delegation_context()
        if not delegation_id:
            return Observation(
                tool_name=self.name,
                success=False,
                output="sub_report is only available inside delegated subagents",
            )
        content = (content or "").strip()
        if not content:
            return Observation(tool_name=self.name, success=False, output="content is required")
        if kind not in _KINDS:
            kind = "progress"
        get_broker().publish(
            SubReport(delegation_id=delegation_id, sender=sub_name, kind=kind, content=content)
        )
        return Observation(
            tool_name=self.name,
            success=True,
            output=f"reported {kind}: {content[:80]}",
        )


ToolRegistry.register(SubReportTool())


class SharedDataTool(ToolDefinition):
    """KV exchange between sibling subagents of the SAME delegation batch."""

    name = "shared_data"
    pure_read = False
    description = (
        "Exchange data with SIBLING subagents of the same delegation batch "
        "(parallel_delegate gives all its subagents one shared namespace). "
        "actions: set/get/list/delete. Use: a research subagent stores findings "
        "under a key; a sibling subagent reads them without a main-agent roundtrip."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["set", "get", "list", "delete"],
            },
            "key": {
                "type": "string",
            },
            "value": {
                "type": "string",
                "description": "set: value to store (string, preferably JSON; keep it small).",
            },
        },
        "required": ["action"],
    }
    annotations = ToolAnnotations(read_only_hint=False)

    async def execute(self, action: str = "list", key: str = "", value: str = "") -> Observation:
        from scout.multiagent.runtime import get_shared_state

        delegation_id, _sub = _delegation_context()
        if not delegation_id:
            return Observation(
                tool_name=self.name,
                success=False,
                output="shared_data is only available inside delegated subagents",
            )
        ns = "shared:delegation:" + delegation_id
        sm = get_shared_state()

        if action == "set":
            if not key:
                return Observation(tool_name=self.name, success=False, output="key is required")
            payload = value
            try:
                payload = json.dumps(json.loads(value), ensure_ascii=False)
            except Exception:
                pass
            # SharedStateManager 是扁平键空间：用前缀约定实现委派组命名空间
            await sm.set(f"{ns}:{key}", payload, owner=_delegation_context()[1])
            return Observation(
                tool_name=self.name,
                success=True,
                output=f"stored {ns} :: {key}",
            )

        if action == "get":
            if not key:
                return Observation(tool_name=self.name, success=False, output="key is required")
            data = await sm.get(f"{ns}:{key}")
            if data is None:
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=f"key not found: {key}",
                )
            return Observation(tool_name=self.name, success=True, output=f"{key} = {data}")

        if action == "delete":
            await sm.delete(f"{ns}:{key}")
            return Observation(tool_name=self.name, success=True, output=f"deleted {key}")

        raw_keys = await sm.list_keys(ns + ":")
        keys = [k[len(ns) + 1:] for k in raw_keys]
        return Observation(
            tool_name=self.name,
            success=True,
            output="shared keys: " + (", ".join(keys) if keys else "(empty)"),
        )


ToolRegistry.register(SharedDataTool())
