"""A2A (Agent-to-Agent) Protocol Support.

Google A2A 规范（JSON-RPC 2.0，见 scout/a2a/jsonrpc.py）+ 旧自定义
REST 协议（deprecated，向后兼容保留）.
"""

from scout.a2a import jsonrpc
from scout.a2a.client import A2AClient, A2AManager, A2ARemoteError
from scout.a2a.types import (
    AgentCard,
    AgentCapabilities,
    A2AMessage,
    Task,
    TaskStatus,
    TaskSendRequest,
    TaskSendResponse,
    TextPart,
    FilePart,
    DataPart,
    Part,
)

__all__ = [
    "AgentCard",
    "AgentCapabilities",
    "A2AMessage",
    "Task",
    "TaskStatus",
    "TaskSendRequest",
    "TaskSendResponse",
    "TextPart",
    "FilePart",
    "DataPart",
    "Part",
    "jsonrpc",
    "A2AClient",
    "A2AManager",
    "A2ARemoteError",
]
