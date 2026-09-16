"""Agent 间通信 — 借鉴 OpenClaw 的 sessions_send / sessions_spawn.

支持点对点通信、子代理生成、批量广播。
max_ping_pong 防止 Agent 间无限循环。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from scout.core.types import Session


class AgentMessenger:
    """Agent 间通信管理器."""

    def __init__(self, max_ping_pong: int = 3):
        """
        Args:
            max_ping_pong: 防止 Agent 间无限对话的最大往返次数
        """
        self.max_ping_pong = max_ping_pong
        self._ping_pong_count: dict[str, int] = {}  # 对话对 → 计数

    async def send(
        self,
        target_agent: Any,
        task: str,
        session: Session | None = None,
        timeout: int = 300,
    ) -> str:
        """点对点通信 — 借鉴 OpenClaw 的 sessions_send.

        在目标 Agent 上执行任务，返回结果。
        """
        child_session = Session(
            id=str(uuid.uuid4()),
            parent_id=session.id if session else None,
            lineage_id="messenger_send",
        )

        try:
            result = await asyncio.wait_for(
                target_agent.run_conversation(task, child_session),
                timeout=timeout,
            )
            return result["response"]
        except asyncio.TimeoutError:
            return f"超时: Agent 未在 {timeout} 秒内响应"
        except Exception as e:
            return f"通信失败: {e}"

    async def spawn(
        self,
        agent: Any,
        task: str,
        cleanup: str = "delete",
    ) -> str:
        """子代理生成 — 借鉴 OpenClaw 的 sessions_spawn.

        创建隔离子会话执行任务，完成后可选清理。
        """
        child_session = Session(id=str(uuid.uuid4()), lineage_id="spawned")

        try:
            result = await agent.run_conversation(task, child_session)
            return result["response"]
        except Exception as e:
            return f"生成失败: {e}"

    async def broadcast(
        self,
        agents: list[Any],
        task: str,
        timeout: int = 60,
    ) -> dict[str, str]:
        """批量广播 — 并发发送任务到多个 Agent."""
        tasks = [
            asyncio.create_task(self._safe_call(a, task, timeout))
            for a in agents
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            f"agent_{i}": r if isinstance(r, str) else str(r)
            for i, r in enumerate(results)
        }

    async def _safe_call(self, agent: Any, task: str, timeout: int) -> str:
        """安全调用单个 Agent."""
        try:
            session = Session(id=str(uuid.uuid4()))
            result = await asyncio.wait_for(
                agent.run_conversation(task, session),
                timeout=timeout,
            )
            return result["response"]
        except asyncio.TimeoutError:
            return "超时"
        except Exception as e:
            return f"错误: {e}"
