"""A2A Server - Exposes Scout Agent as an A2A-compatible endpoint.

Allows other A2A agents to send tasks to Scout.

支持两种协议形态：
1. Google A2A 规范（JSON-RPC 2.0，默认）：单端点 POST /a2a，
   方法 message/send、tasks/get、tasks/cancel，卡片在
   /.well-known/agent-card.json。见 handle_jsonrpc。
2. 旧自定义 REST（deprecated，向后兼容保留）：
   /a2a/tasks/send 等，见 handle_task。

TODO(A2A): message/stream（SSE 流式）与 tasks/pushNotificationConfig/*
（推送通知）未实现 —— 工作量原因暂缓，请求会返回
UNSUPPORTED_OPERATION(-32003)。
"""

# 注意：不使用 from __future__ import annotations。
# FastAPI 对字符串化注解的 body 参数构建 TypeAdapter 时，会因 ForwardRef
# 无法解析而报 PydanticUserError（class-not-fully-defined）。
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from scout.a2a import jsonrpc as rpc
from scout.a2a.types import (
    AgentCard,
    AgentCapabilities,
    A2AMessage,
    Task,
    TaskStatus,
    TaskSendRequest,
    TaskSendResponse,
    TextPart,
)

logger = logging.getLogger(__name__)

# 终态：不可取消
_TERMINAL_STATES = ("completed", "failed", "canceled")


