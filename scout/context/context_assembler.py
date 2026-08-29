"""跨会话上下文组装 — 为新回合组装「相关记忆 + 历史会话摘要」上下文.

对标 DeepSeek Harness「上下文压缩」的跨会话维度（docs/dsh-comparison.md E4）：

- 单会话内压缩 / 剪枝由 :class:`scout.context.manager.ContextManager` 负责；
- 跨会话由本模块负责：把历史会话沉淀的记忆与摘要按「相关性 × 重要性 ×
  时间衰减」排序，在预算内组装成纯文本（供 ``<memories>`` / ``<summary>``
  注入 ``runtime_context``），实现跨会话记忆复用而不撑爆上下文窗口。

用法::

    from scout.context import ContextAssembler

    assembler = ContextAssembler(memory_store=store, session_store=s_store)
    memory_text, summary_text = await assembler.assemble(
        query="继续优化爬虫", exclude_session_id="sess-1"
    )
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 会话状态：视为"已结束、可贡献摘要"的状态
_DONE_STATUSES = ("done", "completed", "error")


def _rank_score(entry: Any) -> float:
    """记忆排序分：优先 decay_score（importance × 时间衰减），兼容其他实现."""
    if hasattr(entry, "decay_score"):
        try:
            return entry.decay_score()
        except Exception:
            pass
    f = getattr(entry, "rank_score", None)
    if callable(f):
        try:
            return f()
        except Exception:
            pass
    return getattr(entry, "importance", 0.5)


def _truncate_budget(text: str, budget: int) -> str:
    """按字符预算截断文本：超出时保留头部，尾部提示省略."""
    if not text:
        return ""
    if len(text) <= budget:
        return text
    return text[: budget - 30].rstrip() + "\n…（已截断）"


class ContextAssembler:
    """跨会话上下文组装器.

    Args:
        memory_store: ``MemoryStore`` 实例（跨会话记忆召回源）。
        session_store: ``SessionStore`` 实例（历史会话摘要源，可空）。
        max_memory_chars: 记忆块总预算（字符）。
        max_summary_chars: 历史摘要总预算（字符）。
        memory_limit: 单次召回的最终记忆条数。
        recall_multiplier: 召回时多取几倍候选，排序后截断到 ``memory_limit``。
    """

    def __init__(
        self,
        memory_store: Any | None = None,
        session_store: Any | None = None,
        max_memory_chars: int = 900,
        max_summary_chars: int = 600,
        memory_limit: int = 5,
        recall_multiplier: int = 3,
    ) -> None:
        self.memory_store = memory_store
        self.session_store = session_store
        self.max_memory_chars = max_memory_chars
        self.max_summary_chars = max_summary_chars
        self.memory_limit = memory_limit
        self.recall_multiplier = max(1, recall_multiplier)

    # ── 记忆块 ──────────────────────────────────────────────────────────
    async def build_memory_context(
        self, query: str, budget_chars: int | None = None
    ) -> str:
        """跨会话记忆召回 → 按 rank_score 排序 → 预算截断 → 纯文本.

        输出格式与 agent._inject_context 既有约定一致：每行 ``- content``，
        由调用方包裹进 ``<memories>`` 标签。
        """
        budget = budget_chars if budget_chars is not None else self.max_memory_chars
        if not self.memory_store or not query:
            return ""
        try:
            candidates = await self.memory_store.search_async(
                query, limit=self.memory_limit * self.recall_multiplier
            )
        except Exception as exc:
            logger.debug("记忆召回失败: %s", exc)
            return ""
        if not candidates:
            return ""

        # decay_score = importance × 时间衰减（MemoryEntry.decay_score）
        ranked = sorted(candidates, key=_rank_score, reverse=True)[: self.memory_limit]

        lines = []
        for m in ranked:
            text = (getattr(m, "content", "") or "").strip()
            if not text:
                continue
            text = text.replace("\n", " ")
            text = text[:300]
            kind = getattr(m, "category", "") or "general"
            lines.append(f"- [{kind}] {text}")
        return _truncate_budget("\n".join(lines), budget)

    # ── 历史会话摘要 ────────────────────────────────────────────────────
    async def build_session_summary(
        self,
        exclude_session_id: str | None = None,
        budget_chars: int | None = None,
        max_sessions: int = 3,
    ) -> str:
        """最近已完成会话的摘要 → 纯文本（供 ``<summary>`` 注入）.

        每个会话取：标题（若有）+ extra 中压缩摘要（若有）；均缺省时
        回退为该会话首条 user 消息的前 60 字符。
        """
        budget = budget_chars if budget_chars is not None else self.max_summary_chars
        if not self.session_store:
            return ""
        try:
            sessions = await self.session_store.async_list(limit=max_sessions + 2)
        except Exception as exc:
            logger.debug("历史会话列表失败: %s", exc)
            return ""

        lines: list[str] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            if s.get("status") not in _DONE_STATUSES:
                continue
            if exclude_session_id and s.get("id") == exclude_session_id:
                continue
            if len(lines) >= max_sessions:
                break
            extra = s.get("extra") or {}
            title = str(extra.get("title") or "").strip()
            summary = str(extra.get("summary") or "").strip()
            if title:
                lines.append(f"- 会话《{title}》")
            if summary:
                lines.append(f"  {summary[:200]}")
        return _truncate_budget("\n".join(lines), budget)

    # ── 组合入口 ────────────────────────────────────────────────────────
    async def assemble(
        self,
        query: str,
        exclude_session_id: str | None = None,
        max_memory_chars: int | None = None,
        max_summary_chars: int | None = None,
    ) -> tuple[str, str]:
        """一次组装记忆块与历史摘要块.

        Returns:
            ``(memory_text, summary_text)`` 纯文本对；任一为空字符串表示
            该块无可用内容（调用方据此省略对应标签）。
        """
        memory_text = await self.build_memory_context(
            query, budget_chars=max_memory_chars
        )
        summary_text = await self.build_session_summary(
            exclude_session_id=exclude_session_id, budget_chars=max_summary_chars
        )
        return memory_text, summary_text
