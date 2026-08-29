"""技能检索器 — 在 Agent 主循环中动态注入历史沉淀技能.

工作流：
1. 用户请求进入 → 提取查询语义
2. 向量检索匹配的 SynthesizedSkill
3. 格式化为 prompt hint 注入 LLM 上下文
4. 执行后根据结果更新技能统计（正向/负向反馈）
"""

from __future__ import annotations

import logging
import time
from typing import Any

# VectorSkillStore imported lazily
from scout.engine.skill_types import SkillStatus, SynthesizedSkill
# Reranker imported lazily to avoid threading/numpy dependency chain

logger = logging.getLogger(__name__)


class SkillRetriever:
    """技能检索器 — 为当前任务找到最相关的历史技能."""

    def __init__(
        self,
        skill_store: Any,  # VectorSkillStore
        reranker: Any = None,  # Reranker
        max_skills: int = 3,
        min_similarity: float = 0.3,
    ):
        self.store = skill_store
        if reranker is not None:
            self.reranker = reranker
        else:
            from scout.memory.vector.reranker import KeywordReranker
            self.reranker = KeywordReranker()
        self.max_skills = max_skills
        self.min_similarity = min_similarity
        # 跟踪当前会话中注入的技能（用于后续反馈）
        self._injected_skills: list[SynthesizedSkill] = []

    async def retrieve_for_task(
        self,
        user_message: str,
        tool_name: str | None = None,
        error_context: str | None = None,
    ) -> list[SynthesizedSkill]:
        """为当前任务检索相关技能.

        Args:
            user_message: 用户输入
            tool_name: 当前要执行的工具（可选）
            error_context: 当前错误信息（可选，用于自愈场景）

        Returns:
            匹配的技能列表
        """
        # 构造查询：优先使用错误上下文，其次用户输入
        query = error_context or user_message
        tools_filter = [tool_name] if tool_name else None

        skills = await self.store.search_skills(
            query=query,
            top_k=self.max_skills * 2,
            min_success_rate=0.3,
            tools=tools_filter,
        )

        if not skills:
            return []

        # 转换为 reranker 格式
        rerank_input = []
        for s in skills:
            rerank_input.append({
                "id": s.id,
                "content": s.to_embedding_text(),
                "score": s.success_rate,
                "importance": s.success_rate,
                "time_decay": self._time_decay(s.last_used_at),
            })

        # 重排序
        reranked = await self.reranker.rerank(
            query=query,
            results=rerank_input,
            top_k=self.max_skills,
        )

        # 映射回 SynthesizedSkill
        result = []
        for r in reranked:
            skill = await self.store.get_skill(r["id"])
            if skill:
                result.append(skill)

        self._injected_skills = result
        return result

    async def retrieve_for_error(
        self,
        tool_name: str,
        error_message: str,
    ) -> list[SynthesizedSkill]:
        """专门为错误修复场景检索技能."""
        return await self.retrieve_for_task(
            user_message=error_message,
            tool_name=tool_name,
            error_context=f"{tool_name} error: {error_message}",
        )

    def format_as_prompt_hint(self, skills: list[SynthesizedSkill]) -> str:
        """将技能列表格式化为 LLM 可理解的提示词片段.

        这段文本会被注入到 system prompt 中。
        """
        if not skills:
            return ""

        lines = [
            "## 📚 历史经验参考",
            "以下是从历史修复记录中匹配到的相关经验，请优先参考：",
            "",
        ]

        for i, skill in enumerate(skills, 1):
            lines.append(f"### 经验 {i}: {skill.name}")
            lines.append(f"- **问题**: {skill.intent}")
            lines.append(f"- **成功率**: {skill.success_rate:.0%} (使用 {skill.usage_count} 次)")
            if skill.solution_template:
                lines.append(f"- **解决方案模板**:")
                lines.append(f"```")
                lines.append(skill.solution_template)
                lines.append(f"```")
            lines.append("")

        lines.append("请根据当前任务的具体情况，参考上述经验进行调整。")
        return "\n".join(lines)

    async def record_feedback(
        self,
        skill_id: str,
        success: bool,
    ) -> None:
        """记录技能使用反馈 — 更新成功率和计数."""
        skill = await self.store.get_skill(skill_id)
        if not skill:
            return

        skill.record_usage(success)

        # 检查是否应该弃用
        if skill.should_deprecate():
            await self.store.deprecate_skill(skill_id)
            logger.info(f"Skill {skill.name} deprecated due to low success rate")
        else:
            await self.store.update_skill(skill)

    async def record_session_feedback(
        self,
        session_success: bool,
    ) -> None:
        """对整个会话中注入的所有技能记录反馈."""
        for skill in self._injected_skills:
            await self.record_feedback(skill.id, session_success)
        self._injected_skills.clear()

    def _time_decay(self, last_used: float) -> float:
        """计算时间衰减因子."""
        if not last_used:
            return 0.5
        hours = (time.time() - last_used) / 3600
        # 24小时内不衰减，之后线性衰减
        if hours < 24:
            return 1.0
        return max(0.3, 1.0 - (hours - 24) / 720)  # 30天衰减到0.3