class A2AServer:
    """A2A Server - exposes Scout as an A2A agent."""

    def __init__(self, agent, host: str = "0.0.0.0", port: int = 8849):
        """Initialize A2A server.

        Args:
            agent: Scout Agent instance
            host: Host to bind to
            port: Port to listen on
        """
        self.agent = agent
        self.host = host
        self.port = port
        self.tasks: dict[str, Task] = {}  # task_id -> Task（内存态）

    def get_agent_card(self) -> AgentCard:
        """Get agent card describing this agent.

        同一份卡片同时由 /.well-known/agent-card.json（规范路径）与
        /.well-known/agent.json（旧路径，兼容保留）提供。
        """
        return AgentCard(
            name="Scout Agent",
            description="A capable AI assistant with tools, memory, and multi-agent coordination",
            url=f"http://{self.host}:{self.port}/a2a",
            version="1.0.0",
            capabilities=AgentCapabilities(
                streaming=False,
                push_notifications=False,
            ),
        )

    # ── Google A2A 规范：JSON-RPC 2.0 入口 ──────────────────

    async def handle_jsonrpc(self, payload: Any) -> dict[str, Any]:
        """处理一条 JSON-RPC 2.0 请求（POST /a2a 的 body）.

        Args:
            payload: 已解析的 JSON 请求体（dict）

        Returns:
            JSON-RPC 响应 dict（成功带 result，失败带 error）
        """
        if not isinstance(payload, dict):
            return rpc.rpc_error(None, rpc.INVALID_REQUEST, "请求必须是 JSON-RPC 对象")
        rpc_id = payload.get("id")
        if rpc_id is not None and not isinstance(rpc_id, (str, int, float)):
            return rpc.rpc_error(None, rpc.INVALID_REQUEST, "id 必须为字符串或数字")
        if payload.get("jsonrpc") != "2.0":
            return rpc.rpc_error(rpc_id, rpc.INVALID_REQUEST, 'jsonrpc 字段必须为 "2.0"')
        method = payload.get("method")
        if not isinstance(method, str) or not method:
            return rpc.rpc_error(rpc_id, rpc.INVALID_REQUEST, "缺少 method 字段")
        params = payload.get("params")
        if params is not None and not isinstance(params, dict):
            return rpc.rpc_error(rpc_id, rpc.INVALID_REQUEST, "params 必须为对象")

        try:
            result = await self._dispatch_jsonrpc(method, params or {})
        except rpc.JSONRPCError as e:
            return rpc.rpc_error(rpc_id, e.code, e.message, e.data)
        except Exception as e:  # noqa: BLE001 — 兜底为 INTERNAL_ERROR
            logger.exception(f"A2A: JSON-RPC 方法 {method} 内部错误")
            return rpc.rpc_error(rpc_id, rpc.INTERNAL_ERROR, f"内部错误: {e}")
        return rpc.rpc_result(rpc_id, result)

    async def _dispatch_jsonrpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """按方法名分发；未识别方法抛 METHOD_NOT_FOUND."""
        if method in ("message/send", "tasks/send"):
            if method == "tasks/send":
                logger.warning("A2A: tasks/send 为旧版方法名，请改用 message/send")
            return await self._jsonrpc_message_send(params)
        if method in ("tasks/get", "message/get"):
            if method == "message/get":
                logger.warning("A2A: message/get 为兼容别名，规范方法名为 tasks/get")
            return self._jsonrpc_tasks_get(params)
        if method == "tasks/cancel":
            return self._jsonrpc_tasks_cancel(params)
        if method in ("message/stream", "tasks/resubscribe"):
            # TODO(A2A): streaming/SSE 未实现，暂不支持
            raise rpc.JSONRPCError(
                rpc.UNSUPPORTED_OPERATION,
                f"方法 {method}（streaming）未实现",
                {"method": method},
            )
        if method.startswith("tasks/pushNotificationConfig"):
            # TODO(A2A): push notification 未实现，暂不支持
            raise rpc.JSONRPCError(
                rpc.UNSUPPORTED_OPERATION,
                f"方法 {method}（push notification）未实现",
                {"method": method},
            )
        raise rpc.JSONRPCError(rpc.METHOD_NOT_FOUND, f"未知方法: {method}")

    async def _jsonrpc_message_send(self, params: dict[str, Any]) -> dict[str, Any]:
        """message/send：规范入参为 {message: {role, parts[]}, ...}."""
        msg_raw = params.get("message")
        if not isinstance(msg_raw, dict):
            raise rpc.JSONRPCError(rpc.INVALID_PARAMS, "params.message 缺失或不是对象")
        try:
            spec_msg = rpc.SpecMessage.model_validate(msg_raw)
        except ValidationError as e:
            raise rpc.JSONRPCError(
                rpc.INVALID_PARAMS,
                f"message 格式无效: {e.errors()[0].get('msg', 'validation error')}",
                {"details": e.errors(include_url=False)},
            ) from e

        internal_msg = rpc.message_from_spec(spec_msg)
        user_message = "".join(
            p.text for p in internal_msg.parts if isinstance(p, TextPart)
        )
        if not user_message:
            raise rpc.JSONRPCError(
                rpc.INVALID_PARAMS, "message 中未找到可处理的文本内容（text part）"
            )

        # 规范约定：任务 ID 由服务端分配
        task = Task(
            id=str(uuid.uuid4()),
            session_id=str(params.get("contextId") or ""),
            status=TaskStatus(state="submitted"),
            messages=[internal_msg],
            metadata={"created_at": datetime.now(timezone.utc).isoformat()},
        )
        if spec_msg.message_id:
            task.metadata["client_message_id"] = spec_msg.message_id

        await self._execute_task(task)
        return rpc.task_to_spec(task)

    def _jsonrpc_tasks_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """tasks/get：按 taskId 取回任务（含 artifacts）。"""
        task_id = params.get("taskId") or params.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise rpc.JSONRPCError(rpc.INVALID_PARAMS, "params.taskId 缺失")
        task = self.tasks.get(task_id)
        if task is None:
            raise rpc.JSONRPCError(rpc.TASK_NOT_FOUND, f"任务不存在: {task_id}")
        return rpc.task_to_spec(task)

    def _jsonrpc_tasks_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """tasks/cancel：取消任务.

        注意：当前实现任务在 message/send 请求内同步执行完毕，
        cancel 只对非终态任务生效（终态抛 TASK_NOT_CANCELABLE）。
        """
        task_id = params.get("taskId") or params.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise rpc.JSONRPCError(rpc.INVALID_PARAMS, "params.taskId 缺失")
        task = self.tasks.get(task_id)
        if task is None:
            raise rpc.JSONRPCError(rpc.TASK_NOT_FOUND, f"任务不存在: {task_id}")
        if task.status.state in _TERMINAL_STATES:
            raise rpc.JSONRPCError(
                rpc.TASK_NOT_CANCELABLE,
                f"任务已处于终态 {task.status.state}，无法取消",
            )
        task.status = TaskStatus(state="canceled")
        logger.info(f"A2A: Task {task_id} canceled via tasks/cancel")
        return rpc.task_to_spec(task)

    # ── 任务执行（新旧协议共用） ────────────────────────────

    async def _execute_task(self, task: Task) -> Task:
        """执行任务：working → completed/failed，并收集 artifacts."""
        logger.info(f"A2A: Received task {task.id} with {len(task.messages)} messages")
        task.status = TaskStatus(state="working")
        self.tasks[task.id] = task

        try:
            user_message = ""
            for msg in task.messages:
                if msg.role == "user":
                    for part in msg.parts:
                        if isinstance(part, TextPart):
                            user_message += part.text

            if not user_message:
                raise ValueError("No user message found in task")

            result = await self._run_agent(user_message)

            if isinstance(result, dict):
                response_text = result.get("response", "No response generated")
                task.artifacts = self._collect_artifacts(result)
            else:
                response_text = str(result)
                task.artifacts = []

            task.messages.append(
                A2AMessage(role="agent", parts=[TextPart(text=response_text)])
            )
            # 规范约定：终态任务的 status.message 携带最终结论
            task.status = TaskStatus(state="completed", message=response_text)
            logger.info(f"A2A: Task {task.id} completed successfully")

        except Exception as e:
            logger.error(f"A2A: Task {task.id} failed: {e}")
            task.status = TaskStatus(state="failed", message=str(e))

        return task

    @staticmethod
    def _collect_artifacts(result: dict[str, Any]) -> list[dict[str, Any]]:
        """从 agent 执行结果中提取 artifacts（wire 形状：name/parts）.

        支持结果里的 parts 用规范 kind 字段或内部 type 字段描述。
        """
        raw = result.get("artifacts")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if isinstance(item, dict) and "parts" in item:
                out.append({
                    "name": str(item.get("name") or "artifact"),
                    "parts": [rpc.part_to_spec(p) for p in (item.get("parts") or [])],
                })
        return out

    # ── 旧自定义 REST 协议（deprecated，向后兼容） ───────────

    async def handle_task(self, request: TaskSendRequest) -> TaskSendResponse:
        """Handle incoming task from another agent（旧 REST 协议，deprecated）.

        Args:
            request: Task send request

        Returns:
            Task send response with updated task status
        """
        task = await self._execute_task(request.task)
        return TaskSendResponse(task=task)

    async def _run_agent(self, user_message: str) -> Any:
        """Run Scout Agent and get response.

        Args:
            user_message: User message to process

        Returns:
            Agent 执行结果 dict（含 "response" 文本，可选 "artifacts"）
        """
        # Create a simple session
        from scout.core.types import Session
        session = Session(id=f"a2a-{id(user_message)}")

        # Run agent
        result = await self.agent.run_conversation(user_message, session)

        return result

    def get_task(self, task_id: str) -> Task | None:
        """Get task by ID.

        Args:
            task_id: Task ID

        Returns:
            Task or None if not found
        """
        return self.tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        """List all tasks.

        Returns:
            List of tasks
        """
        return list(self.tasks.values())
