"""反馈路由组（/api/feedback：提交消息级 👍/👎 + 查询汇总）.

2026-09-24 新增：补齐「用户显式反馈」这一自进化链路缺失的最强信号源。
核心落盘/学习逻辑在 scout/memory/feedback.py（纯函数、可单测），本文件仅做
HTTP 封装与「点踩 → 记忆」旁路接入。
"""

from fastapi.responses import JSONResponse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class FeedbackRoutes:
    """反馈路由组（mixin，由 WebAdapter 继承）."""

    def _setup_feedback_routes(self):
        from scout.memory import feedback as fb

        @self.app.post("/api/feedback")
        async def submit_feedback(req: dict):
            """提交一条消息级反馈.

            body: {rating: "up"|"down", session_id?, message_id?, reason?,
                   comment?, question?, answer?}
            """
            req = req or {}
            rating = str(req.get("rating", "")).strip().lower()
            if rating not in ("up", "down"):
                return JSONResponse(
                    {"error": "rating 必须是 'up' 或 'down'"}, status_code=400
                )
            try:
                rec = fb.record_feedback(
                    rating=rating,
                    session_id=req.get("session_id", ""),
                    message_id=req.get("message_id", ""),
                    reason=req.get("reason", ""),
                    comment=req.get("comment", ""),
                    question=req.get("question", ""),
                    answer=req.get("answer", ""),
                )
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": f"落盘失败: {e}"}, status_code=500)

            # 点踩 → 接入记忆/自进化链路（尽力而为，失败不影响回执）
            if rating == "down":
                try:
                    ms = getattr(self._agent, "memory_store", None) if self._agent else None
                    fb.learn_from_negative(rec, ms)
                except Exception:
                    pass
            return {"ok": True, "id": rec["id"]}

        @self.app.get("/api/feedback")
        async def get_feedback(limit: int = 50, rating: str = ""):
            """查询最近反馈与汇总统计（供后续仪表盘/自省使用）."""
            return {
                "feedback": fb.list_feedback(limit=limit, rating=rating or None),
                "stats": fb.stats(),
            }
