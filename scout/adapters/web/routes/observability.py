"""观测路由组（/api/traces、/api/events、/api/observability）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class ObservabilityRoutes:
    """观测路由组（/api/traces、/api/events、/api/observability）（mixin）."""

    def _setup_trace_routes(self):
        """观测时间线聚合 API（2026-08-13）."""

        # ── 观测时间线：按会话聚合 trace 列表 ──

        @self.app.get("/api/traces/by-session")
        async def traces_by_session(limit: int = 30):
            """按 session 聚合最近的 trace（观测页左栏列表用）."""
            if not self._agent or not self._agent.observability:
                return {"sessions": []}
            obs = self._agent.observability
            conn = obs._get_conn()
            rows = conn.execute(
                """SELECT session_id,
                          COUNT(*) as trace_count,
                          MIN(user_message) as first_message,
                          MAX(start_time) as last_time,
                          COALESCE(SUM(total_tokens), 0) as tokens,
                          COALESCE(SUM(total_cost), 0.0) as cost,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as successes
                   FROM traces
                   GROUP BY session_id
                   ORDER BY last_time DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return {"sessions": [dict(r) for r in rows]}

        @self.app.get("/api/traces/session/{session_id}")
        async def traces_of_session(session_id: str):
            """某会话的全部 trace（观测页时间线用）."""
            if not self._agent or not self._agent.observability:
                return {"traces": []}
            obs = self._agent.observability
            conn = obs._get_conn()
            rows = conn.execute(
                "SELECT id FROM traces WHERE session_id = ? ORDER BY start_time ASC",
                (session_id,),
            ).fetchall()
            traces = []
            for r in rows:
                t = obs.get_trace(r["id"])
                if t:
                    traces.append(t.to_dict())
            return {"traces": traces}

    def _setup_event_routes(self):
        """事件历史 API."""

        # ── 事件历史 API ──

        @self.app.get("/api/events")
        async def get_events(limit: int = 20):
            """获取事件历史."""
            if self._agent and self._agent.bus:
                events = self._agent.bus.get_history(limit=limit)
                return {"events": events}
            return {"events": []}

        @self.app.get("/api/events/dlq")
        async def get_dlq(limit: int = 20):
            """获取死信队列（事件处理失败的记录）."""
            bus = (self._agent.bus if self._agent else None)
            if bus:
                return {"dlq": bus.get_dlq(limit=limit), "size": bus.dlq_size}
            # 无 agent 时尝试全局 bus
            try:
                from scout.bus.hub import bus as global_bus
                return {"dlq": global_bus.get_dlq(limit=limit), "size": global_bus.dlq_size}
            except Exception:
                return {"dlq": [], "size": 0}

        @self.app.delete("/api/events/dlq")
        async def clear_dlq():
            """清空死信队列."""
            bus = (self._agent.bus if self._agent else None)
            cleared = 0
            if bus:
                cleared = bus.clear_dlq()
            else:
                try:
                    from scout.bus.hub import bus as global_bus
                    cleared = global_bus.clear_dlq()
                except Exception:
                    pass
            return {"status": "ok", "cleared": cleared}

    def _setup_observability_routes(self):
        """可观测性 API."""

        @self.app.get("/api/traces")
        async def list_traces(limit: int = 20):
            """列出最近的追踪."""
            if not self._agent or not self._agent.observability:
                return {"traces": []}
            traces = self._agent.observability.list_recent_traces(limit=limit)
            return {"traces": traces}

        @self.app.get("/api/traces/{trace_id}")
        async def get_trace(trace_id: str):
            """获取追踪详情."""
            if not self._agent or not self._agent.observability:
                return JSONResponse({"error": "可观测性未启用"}, status_code=404)
            trace = self._agent.observability.get_trace(trace_id)
            if not trace:
                return JSONResponse({"error": "追踪不存在"}, status_code=404)
            return trace.to_dict()

        @self.app.get("/api/observability/stats")
        async def get_observability_stats(hours: int = 24):
            """获取可观测性统计."""
            if not self._agent or not self._agent.observability:
                return {"stats": {}}
            stats = self._agent.observability.get_stats(hours=hours)
            return {"stats": stats}
