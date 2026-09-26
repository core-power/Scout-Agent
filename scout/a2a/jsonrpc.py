"""A2A JSON-RPC 2.0 线格式（Google A2A 规范）—— 边界转换层.

将内部类型（types.py 的 Task/A2AMessage/Part）与规范线格式互转：
- Message: {role, parts[], messageId, kind:"message"}
- Part:    {kind:"text"|"file"|"data", ...}
- Task:    {id, contextId, status:{state, message, timestamp}, artifacts[], history[], kind:"task"}
- Artifact: {artifactId, name, parts[], kind:"artifact"}

设计原则：尽量不动内部类型，所有规范形状的映射都收敛在本模块与
server 边界。内部 Part 用 "type" 判别字段，规范 Part 用 "kind"。

注意：与 types.py / server.py 一致，不使用 from __future__ import annotations，
避免 pydantic v2 对 Union 别名的 ForwardRef 解析问题。
"""

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union
from uuid import uuid4

from pydantic import BaseModel, Field

from scout.a2a.types import (
    A2AMessage,
    DataPart,
    FilePart,
    Task,
    TaskStatus,
    TextPart,
)


# ── JSON-RPC 2.0 / A2A 错误码 ──────────────────────────────

PARSE_ERROR = -32700          # 无效 JSON
INVALID_REQUEST = -32600      # 无效请求（缺 jsonrpc/method 等结构错误）
METHOD_NOT_FOUND = -32601     # 方法不存在
INVALID_PARAMS = -32602       # 无效参数
INTERNAL_ERROR = -32603       # 内部错误
# A2A 服务端错误（-32000 ~ -32099 规范保留段）
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
UNSUPPORTED_OPERATION = -32003


