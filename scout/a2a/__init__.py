"""A2A (Agent-to-Agent) Protocol Support.

Simplified implementation of Google's A2A protocol for agent interoperability.
"""

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
]
