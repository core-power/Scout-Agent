"""A2A 工具 — 跨 Agent 协作（Agent-to-Agent 协议，2026-09-07）.

让 LLM 在 ReAct 循环中直接使用已注册的远程 A2A agent：
- list_agents: 查看已注册的远程 agent
- add_agent / remove_agent: 运行时注册/移除远程 agent（SSRF 校验内置）
- send_task: 把子任务发给远程 agent 并等待其结论
- get_task: 查询远程 agent 上某任务的状态

与 delegate_task（本地子代理）的关系：
- delegate_task = 同机隔离子代理（快、共享本机工具、冷启动 ~1s）
- a2a send_task = 远程独立 agent（跨机器/跨能力，适合对方有本机没有的能力，
  或需要把任务外包给另一台机器上的 agent 分摊负载）
两套委派可叠加：主 agent → 本地子代理 → 远程 A2A agent。

安全（2026-09-07）：
- URL 解析走 A2AClient._assert_url_allowed（SSRF 防护）：
  默认拦截私网/环回/链路本地地址；内网互通需配置 a2a_allow_private=true
- send_task 会让远程 agent 真实执行任务（消耗对方资源），description 已引导
  仅在用户明确要求跨 agent 协作或任务确实超出本机能力时使用
"""

from __future__ import annotations

import asyncio
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry


class A2ATool(ToolDefinition):
    """跨 Agent 协作工具（A2A 协议客户端）."""

    name = "a2a"
    pure_read = False  # send_task 会触发远程 agent 真实执行
    description = (
        "Collaborate with OTHER agent instances over the A2A protocol "
        "(Agent-to-Agent). Actions: list_agents (registered remote agents), "
        "add_agent (register one by URL at runtime), remove_agent, "
        "send_task (send a task to a remote agent, wait for its final "
        "answer), get_task (poll a task's status).\n"
        "WHEN TO USE: the task needs capabilities/authority that live on "
        "another machine's agent (different tools, different data access), "
        "or the user explicitly asks to involve another agent. For local "
        "subtask isolation prefer delegate_task (same machine, faster).\n"
        "send_task blocks until the remote agent finishes (timeout param, "
        "default 120s) - give it a self-contained task description: the "
        "remote agent cannot see this conversation. Long remote tasks may "
        "time out; use get_task with the returned task_id to poll instead."
    )
    annotations = ToolAnnotations(
        title="A2A 跨 Agent 协作",
        read_only_hint=False,
        destructive_hint=True,  # send_task 触发远程执行
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list_agents", "add_agent", "remove_agent", "send_task", "get_task"],
                "description": "The A2A operation to perform.",
            },
            "name": {
                "type": "string",
                "description": "Remote agent name (registration key).",
            },
            "url": {
                "type": "string",
                "description": "add_agent: base URL of the remote A2A endpoint, e.g. http://192.168.1.10:8848.",
            },
            "message": {
                "type": "string",
                "description": "send_task: self-contained task description for the remote agent "
                "(it cannot see this conversation - include goal, constraints, expected output).",
            },
            "task_id": {
                "type": "string",
                "description": "get_task: task id returned by send_task.",
            },
            "timeout": {
                "type": "number",
                "description": "send_task: seconds to wait for the remote result (default 120).",
            },
        },
        "required": ["action"],
    }

    def _manager(self):
        agent = getattr(ToolRegistry, "_main_agent", None)
        return getattr(agent, "a2a_manager", None) if agent else None

    def _client(self, manager, name: str):
        client = (manager.clients or {}).get(name)
        if client is None:
            known = ", ".join((manager.clients or {}).keys()) or "none"
            raise KeyError(f"远程 agent '{name}' 未注册（已注册: {known}）。请先 add_agent。")
        return client

    async def execute(self, **kwargs) -> Observation:
        action = (kwargs.get("action") or "").strip()
        manager = self._manager()
        if manager is None:
            return Observation(tool_name=self.name, success=False, output="A2A 未启用（主 Agent 未初始化）")

        try:
            if action == "list_agents":
                agents = manager.list_agents()
                if not agents:
                    return Observation(tool_name=self.name, success=True, output="尚未注册任何远程 A2A agent。")
                lines = [f"- {a.get('name', '?')}: {a.get('url', '?')}" for a in agents]
                return Observation(tool_name=self.name, success=True, output="已注册的远程 A2A agent:\n" + "\n".join(lines))

            if action == "add_agent":
                name = (kwargs.get("name") or "").strip()
                url = (kwargs.get("url") or "").strip()
                if not name or not url:
                    return Observation(tool_name=self.name, success=False, output="add_agent 需要 name 与 url 参数")
                manager.add_agent(name, url)
                return Observation(
                    tool_name=self.name, success=True,
                    output=f"已注册远程 agent '{name}' -> {url}\n现在可以 send_task 给它了。",
                )

            if action == "remove_agent":
                name = (kwargs.get("name") or "").strip()
                ok = manager.remove_agent(name)
                return Observation(
                    tool_name=self.name, success=ok,
                    output=f"已移除 '{name}'" if ok else f"'{name}' 不存在",
                )

            if action == "send_task":
                name = (kwargs.get("name") or "").strip()
                message = (kwargs.get("message") or "").strip()
                if not name or not message:
                    return Observation(tool_name=self.name, success=False, output="send_task 需要 name 与 message 参数")
                client = self._client(manager, name)
                timeout = float(kwargs.get("timeout") or 120)
                task = await asyncio.wait_for(client.send_task(message), timeout=timeout)
                # 结论在最后一条 role=agent 消息的 text part 里
                answer = ""
                for msg in reversed(task.messages):
                    if msg.role == "agent":
                        answer = "".join(
                            getattr(p, "text", "") for p in msg.parts
                        )
                        break
                state = getattr(task.status, "state", "unknown") if task.status else "unknown"
                if state == "failed":
                    reason = getattr(task.status, "message", "") or ""
                    return Observation(
                        tool_name=self.name, success=False,
                        output=f"远程 agent '{name}' 任务失败: {reason}",
                    )
                tid = f"（task_id={task.id}，可 get_task 查询）" if task.id else ""
                return Observation(
                    tool_name=self.name, success=True,
                    output=f"远程 agent '{name}' 已完成{tid}:\n{answer or '(空响应)'}",
                )

            if action == "get_task":
                name = (kwargs.get("name") or "").strip()
                task_id = (kwargs.get("task_id") or "").strip()
                if not name or not task_id:
                    return Observation(tool_name=self.name, success=False, output="get_task 需要 name 与 task_id 参数")
                client = self._client(manager, name)
                task = await client.get_task(task_id)
                if task is None:
                    return Observation(tool_name=self.name, success=False, output=f"任务 {task_id} 不存在")
                state = getattr(task.status, "state", "?") if task.status else "?"
                return Observation(
                    tool_name=self.name, success=True,
                    output=f"任务 {task_id} 状态: {state}",
                )

            return Observation(tool_name=self.name, success=False, output=f"未知 action: {action}")

        except KeyError as e:
            return Observation(tool_name=self.name, success=False, output=str(e))
        except asyncio.TimeoutError:
            return Observation(
                tool_name=self.name, success=False,
                output=f"远程 agent '{kwargs.get('name', '')}' 在 {kwargs.get('timeout') or 120}s 内未完成。"
                "任务可能仍在后台执行——稍后用 get_task + task_id 轮询结果。",
            )
        except Exception as e:  # noqa: BLE001 — 网络/SSRF/协议错误统一兜底
            return Observation(tool_name=self.name, success=False, output=f"A2A 调用失败: {e}")


ToolRegistry.register(A2ATool())
