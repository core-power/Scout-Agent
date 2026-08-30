"""Agent 循环策略 — 对标 DeepSeek Harness 的可插拔 Agent Loop.

背景：此前 scout 的 ReAct 主循环（think → act → observe）内联在
Agent.run_conversation 内部，循环策略无法替换（DAGPlanner 有实现但从未接线）。

本模块定义：
- AgentLoop: 循环策略抽象接口。Agent 通过 agent_mode / SCOUT_LOOP_MODE 选择，
  替换策略无需改动 Agent 核心（事件、插件、上下文、自愈等仍复用同一 Agent）。
- ReActLoop: 默认策略，委托 Agent._run_react（原内联循环，行为不变）。
- DAGLoop: 计划-执行策略（DAGPlanner 落地）。先 LLM 拆解目标为带依赖的步骤，
  按拓扑序逐步骤执行（每步为独立子会话），最后 LLM 汇总为最终答复。
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any

from scout.core.types import Session
from scout.planner.dag_planner import DAGPlanner

logger = logging.getLogger("scout.loops")


class AgentLoop(ABC):
    """Agent 主循环策略接口.

    Agent 在 __init__ 时根据 agent_mode 实例化一个策略对象，
    run_conversation 只做分发：``return await self.loop.run(...)``。
    """

    name: str = "base"

    def __init__(self, agent: Any):
        self.agent = agent

    @abstractmethod
    async def run(
        self,
        user_message: str,
        session: Session | None,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        """驱动一轮完整对话，返回 {"response", "session", "steps"}."""
        ...


class ReActLoop(AgentLoop):
    """默认 ReAct 循环 — 委托 Agent._run_react（原内联循环，行为不变）."""

    name = "react"

    async def run(
        self,
        user_message: str,
        session: Session | None,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        return await self.agent._run_react(user_message, session, attachments)


class DAGLoop(AgentLoop):
    """DAG 计划-执行循环 — 让 DAGPlanner 正式落地.

    流程：
    1. plan：LLM 把目标拆解为带依赖（depends_on）的步骤列表。
    2. execute：按拓扑序逐步骤执行；每步通过 Agent._run_react 跑一个独立
       子会话（隔离上下文），依赖步骤的结果作为下一步的附加上下文。
    3. synthesize：收集各步结果，LLM 汇总为最终答复。

    执行细节：
    - 每步默认复用 agent.max_turns 预算；可用 step_max_turns 收紧。
    - 步骤执行失败（_run_react 抛异常）不会中断整条 DAG，
      失败信息记入结果并由汇总步骤处理。
    """

    name = "dag"

    def __init__(
        self,
        agent: Any,
        planner: DAGPlanner | None = None,
        step_max_turns: int | None = None,
    ):
        super().__init__(agent)
        self.planner = planner or DAGPlanner(agent.llm)
        self.step_max_turns = step_max_turns

    # ── 辅助 ──────────────────────────────────────────────

    async def _notify(self, stage: str, text: str) -> None:
        try:
            await self.agent.callbacks.on_status(stage)
            await self.agent.callbacks.on_tool_progress("dag", stage, text)
        except Exception:
            pass

    @staticmethod
    def _topo_order(steps: list[dict[str, Any]]) -> list[str]:
        """按 depends_on 拓扑排序；含环或缺失依赖时回退到原顺序."""
        ids = [s["id"] for s in steps]
        id_set = set(ids)
        deps: dict[str, set[str]] = {}
        for s in steps:
            d = set(s.get("depends_on") or []) & id_set
            d.discard(s["id"])
            deps[s["id"]] = d

        order: list[str] = []
        visited: set[str] = set()
        temp: set[str] = set()

        def visit(n: str) -> bool:
            if n in visited:
                return True
            if n in temp:  # 检测到环
                return False
            temp.add(n)
            for dep in deps.get(n, ()):
                if not visit(dep):
                    return False
            temp.discard(n)
            visited.add(n)
            order.append(n)
            return True

        for n in ids:
            if n not in visited:
                if not visit(n):
                    logger.warning("DAG 计划存在依赖环，回退到原始顺序")
                    return ids
        return order

    async def _synthesize(
        self,
        goal: str,
        results: list[dict[str, str]],
        session: Session,
    ) -> str:
        """汇总各步骤结果，生成最终答复."""
        if len(results) == 1:
            return results[0]["response"]

        steps_text = "\n".join(
            f"## 步骤 {r['id']}: {r['description']}\n{r['response']}"
            for r in results
        )
        prompt = (
            "你是一个任务协调器。用户的目标已被拆分为多个步骤分别执行。\n"
            f"用户目标: {goal}\n\n"
            f"各步骤执行结果:\n{steps_text}\n\n"
            "请把这些结果整合为一份连贯、完整的最终答复，直接回应用户目标。"
            "不要重复步骤过程，不要编造结果中不存在的信息。"
        )
        try:
            resp = await self.agent.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                _role="dag_synthesizer",
                _session_id=session.id,
            )
            return (resp.content or "").strip() or results[-1]["response"]
        except Exception as e:
            logger.warning(f"DAG 汇总失败，回退拼接结果: {e}")
            return "\n\n".join(r["response"] for r in results)

    async def _exec_step(
        self,
        step: dict[str, Any],
        dag_meta: dict[str, Any],
        session: Session,
        attachments: list[dict] | None,
    ) -> dict[str, Any]:
        """执行单个 DAG 步骤（独立子会话/子代理）.

        并行安全（2026-08-30）：step_max_turns 通过浅拷贝 agent 隔离设置，
        避免并发步骤间互相覆盖共享实例的 max_turns；callbacks/llm/tools 共享引用。
        """
        agent = self.agent
        step_id = step["id"]
        description = step.get("description", step_id)

        # 收集依赖步骤的结果作为上下文
        dep_text = ""
        dep_ids = [d for d in (step.get("depends_on") or []) if d in dag_meta["steps"]]
        if dep_ids:
            dep_text = "。参考前置结果:\n" + "\n".join(
                f"- {d}: {dag_meta['steps'][d].get('summary', '')[:500]}"
                for d in dep_ids
            )

        await self._notify("planning", f"执行步骤 {step_id}: {description[:60]}")

        step_msg = f"[子任务 {step_id}] {description}{dep_text}"
        step_session = Session(
            id=str(uuid.uuid4()),
            parent_id=session.id,
            agent_id=session.agent_id,
        )
        if self.step_max_turns:
            worker = copy.copy(agent)
            worker.max_turns = self.step_max_turns
        else:
            worker = agent
        try:
            res = await worker._run_react(step_msg, step_session, attachments)
            return {"response": res.get("response", ""), "steps": res.get("steps", 0)}
        except Exception as e:
            logger.warning(f"DAG 步骤 {step_id} 执行异常: {e}")
            return {"response": f"（步骤执行异常: {e}）", "steps": 0}

    # ── 主流程 ────────────────────────────────────────────

    async def run(
        self,
        user_message: str,
        session: Session | None,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        agent = self.agent
        if session is None:
            session = Session(id=str(uuid.uuid4()))
        session.status = "planning"
        session.extra.setdefault("dag", {"plan": [], "steps": {}, "status": "running"})
        dag_meta = session.extra["dag"]

        # 1. 规划
        await self._notify("planning", "正在拆解任务计划（DAG 模式）...")
        try:
            steps = await self.planner.plan(user_message)
        except Exception as e:
            logger.warning(f"DAG 规划失败，退化为单步骤: {e}")
            steps = [{"id": "default_step", "description": user_message, "depends_on": []}]
        if not steps:
            steps = [{"id": "default_step", "description": user_message, "depends_on": []}]
        dag_meta["plan"] = steps

        # 2. 按拓扑序分层，同层无依赖步骤并发执行（2026-08-30 多 agent 并行）
        #    对齐 WorkBuddy 的"多 Agents 并行交付"：同一层的子任务并行跑，
        #    跨层保持依赖顺序。步骤数 ≤1 时退化为串行（无并发开销）。
        order = self._topo_order(steps)
        step_map = {s["id"]: s for s in steps}

        # 计算每步所属层：level = max(依赖层)+1
        level_idx: dict[str, int] = {}
        for sid in order:
            deps = [d for d in (step_map[sid].get("depends_on") or []) if d in step_map]
            lvl = 0
            for d in deps:
                lvl = max(lvl, level_idx.get(d, 0) + 1)
            level_idx[sid] = lvl
        levels: dict[int, list[str]] = {}
        for sid, lvl in level_idx.items():
            levels.setdefault(lvl, []).append(sid)

        results: list[dict[str, str]] = []
        total_steps = 0
        for lvl in sorted(levels):
            group = levels[lvl]
            if len(group) > 1:
                await self._notify(
                    "planning",
                    f"并行执行第 {lvl + 1} 层（{len(group)} 个独立子任务）...",
                )
            coros = [self._exec_step(step_map[sid], dag_meta, session, attachments) for sid in group]
            group_results = await asyncio.gather(*coros, return_exceptions=True)
            for step_id, r in zip(group, group_results):
                description = step_map[step_id].get("description", step_id)
                if isinstance(r, Exception):
                    response = f"（步骤执行异常: {r}）"
                    step_steps = 0
                else:
                    response = r["response"]
                    step_steps = r.get("steps", 0)
                total_steps += step_steps
                dag_meta["steps"][step_id] = {
                    "description": description,
                    "summary": response[:500],
                }
                results.append({"id": step_id, "description": description, "response": response})

        # 3. 汇总
        dag_meta["status"] = "done"
        await self._notify("planning", "汇总各子任务结果...")
        final = await self._synthesize(user_message, results, session)
        session.status = "done"

        return {
            "response": final,
            "session": session,
            "steps": total_steps + len(results),
        }
