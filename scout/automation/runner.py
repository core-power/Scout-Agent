"""自动化执行器 — 无人值守任务的统一执行入口.

把 Agent、AutomationPolicy、RunStore、TaskVerifier、TriggerManager
组装成完整的无人值守运行栈：

    TriggerManager（事件/webhook/级联触发）
            ↓ fire()
    AutomationRunner.run_task()
            ↓ 复制 Agent + 应用 AutomationPolicy
    Agent.run_conversation()（工具调用受策略门控）
            ↓
    TaskVerifier.verify()（Done State + 证据）
            ↓
    RunStore.finish_run()（留痕） → bus("task.complete") → 级联
"""

from __future__ import annotations

import copy
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class AutomationRunner:
    """无人值守执行器."""

    def __init__(self, agent: Any, bus: Any = None):
        from scout.engine.runs import RunStore
        from scout.engine.verifier import TaskVerifier
        from scout.security.automation_policy import AutomationPolicyManager
        from scout.automation.triggers import TriggerManager

        self.agent = agent
        self.bus = bus or getattr(agent, "bus", None)
        self.policy_mgr = AutomationPolicyManager()
        self.run_store = RunStore()
        self.verifier = TaskVerifier(llm_client=getattr(agent, "llm", None))
        self.trigger_mgr = TriggerManager(
            bus=self.bus,
            run_store=self.run_store,
            verifier=self.verifier,
            policy_manager=self.policy_mgr,
        )
        self.trigger_mgr.set_agent_runner(self.run_task)

    def attach(self) -> None:
        """挂载事件订阅（服务启动时调用）."""
        self.trigger_mgr.attach_to_bus()

    async def run_task(self, task: str, meta: dict | None = None) -> dict:
        """执行一次自动化任务.

        Args:
            task: 任务描述（已渲染的文本）
            meta: {"run_id", "trigger_id", "trigger_type", "automated", "event"}
                  run_id 已存在时（来自 TriggerManager）不再重复开记录

        Returns:
            {"response", "steps", "tool_calls", "session_id", "status"}
        """
        meta = meta or {}
        if not self.agent:
            return {"status": "error", "response": "Agent 未配置", "steps": 0, "tool_calls": 0}

        run_id = meta.get("run_id", "")
        if not run_id:
            run_id = self.run_store.start_run(
                source=meta.get("trigger_type", "manual"),
                task=task,
                trigger_id=meta.get("trigger_id", ""),
            )
            meta["run_id"] = run_id

        # 复制 Agent — 注入自动化策略，关闭交互式审批
        from scout.core.callbacks import NullCallbacks
        from scout.core.types import Session

        agent_copy = copy.copy(self.agent)
        agent_copy.callbacks = NullCallbacks()
        agent_copy.automation_policy = self.policy_mgr.get_policy()
        agent_copy.auto_run_meta = dict(meta)
        # 无人值守不做交互式审批（策略门控 + 危险命令硬拦截兜底）
        if getattr(agent_copy, "security", None):
            agent_copy.security.auto_approve = True

        session = Session(id=str(uuid.uuid4()))
        meta["session_id"] = session.id

        try:
            result = await agent_copy.run_conversation(task, session)
            response = result.get("response", "")
            steps = result.get("steps", 0)

            # 统计工具调用数
            tool_calls = sum(
                1 for m in session.messages
                if getattr(m, "role", None) and getattr(m.role, "value", "") == "tool"
            ) if hasattr(session, "messages") else 0
            try:
                tool_calls = len(getattr(session, "observations", []))
            except Exception:
                pass

            status = "success"
            if session.status == "error":
                status = "failed"

            out = {
                "status": status,
                "response": response,
                "steps": steps,
                "tool_calls": tool_calls,
                "session_id": session.id,
                "run_id": run_id,
            }

            # 直接调用（非触发器路径）在此收尾；触发器路径由 TriggerManager 收尾
            if not meta.get("trigger_id"):
                self.run_store.finish_run(
                    run_id, status=status, steps=steps,
                    tool_calls=tool_calls, response_summary=response[:3000],
                )
            return out
        except Exception as e:
            logger.exception(f"自动化任务执行异常: {e}")
            if not meta.get("trigger_id"):
                self.run_store.finish_run(run_id, status="failed", response_summary=f"异常: {e}")
            return {
                "status": "failed",
                "response": f"执行异常: {e}",
                "steps": 0, "tool_calls": 0,
                "session_id": session.id, "run_id": run_id,
            }

    # ── 便捷入口 ──

    async def run_webhook_task(self, task: str, webhook_name: str = "") -> dict:
        """兼容旧 webhook 路径 — 带策略与留痕."""
        return await self.run_task(task, {
            "trigger_type": "webhook",
            "trigger_id": webhook_name,
        })

    def stats(self, days: int = 7) -> dict:
        return self.run_store.stats(days=days)
