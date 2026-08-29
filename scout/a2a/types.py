"""A2A (Agent-to-Agent) Protocol Types.

Simplified implementation of Google's A2A protocol for agent interoperability.
Reference: https://google.github.io/A2A/
"""

# 注意：不使用 from __future__ import annotations。
# pydantic v2 在字符串化注解下无法解析 list[Part]（Union 类型别名）的
# ForwardRef，会导致生成 OpenAPI schema 时 PydanticUserError。
# 本文件定义顺序即依赖顺序，运行时求值安全。

from typing import Any, Literal, Union
from pydantic import BaseModel, Field


# ── Parts ──

class TextPart(BaseModel):
    """Text content part."""
    type: Literal["text"] = "text"
    text: str


class FilePart(BaseModel):
    """File content part (simplified)."""
    type: Literal["file"] = "file"
    name: str
    content: str  # base64 or text content
    mime_type: str = "text/plain"


class DataPart(BaseModel):
    """Structured data part."""
    type: Literal["data"] = "data"
    data: dict[str, Any]


Part = Union[TextPart, FilePart, DataPart]


# ── Message ──

class A2AMessage(BaseModel):
    """A2A Message."""
    role: Literal["user", "agent"]
    parts: list[Part]


# ── Task ──

class TaskStatus(BaseModel):
    """Task status."""
    state: Literal["submitted", "working", "completed", "failed", "canceled"] = "submitted"
    message: str | None = None


class Task(BaseModel):
    """A2A Task."""
    id: str
    session_id: str = ""
    status: TaskStatus = Field(default_factory=TaskStatus)
    messages: list[A2AMessage] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Agent Card ──

class AgentCapabilities(BaseModel):
    """Agent capabilities."""
    streaming: bool = False
    push_notifications: bool = False


class AgentCard(BaseModel):
    """Agent Card - describes agent capabilities and endpoint."""
    name: str
    description: str
    url: str
    version: str = "1.0.0"
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = Field(default=["text"])
    default_output_modes: list[str] = Field(default=["text"])


# ── Request/Response ──

class TaskSendRequest(BaseModel):
    """Request to send a task to an agent."""
    task: Task


class TaskSendResponse(BaseModel):
    """Response after processing a task."""
    task: Task
