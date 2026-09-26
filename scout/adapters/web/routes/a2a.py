"""A2A 协议路由组（/a2a JSON-RPC、/a2a/tasks/*、/api/a2a/agents、.well-known）.

W1 拆分（2026-09-14）：自 adapters/web.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。
2026-09-21：升级到 Google A2A 规范（JSON-RPC 2.0）：
- 新增 POST /a2a（规范单端点：message/send、tasks/get、tasks/cancel）
- 新增 GET /.well-known/agent-card.json（规范卡片路径）
- 旧 /a2a/tasks/* 与 /.well-known/agent.json 保留（deprecated，向后兼容）
"""

import json
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, Response
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from urllib.parse import urlparse

if TYPE_CHECKING:  # 仅为类型检查，运行时无循环依赖
    from scout.adapters.web.adapter import WebAdapter


class A2aRoutes:
    """A2A 协议路由组（/a2a JSON-RPC、/a2a/tasks/*、/api/a2a/agents、.well-known）（mixin）."""

    def _setup_a2a_routes(self):
        """A2A (Agent-to-Agent) 协议 API."""
        from scout.a2a import jsonrpc as rpc
        from scout.a2a.server import A2AServer
        from scout.a2a.types import TaskSendRequest, A2AMessage, TextPart, TaskStatus

        # 初始化 A2A Server
        self._a2a_server = A2AServer(self._agent) if self._agent else None

        # ── Google A2A 规范（JSON-RPC 2.0） ──

        @self.app.get("/.well-known/agent-card.json")
        async def get_agent_card_spec():
            """获取 Agent Card —— A2A 规范路径（0.3+）."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            card = self._a2a_server.get_agent_card()
            return card.model_dump()

        @self.app.post("/a2a")
        async def a2a_jsonrpc(req: Request):
            """A2A 规范 JSON-RPC 2.0 端点（message/send、tasks/get、tasks/cancel）.

            协议层错误（含 -32700/-32600/-32601/-32602）按 JSON-RPC over HTTP
            约定仍返回 200，错误信息在 error 字段中。
            """
            if not self._a2a_server:
                return JSONResponse(
                    rpc.rpc_error(None, rpc.INTERNAL_ERROR, "Agent 未初始化"),
                    status_code=500,
                )
            body = await req.body()
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JSONResponse(
                    rpc.rpc_error(None, rpc.PARSE_ERROR, "请求体不是有效的 JSON")
                )
            response = await self._a2a_server.handle_jsonrpc(payload)
            return JSONResponse(response)

        # ── 旧自定义 REST 协议（deprecated，向后兼容保留） ──

        @self.app.get("/.well-known/agent.json", deprecated=True)
        async def get_agent_card():
            """获取 Agent Card - 旧路径（deprecated，请改用 agent-card.json）."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            card = self._a2a_server.get_agent_card()
            return card.model_dump()

        @self.app.post("/a2a/tasks/send", deprecated=True)
        async def send_task(request: TaskSendRequest):
            """接收并执行 A2A 任务（deprecated，请改用 POST /a2a message/send）."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            response = await self._a2a_server.handle_task(request)
            return response.model_dump()

        @self.app.get("/a2a/tasks/{task_id}", deprecated=True)
        async def get_task(task_id: str):
            """获取任务状态（deprecated，请改用 POST /a2a tasks/get）."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            task = self._a2a_server.get_task(task_id)
            if not task:
                return JSONResponse({"error": "任务不存在"}, status_code=404)
            return task.model_dump()

        @self.app.get("/a2a/tasks", deprecated=True)
        async def list_tasks():
            """列出所有任务（deprecated，自定义扩展端点）."""
            if not self._a2a_server:
                return JSONResponse({"error": "Agent 未初始化"}, status_code=500)
            tasks = self._a2a_server.list_tasks()
            return {"tasks": [t.model_dump() for t in tasks]}
        
        # A2A 客户端管理 API
        @self.app.get("/api/a2a/agents")
        async def list_a2a_agents():
            """列出已注册的远程 A2A agents."""
            if not self._agent or not self._agent.a2a_manager:
                return {"agents": []}
            agents = self._agent.a2a_manager.list_agents()
            return {"agents": agents}
        
        @self.app.post("/api/a2a/agents")
        async def add_a2a_agent(req: Request):
            """注册远程 A2A agent."""
            if not self._agent or not self._agent.a2a_manager:
                return JSONResponse({"error": "A2A 未启用"}, status_code=400)
            
            body = await req.json()
            name = body.get("name")
            url = body.get("url")
            
            if not name or not url:
                return JSONResponse({"error": "缺少 name 或 url"}, status_code=400)
            
            # SSRF 缓解：scheme/主机名校验 + 私有地址拦截（a2a/client.check_url_ssrf）
            try:
                parsed = urlparse(url)
            except Exception:
                parsed = None
            if not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname:
                return JSONResponse({"error": "仅支持 http/https 且包含主机名的 URL"}, status_code=400)
            
            try:
                self._agent.a2a_manager.add_agent(name, url)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return {"status": "ok", "agent": {"name": name, "url": url}}
        
        @self.app.delete("/api/a2a/agents/{name}")
        async def remove_a2a_agent(name: str):
            """移除远程 A2A agent."""
            if not self._agent or not self._agent.a2a_manager:
                return JSONResponse({"error": "A2A 未启用"}, status_code=400)
            
            removed = self._agent.a2a_manager.remove_agent(name)
            if not removed:
                return JSONResponse({"error": "Agent 不存在"}, status_code=404)
            return {"status": "ok"}
        
        @self.app.post("/api/a2a/send")
        async def send_to_a2a_agent(req: Request):
            """向远程 A2A agent 发送任务."""
            if not self._agent or not self._agent.a2a_manager:
                return JSONResponse({"error": "A2A 未启用"}, status_code=400)
            
            body = await req.json()
            agent_name = body.get("agent")
            message = body.get("message")
            
            if not agent_name or not message:
                return JSONResponse({"error": "缺少 agent 或 message"}, status_code=400)
            
            client = self._agent.a2a_manager.get_client(agent_name)
            if not client:
                return JSONResponse({"error": "Agent 不存在"}, status_code=404)

            # 防滥用：任务消息长度上限
            if len(message) > 100_000:
                return JSONResponse({"error": "任务消息过长（上限 100KB）"}, status_code=400)

            try:
                task = await client.send_task(message)
                return {
                    "status": "ok",
                    "task_id": task.id,
                    "task_status": task.status.state,
                    "response": task.messages[-1].parts[0].text if task.messages and task.messages[-1].role == "agent" else None
                }
            except Exception as e:
                return JSONResponse({"error": f"发送失败: {str(e)}"}, status_code=500)
