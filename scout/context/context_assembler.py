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


# 混进 preference 类的**系统行为规则 / 工具约定 / 测试残留**特征（2026-09-16）。
# 这类文本描述的是"助手该怎么做事"，不是"用户的长期事实"，无脑钉进上下文只会
# 挤占预算并误导模型（实测它们与"不吃辣"一起被注入）。
_PINNED_NOISE_HINTS = (
    "不要调用", "只回复", "调用任何工具", "token", "熔断", "省略标记",
    "存盘路径", "工具返回", "预算告警", "快速评估", "分割发送", "输出存盘",
)


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
        # ★ 2026-09-16（分层记忆 · 必注入事实 / pinned facts）：
        # 用户偏好与"带 fact_key 的稳定事实"必须注入，**不参与语义相似度竞争**。
        # 实测根因：query="推荐餐厅" 时，库里的"推荐算法/推荐系统"（简历背景）
        # 向量相似度远高于"不吃辣"，把召回集占满 → 忌口从未进入上下文 →
        # 模型给不吃辣的用户推荐了麻辣小龙虾。这类信息与当前问句的字面相似度
        # 可能极低，却对回答正确性起决定作用，因此单独走钉住通道。
        pinned: list = []
        try:
            # ① 带 fact_key 的稳定事实（居住地/忌口/约束…）—— **必钉**。
            #    它们数量少、价值最高，且经过冲突管理（同键只留最新值）。
            for _cat in ("preference", "constraint", "fact"):
                for _m in (self.memory_store.list_recent(category=_cat, limit=40) or []):
                    if (getattr(_m, "status", "active") or "active") == "deprecated":
                        continue
                    if (getattr(_m, "fact_key", "") or "").strip():
                        pinned.append(_m)

            # ② 无 fact_key 的偏好 —— **限量 + 去噪**（2026-09-16）。
            #    原实现把最近 20 条 preference 全部钉住，实测混入大量
            #    "系统行为规则 / 工具约定 / 测试残留"（见 _PINNED_NOISE_HINTS），
            #    既挤占预算又干扰模型；这里按重要度取前若干条并过滤噪音。
            _pref: list = []
            for _m in (self.memory_store.list_recent(category="preference", limit=20) or []):
                if (getattr(_m, "status", "active") or "active") == "deprecated":
                    continue
                if (getattr(_m, "fact_key", "") or "").strip():
                    continue  # ① 已收
                _txt = (getattr(_m, "content", "") or "").strip()
                if not _txt or len(_txt) > 180:      # 过长的不是"一句话长期事实"
                    continue
                if any(_h in _txt for _h in _PINNED_NOISE_HINTS):
                    continue
                _pref.append(_m)
            _pref.sort(key=lambda x: getattr(x, "importance", 0.0) or 0.0, reverse=True)
            pinned.extend(_pref[:5])
        except Exception:  # noqa: BLE001 — 钉住失败不影响常规召回
            pinned = []

        _seen_pinned: set = set()
        _pinned_uniq: list = []
        for _m in pinned:
            _mid = getattr(_m, "id", None)
            if _mid is not None and _mid in _seen_pinned:
                continue
            if _mid is not None:
                _seen_pinned.add(_mid)
            _pinned_uniq.append(_m)

        # ★ 2026-09-16（分层记忆 · 召回配额）：**不在这里做全局截断**。
        # 实测问题：全局 top-N 截断下，数量多、篇幅长的 skill / session_history
        # 会把 preference / fact 这类"短小但价值最高"的记忆挤出注入窗口
        # （表现为：新会话问餐厅推荐时没有体现用户忌口）。配额改在下面按层施加。
        _pinned_sorted = sorted(_pinned_uniq, key=_rank_score, reverse=True)
        _seen_all = {getattr(m, "id", None) for m in _pinned_sorted}
        _rest = [m for m in candidates if getattr(m, "id", None) not in _seen_all]
        # pinned 排在最前（内部按 rank 排序），保证偏好/稳定事实一定拿到 facts 层配额
        ranked = _pinned_sorted + sorted(_rest, key=_rank_score, reverse=True)

        # ★ 2026-09-15（分层记忆 · 分层注入）：召回结果按"记忆层"分组输出，
        # 而不是混成一个扁平列表让模型自己分辨。三层在决策中的角色不同：
        #   facts    长期稳定事实/偏好 —— 直接约束本次回答
        #   episodes 历史片段（压缩归档）—— 提供"上次做到哪"的来龙去脉
        #   rules    行为规则/可复用技能 —— 告诉模型"该怎么做事"
        # 同时：跳过已失效（deprecated）事实；对带 fact_key 的事实注明键名，
        # 便于模型认识到"同一属性只应以最新值为准"。
        _group_of = {
            "preference": "facts",
            "fact": "facts",
            "decision": "facts",
            "conclusion": "facts",
            "general": "facts",
            "skill": "rules",
            "session_history": "episodes",
        }
        _label = {
            "facts": "用户长期事实与偏好",
            "episodes": "相关历史片段（压缩归档，可用 memory_search 检索更多）",
            "rules": "行为规则与可复用技能",
        }

        # 每层配额（可调）：事实层最大，其次是情节与规则。
        # 依据方案建议的"语义事实 top 5~20 / 情节摘要 top 3~10 / 程序规则 top 3~10"。
        _quota = {"facts": 10, "episodes": 5, "rules": 5}
        # 事实层保底：只要候选里确实有事实，就至少注入这么多条 ——
        # 长期事实（居住地/忌口/约束）对回答正确性的影响远大于一条相关技能。
        _min_facts = 3

        buckets: dict[str, list[str]] = {"facts": [], "episodes": [], "rules": []}
        overflow: dict[str, list[str]] = {"facts": [], "episodes": [], "rules": []}
        for m in ranked:  # 保持 rank_score 顺序（组内即按相关性×重要性×时效）
            if (getattr(m, "status", "active") or "active") == "deprecated":
                continue
            text = (getattr(m, "content", "") or "").strip()
            if not text:
                continue
            text = text.replace("\n", " ")[:300]
            kind = getattr(m, "category", "") or "general"
            _fk = (getattr(m, "fact_key", "") or "").strip()
            _suffix = f"  (fact_key={_fk})" if _fk else ""
            _g = _group_of.get(kind, "facts")
            _line = f"- [{kind}] {text}{_suffix}"
            if len(buckets[_g]) < _quota[_g]:
                buckets[_g].append(_line)
            else:
                overflow[_g].append(_line)

        # 事实层保底补齐（用同层 overflow 中排名最靠前的若干条）
        if len(buckets["facts"]) < _min_facts:
            for _ln in overflow["facts"]:
                if len(buckets["facts"]) >= _min_facts:
                    break
                buckets["facts"].append(_ln)

        lines: list[str] = []
        for _g in ("facts", "episodes", "rules"):
            _items = buckets.get(_g) or []
            if _items:
                lines.append(f"# {_label[_g]}")
                lines.extend(_items)
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
