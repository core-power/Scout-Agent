"""回调面 — 借鉴 Hermes 的 8 种回调，让 Agent 执行过程对用户可见."""

from __future__ import annotations

from typing import Any, Protocol


class Callbacks(Protocol):
    """平台无关的回调接口 — 不同平台实现不同 UI 反馈."""

    async def on_tool_progress(self, tool_name: str, stage: str, message: str) -> None: ...

    async def on_thinking(self, started: bool) -> None: ...

    async def on_reasoning(self, content: str) -> None: ...

    async def on_clarify(self, question: str) -> str: ...

    async def on_step(self, step: int, total_budget: int) -> None: ...

    async def on_stream_delta(self, text: str) -> None: ...

    async def on_tool_gen(self, tool_name: str, args: dict) -> None: ...

    async def on_status(self, status: str) -> None: ...

    async def on_reflection(self, hint: str) -> None: ...

    async def on_goals_extracted(self, goals: list[dict]) -> None: ...

    async def on_confirm(self, request_id: str, tool_name: str, args: dict, reason: str) -> bool: ...

    async def on_file(self, file_path: str, file_name: str = "", file_size: int = 0) -> None: ...


class NullCallbacks:
    """空实现 — 无 UI 反馈时使用."""

    async def on_tool_progress(self, tool_name: str, stage: str, message: str, metadata: dict | None = None) -> None:
        pass

    async def on_thinking(self, started: bool) -> None:
        pass

    async def on_reasoning(self, content: str) -> None:
        pass

    async def on_clarify(self, question: str) -> str:
        return ""

    async def on_step(self, step: int, total_budget: int) -> None:
        pass

    async def on_stream_delta(self, text: str) -> None:
        pass

    async def on_tool_gen(self, tool_name: str, args: dict) -> None:
        pass

    async def on_status(self, status: str) -> None:
        pass

    async def on_reflection(self, hint: str) -> None:
        pass

    async def on_goals_extracted(self, goals: list[dict]) -> None:
        pass

    async def on_confirm(self, request_id: str, tool_name: str, args: dict, reason: str) -> bool:
        return True  # 默认批准

    async def on_file(self, file_path: str, file_name: str = "", file_size: int = 0) -> None:
        pass


class TaggedCallbacks:
    """给回调事件打上 Agent 身份标签，前端可区分主/子代理编排过程.

    multi_agent 模式：主 agent（orchestrator）与子 agent（executor）都通过
    同一个 WebCallbacks 推送到前端，但事件里带上 agent_role/agent_name：
      - 主 agent:  role="main"   name="主代理"
      - 子 agent:  role="sub"    name="子代理-调研" 等
    前端据此渲染「编排」与「执行」的不同视觉样式.

    包装所有方法，转发时在 metadata/args 注入标签；不修改协议签名.
    若 inner 已是 TaggedCallbacks，则复用其链尾并替换角色（避免多层嵌套）.
    """

    def __init__(self, inner: Any, agent_role: str = "main", agent_name: str = "main", delegation_id: str | None = None):
        # 解嵌套：如果 inner 是 TaggedCallbacks，取它的真实 inner 并覆盖标签
        if isinstance(inner, TaggedCallbacks):
            self._inner = inner._inner
        else:
            self._inner = inner
        self.agent_role = agent_role
        self.agent_name = agent_name
        self.delegation_id = delegation_id

    def _tag_metadata(self, metadata: dict | None) -> dict:
        md = dict(metadata or {})
        md["agent_role"] = self.agent_role
        md["agent_name"] = self.agent_name
        if self.delegation_id:
            md["delegation_id"] = self.delegation_id
        return md

    async def on_tool_progress(self, tool_name: str, stage: str, message: str, metadata: dict | None = None) -> None:
        fn = getattr(self._inner, "on_tool_progress", None)
        if fn:
            return await fn(tool_name, stage, message, self._tag_metadata(metadata))

    async def on_tool_gen(self, tool_name: str, args: dict) -> None:
        fn = getattr(self._inner, "on_tool_gen", None)
        if fn:
            tagged_args = dict(args or {})
            tagged_args.setdefault("_agent", {"role": self.agent_role, "name": self.agent_name})
            if self.delegation_id:
                tagged_args["_agent"]["delegation_id"] = self.delegation_id
                tagged_args["_delegation"] = {"id": self.delegation_id}
            return await fn(tool_name, tagged_args)

    async def on_thinking(self, started: bool) -> None:
        fn = getattr(self._inner, "on_thinking", None)
        if fn:
            return await fn(started)

    async def on_status(self, status: str) -> None:
        fn = getattr(self._inner, "on_status", None)
        if fn:
            return await fn(status)

    async def on_reasoning(self, content: str) -> None:
        fn = getattr(self._inner, "on_reasoning", None)
        if fn:
            # 子代理（role=sub）的推理内容打上身份前缀，前端可路由到对应子代理卡片
            if self.agent_role == "sub" and content:
                content = f"[{self.agent_name}] {content}"
            return await fn(content)

    async def on_step(self, step: int, total_budget: int) -> None:
        fn = getattr(self._inner, "on_step", None)
        if fn:
            return await fn(step, total_budget)

    async def on_stream_delta(self, text: str) -> None:
        fn = getattr(self._inner, "on_stream_delta", None)
        if fn:
            return await fn(text)

    async def on_clarify(self, question: str) -> str:
        fn = getattr(self._inner, "on_clarify", None)
        if fn:
            return await fn(question)
        return ""

    async def on_reflection(self, hint: str) -> None:
        fn = getattr(self._inner, "on_reflection", None)
        if fn:
            return await fn(hint)

    async def on_goals_extracted(self, goals: list[dict]) -> None:
        fn = getattr(self._inner, "on_goals_extracted", None)
        if fn:
            return await fn(goals)

    async def on_confirm(self, request_id: str, tool_name: str, args: dict, reason: str) -> bool:
        fn = getattr(self._inner, "on_confirm", None)
        if fn:
            return await fn(request_id, tool_name, args, reason)
        return True

    async def on_file(self, file_path: str, file_name: str = "", file_size: int = 0) -> None:
        fn = getattr(self._inner, "on_file", None)
        if fn:
            return await fn(file_path, file_name, file_size)
