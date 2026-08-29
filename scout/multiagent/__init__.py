"""Scout Agent 多 Agent 层."""

from scout.multiagent.router import AgentRouter, Binding
from scout.multiagent.messenger import AgentMessenger
from scout.multiagent.coordinator import (
    MultiAgentCoordinator,
    TaskDecomposer,
    TaskExecutor,
    ResultAggregator,
    Task,
    TaskStatus,
)
from scout.multiagent.workflow import (
    WorkflowExecutor,
    WorkflowBuilder,
    Workflow,
    WorkflowNode,
    WorkflowStatus,
)
from scout.multiagent.shared_state import (
    SharedStateManager,
    SharedData,
    AgentWorkspace,
)

# 接线状态（2026-08-27 更新，此前注释已过时）：
# - scout.multiagent.runtime 提供进程内单例（get_router / get_messenger / get_shared_state）。
# - AgentRouter / AgentMessenger：gateway/control.py 实例化并用于 route_message() / collaborate()；
#   ControlGateway 将 default 主 Agent 注册进全局 router。
# - delegate_task / parallel_delegate：build_sub_agent() 将子代理注册进全局 router，
#   执行完毕由 unregister_sub_agent() 自动注销（防泄漏）。
# - collaborate_task：delegate/collaborate.py 通过全局 router 驱动 MultiAgentCoordinator，
#   完成「分解 → 并行执行 → 聚合」完整协作流程。
# - SharedStateManager：由 runtime.get_shared_state() 按需创建，供协作流程共享状态。
# 以上均有 tests/unit/test_multiagent.py 单测覆盖。
__all__ = [
    # 通信和路由
    "AgentRouter",
    "Binding",
    "AgentMessenger",
    # 协作协调
    "MultiAgentCoordinator",
    "TaskDecomposer",
    "TaskExecutor",
    "ResultAggregator",
    "Task",
    "TaskStatus",
    # 工作流
    "WorkflowExecutor",
    "WorkflowBuilder",
    "Workflow",
    "WorkflowNode",
    "WorkflowStatus",
    # 共享状态
    "SharedStateManager",
    "SharedData",
    "AgentWorkspace",
]
