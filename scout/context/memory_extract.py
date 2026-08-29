"""跨会话关键记忆抽取 — 会话结束后把重要信息沉淀为可复用的长期记忆.

对标 DeepSeek Harness「记忆工程化」的跨会话维度（docs/dsh-comparison.md E4）：

- 单会话内的压缩 / 剪枝由 :class:`scout.context.manager.ContextManager` 负责；
- 会话结束后的关键信息（用户偏好、决策、结论、事实、沉淀技能）由本模块
  抽取为结构化长期记忆写入 ``MemoryStore``，供后续会话 ``<memories>`` 召回复用；
- 抽取带类别 / 重要性标注，并与库内已有记忆做 Jaccard 相似度去重，
  避免记忆库被重复信息污染；
- 支持对历史已完成会话批量补抽取（``extract_pending_sessions``）。

用法::

    from scout.context import SessionMemoryExtractor
    from scout.memory.store import MemoryStore

    extractor = SessionMemoryExtractor(memory_store=MemoryStore())
    report = await extractor.extract(session)
    print(report.summary())
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from scout.core.types import Message, Role, Session, LLMResponse

logger = logging.getLogger(__name__)

# 记忆类型 → 默认重要性（无 LLM 降级与 LLM 缺省时兜底）
KIND_DEFAULT_IMPORTANCE: dict[str, float] = {
    "preference": 0.85,  # 用户偏好：长期有效
    "decision": 0.75,    # 关键决策
    "conclusion": 0.70,  # 结论 / 总结
    "skill": 0.80,       # 沉淀技能 / 经验
    "fact": 0.60,        # 事实 / 需求
    "general": 0.50,
}
KINDS: tuple[str, ...] = ("preference", "decision", "conclusion", "skill", "fact", "general")

# 需忽略的消息：压缩摘要、归档块、注入块
_SKIP_METADATA_TYPES = {"compression", "archived", "system"}
_SKIP_CONTENT_MARKERS = (
    "<runtime_context>", "<memories>", "<summary>",
    "skill_match", "memory_extracted", "以下为历史会话",
)

# 启发式信号词
_PREFERENCE_WORDS = (
    "我喜欢", "我希望", "请务必", "请一定", "请记住", "以后", "下次",
    "偏好", "倾向", "习惯", "prefer", "preference", "please", "always",
)
_AVOID_WORDS = ("不要", "禁止", "别用", "避免", "don't", "never", "avoid", "please don't")
_DECISION_WORDS = (
    "决定", "采用", "选定", "确定用", "就用", "换成", "改为", "方案是",
    "decision", "choose", "switch to",
)
_CONCLUSION_WORDS = (
    "结论", "总结", "综上", "因此", "最终", "总而言之",
    "conclusion", "summary", "in short", "in summary",
)
_SKILL_WORDS = ("步骤", "命令", "代码", "用法", "教程", "step", "command", "recipe")


def _strip_content_tags(text: str) -> str:
    """剥离 runtime_context 注入块与 XML 标签，只保留对话正文."""
    text = re.sub(r"<runtime_context>[\s\S]*?</runtime_context>", "", text)
    text = re.sub(r"<memories>[\s\S]*?</memories>", "", text)
    text = re.sub(r"<summary>[\s\S]*?</summary>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _tokenize(text: str) -> set[str]:
    """轻量分词：中文按字符 + 英文按单词，用于 Jaccard 相似度."""
    cn = re.findall(r"[\u4e00-\u9fff]", text)
    en = re.findall(r"[a-zA-Z0-9_]{2,}", text.lower())
    return set(cn) | set(en)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class ExtractedItem:
    """一条待写入的抽取结果."""
    content: str
    kind: str = "fact"
    importance: float = 0.6
    source_turn: int = 0


@dataclass
class ExtractReport:
    """一次抽取的结果报告."""
    session_id: str
    added: list[ExtractedItem] = field(default_factory=list)
    skipped_duplicates: list[ExtractedItem] = field(default_factory=list)
    skipped_low_importance: list[ExtractedItem] = field(default_factory=list)
    used_llm: bool = False

    @property
    def items(self) -> list[ExtractedItem]:
        return self.added

    def summary(self) -> str:
        return (
            f"session={self.session_id} added={len(self.added)} "
            f"dups={len(self.skipped_duplicates)} low={len(self.skipped_low_importance)} "
            f"llm={self.used_llm}"
        )


_LLM_EXTRACT_PROMPT = """你是记忆工程器。从下面的对话中抽取"跨会话仍值得记住"的关键信息：
1. preference：用户偏好（喜欢/不喜欢/习惯/长期要求）
2. decision：关键决策（采用了什么方案/工具/配置）
3. conclusion：结论（问题的最终答案/总结）
4. fact：重要事实/需求背景
5. skill：沉淀技能（可复用的经验/命令/代码模式）
要求：
- 只输出 JSON（无任何其他文字）：{{"memories":[{{"content":"...","kind":"preference|decision|conclusion|fact|skill","importance":0.0-1.0}}]}}
- content 用一句话概括，不含标签
- 忽略寒暄、工具输出细节、临时性内容
对话：
{snippet}"""


class SessionMemoryExtractor:
    """会话记忆抽取器 — 从会话消息中抽取结构化长期记忆（去重后写入 MemoryStore）.

    Args:
        memory_store: ``MemoryStore`` 实例（写入与去重查询目标）。
        llm: 可选 LLM 客户端（``LLMClient.complete`` 协议）。提供后优先走
            结构化抽取；失败或未提供时降级为启发式抽取。
        min_importance: 低于该重要性的抽取项被丢弃。
        dedup_threshold: 与库内已有记忆的 Jaccard 相似度阈值，≥ 视为重复。
        max_input_chars: 送 LLM 的对话片段最大长度（截尾）。
    """

    def __init__(
        self,
        memory_store: Any | None = None,
        llm: Any | None = None,
        min_importance: float = 0.5,
        dedup_threshold: float = 0.85,
        max_input_chars: int = 6000,
    ) -> None:
        self.memory_store = memory_store
        self.llm = llm
        self.min_importance = min_importance
        self.dedup_threshold = dedup_threshold
        self.max_input_chars = max_input_chars

    # ── 主入口 ──────────────────────────────────────────────────────────
    async def extract(
        self,
        session: Session,
        memory_store: Any | None = None,
        llm: Any | None = None,
    ) -> ExtractReport:
        """抽取会话中的关键记忆并（在提供 store 时）去重写入.

        Returns:
            :class:`ExtractReport`：added / skipped_duplicates /
            skipped_low_importance 明细。
        """
        store = memory_store or self.memory_store
        llm_client = llm if llm is not None else self.llm
        report = ExtractReport(session_id=session.id)
        if not session.messages:
            return report

        items: list[ExtractedItem] = []
        if llm_client is not None:
            try:
                items = await self._llm_extract(session, llm_client)
                report.used_llm = True
            except Exception as exc:  # 结构化失败 → 降级，不阻塞主流程
                logger.warning("LLM 结构化抽取失败，降级启发式: %s", exc)
                items = []
        if not items:
            items = self._heuristic_extract(session)

        for item in items:
            if item.importance < self.min_importance:
                report.skipped_low_importance.append(item)
                continue
            if store is not None and self._dedup(store, item):
                report.skipped_duplicates.append(item)
                continue
            if store is not None:
                try:
                    store.add(item.content, category=item.kind, importance=item.importance)
                except Exception as exc:
                    logger.warning("记忆写入失败 %r: %s", item.content[:20], exc)
                    continue
            report.added.append(item)
        return report

    # ── LLM 结构化抽取 ──────────────────────────────────────────────────
    async def _llm_extract(self, session: Session, llm: Any) -> list[ExtractedItem]:
        parts: list[str] = []
        for msg in session.messages:
            if msg.metadata.get("type") in _SKIP_METADATA_TYPES:
                continue
            text = _strip_content_tags(msg.content)
            if text:
                parts.append(f"{msg.role.value}: {text}")
        snippet = "\n".join(parts)[-self.max_input_chars:]
        prompt = _LLM_EXTRACT_PROMPT.format(snippet=snippet)

        resp: LLMResponse = await llm.complete([{"role": "user", "content": prompt}])
        raw = resp.content.strip()
        # 容忍 ```json 围栏
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
        memories = data.get("memories") or data if isinstance(data, list) else data.get("memories", [])
        items: list[ExtractedItem] = []
        for m in memories if isinstance(memories, list) else []:
            content = str(m.get("content", "")).strip()
            if not content:
                continue
            kind = str(m.get("kind", "general"))
            if kind not in KINDS:
                kind = "general"
            try:
                importance = float(m.get("importance", KIND_DEFAULT_IMPORTANCE.get(kind, 0.5)))
            except (TypeError, ValueError):
                importance = KIND_DEFAULT_IMPORTANCE.get(kind, 0.5)
            importance = max(0.0, min(1.0, importance))
            items.append(ExtractedItem(content=content, kind=kind, importance=importance))
        return items

    # ── 启发式降级抽取 ──────────────────────────────────────────────────
    def _heuristic_extract(self, session: Session) -> list[ExtractedItem]:
        items: list[ExtractedItem] = []
        for idx, msg in enumerate(session.messages):
            if msg.role not in (Role.USER, Role.ASSISTANT):
                continue
            if msg.metadata.get("type") in _SKIP_METADATA_TYPES:
                continue
            text = _strip_content_tags(msg.content)
            if not text or any(m in text for m in _SKIP_CONTENT_MARKERS):
                continue
            for line in text.splitlines():
                line = line.strip()
                if len(line) < 8:
                    continue
                if msg.role == Role.USER:
                    kind, importance = self._classify_user_line(line)
                else:
                    kind, importance = self._classify_assistant_line(line)
                # 命中类别的候选全部保留；min_importance 过滤统一由 extract() 裁决
                if kind:
                    items.append(
                        ExtractedItem(
                            content=line, kind=kind, importance=importance, source_turn=idx
                        )
                    )
        return items

    @staticmethod
    def _classify_user_line(line: str) -> tuple[str, float]:
        low = line.lower()
        if any(w in low for w in _AVOID_WORDS):
            return ("preference", 0.9)
        if any(w in low for w in _PREFERENCE_WORDS):
            return ("preference", 0.85)
        if any(w in low for w in _DECISION_WORDS):
            return ("decision", 0.75)
        return ("fact", 0.6)

    @staticmethod
    def _classify_assistant_line(line: str) -> tuple[str, float]:
        low = line.lower()
        if any(w in low for w in _CONCLUSION_WORDS):
            return ("conclusion", 0.7)
        if len(line) > 60 and any(w in low for w in _SKILL_WORDS):
            return ("skill", 0.8)
        return ("", 0.0)  # assistant 普通语句不抽取，避免噪声

    # ── 去重 ────────────────────────────────────────────────────────────
    def _dedup(self, store: Any, item: ExtractedItem) -> bool:
        """与库内已有记忆比较相似度，超过阈值视为重复."""
        try:
            candidates = store.search(item.content, limit=5)
        except Exception:
            return False
        item_tokens = _tokenize(item.content)
        for cand in candidates:
            if _jaccard(item_tokens, _tokenize(cand.content)) >= self.dedup_threshold:
                return True
        return False

    # ── 会话抽取状态标记（防重复抽取）───────────────────────────────────
    async def mark_extracted(
        self, session_store: Any | None, session_id: str, tag: str = "memory_extracted_at"
    ) -> None:
        """在会话 extra 中记录抽取时间戳，防止重复抽取."""
        if session_store is None:
            return
        try:
            await session_store.async_update_extra(session_id, {tag: datetime.now().isoformat()})
        except Exception as exc:
            logger.warning("标记会话 %s 抽取状态失败: %s", session_id, exc)

    async def is_extracted(
        self, session_store: Any | None, session_id: str, tag: str = "memory_extracted_at"
    ) -> bool:
        if session_store is None:
            return False
        try:
            s = await session_store.async_get(session_id)
            extra = s.get("extra", {}) if isinstance(s, dict) else {}
            return bool(extra.get(tag))
        except Exception:
            return False

    # ── 批量补抽取 ──────────────────────────────────────────────────────
    async def extract_pending_sessions(
        self,
        session_store: Any,
        memory_store: Any | None = None,
        limit: int = 10,
        done_statuses: tuple[str, ...] = ("done", "completed", "error"),
        tag: str = "memory_extracted_at",
    ) -> list[ExtractReport]:
        """扫描已完成但未抽取过的会话，逐个抽取并标记.

        Args:
            session_store: ``SessionStore`` 实例（列表 / 标记）。
            memory_store: 记忆写入目标；缺省用构造时的实例。
            limit: 最多扫描的会话数（按 updated_at 倒序）。
            done_statuses: 视为"已结束"的会话状态集合。
            tag: extra 中的抽取标记键名。

        Returns:
            每个已结束会话对应的 :class:`ExtractReport` 列表。
        """
        store = memory_store or self.memory_store
        reports: list[ExtractReport] = []
        sessions = await session_store.async_list(limit=limit)
        for s in sessions:
            if not isinstance(s, dict):
                continue
            if s.get("status") not in done_statuses:
                continue
            sid = s.get("id", "")
            if not sid or await self.is_extracted(session_store, sid, tag):
                continue
            # ★ 必须加载完整消息：仅用 id/status 构造的空 Session 会让
            #   extract() 因 `not session.messages` 直接空转，批量补抽取永远无效
            try:
                loaded = session_store.load_session(sid)
                session = loaded if (loaded and getattr(loaded, "messages", None)) else None
            except Exception:
                session = None
            if session is None:
                session = Session(id=sid, status=s.get("status", "done"), extra=s.get("extra", {}))
            report = await self.extract(session, memory_store=store)
            # 无论是否抽出记忆都标记，避免空会话被反复扫描
            await self.mark_extracted(session_store, sid, tag)
            reports.append(report)
        return reports
