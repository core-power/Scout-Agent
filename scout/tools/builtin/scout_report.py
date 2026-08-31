"""自我报告工具 — 汇总 Scout 自身运行状况（对话/自动化/技能/记忆）.

数据全部来自本地存储（零外部依赖）：
- ~/.scout/observability.db: 对话轮次、成功率、token、成本、热门工具
- ~/.scout/runs.db: 自动化任务运行与成功率
- ~/.scout/introspection_log.json: 自省动作（技能淘汰/记忆合并）
- ~/.scout/skills.db (VectorSkillStore): 沉淀技能健康度
- ~/.scout/memory.db: 记忆总量与当日新增

配合 scheduler 工具的 ai_task 可实现定时自我报告，例如：
"每天早上9点调用 scout_report 生成自我报告并发送给我"
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

_INTROSPECTION_LOG = _SCOUT_DATA_DIR / "introspection_log.json"


class ScoutReportTool(ToolDefinition):
    """自我报告 — 生成 Scout 运行状况汇总."""

    name = "scout_report"
    description = (
        "生成 Scout 自身的运行状况报告（markdown 文本）：对话轮次与成功率、"
        "Token 消耗与成本、热门工具、自动化任务统计、自省动作、技能库健康度、记忆库统计。"
        "当用户询问\"最近运行得怎么样/自我报告/日报/状态汇总\"时使用。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "hours": {
                "type": "integer",
                "description": "统计时间窗口（小时），默认 24",
            },
        },
    }
    annotations = ToolAnnotations(read_only=True, open_world=False)

    async def execute(self, hours: int = 24) -> Observation:
        try:
            hours = max(1, min(int(hours), 24 * 7))
        except (TypeError, ValueError):
            hours = 24

        lines = [
            f"📊 **Scout 自我报告** · {datetime.now().strftime('%Y-%m-%d %H:%M')} · 近 {hours}h",
            "",
        ]
        self._section_conversation(lines, hours)
        self._section_automation(lines, hours)
        self._section_introspection(lines, hours)
        self._section_skills(lines)
        self._section_memory(lines)
        return Observation(
            tool_name=self.name,
            success=True,
            output="\n".join(lines),
        )

    # ── 各数据源 ──

    def _section_conversation(self, lines: list, hours: int) -> None:
        try:
            from scout.engine.observability import ObservabilityTracker
            stats = ObservabilityTracker().get_stats(hours=hours)
        except Exception:
            return
        lines.append("**对话与消耗**")
        lines.append(f"- 对话 {stats['total_traces']} 轮，成功率 {stats['success_rate']:.0%}")
        if stats.get("total_tokens"):
            cost = stats.get("total_cost") or 0
            lines.append(
                f"- Token 消耗 {stats['total_tokens']:,}"
                + (f"，成本 ¥{cost:.3f}" if cost else "")
            )
        top_tools = stats.get("top_tools") or []
        if top_tools:
            names = "、".join(f"{t['name']}×{t['calls']}" for t in top_tools[:5])
            lines.append(f"- 热门工具: {names}")
        lines.append("")

    def _section_automation(self, lines: list, hours: int) -> None:
        try:
            from scout.engine.runs import RunStore
            days = max(1, -(-hours // 24))  # 向上取整天数
            run_stats = RunStore().stats(days=days)
        except Exception:
            return
        total = run_stats.get("total", 0)
        lines.append("**自动化任务**")
        if total == 0:
            lines.append("- 无自动化任务运行")
        else:
            lines.append(f"- 共 {total} 次运行")
            for src, bucket in run_stats.get("by_source", {}).items():
                rate = bucket.get("success_rate")
                rate_str = f"{rate:.0%}" if rate is not None else "-"
                detail = f"- {src}: {bucket['total']} 次，成功率 {rate_str}"
                if bucket.get("verification_failed"):
                    detail += f"，验证失败 {bucket['verification_failed']} 次"
                lines.append(detail)
        lines.append("")

    def _section_introspection(self, lines: list, hours: int) -> None:
        try:
            if not _INTROSPECTION_LOG.exists():
                return
            log = json.loads(_INTROSPECTION_LOG.read_text(encoding="utf-8"))
        except Exception:
            return
        cutoff = time.time() - hours * 3600
        actions = []
        for entry in log:
            try:
                ts = datetime.strptime(entry.get("ts", ""), "%Y-%m-%d %H:%M:%S").timestamp()
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                actions.extend(entry.get("actions", []))
        lines.append("**自我维护**")
        if actions:
            for a in actions[:8]:
                lines.append(f"- {a}")
        else:
            lines.append("- 无自省动作（技能库与记忆库健康）")
        lines.append("")

    def _section_skills(self, lines: list) -> None:
        got = False
        try:
            from scout.engine.skill_store import VectorSkillStore
            sstats = VectorSkillStore().stats()
            lines.append("**技能库**")
            lines.append(
                f"- 共 {sstats['total_skills']} 个沉淀技能"
                f"（活跃 {sstats['active']}，弃用 {sstats['deprecated']}），"
                f"平均成功率 {sstats['avg_success_rate']:.0%}，累计使用 {sstats['total_usage']} 次"
            )
            got = True
        except Exception:
            pass
        try:
            from scout.context.skills import SkillManager
            file_skills = SkillManager().list_skills()
            if file_skills:
                if not got:
                    lines.append("**技能库**")
                names = "、".join(s.name for s in file_skills[:5])
                more = f" 等 {len(file_skills)} 个" if len(file_skills) > 5 else ""
                lines.append(f"- 文件技能: {names}{more}")
                got = True
        except Exception:
            pass
        if got:
            lines.append("")

    def _section_memory(self, lines: list) -> None:
        try:
            from scout.memory.store import MemoryStore
            mem = MemoryStore()
            total = mem.count()
            conn = mem._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) as n FROM memories WHERE date(created_at) = date('now', 'localtime')"
            ).fetchone()
            new_today = row["n"] if row else 0
            lines.append("**记忆库**")
            lines.append(f"- 共 {total} 条记忆，今日新增 {new_today} 条")
            lines.append("")
        except Exception:
            pass


ToolRegistry.register(ScoutReportTool())
