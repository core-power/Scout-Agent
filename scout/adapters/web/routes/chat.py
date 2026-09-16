"""对话路由组（OpenAI 兼容 /v1/*、/api/chat、/api/tools）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from sse_starlette.sse import EventSourceResponse
from fastapi.responses import JSONResponse, Response
from scout.core.callbacks import Callbacks, NullCallbacks
from scout.core.types import Message, Role, Session
import asyncio
from datetime import datetime
import json
import uuid
from scout.tools.registry import ToolRegistry
from scout.adapters.web.callbacks import WebCallbacks
from pydantic import BaseModel as PydanticModel

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class ChatRoutes:
    """对话路由组（OpenAI 兼容 /v1/*、/api/chat、/api/tools）（mixin）."""

    def _setup_chat_routes(self):
        """OpenAPI 兼容 API."""

        # ── OpenAPI 兼容 API ──

        @self.app.get("/v1/models")
        async def list_models():
            return {
                "object": "list",
                "data": [
                    {"id": "scout", "object": "model", "owned_by": "scout"},
                    {"id": "scout/default", "object": "model", "owned_by": "scout"},
                ],
            }

        @self.app.post("/v1/chat/completions")
        async def chat_completions(req: ChatRequest):
            """OpenAI 兼容 chat completions."""
            # 提取最后一条用户消息
            user_msg = ""
            for msg in reversed(req.messages):
                if msg.get("role") == "user":
                    user_msg = msg.get("content", "")
                    break

            if not user_msg:
                return JSONResponse({"error": "No user message found"}, status_code=400)

            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)

            # 获取或创建会话
            session_id = str(uuid.uuid4())
            session = Session(id=session_id)

            # 为每个请求创建独立回调（不修改共享 agent 的状态）
            import copy
            agent_copy = copy.copy(self.agent)
            agent_copy.callbacks = NullCallbacks()

            # 运行 Agent
            result = await agent_copy.run_conversation(
                user_msg, session, 
            )

            # 构建 OpenAI 兼容响应
            return ChatResponse(
                id=f"chatcmpl-{session_id}",
                created=int(datetime.now().timestamp()),
                model=req.model,
                choices=[ChatChoice(
                    message={"role": "assistant", "content": result["response"]},
                    finish_reason="stop",
                )],
            )

        # ── SSE 流式 ──

        @self.app.post("/api/chat")
        async def chat(req: ChatRequest):
            """聊天 API — 返回 SSE 流."""
            user_msg = ""
            for msg in reversed(req.messages):
                if msg.get("role") == "user":
                    user_msg = msg.get("content", "")
                    break

            if not user_msg:
                return JSONResponse({"error": "No user message"}, status_code=400)

            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)

            session_id = str(uuid.uuid4())
            session = Session(id=session_id)

            callbacks = WebCallbacks()
            callbacks._adapter = self  # 2026-09-09：HITL future 注册接线（同 WS 路径）
            import copy
            agent_copy = copy.copy(self.agent)
            # 主 agent 事件打 main 标签，与子代理(sub)区分编排过程
            from scout.core.callbacks import TaggedCallbacks
            agent_copy.callbacks = TaggedCallbacks(callbacks, agent_role="main", agent_name="主代理")
            # 让 delegate/parallel 拿到当前请求 agent（callbacks 已包装）
            from scout.tools.registry import ToolRegistry
            _prev_main_agent = getattr(ToolRegistry, "_main_agent", None)
            ToolRegistry._main_agent = agent_copy

            async def event_stream():
                # 启动 Agent 任务
                agent_task = asyncio.create_task(
                    agent_copy.run_conversation(user_msg, session)
                )
                while not agent_task.done():
                    try:
                        event = await asyncio.wait_for(callbacks.events.get(), timeout=0.1)
                        yield {"event": "message", "data": json.dumps(event, ensure_ascii=False)}
                    except asyncio.TimeoutError:
                        continue

                # ★ 2026-09-09：任务结束后清空残留事件队列 —— on_file 等在 task
                # 收尾阶段才入队的事件（文件卡片等）此前被直接丢弃，SSE 客户端
                # 收不到 file 事件。done 之前先 drain，保证事件完整。
                while not callbacks.events.empty():
                    event = callbacks.events.get_nowait()
                    yield {"event": "message", "data": json.dumps(event, ensure_ascii=False)}

                # 获取最终结果
                try:
                    result = await agent_task
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "done",
                            "data": {"response": result["response"], "steps": result["steps"]},
                            "timestamp": datetime.now().isoformat(),
                        }, ensure_ascii=False),
                    }
                except Exception as e:
                    yield {
                        "event": "message",
                        "data": json.dumps({
                            "type": "error",
                            "data": {"error": str(e)},
                            "timestamp": datetime.now().isoformat(),
                        }, ensure_ascii=False),
                    }
                finally:
                    # 恢复主 Agent 引用（防止并发请求串扰）
                    try:
                        ToolRegistry._main_agent = _prev_main_agent
                    except Exception:
                        pass

            return EventSourceResponse(event_stream())

    def _setup_tool_routes(self):
        """工具列表 API."""

        # ── 工具列表 ──

        @self.app.get("/api/tools")
        async def list_tools():
            tools = ToolRegistry.all_tools()
            return {
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                        "annotations": t.annotations.model_dump(),
                    }
                    for t in tools.values()
                ]
            }
class ChatRequest(PydanticModel):
    """OpenAI 兼容的 chat 请求."""
    model: str = "scout"
    messages: list[dict]
    stream: bool = False
    temperature: float = 0.7
    max_tokens: int | None = None

class ChatChoice(PydanticModel):
    index: int = 0
    message: dict
    finish_reason: str = "stop"

class ChatResponse(PydanticModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str = "scout"
    choices: list[ChatChoice]

