"""多 Agent 协作协调器 - 任务分解、分配和结果聚合"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
import logging

from scout.core.types import Session

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """协作任务"""
    id: str
    description: str
    subtasks: list[Task] = field(default_factory=list)
    parent: Task | None = None
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    agent_id: str | None = None
    dependencies: list[str] = field(default_factory=list)  # 依赖的 task_id
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskDecomposer:
    """任务分解器 - 将复杂任务分解为子任务"""
    
    async def decompose(
        self,
        task: str,
        llm: Any,
        available_agents: list[dict],
    ) -> list[Task]:
        """使用 LLM 分解任务
        
        Args:
            task: 原始任务描述
            llm: LLM 实例
            available_agents: 可用的 Agent 列表 [{"id": "xxx", "specialty": "xxx"}]
        
        Returns:
            子任务列表
        """
        agents_desc = "\n".join([
            f"- {a['id']}: {a.get('specialty', '通用')}"
            for a in available_agents
        ])
        
        prompt = f"""将以下任务分解为可执行的子任务：

任务：{task}

可用的 Agent：
{agents_desc}

请以 JSON 格式返回子任务列表：
{{
  "subtasks": [
    {{
      "description": "子任务描述",
      "agent_id": "执行的 agent id",
      "dependencies": []  // 依赖的其他子任务索引（0-based）
    }}
  ]
}}

只返回 JSON，不要其他内容。"""

        try:
            response = await llm.complete([{"role": "user", "content": prompt}])
            import json
            result = json.loads(response.content)
            
            tasks = []
            for i, subtask in enumerate(result.get("subtasks", [])):
                task_obj = Task(
                    # id 使用索引序号，与 dependencies 中的 task_{j} 引用保持一致
                    id=f"task_{i}",
                    description=subtask["description"],
                    agent_id=subtask.get("agent_id"),
                    dependencies=[f"task_{j}" for j in subtask.get("dependencies", [])],
                )
                tasks.append(task_obj)
            
            return tasks
        except Exception as e:
            logger.error(f"任务分解失败: {e}")
            # 返回单个任务作为降级
            return [Task(id=str(uuid.uuid4()), description=task)]


class TaskExecutor:
    """任务执行器 - 按依赖关系执行任务"""
    
    def __init__(self, router: Any, agent_factory: Callable[[str], Any] | None = None):
        self.router = router
        self.agent_factory = agent_factory  # agent_id -> Agent，找不到时懒创建并注册
    
    async def execute(self, task: Task, timeout: int = 300) -> Any:
        """执行单个任务"""
        task.status = TaskStatus.RUNNING
        
        try:
            agent = self.router.get_agent(task.agent_id or "default")
            if not agent:
                if self.agent_factory is not None:
                    # 懒创建子代理并注册，供后续任务/协作复用
                    agent = self.agent_factory(task.agent_id or "sub-agent")
                    if agent is not None:
                        self.router.register_agent(task.agent_id or "sub-agent", agent)
                if not agent:
                    raise ValueError(f"Agent {task.agent_id} 不存在")
            
            session = Session(id=str(uuid.uuid4()))
            result = await asyncio.wait_for(
                agent.run_conversation(task.description, session),
                timeout=timeout,
            )
            
            task.status = TaskStatus.COMPLETED
            task.result = result.get("response", "")
            return task.result
            
        except asyncio.TimeoutError:
            task.status = TaskStatus.FAILED
            task.result = f"超时: {timeout}秒"
            return task.result
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.result = f"失败: {e}"
            logger.error(f"任务 {task.id} 执行失败: {e}")
            return task.result
    
    async def execute_all(self, tasks: list[Task]) -> dict[str, Any]:
        """按依赖关系执行所有任务"""
        results = {}
        completed = set()
        
        while len(completed) < len(tasks):
            # 找出可以执行的任务（依赖已完成）
            ready = [
                t for t in tasks
                if t.id not in completed
                and all(dep in completed for dep in t.dependencies)
            ]
            
            if not ready:
                # 可能存在循环依赖
                logger.warning("检测到循环依赖，强制完成剩余任务")
                for t in tasks:
                    if t.id not in completed:
                        t.status = TaskStatus.FAILED
                        t.result = "循环依赖"
                        results[t.id] = t.result
                        completed.add(t.id)
                break
            
            # 并行执行所有就绪任务
            batch_results = await asyncio.gather(
                *[self.execute(t) for t in ready],
                return_exceptions=True
            )
            
            for task, result in zip(ready, batch_results):
                results[task.id] = result
                completed.add(task.id)
        
        return results


class ResultAggregator:
    """结果聚合器 - 合并多个子任务的结果"""
    
    async def aggregate(
        self,
        original_task: str,
        subtasks: list[Task],
        llm: Any,
    ) -> str:
        """聚合子任务结果生成最终答案"""
        
        results_desc = "\n\n".join([
            f"子任务 {i+1}: {t.description}\n结果: {t.result}"
            for i, t in enumerate(subtasks)
        ])
        
        prompt = f"""基于以下子任务的结果，生成原始任务的完整答案：

