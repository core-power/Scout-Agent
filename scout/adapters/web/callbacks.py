"""Web 回调（WebSocket/事件推送）——chat 与 ws 路由共用（W4 抽出）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉，类体零改动。
"""

import asyncio

from scout.core.callbacks import Callbacks, NullCallbacks
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from datetime import datetime


class WebCallbacks(Callbacks):
    """Web 回调 — 直接通过 WebSocket 推送事件给前端."""

    def __init__(self, ws: WebSocket | None = None):
        self.ws = ws
        self.events: asyncio.Queue = asyncio.Queue()  # 保留兼容

    def set_ws(self, ws: WebSocket):
        self.ws = ws

    async def _push(self, event_type: str, data: dict):
        payload = {"type": event_type, "data": data, "timestamp": datetime.now().isoformat()}
        if self.ws:
            try:
                await self.ws.send_json(payload)
            except Exception:
                await self.events.put(payload)
        else:
            await self.events.put(payload)

    async def on_tool_progress(self, tool_name: str, stage: str, message: str, metadata: dict | None = None):
        data = {"tool_name": tool_name, "stage": stage, "message": message}
        if metadata:
            data["metadata"] = metadata
        await self._push("tool_progress", data)

    async def on_thinking(self, started: bool):
        await self._push("thinking", {"started": started})

    async def on_reasoning(self, content: str):
        await self._push("reasoning", {"content": content})

    async def on_clarify(self, question: str) -> str:
        await self._push("clarify", {"question": question})
        return ""

    async def on_step(self, step: int, total_budget: int):
        await self._push("step", {"step": step, "total": total_budget})

    async def on_stream_delta(self, text: str):
        await self._push("stream_delta", {"text": text})

    async def on_tool_gen(self, tool_name: str, args: dict):
        await self._push("tool_gen", {"tool_name": tool_name, "args": args})

    async def on_status(self, status: str):
        await self._push("status", {"status": status})

    async def on_reflection(self, hint: str):
        await self._push("reflection", {"hint": hint})

    async def on_goals_extracted(self, goals: list[dict]):
        await self._push("goals_extracted", {"goals": goals})

    async def on_confirm(self, request_id: str, tool_name: str, args: dict, reason: str) -> bool:
        """请求用户确认 — 通过 WebSocket 推送确认请求并等待响应.

        ★ 2026-09-09 修复：此前依赖 self._adapter（从未被赋值）注册 future，
        前端"批准"响应在 adapter._pending_confirmations 里查不到 request_id
        → 60s 超时自动拒绝，HITL 批准按钮永远无效。改为回调对象自持注册表，
        WS 处理器消费时直接读本表。
        """
        import asyncio
        # 创建 Future 等待用户响应
        future = asyncio.Future()
        # 注册到自持表（adapter 消费端同步读取本表）
        if not hasattr(self, "pending_confirmations"):
            self.pending_confirmations: dict = {}
        self.pending_confirmations[request_id] = future
        # 兼容旧路径：若已挂 adapter 引用则同步注册（SSE 等走 adapter 表的场景）
        if getattr(self, "_adapter", None):
            self._adapter._pending_confirmations[request_id] = future
        # 推送确认请求到前端
        await self._push("confirm_request", {
            "request_id": request_id,
            "tool_name": tool_name,
            "args": args,
            "reason": reason
        })
        # 等待用户响应（超时 60 秒）
        try:
            approved = await asyncio.wait_for(future, timeout=60.0)
            return approved
        except asyncio.TimeoutError:
            # 超时默认拒绝；清理注册表防泄漏
            self.pending_confirmations.pop(request_id, None)
            if getattr(self, "_adapter", None):
                self._adapter._pending_confirmations.pop(request_id, None)
            return False  # 超时默认拒绝

    async def on_file(self, file_path: str, file_name: str = "", file_size: int = 0) -> None:
        """推送文件事件给前端（2026-08-12 修复: 此前 file 事件只发 bus 不推 WebSocket）"""
        await self._push("file", {
            "file_path": file_path,
            "file_name": file_name or (file_path.split("/")[-1] if file_path else ""),
            "file_size": file_size,
        })
