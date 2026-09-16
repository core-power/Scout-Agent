"""目标与任务路由组（/api/goals、/api/tasks）.

W3 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class GoalRoutes:
    """目标与任务路由组（/api/goals、/api/tasks）（mixin）."""

    def _setup_goal_routes(self):
        """目标管理 API."""

        @self.app.get("/api/goals")
        async def list_goals():
            """列出活跃目标."""
            if not self._agent or not self._agent.goal_manager:
                return {"goals": []}
            goals = self._agent.goal_manager.list_active_goals()
            return {
                "goals": [
                    {
                        "id": g.id,
                        "title": g.title,
                        "description": g.description,
                        "status": g.status,
                        "progress": g.overall_progress,
                        "tasks_count": len(g.tasks),
                        "completed_tasks": g.completed_tasks,
                        "created_at": g.created_at.isoformat(),
                    }
                    for g in goals
                ]
            }

        @self.app.get("/api/goals/{goal_id}")
        async def get_goal(goal_id: str):
            """获取目标详情."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=404)
            goal = self._agent.goal_manager.get_goal(goal_id)
            if not goal:
                return JSONResponse({"error": "目标不存在"}, status_code=404)
            return {
                "id": goal.id,
                "title": goal.title,
                "description": goal.description,
                "status": goal.status,
                "progress": goal.overall_progress,
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description,
                        "status": t.status,
                        "progress": t.progress,
                        "created_at": t.created_at.isoformat(),
                        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                    }
                    for t in goal.tasks
                ],
                "created_at": goal.created_at.isoformat(),
            }

        @self.app.post("/api/goals")
        async def create_goal(req: Request):
            """创建新目标."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            title = body.get("title")
            description = body.get("description", "")
            if not title:
                return JSONResponse({"error": "标题不能为空"}, status_code=400)
            goal = self._agent.goal_manager.create_goal(title, description)
            return {"status": "ok", "goal_id": goal.id}

        @self.app.post("/api/goals/{goal_id}/tasks")
        async def add_task(goal_id: str, req: Request):
            """为目标添加任务."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            title = body.get("title")
            description = body.get("description", "")
            if not title:
                return JSONResponse({"error": "标题不能为空"}, status_code=400)
            task = self._agent.goal_manager.add_task(goal_id, title, description)
            if not task:
                return JSONResponse({"error": "目标不存在"}, status_code=404)
            return {"status": "ok", "task_id": task.id}

        @self.app.put("/api/tasks/{task_id}")
        async def update_task(task_id: str, req: Request):
            """更新任务进度."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            progress = body.get("progress")
            status = body.get("status")
            if progress is not None:
                self._agent.goal_manager.update_task_progress(task_id, progress, status)
            return {"status": "ok"}

        @self.app.put("/api/goals/{goal_id}")
        async def update_goal(goal_id: str, req: Request):
            """更新目标状态."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            body = await req.json()
            status = body.get("status")
            if status and status in ("active", "completed", "paused", "abandoned"):
                self._agent.goal_manager.update_goal_status(goal_id, status)
            return {"status": "ok"}

        @self.app.delete("/api/goals/{goal_id}")
        async def delete_goal(goal_id: str):
            """删除目标及其所有任务."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            self._agent.goal_manager.delete_goal(goal_id)
            return {"status": "ok"}

        @self.app.delete("/api/tasks/{task_id}")
        async def delete_task(task_id: str):
            """删除任务."""
            if not self._agent or not self._agent.goal_manager:
                return JSONResponse({"error": "目标管理未启用"}, status_code=400)
            self._agent.goal_manager.delete_task(task_id)
            return {"status": "ok"}