原始任务：{original_task}

子任务结果：
{results_desc}

请综合所有子任务的结果，生成一个完整、连贯的答案。直接返回答案内容。"""

        try:
            response = await llm.complete([{"role": "user", "content": prompt}])
            return response.content
        except Exception as e:
            logger.error(f"结果聚合失败: {e}")
            # 降级：简单拼接
            return "\n\n".join([t.result for t in subtasks if t.result])


class MultiAgentCoordinator:
    """多 Agent 协调器 - 完整的协作流程"""
    
    def __init__(
        self,
        router: Any,
        llm: Any,
        agent_factory: Callable[[str], Any] | None = None,
    ):
        self.router = router
        self.llm = llm
        self.agent_factory = agent_factory
        self.decomposer = TaskDecomposer()
        self.executor = TaskExecutor(router, agent_factory)
        self.aggregator = ResultAggregator()
    
    async def coordinate(
        self,
        task: str,
        auto_decompose: bool = True,
    ) -> dict[str, Any]:
        """协调多 Agent 执行任务
        
        Args:
            task: 任务描述
            auto_decompose: 是否自动分解任务
        
        Returns:
            {
                "original_task": str,
                "subtasks": list[Task],
                "final_result": str,
                "execution_time": float
            }
        """
        import time
        start_time = time.time()
        
        # 1. 获取可用 Agent
        available_agents = self.router.list_agents()
        
        # 1.1 仅有默认 Agent 但提供 agent_factory 时，向分解器暴露通用子代理池，
        #     由 agent_factory 在执行阶段懒创建
        if self.agent_factory is not None and len(available_agents) <= 1:
            available_agents = [
                {"id": f"sub-agent-{i}", "specialty": "通用"}
                for i in range(1, 5)
            ]
        
        # 2. 任务分解
        if auto_decompose and len(available_agents) > 1:
            subtasks = await self.decomposer.decompose(
                task, self.llm, available_agents
            )
        else:
            subtasks = [Task(id=str(uuid.uuid4()), description=task)]
        
        # 3. 执行子任务
        await self.executor.execute_all(subtasks)
        
        # 4. 结果聚合
        final_result = await self.aggregator.aggregate(
            task, subtasks, self.llm
        )
        
        execution_time = time.time() - start_time
        
        return {
            "original_task": task,
            "subtasks": subtasks,
            "final_result": final_result,
            "execution_time": execution_time,
        }
    
    async def collaborative_solve(
        self,
        problem: str,
        strategy: str = "consensus",
        rounds: int = 3,
    ) -> str:
        """协作解决问题
        
        Args:
            problem: 问题描述
            strategy: 协作策略
                - consensus: 共识模式（多轮讨论达成一致）
                - debate: 辩论模式（正反方辩论）
                - voting: 投票模式（多数表决）
            rounds: 讨论轮数
        
        Returns:
            最终答案
        """
        agents = self.router.list_agents()
        if len(agents) < 2:
            # 单个 Agent 直接回答
            agent = self.router.get_agent(agents[0]["id"])
            session = Session(id=str(uuid.uuid4()))
            result = await agent.run_conversation(problem, session)
            return result.get("response", "")
        
        if strategy == "consensus":
            return await self._consensus_strategy(problem, agents, rounds)
        elif strategy == "debate":
            return await self._debate_strategy(problem, agents, rounds)
        elif strategy == "voting":
            return await self._voting_strategy(problem, agents)
        else:
            raise ValueError(f"未知策略: {strategy}")
    
    async def _consensus_strategy(
        self,
        problem: str,
        agents: list[dict],
        rounds: int,
    ) -> str:
        """共识模式 - 多轮讨论达成一致"""
        current_answers = []
        
        # 第一轮：各自独立思考
        for agent_info in agents:
            agent = self.router.get_agent(agent_info["id"])
            session = Session(id=str(uuid.uuid4()))
            result = await agent.run_conversation(problem, session)
            current_answers.append(result.get("response", ""))
        
        # 后续轮次：基于他人答案改进
        for round_num in range(1, rounds):
            new_answers = []
            for i, agent_info in enumerate(agents):
                # 构建包含他人答案的上下文
                others_answers = [
                    f"Agent {j}: {ans}"
                    for j, ans in enumerate(current_answers)
                    if j != i
                ]
                
                prompt = f"""问题：{problem}

