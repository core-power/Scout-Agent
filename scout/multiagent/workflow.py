"""工作流引擎 - 支持串行/并行/DAG 任务编排"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
import logging

logger = logging.getLogger(__name__)


class WorkflowStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class WorkflowNode:
    """工作流节点"""
    id: str
    name: str
    task_fn: Callable | None = None  # 异步函数
    task_description: str = ""  # 如果用 LLM 执行
    agent_id: str | None = None
    dependencies: list[str] = field(default_factory=list)
    status: WorkflowStatus = WorkflowStatus.PENDING
    result: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Workflow:
    """工作流定义"""
    id: str
    name: str
    description: str = ""
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)
    status: WorkflowStatus = WorkflowStatus.PENDING
    created_at: float = field(default_factory=lambda: __import__("time").time())
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def add_node(
        self,
        name: str,
        task_fn: Callable | None = None,
        task_description: str = "",
        agent_id: str | None = None,
        dependencies: list[str] | None = None,
    ) -> WorkflowNode:
        """添加工作流节点"""
        node_id = f"node_{uuid.uuid4().hex[:8]}"
        node = WorkflowNode(
            id=node_id,
            name=name,
            task_fn=task_fn,
            task_description=task_description,
            agent_id=agent_id,
            dependencies=dependencies or [],
        )
        self.nodes[node_id] = node
        return node
    
    def add_dependency(self, from_node_id: str, to_node_id: str):
        """添加依赖关系"""
        if to_node_id in self.nodes:
            self.nodes[to_node_id].dependencies.append(from_node_id)
    
    def get_ready_nodes(self) -> list[WorkflowNode]:
        """获取可以执行的节点（依赖已完成）"""
        completed = {
            node_id for node_id, node in self.nodes.items()
            if node.status == WorkflowStatus.COMPLETED
        }
        
        ready = []
        for node_id, node in self.nodes.items():
            if node.status != WorkflowStatus.PENDING:
                continue
            
            # 检查所有依赖是否完成
            if all(dep in completed for dep in node.dependencies):
                ready.append(node)
        
        return ready
    
    def is_completed(self) -> bool:
        """检查工作流是否完成"""
        return all(
            node.status in [WorkflowStatus.COMPLETED, WorkflowStatus.FAILED]
            for node in self.nodes.values()
        )


class WorkflowExecutor:
    """工作流执行器"""
    
    def __init__(self, router: Any, llm: Any):
        self.router = router
        self.llm = llm
        self._running_workflows: dict[str, Workflow] = {}
    
    async def execute(self, workflow: Workflow) -> Workflow:
        """执行工作流"""
        workflow.status = WorkflowStatus.RUNNING
        self._running_workflows[workflow.id] = workflow
        
        try:
            while not workflow.is_completed():
                # 获取可执行的节点
                ready_nodes = workflow.get_ready_nodes()
                
                if not ready_nodes:
                    # 没有可执行节点，检查是否有循环依赖
                    pending = [
                        n for n in workflow.nodes.values()
                        if n.status == WorkflowStatus.PENDING
                    ]
                    if pending:
                        logger.error(f"工作流 {workflow.id} 检测到循环依赖")
                        for node in pending:
                            node.status = WorkflowStatus.FAILED
                            node.error = "循环依赖"
                    break
                
                # 并行执行所有就绪节点
                tasks = [self._execute_node(node) for node in ready_nodes]
                await asyncio.gather(*tasks, return_exceptions=True)
            
            # 检查工作流最终状态
            if all(n.status == WorkflowStatus.COMPLETED for n in workflow.nodes.values()):
                workflow.status = WorkflowStatus.COMPLETED
            else:
                workflow.status = WorkflowStatus.FAILED
            
        except Exception as e:
            logger.error(f"工作流 {workflow.id} 执行失败: {e}")
            workflow.status = WorkflowStatus.FAILED
            workflow.metadata["error"] = str(e)
        finally:
            self._running_workflows.pop(workflow.id, None)
        
        return workflow
    
    async def _execute_node(self, node: WorkflowNode):
        """执行单个节点"""
        node.status = WorkflowStatus.RUNNING
        
        try:
            if node.task_fn:
                # 使用自定义函数
                result = await node.task_fn()
            elif node.task_description:
                # 使用 LLM Agent
                agent = self.router.get_agent(node.agent_id or "default")
                if not agent:
                    raise ValueError(f"Agent {node.agent_id} 不存在")
                
                from scout.core.types import Session
                session = Session(id=str(uuid.uuid4()))
                result = await agent.run_conversation(node.task_description, session)
                result = result.get("response", "")
            else:
                raise ValueError(f"节点 {node.name} 没有定义任务")
            
            node.status = WorkflowStatus.COMPLETED
            node.result = result
            
        except Exception as e:
            logger.error(f"节点 {node.name} 执行失败: {e}")
            node.status = WorkflowStatus.FAILED
            node.error = str(e)
            raise
    
    def get_workflow(self, workflow_id: str) -> Workflow | None:
        """获取工作流"""
        return self._running_workflows.get(workflow_id)
    
    def list_workflows(self) -> list[dict]:
        """列出所有运行中的工作流"""
        return [
            {
                "id": wf.id,
                "name": wf.name,
                "status": wf.status.value,
                "nodes_count": len(wf.nodes),
                "completed": sum(
                    1 for n in wf.nodes.values()
                    if n.status == WorkflowStatus.COMPLETED
                ),
            }
            for wf in self._running_workflows.values()
        ]


class WorkflowBuilder:
    """工作流构建器 - 简化工作流创建"""
    
    @staticmethod
    def sequential(name: str, tasks: list[dict]) -> Workflow:
        """创建顺序工作流"""
        workflow = Workflow(id=str(uuid.uuid4()), name=name)
        
        prev_node_id = None
        for task in tasks:
            node = workflow.add_node(
                name=task.get("name", "task"),
                task_description=task.get("description", ""),
                agent_id=task.get("agent_id"),
                dependencies=[prev_node_id] if prev_node_id else [],
            )
            prev_node_id = node.id
        
        return workflow
    
    @staticmethod
    def parallel(name: str, tasks: list[dict]) -> Workflow:
        """创建并行工作流"""
        workflow = Workflow(id=str(uuid.uuid4()), name=name)
        
        for task in tasks:
            workflow.add_node(
                name=task.get("name", "task"),
                task_description=task.get("description", ""),
                agent_id=task.get("agent_id"),
                dependencies=[],  # 无依赖，并行执行
            )
        
        return workflow
    
    @staticmethod
    def fan_out_fan_in(
        name: str,
        parallel_tasks: list[dict],
        final_task: dict,
    ) -> Workflow:
        """创建扇出-扇入工作流（并行执行后汇总）"""
        workflow = Workflow(id=str(uuid.uuid4()), name=name)
        
        # 并行节点
        parallel_node_ids = []
        for task in parallel_tasks:
            node = workflow.add_node(
                name=task.get("name", "parallel_task"),
                task_description=task.get("description", ""),
                agent_id=task.get("agent_id"),
            )
            parallel_node_ids.append(node.id)
        
        # 汇总节点
        workflow.add_node(
            name=final_task.get("name", "final_task"),
            task_description=final_task.get("description", ""),
            agent_id=final_task.get("agent_id"),
            dependencies=parallel_node_ids,
        )
        
        return workflow
    
    @staticmethod
    def from_dag(name: str, dag: dict) -> Workflow:
        """从 DAG 定义创建工作流
        
        dag 格式:
        {
            "nodes": {
                "node1": {"description": "...", "agent_id": "..."},
                "node2": {"description": "...", "agent_id": "..."},
            },
            "edges": [
                {"from": "node1", "to": "node2"}
            ]
        }
        """
        workflow = Workflow(id=str(uuid.uuid4()), name=name)
        
        # 添加节点
        node_id_map = {}
        for node_name, node_config in dag.get("nodes", {}).items():
            node = workflow.add_node(
                name=node_name,
                task_description=node_config.get("description", ""),
                agent_id=node_config.get("agent_id"),
            )
            node_id_map[node_name] = node.id
        
        # 添加依赖
        for edge in dag.get("edges", []):
            from_id = node_id_map.get(edge["from"])
            to_id = node_id_map.get(edge["to"])
            if from_id and to_id:
                workflow.nodes[to_id].dependencies.append(from_id)
        
        return workflow
