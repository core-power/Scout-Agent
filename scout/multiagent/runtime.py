"""多 Agent 运行时 — 全局共享的 router / messenger / shared_state 单例。

历史背景：scout/multiagent 下的组件（AgentRouter / AgentMessenger / MultiAgentCoordinator /
SharedStateManager）此前仅有实现与单测，没有任何运行时调用方（死代码）。
本模块为它们提供进程内单例，供以下运行时接线点使用：

- delegate_task / parallel_delegate 创建子代理后注册到全局 router（委派期间可被
  coordinator / messenger 按 ID 获取），执行完毕自动注销
- collaborate_task 工具通过全局 router 驱动 MultiAgentCoordinator 完成
  「分解 → 并行执行 → 聚合」的完整协作流程
- ControlGateway 将 default 主 Agent 注册进全局 router，并通过
  route_message() / collaborate() 真正使用 AgentRouter / AgentMessenger
"""

from __future__ import annotations

from scout.multiagent.messenger import AgentMessenger
from scout.multiagent.router import AgentRouter
from scout.multiagent.shared_state import SharedStateManager

_router: AgentRouter | None = None
_messenger: AgentMessenger | None = None
_shared_state: SharedStateManager | None = None


def get_router() -> AgentRouter:
    """返回全局 AgentRouter 单例（首次调用时创建）."""
    global _router
    if _router is None:
        _router = AgentRouter()
    return _router


def get_messenger() -> AgentMessenger:
    """返回全局 AgentMessenger 单例."""
    global _messenger
    if _messenger is None:
        _messenger = AgentMessenger()
    return _messenger


def get_shared_state() -> SharedStateManager:
    """返回全局 SharedStateManager 单例（内存态）."""
    global _shared_state
    if _shared_state is None:
        _shared_state = SharedStateManager()
    return _shared_state