其他 Agent 的答案：
{chr(10).join(others_answers)}

你的上一轮答案：{current_answers[i]}

请基于他人的答案，改进你的答案。如果你认为自己的答案更好，可以保持。
直接返回你的答案。"""
                
                agent = self.router.get_agent(agent_info["id"])
                session = Session(id=str(uuid.uuid4()))
                result = await agent.run_conversation(prompt, session)
                new_answers.append(result.get("response", ""))
            
            current_answers = new_answers
        
        # 最后聚合
        all_answers = "\n\n".join([
            f"Agent {i}: {ans}"
            for i, ans in enumerate(current_answers)
        ])
        
        summary_prompt = f"""问题：{problem}

多个 Agent 的答案：
{all_answers}

请综合所有答案，生成一个最终的共识答案。"""
        
        response = await self.llm.complete([
            {"role": "user", "content": summary_prompt}
        ])
        return response.content
    
    async def _debate_strategy(
        self,
        problem: str,
        agents: list[dict],
        rounds: int,
    ) -> str:
        """辩论模式 - 正反方辩论"""
        # 分配正反方
        pro_agent = agents[0]
        con_agent = agents[1] if len(agents) > 1 else agents[0]
        
        pro_arguments = []
        con_arguments = []
        
        for round_num in range(rounds):
            # 正方论证
            pro_prompt = f"""问题：{problem}

你支持这个问题。请提出你的论点。
"""
            if con_arguments:
                pro_prompt += f"\n反方的论点：\n{con_arguments[-1]}\n请反驳反方。"
            
            pro_agent_obj = self.router.get_agent(pro_agent["id"])
            session = Session(id=str(uuid.uuid4()))
            result = await pro_agent_obj.run_conversation(pro_prompt, session)
            pro_arguments.append(result.get("response", ""))
            
            # 反方论证
            con_prompt = f"""问题：{problem}

你反对这个问题。请提出你的论点。
"""
            if pro_arguments:
                con_prompt += f"\n正方的论点：\n{pro_arguments[-1]}\n请反驳正方。"
            
            con_agent_obj = self.router.get_agent(con_agent["id"])
            session = Session(id=str(uuid.uuid4()))
            result = await con_agent_obj.run_conversation(con_prompt, session)
            con_arguments.append(result.get("response", ""))
        
        # 裁判总结
        judge_prompt = f"""问题：{problem}

正方论点：
{chr(10).join([f"轮次 {i+1}: {arg}" for i, arg in enumerate(pro_arguments)])}

反方论点：
{chr(10).join([f"轮次 {i+1}: {arg}" for i, arg in enumerate(con_arguments)])}

作为裁判，请总结双方论点，给出你的判断和最终答案。"""
        
        response = await self.llm.complete([
            {"role": "user", "content": judge_prompt}
        ])
        return response.content
    
    async def _voting_strategy(
        self,
        problem: str,
        agents: list[dict],
    ) -> str:
        """投票模式 - 多数表决"""
        answers = []
        
        # 收集所有答案
        for agent_info in agents:
            agent = self.router.get_agent(agent_info["id"])
            session = Session(id=str(uuid.uuid4()))
            result = await agent.run_conversation(problem, session)
            answers.append({
                "agent_id": agent_info["id"],
                "answer": result.get("response", "")
            })
        
        # 让每个 Agent 投票
        votes = []
        for i, voter_info in enumerate(agents):
            voting_prompt = f"""问题：{problem}

以下是不同 Agent 的答案：
"""
            for j, ans in enumerate(answers):
                voting_prompt += f"\n答案 {j+1}: {ans['answer']}\n"
            
            voting_prompt += "\n请选择你认为最好的答案（返回答案编号，如 1, 2, 3）。"
            
            voter = self.router.get_agent(voter_info["id"])
            session = Session(id=str(uuid.uuid4()))
            result = await voter.run_conversation(voting_prompt, session)
            vote_text = result.get("response", "")
            
            # 提取投票编号
            import re
            match = re.search(r'\d+', vote_text)
            if match:
                vote_idx = int(match.group()) - 1
                votes.append(vote_idx)
        
        # 统计投票
        from collections import Counter
        vote_counts = Counter(votes)
        winner_idx = vote_counts.most_common(1)[0][0]
        
        if 0 <= winner_idx < len(answers):
            return answers[winner_idx]["answer"]
        else:
            return answers[0]["answer"]
