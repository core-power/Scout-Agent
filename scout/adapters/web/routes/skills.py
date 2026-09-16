"""技能管理路由组（/api/skills 列表/删除）.

W4 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi.responses import JSONResponse, Response
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class SkillRoutes:
    """技能管理路由组（/api/skills 列表/删除）（mixin）."""

    def _setup_skill_routes(self):
        """技能 API."""

        # ── 技能 API ──

        @self.app.get("/api/skills")
        async def list_skills():
            """列出所有技能."""
            if self._agent and self._agent.skill_mgr:
                skills = self._agent.skill_mgr.list_skills()
                return {"skills": [s.model_dump() for s in skills]}
            return {"skills": []}

        @self.app.delete("/api/skills/{name}")
        async def delete_skill(name: str):
            """卸载技能（从 $SCOUT_DATA_DIR/skills 删除 SKILL.md 目录）."""
            if not self._agent or not getattr(self._agent, "skill_mgr", None):
                return JSONResponse({"error": "技能系统未启用"}, status_code=503)
            ok = self._agent.skill_mgr.remove_skill(name, scope="user")
            if not ok:
                return JSONResponse({"error": f"技能 {name} 不存在或已删除"}, status_code=404)
            return {"status": "ok", "message": f"已卸载技能 {name}"}
