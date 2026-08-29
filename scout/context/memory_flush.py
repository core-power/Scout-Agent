"""记忆 Flush — 借鉴 OpenClaw 的 memoryFlush.

在上下文压缩前，先把「将被压缩替换掉的关键信息」写入长期记忆，防止
压缩摘要丢失跨会话仍值得记住的内容（用户偏好/决策/结论/事实/技能）。

2026-08-27 重构（E4 闭环补充）：
- 此前本类仅被导出从未接入压缩流程，且 `_simple_flush` 会把用户消息
  原文整条写入记忆库（无类别、无去重，易污染长期记忆）；
- 现内部组合 :class:`scout.context.memory_extract.SessionMemoryExtractor`，
  flush 即委托其做结构化抽取（LLM/启发式降级 + min_importance 过滤 +
  Jaccard 去重），返回抽取摘要文本；
- 接入点：``ContextManager.compress(..., memory_flush=...)`` 在压缩前调用。
"""

from __future__ import annotations

from typing import Any

from scout.core.types import Message, Role, Session

# 延迟导入避免循环依赖（context/__init__ 同时导出两者）
from scout.context.memory_extract import SessionMemoryExtractor  # noqa: E402


class MemoryFlush:
    """记忆 flush — 压缩前提取关键信息写入长期记忆.

    Args:
        llm: 可选 LLM 客户端（结构化抽取；缺失时启发式降级）。
        memory_store: 长期记忆写入目标（``MemoryStore``）。
        extractor: 直接注入 ``SessionMemoryExtractor``（优先于 llm/memory_store）。
    """

    def __init__(
        self,
        llm: Any | None = None,
        memory_store: Any | None = None,
        extractor: SessionMemoryExtractor | None = None,
    ) -> None:
        if extractor is not None:
            self.extractor = extractor
        else:
            self.extractor = SessionMemoryExtractor(
                memory_store=memory_store, llm=llm
            )

    async def flush(
        self,
        session: Session,
        messages: list[Message] | None = None,
    ) -> str:
        """从会话（或其中被压缩的消息段）提取关键信息写入长期记忆.

        Args:
            session: 会话（提供 id 用于报告）。
            messages: 可选——只抽取这批消息（压缩场景传入将被替换的旧消息段）。

        Returns:
            抽取摘要文本（要点列表）；无抽取结果时返回空字符串。
        """
        if messages is not None:
            target = Session(id=session.id, messages=list(messages))
        else:
            target = session

        report = await self.extractor.extract(target)
        if not report.added:
            return ""
        return "\n".join(
            f"- [{i.kind}] {i.content}" for i in report.added[:5]
        )
