"""记忆路由组（/api/memory：搜索/增删改）.

W3 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class MemoryRoutes:
    """记忆路由组（/api/memory：搜索/增删改）（mixin）."""

    def _setup_memory_routes(self):
        """记忆系统 API."""

        # ── 记忆 API ──

        @self.app.get("/api/memory")
        async def list_memory(limit: int = 20):
            """列出记忆."""
            if self._agent and self._agent.memory_store:
                memories = self._agent.memory_store.list_recent(limit=limit)
                return {"memories": [m.to_dict() for m in memories]}
            return {"memories": []}

        @self.app.get("/api/memory/search")
        async def search_memory(q: str = "", limit: int = 20):
            """搜索记忆."""
            if self._agent and self._agent.memory_store and q:
                results = self._agent.memory_store.search(q, limit=limit)
                return {"memories": [m.to_dict() for m in results]}
            return {"memories": []}

        @self.app.post("/api/memory")
        async def add_memory(req: dict):
            """手动添加记忆."""
            if self._agent and self._agent.memory_store:
                content = req.get("content", "").strip()
                if content:
                    self._agent.memory_store.add(
                        content=content,
                        category=req.get("category", "general"),
                        importance=req.get("importance", 0.5),
                    )
                    return {"status": "ok"}
                return JSONResponse({"error": "内容不能为空"}, status_code=400)
            return JSONResponse({"error": "记忆系统未启用"}, status_code=400)

        @self.app.delete("/api/memory/{memory_id}")
        async def delete_memory(memory_id: int):
            """删除记忆."""
            if self._agent and self._agent.memory_store:
                self._agent.memory_store.delete(memory_id)
                return {"status": "ok"}
            return JSONResponse({"error": "记忆系统未启用"}, status_code=400)

        @self.app.put("/api/memory/{memory_id}")
        async def update_memory(memory_id: int, req: Request):
            """更新记忆内容."""
            if not self._agent or not self._agent.memory_store:
                return JSONResponse({"error": "记忆系统未启用"}, status_code=400)
            body = await req.json()
            self._agent.memory_store.update(
                memory_id,
                content=body.get("content"),
                category=body.get("category"),
                importance=body.get("importance"),
            )
            return {"status": "ok"}