class JSONRPCError(Exception):
    """JSON-RPC 层错误 —— 携带规范错误码，由 handle_jsonrpc 统一转成 error 响应."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


# ── 响应 envelope 构造 ─────────────────────────────────────

def rpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应."""
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def rpc_error(rpc_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """构造 JSON-RPC 错误响应（A2A over HTTP 约定：协议层错误也返回 200）."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


# ── 规范 Part / Message 模型（仅用于入参解析） ──────────────

class SpecTextPart(BaseModel):
    """规范 TextPart."""
    kind: Literal["text"] = "text"
    text: str


class SpecFileContent(BaseModel):
    """规范 FilePart.file —— bytes(base64) 与 uri 二选一."""
    name: str | None = None
    mime_type: str | None = Field(None, alias="mimeType")
    bytes: str | None = None
    uri: str | None = None

    model_config = {"populate_by_name": True}


class SpecFilePart(BaseModel):
    """规范 FilePart."""
    kind: Literal["file"] = "file"
    file: SpecFileContent


class SpecDataPart(BaseModel):
    """规范 DataPart."""
    kind: Literal["data"] = "data"
    data: dict[str, Any]


SpecPart = Annotated[
    Union[SpecTextPart, SpecFilePart, SpecDataPart],
    Field(discriminator="kind"),
]


class SpecMessage(BaseModel):
    """规范 Message —— message/send 的 params.message."""
    role: Literal["user", "agent"]
    parts: list[SpecPart]
    message_id: str | None = Field(None, alias="messageId")

    model_config = {"populate_by_name": True}


# ── 内部 Part ↔ 规范 Part ──────────────────────────────────

def part_to_spec(part: Any) -> dict[str, Any]:
    """内部 Part（或其 dump dict / 已是规范形状的 dict）→ 规范 Part dict."""
    if isinstance(part, TextPart):
        return {"kind": "text", "text": part.text}
    if isinstance(part, FilePart):
        return {
            "kind": "file",
            "file": {
                "name": part.name,
                "mimeType": part.mime_type,
                "bytes": part.content,
            },
        }
    if isinstance(part, DataPart):
        return {"kind": "data", "data": part.data}
    if isinstance(part, dict):
        # 已是规范形状（带 kind）→ 原样透传（拷贝防共享引用）
        kind = part.get("kind")
        if kind in ("text", "file", "data"):
            return dict(part)
        # 内部 dump（带 type）→ 转规范形状
        ptype = part.get("type")
        if ptype == "text":
            return {"kind": "text", "text": str(part.get("text", ""))}
        if ptype == "file":
            return {
                "kind": "file",
                "file": {
                    "name": str(part.get("name", "")),
                    "mimeType": str(part.get("mime_type", "application/octet-stream")),
                    "bytes": str(part.get("content", "")),
                },
            }
        if ptype == "data":
            return {"kind": "data", "data": dict(part.get("data") or {})}
    # 兜底：标量内容按纯文本处理
    return {"kind": "text", "text": str(part)}


def part_from_spec(part: SpecTextPart | SpecFilePart | SpecDataPart):
    """规范 Part → 内部 Part."""
    if isinstance(part, SpecTextPart):
        return TextPart(text=part.text)
    if isinstance(part, SpecFilePart):
        f = part.file
        return FilePart(
            name=f.name or "file",
            content=f.bytes or f.uri or "",
            mime_type=f.mime_type or "application/octet-stream",
        )
    return DataPart(data=part.data)


# ── 内部 Message ↔ 规范 Message ────────────────────────────

def message_from_spec(msg: SpecMessage) -> A2AMessage:
    """规范 Message → 内部 A2AMessage."""
    return A2AMessage(role=msg.role, parts=[part_from_spec(p) for p in msg.parts])


def message_to_spec(msg: A2AMessage, message_id: str | None = None) -> dict[str, Any]:
    """内部 A2AMessage → 规范 Message dict."""
    return {
        "role": msg.role,
        "parts": [part_to_spec(p) for p in msg.parts],
        "messageId": message_id or uuid4().hex,
        "kind": "message",
    }


def extract_text(message: dict[str, Any]) -> str:
    """从规范 Message dict 中抽取全部 text part 的文本（用于状态消息降维）."""
    texts = []
    for part in message.get("parts") or []:
        if isinstance(part, dict) and part.get("kind") == "text":
            texts.append(str(part.get("text", "")))
    return "".join(texts)


# ── 内部 Task ↔ 规范 Task ──────────────────────────────────

# 内部状态集合之外的规范状态 → 内部状态（internal Literal 收窄）
_INTERNAL_STATES = {"submitted", "working", "completed", "failed", "canceled"}
_SPEC_STATE_MAP = {
    "input-required": "working",
    "rejected": "failed",
    "auth-required": "failed",
    "unknown": "submitted",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def task_to_spec(task: Task) -> dict[str, Any]:
    """内部 Task → 规范 Task dict（message/send、tasks/get 的 result）.

    artifacts 内部存储为 wire 形状 dict（name/parts，可缺 artifactId），
    此处补齐 artifactId 与 kind。
    """
    status_message = None
    if task.status.message:
        status_message = {
            "role": "agent",
            "parts": [{"kind": "text", "text": task.status.message}],
            "messageId": uuid4().hex,
            "kind": "message",
        }
    artifacts = []
    for i, art in enumerate(task.artifacts):
        if not isinstance(art, dict):
            continue
        a = dict(art)
        a.setdefault("artifactId", f"{task.id}-artifact-{i}")
        a.setdefault("name", "")
        a["parts"] = [part_to_spec(p) for p in (a.get("parts") or [])]
        a["kind"] = "artifact"
        artifacts.append(a)
    spec_task: dict[str, Any] = {
        "id": task.id,
        "contextId": task.session_id or task.id,
        "kind": "task",
        "status": {
            "state": task.status.state,
            "message": status_message,
        },
        "artifacts": artifacts,
        "history": [message_to_spec(m) for m in task.messages],
        "metadata": dict(task.metadata or {}),
    }
    # 服务端创建任务时写入 metadata["created_at"]，作为规范 status.timestamp
    created = (task.metadata or {}).get("created_at")
    if created:
        spec_task["status"]["timestamp"] = created
    return spec_task


def task_from_spec(data: dict[str, Any]) -> Task:
    """规范 Task dict → 内部 Task（client 侧解析远端响应）."""
    status_d = data.get("status") or {}
    state = str(status_d.get("state") or "unknown")
    state = _SPEC_STATE_MAP.get(state, state)
    if state not in _INTERNAL_STATES:
        state = "working"
    # 规范 status.message 是 Message 对象，内部 TaskStatus.message 是纯文本
    status_text = None
    status_msg = status_d.get("message")
    if isinstance(status_msg, dict):
        status_text = extract_text(status_msg) or None

    messages: list[A2AMessage] = []
    for m in data.get("history") or []:
        if not isinstance(m, dict):
            continue
        try:
            messages.append(message_from_spec(SpecMessage.model_validate(m)))
        except Exception:  # noqa: BLE001 — 单条消息解析失败不影响整体
            continue

    artifacts: list[dict[str, Any]] = []
    for a in data.get("artifacts") or []:
        if isinstance(a, dict) and "parts" in a:
            artifacts.append(dict(a))

    return Task(
        id=str(data.get("id") or ""),
        session_id=str(data.get("contextId") or ""),
        status=TaskStatus(state=state, message=status_text),  # type: ignore[arg-type]
        messages=messages,
        artifacts=artifacts,
        metadata=dict(data.get("metadata") or {}),
    )
