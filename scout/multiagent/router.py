"""多 Agent 路由 — 借鉴 OpenClaw 的 Bindings 路由机制.

将不同渠道/用户/群组路由到不同 Agent，实现多 Agent 隔离。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from scout.core.types import Message


@dataclass
class Binding:
    """路由绑定规则 — 匹配消息来源到 Agent."""

    agent_id: str
    channel: str | None = None       # 渠道名: telegram / wechat / web
    peer_id: str | None = None       # 用户/群组 ID
    content_pattern: str | None = None  # 内容正则匹配

    def matches(self, message: Message) -> bool:
        """检查消息是否匹配此绑定."""
        if self.channel and message.source != self.channel:
            return False
        if self.peer_id and message.sender != self.peer_id:
            return False
        if self.content_pattern and not re.search(self.content_pattern, message.content):
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "channel": self.channel,
            "peer_id": self.peer_id,
            "content_pattern": self.content_pattern,
        }


class AgentRouter:
    """Agent 路由器 — 根据消息来源路由到不同 Agent."""

    def __init__(self):
        self._bindings: list[Binding] = []
        self._agents: dict[str, Any] = {}  # agent_id -> Agent

    def register_agent(self, agent_id: str, agent: Any) -> None:
        """注册 Agent."""
        self._agents[agent_id] = agent

    def unregister_agent(self, agent_id: str) -> None:
        """注销 Agent（子代理执行完毕后调用，防止泄漏）."""
        self._agents.pop(agent_id, None)

    def agent_count(self) -> int:
        """当前注册的 Agent 数量."""
        return len(self._agents)

    def add_binding(self, binding: Binding) -> None:
        """添加路由绑定."""
        self._bindings.append(binding)

    def remove_binding(self, index: int) -> None:
        """移除路由绑定."""
        if 0 <= index < len(self._bindings):
            self._bindings.pop(index)

    def route(self, message: Message) -> Any | None:
        """根据消息路由到 Agent."""
        for binding in self._bindings:
            if binding.matches(message):
                agent = self._agents.get(binding.agent_id)
                if agent:
                    return agent
        return self._agents.get("default")

    def get_agent(self, agent_id: str) -> Any | None:
        """按 ID 获取 Agent."""
        return self._agents.get(agent_id)

    def list_agents(self) -> list[dict]:
        """列出所有注册的 Agent（含 specialty，供 coordinator 任务分解参考）."""
        return [
            {
                "id": aid,
                "type": type(a).__name__,
                "specialty": getattr(a, "specialty", "") or "通用",
            }
            for aid, a in self._agents.items()
        ]

    def list_bindings(self) -> list[dict]:
        """列出所有路由绑定."""
        return [b.to_dict() for b in self._bindings]
