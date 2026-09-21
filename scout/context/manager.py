"""上下文治理 — 压缩 + 剪枝 + 记忆 flush 三级防护.

借鉴 OpenClaw 的上下文三级治理：
1. 压缩：对话过长时，用 LLM 将旧消息压缩为摘要
2. 剪枝：移除过期的工具输出（只保留最近 N 条）
3. 记忆 flush：将重要信息提取到长期记忆
"""

from __future__ import annotations

import re
from typing import Any
from datetime import datetime

from scout.core.types import Message, Role, Session

# 压缩元数据提取 — 匹配 URL 与图片 URL（用于压缩后保留可复用链接）
_URL_RE = re.compile(r"https?://[^\s\"'<>()]+")
_IMG_RE = re.compile(
    r"https?://[^\s\"'<>()]+\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s\"'<>()]*)?",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（无需 tiktoken，可跨平台离线运行）.

    ★ 2026-09-19 校准（usage.db 实测反推，原实现误差达 2 倍）：
    原规则把「非 CJK」一律按 4 字符 ≈ 1 token。这在英文散文上成立，但 Agent
    上下文的主要成分是 **Windows 路径、代码、JSON、base64、shell 输出** ——
    反斜杠/下划线/点号/冒号/引号几乎各自独立成 token，实测密度只有
    1.5~2.2 字符/token，按 4 算等于**系统性低估 2 倍**，直接后果是
    ``max_tokens`` 预算阈值永远判定"未超预算" → 压缩全程不触发 →
    单回合 prompt 从 3k 一路涨到 48k（实测会话 4126a066，454 步）。

    同时中文按 1 字 1 token 是**高估**（qwen/GLM 实测 0.6~0.8），一起修正。

    新规则：
    - CJK 全角：0.7 token/字
    - 字母数字与空格：4 字符 ≈ 1 token（英文/代码标识符）
    - 其余符号（路径分隔、标点、括号、引号、换行）：2 字符 ≈ 1 token

    低估的代价（不压缩→上下文爆炸）远大于高估（提前压缩→多一次摘要调用），
    故符号档刻意取保守值。
    """
    if not text:
        return 0
    import unicodedata

    cjk = 0
    alnum = 0
    sym = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("F", "W"):
            cjk += 1
        elif ch.isalnum() or ch == " ":
            alnum += 1
        else:
            sym += 1
    return int(cjk * 0.7) + (alnum + 3) // 4 + (sym + 1) // 2


# ── 情节记忆结构化摘要（2026-09-15，分层记忆 · 情节层）──────────────────
# 设计依据：情节记忆应"按时间组织、可检索、可复用"，因此压缩产物不是一段散文，
# 而是带固定字段的结构化记录；同时保留"不可改写"的关键原话与产物路径清单
# （历史事故：旧摘要丢了产物清单，agent 把"写入 X"当新任务重跑全流程）。
_EPISODE_SUMMARY_PROMPT = """你是 Agent 记忆压缩模块。把下面的对话压缩成**结构化记忆**，
只保留对后续任务有价值的信息。**不要编造、不要推测**；不确定的字段留空。

只输出 JSON（无任何其他文字）：
{{
  "topic": "一句话主题",
  "goal": "当时在做的任务目标",
  "constraints": ["限制条件（预算/时间/平台/口味等）"],
  "decisions": ["已经确定的做法或方案"],
  "actions": ["执行过的动作"],
  "results": ["结果、关键数据、以及**已产出/已修改文件的完整路径**"],
  "failures": ["失败原因或踩过的坑"],
  "open_loops": ["尚未完成的事项（全部完成则为 []）"],
  "user_preferences": ["用户表达的偏好"],
  "important_quotes": ["不可改写的关键原话，逐字引用"],
  "next_steps": ["下一步待办"],
  "importance": 0.0
}}

压缩要求（必须遵守）：
- 用户**最新一次明确的行动指令**必须逐字出现在 goal 或 important_quotes 中
- 已产出/已修改的文件**完整路径**必须逐条列入 results（后续迭代依据，绝不可省略）
- 关键数字、日期、金额、ID 等必须保留原值，不得四舍五入或改写
- open_loops 必须准确反映未完成事项

对话历史：
{dialog}"""

_EPISODE_FIELDS = (
    ("topic", "主题"),
    ("goal", "目标"),
    ("constraints", "约束"),
    ("decisions", "已定方案"),
    ("actions", "已执行"),
    ("results", "结果/产物"),
    ("failures", "失败与教训"),
    ("open_loops", "未完成"),
    ("user_preferences", "用户偏好"),
    ("important_quotes", "关键原话"),
    ("next_steps", "下一步"),
)


def _parse_episode_json(raw: str) -> dict | None:
    """从模型输出中解析结构化摘要；解析不出返回 None（调用方回退）."""
    if not raw:
        return None
    import json as _json
    import re as _re

    text = raw.strip()
    if text.startswith("```"):
        text = _re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = _re.sub(r"\n?```$", "", text)
    # 容错：截取第一个 { 到最后一个 }
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        data = _json.loads(text[i:j + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    # 至少要有一个有内容的字段才算解析成功
    for key, _ in _EPISODE_FIELDS:
        v = data.get(key)
        if (isinstance(v, str) and v.strip()) or (isinstance(v, list) and v and any(str(x).strip() for x in v)):
            return data
    return None


def _render_episode(d: dict) -> str:
    """结构化摘要 → 紧凑可读文本（给模型看的摘要块）."""
    lines: list[str] = []
    topic = str(d.get("topic", "") or "").strip()
    if topic:
        lines.append(f"主题：{topic}")
    for key, label in _EPISODE_FIELDS:
        if key == "topic":
            continue
        v = d.get(key)
        if isinstance(v, str):
            v = v.strip()
            if v:
                lines.append(f"{label}：{v}")
        elif isinstance(v, list):
            items = [str(x).strip() for x in v if str(x).strip()]
            if items:
                lines.append(f"{label}：" + "；".join(items))
    imp = d.get("importance")
    if isinstance(imp, (int, float)):
        lines.append(f"重要度：{imp}")
    return "\n".join(lines)


class ContextManager:
    """上下文治理器 — 管理 Session 的消息列表长度."""

    def __init__(
        self,
        max_messages: int = 50,
        max_tool_outputs: int = 24,
        compress_threshold: int = 40,
        keep_recent: int = 12,
        prune_batch: int = 6,
        max_tokens: int = 0,
        compress_ratio: float = 0.8,
        max_summaries: int = 3,
        summary_token_budget: int = 2500,
        max_summary_segs: int = 200,
    ):
        """
        Args:
            max_messages: 消息列表最大长度
            max_tool_outputs: 保留最近 N 条工具输出（超过后批量剪枝）。
                2026-09-07 从 50 收紧到 24：ReAct 累计 input 随历史长度平方级
                增长，过程性上下文要保持低位；信息不丢由 Running Notes 兜底
                （剪枝要点提炼进末尾笔记），不再需要大量原文陪跑。
            compress_threshold: 达到此长度触发压缩
            keep_recent: 压缩时保留最近 N 条消息
            prune_batch: 剪枝缓冲，批量删到 max_tool_outputs-prune_batch，降低触发频率
            max_tokens: 上下文 token 预算（2026-08-30 新增）。
                0 表示仅按条数治理；>0 时按 ``estimate_tokens`` 估算实际窗口，
                达到 ``max_tokens * compress_ratio`` 即触发压缩——长工具输出
                （搜索抓取全文等）会即时计入，而不是等条数攒够。
                可用环境变量 SCOUT_CONTEXT_MAX_TOKENS 覆盖默认值。
            compress_ratio: 触发压缩的窗口占用比例（默认 80%）
            max_summaries: 视图内最多保留的 [对话摘要] 条数（2026-09-19，见
                ``build_llm_view`` ①.5 步；可用 SCOUT_MAX_SUMMARIES 覆盖）。
            summary_token_budget: 视图内摘要总 token 预算（同上）。
            max_summary_segs: session.extra['summaries'] 保留的摘要段数上限
                （2026-09-19；可用 SCOUT_MAX_SUMMARY_SEGS 覆盖）。
        """
        import os as _os

        try:
            max_summary_segs = int(
                _os.getenv("SCOUT_MAX_SUMMARY_SEGS", str(max_summary_segs))
                or max_summary_segs
            )
            max_summaries = int(
                _os.getenv("SCOUT_MAX_SUMMARIES", str(max_summaries)) or max_summaries
            )
            summary_token_budget = int(
                _os.getenv("SCOUT_SUMMARY_TOKEN_BUDGET", str(summary_token_budget))
                or summary_token_budget
            )
        except ValueError:
            pass

        if not max_tokens:
            try:
                max_tokens = int(_os.getenv("SCOUT_CONTEXT_MAX_TOKENS", "0") or 0)
            except ValueError:
                max_tokens = 0
        self.max_messages = max_messages
        self.max_tool_outputs = max_tool_outputs
        self.compress_threshold = compress_threshold
        self.keep_recent = keep_recent
        self.prune_batch = prune_batch
        self.max_tokens = max_tokens
        self.compress_ratio = compress_ratio
        self.max_summaries = max(1, max_summaries)
        self.summary_token_budget = max(500, summary_token_budget)
        self.max_summary_segs = max(20, max_summary_segs)
        # ★ 2026-09-19：API 回传的真实 prompt token（session_id -> 最近一次实测值）。
        # 本地 estimate_tokens 再准也是估算；usage 表里有**逐次真实值**，优先用它
        # 做预算判定，估算只作为"尚无实测"时的兜底。
        self._real_prompt_tokens: dict[str, int] = {}

    def observe_real_tokens(self, session_id: str, real_prompt_tokens: int) -> None:
        """记录 API 回传的真实 prompt token 数（供 :meth:`_over_budget` 优先采用）.

        调用方在每次主循环 LLM 返回后把 ``usage.prompt_tokens`` 喂进来。
        这是治理唯一可信的标尺：估算器对代码/路径/base64 类内容误差可达 2 倍。
        """
        try:
            if session_id and real_prompt_tokens and real_prompt_tokens > 0:
                self._real_prompt_tokens[str(session_id)] = int(real_prompt_tokens)
        except (TypeError, ValueError):
            pass

    def real_prompt_tokens(self, session_id: str) -> int:
        """返回最近一次 API 回传的真实 prompt token 数（无实测返回 0）.

        供调用方区分「估算超预算」与「实测确已超预算」——后者是硬信号，
        不应被压缩冷却等节流策略挡住。
        """
        return int(self._real_prompt_tokens.get(str(session_id or "")) or 0)

    def count_tokens(self, session: Session) -> int:
        """估算会话当前 token 占用（含消息内容与元数据，不含 system prompt）."""
        total = 0
        for m in session.messages:
            total += estimate_tokens(m.content or "")
            meta = m.metadata or {}
            if meta.get("tool_name"):
                total += 4
        return total

    def _over_budget(self, session: Session) -> bool:
        """token 维度是否已超压缩预算（2026-09-05 提取，供 needs_compression/compress 共用）.

        ★ 2026-09-14（视图分离）：按「视图」而非真相计量 —— 视图才是实际发给 LLM
        的内容（真相已不再被压缩缩短）。

        ★ 2026-09-19（实测优先）：若已通过 :meth:`observe_real_tokens` 拿到 API
        回传的真实 prompt token，直接用它判定——本地估算对代码/路径/JSON 类内容
        系统性低估约 2 倍，实测出现过「真实 48k、估算仍判未超 19.6k」从而
        整个回合不压缩的情况（会话 4126a066，454 步，prompt 单调涨到 48893）。
        真实值只反映到上一为止步的上下文，本步新增量很小（<1k），不影响判定。
        """
        if self.max_tokens <= 0:
            return False
        _real = self._real_prompt_tokens.get(
            str(getattr(session, "id", "") or "")
        )
        if _real:
            return _real >= int(self.max_tokens * self.compress_ratio)
        _total = 0
        for m in self.build_llm_view(session, apply_tool_pruning=False):
            _total += estimate_tokens(m.content or "")
            if (m.metadata or {}).get("tool_name"):
                _total += 4
        return _total >= int(self.max_tokens * self.compress_ratio)

    # ── 真相 / 视图分离（P0，2026-09-14）─────────────────────────────
    #
    # 背景：此前 prune/compress 直接改写 session.messages，用户可见历史被破坏性
    # 裁剪（"历史中段凭空消失"且不可追溯）。分离后：
    #   session.messages  = 真相：只增不减（除显式编辑截断）→ 持久化 / UI 读取
    #   build_llm_view()  = 视图：应用摘要 + 工具裁剪 → 仅用于构造 API 消息（省 token）

    @staticmethod
    def _anchor_of(m: Message) -> tuple:
        """消息锚点：用 (role, timestamp, content 前 40 字) 标识身份.

        不用下标——剪枝/编辑会改变下标，锚点在原地仍稳定。
        """
        ts = getattr(m, "timestamp", None)
        return (
            getattr(getattr(m, "role", None), "value", str(getattr(m, "role", ""))),
            ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            (m.content or "")[:40],
        )

    def _summary_anchors(self, session: Session) -> dict[tuple, str]:
        """锚点 → 摘要文本（来自 session.extra['summaries']，由 compress 写入）."""
        out: dict[tuple, str] = {}
        for s in (getattr(session, "extra", None) or {}).get("summaries", []) or []:
            text = s.get("summary", "") if isinstance(s, dict) else ""
            for a in (s.get("anchors", []) if isinstance(s, dict) else []) or []:
                try:
                    out[tuple(a)] = text
                except TypeError:
                    continue
        return out

    def build_llm_view(
        self, session: Session, apply_tool_pruning: bool = True
    ) -> list[Message]:
        """构建「发给 LLM 的视图」—— 治理只作用于视图，session.messages 保持不变.

        Args:
            apply_tool_pruning: 是否应用「工具输出裁剪/瘦身」。压缩判据与压缩
                区间计算传 False（只应用摘要），使命中逻辑与工具裁剪解耦——
                否则工具裁剪会改变列表长度，干扰区间计算（P0 批 4，2026-09-14）。

        ★ 2026-09-14（P0 真相/视图分离）：解决「历史中段凭空消失」的根因 ——
        为省 token 做的剪枝/压缩此前直接改写 ``session.messages`` 并全量落盘，
        把用户可见的完整历史覆盖成"剪短版"。现在：
        - 真相（``session.messages``）只增不减 → 持久化与 UI 天然完整；
        - 本视图应用「压缩摘要」+「工具输出裁剪/瘦身」→ 只影响发给 LLM 的内容。

        确定性（缓存前缀友好）：给定相同 messages + summaries，输出逐字节一致；
        工具裁剪沿用原「批量边界」策略（保留 ``max_tool_outputs - prune_batch`` 条），
        避免每条新工具消息都让前缀变化。
        """
        msgs: list[Message] = list(session.messages)
        if not msgs:
            return msgs

        # ① 应用压缩摘要：命中锚点的消息移出视图，并在其原位置插入摘要消息
        anchors = self._summary_anchors(session)
        out: list[Message] = []
        emitted: set[str] = set()
        for m in msgs:
            key = self._anchor_of(m)
            if key in anchors:
                text = anchors[key]
                if text and text not in emitted:  # 同一摘要只插入一次
                    emitted.add(text)
                    out.append(
                        Message(
                            role=Role.SYSTEM,
                            content=f"[对话摘要] {text}",
                            metadata={"type": "compression"},
                        )
                    )
                continue
            out.append(m)

        # ①.5 ★ 2026-09-19 摘要收敛（压缩反噬修复）
        #
        # 缺陷：compress() 每次生成一条新摘要写进真相，而摘要消息**自身永远
        # 不会被「命中锚点」移出视图**（anchors 只映射被摘要覆盖的原文）——
        # 于是「压一次、多一条」，摘要只增不减。长回合里里程碑压缩反复触发时，
        # 摘要无限累积，压缩从"减负"变成"膨胀源"：
        #   实测会话 4126a066：215 条摘要 = 47.7k token，占该会话上下文 97.8%，
        #   prompt 从 2.9k 单调涨到 48.9k（454 步），压缩阈值全程判"未超预算"。
        #
        # 修复：视图内只保留最近 max_summaries 条、且受 summary_token_budget 约束。
        # 安全性：被移除的只是**视图**，真相 session.messages 仍完整（UI/导出不丢历史）；
        # 且 compress 已把被压原文归档进记忆库（_archive_to_memory），可 memory_search 召回。
        _sum_idx = [
            i for i, m in enumerate(out) if (m.content or "").startswith("[对话摘要]")
        ]
        if len(_sum_idx) > 1:
            _keep: set[int] = set()
            _cost = 0
            for i in reversed(_sum_idx):  # 从最新往回保留
                if len(_keep) >= self.max_summaries:
                    break
                _c = estimate_tokens(out[i].content or "")
                if _keep and _cost + _c > self.summary_token_budget:
                    break
                _keep.add(i)
                _cost += _c
            out = [m for i, m in enumerate(out) if i not in _sum_idx or i in _keep]

        if not apply_tool_pruning:
            return out

        # ② 工具输出「条数裁剪」：从最旧开始整批移出视图。
        #    必须整批（assistant(tool_calls) + 其全部 TOOL 结果）一起移出，
        #    否则留下缺响应的 tool_calls → API 400（原实现注释中的教训）。
        tool_pos = [i for i, m in enumerate(out) if m.role == Role.TOOL]
        if len(tool_pos) > self.max_tool_outputs:
            target_keep = max(self.prune_batch, self.max_tool_outputs - self.prune_batch)
            skip: set[int] = set()
            cur = list(tool_pos)
            while len(cur) > target_keep and cur:
                i = cur[0]
                start = i - 1 if i > 0 and out[i - 1].role == Role.ASSISTANT else i
                end = i
                while end < len(out) and out[end].role == Role.TOOL:
                    end += 1
                skip.update(range(start, end))
                cur = [k for k in cur if k >= end]
            out = [m for i, m in enumerate(out) if i not in skip]

        # ③ 工具输出「瘦身」（token 维度）：超大输出在视图内替换为占位符。
        #    注意：只替换视图内的引用，绝不修改原 Message 对象（真相不受影响）。
        if self.max_tokens > 0:
            _budget = max(2000, self.max_tokens // 2)
            live = [i for i, m in enumerate(out) if m.role == Role.TOOL]
            while live:
                _total = sum(estimate_tokens(out[i].content or "") for i in live)
                if _total <= _budget:
                    break
                _cand = next(
                    (i for i in live if estimate_tokens(out[i].content or "") >= _budget // 20),
                    None,
                )
                if _cand is None:
                    break
                _orig = out[_cand].content or ""
                out[_cand] = Message(
                    role=Role.TOOL,
                    content=(
                        f"[输出已瘦身：该工具输出原约 {estimate_tokens(_orig)} token，"
                        "为控制上下文预算已移出视图，要点已并入运行笔记]"
                    ),
                    metadata=out[_cand].metadata,
                    timestamp=out[_cand].timestamp,
                    sender=out[_cand].sender,
                    session_id=out[_cand].session_id,
                    source=out[_cand].source,
                )
                live = [i for i in live if i != _cand]

        # ④ 无 USER 保护（★ 2026-09-14）：摘要/裁剪后视图内若没有 USER 消息，
        #    GLM 系（及部分网关）会 400 "No user query found in messages"。
        #    从被摘要覆盖的原文里补回最后一条 user（等价于原 preserved_user 逻辑）。
        if out and not any(m.role == Role.USER for m in out):
            _last_user = next((m for m in reversed(msgs) if m.role == Role.USER), None)
            if _last_user is not None:
                out.append(
                    Message(
                        role=Role.USER,
                        content=_last_user.content,
                        metadata={"type": "preserved_user", "note": "压缩后重注入，防 API 400"},
                        timestamp=_last_user.timestamp,
                    )
                )

        return out

    def reset_governance(self, session: Session) -> None:
        """清空压缩摘要记录（编辑/重新生成截断后调用）——摘要锚点已失效."""
        try:
            (getattr(session, "extra", None) or {}).pop("summaries", None)
        except Exception:  # noqa: BLE001
            pass

    def needs_compression(self, session: Session) -> bool:
        """判断是否需要压缩：条数超限 或 token 超预算（二者任一触发）.

        ★ 2026-09-14（视图分离）：条数按「视图」计 —— 真相不再被压缩缩短，若仍用
        ``len(session.messages)`` 会每步都判需压缩 → 每步白调 LLM 摘要。
        """
        if len(self.build_llm_view(session, apply_tool_pruning=False)) >= self.compress_threshold:
            return True
        return self._over_budget(session)

    def prune_tool_outputs(self, session: Session) -> list[Message]:
        """剪枝 — 控制工具输出数量，保持前缀稳定以命中缓存（2026-08-19 优化）.

        原实现把早期工具输出"截断到 200 字符"：这会让已发送过的历史内容变化，
        导致后续每次调用前缀都不同 → 缓存持续 miss（长任务缓存命中率归零的根因）。

        新策略改为"整体移除最旧工具消息及对应 assistant(tool_calls) 消息"：
        - 移除后剩余消息内容保持不变 → 前缀确定、后续调用可命中缓存
        - 只移除超过 max_tool_outputs 的多余部分，达到稳定状态后不再反复改动
        - 顺带解决"截断 200 字符仍占 prompt、且破坏语义"的问题

        2026-08-20 增强：返回被移除的消息列表（供调用方归档/追溯），
        避免"统计时看不到被剪掉的工具记录"导致总结信息失真。

        Returns: 被移除的 Message 列表（未发生剪枝时为空列表）
        """
        removed: list[Message] = []

        # 只保留最近 N 条工具消息，更早的整体移除（连同匹配的 assistant 消息）。
        # 采用"批量删除到稳定下界"策略：一次性删到 max_tool_outputs - prune_batch，
        # 预留缓冲，避免工具每新增一条就触发一次删除（那会让前缀持续变化、
        # 缓存持续 miss）。批量删除把前缀变化频率降到最低。
        tool_messages = [
            i for i, m in enumerate(session.messages)
            if m.role == Role.TOOL
        ]

        # 条数剪枝：超过 max_tool_outputs 才删（2026-08-30 修复：此前条数
        # 未超限时提前 return，导致 token 维度分支永远执行不到）
        if len(tool_messages) > self.max_tool_outputs:
            # 目标：删到保留 max_tool_outputs - prune_batch 条，留出增长缓冲
            target_keep = max(self.prune_batch, self.max_tool_outputs - self.prune_batch)
            # ★ 2026-09-09：整批删除 —— 一次 assistant 的 N 个并行 tool_call 产生
            # N 条连续 TOOL 结果；此前"删 1 条 TOOL + 紧邻前 1 条 assistant"会把
            # 同批其余 N-1 条结果留成孤儿（tool_call_id 指向已删除的 assistant），
            # 下一次 API 调用报 "tool message must follow tool_calls"，会话永久损坏
            # （系统提示鼓励并行 tool_call，必现）。正确语义：删该批全部 TOOL 结果
            # + 发起它们的 assistant。
            while True:
                cur_tools = [
                    i for i, m in enumerate(session.messages)
                    if m.role == Role.TOOL
                ]
                if len(cur_tools) <= target_keep or not cur_tools:
                    break
                idx = cur_tools[0]
                start = idx
                if idx > 0 and session.messages[idx - 1].role == Role.ASSISTANT:
                    start = idx - 1
                end = idx
                while end < len(session.messages) and session.messages[end].role == Role.TOOL:
                    end += 1
                removed.extend(session.messages[start:end])
                del session.messages[start:end]

        # token 维度补充（2026-08-30）：工具输出总 token 超预算时，从最旧开始
        # 剪掉超大输出（单条搜索抓取全文可达数万字符）。预算 = max_tokens//2，
        # 单条 < 预算/20 的小输出不剪（保护前缀稳定以命中缓存）。
        # ★ 2026-09-09：改为【原地瘦身】而非删除 —— 物理删除批中间的 TOOL 结果
        # 同样会拆散 tool_call 配对（留下缺响应的 tool_calls → API 400）；
        # 原地替换为占位符既省 token 又保配对完整。
        if self.max_tokens > 0:
            _tool_budget = max(2000, self.max_tokens // 2)
            while True:
                _tools = [
                    i for i, m in enumerate(session.messages)
                    if m.role == Role.TOOL
                ]
                if not _tools:
                    break
                _tk_total = sum(
                    estimate_tokens(session.messages[i].content or "") for i in _tools
                )
                if _tk_total <= _tool_budget:
                    break
                _cand: int | None = None
                for i in _tools:
                    if estimate_tokens(session.messages[i].content or "") >= _tool_budget // 20:
                        _cand = i
                        break
                if _cand is None:
                    break
                _orig = session.messages[_cand].content or ""
                removed.append(session.messages[_cand])
                session.messages[_cand].content = (
                    f"[输出已瘦身：该工具输出原约 {estimate_tokens(_orig)} token，"
                    "为控制上下文预算已移除，要点已并入运行笔记]"
                )

        return removed

    # ── Running Notes + 预算告警（2026-09-07）─────────────────────
    # 剪枝是物理删除，模型会"忘记"早期工具发现的关键结论（长任务后半程
    # 重复搜索/重做已完成的步骤）。剪枝时把被删工具输出的要点提炼进一条
    # "运行笔记"消息（始终挂在消息列表末尾 = 纯追加，不破坏前缀缓存），
    # 模型每轮都能看到全部历史要点，上下文却不膨胀。
    _NOTES_TYPE = "running_notes"
    _NOTES_MAX_ITEMS = 30      # 最多保留条目数
    _NOTES_MAX_CHARS = 2400    # 整块字符上限
    _NOTES_ITEM_CHARS = 160    # 单条要点截断长度

    def update_running_notes(self, session: Session, removed: list[Message]) -> bool:
        """把被剪枝工具输出的要点合并进"运行笔记"消息，并将该消息挂到末尾.

        - 仅提炼 role==TOOL 的被删消息（配对的 assistant 是模型自己的话，无信息量）；
        - 无可提炼内容且已存在笔记时，仍会把现有笔记挪到末尾（保证位置正确，
          例如被压缩卷走后重建）；
        - 笔记上限 _NOTES_MAX_ITEMS 条 / _NOTES_MAX_CHARS 字符，超出丢最旧。

        Returns: 笔记是否发生变化。
        """
        notes_idx = next(
            (
                i
                for i, m in enumerate(session.messages)
                if m.metadata.get("type") == self._NOTES_TYPE
            ),
            None,
        )
        old_items: list[str] = []
        if notes_idx is not None:
            old_items = [
                s
                for s in (session.messages[notes_idx].content or "").splitlines()
                if s.startswith("- ")
            ]

        new_items: list[str] = []
        for m in removed or []:
            if m.role != Role.TOOL:
                continue
            name = (m.metadata or {}).get("tool_name", "tool")
            text = re.sub(r"\s+", " ", (m.content or "")).strip()
            if not text:
                continue
            if len(text) > self._NOTES_ITEM_CHARS:
                text = text[: self._NOTES_ITEM_CHARS] + "…"
            new_items.append(f"- [{name}] {text}")

        merged = old_items + [x for x in new_items if x not in old_items]
        # 超限丢最旧（保尾部 = 最近发生的步骤）
        merged = merged[-self._NOTES_MAX_ITEMS :]
        total = sum(len(x) + 1 for x in merged)
        while total > self._NOTES_MAX_CHARS and len(merged) > 1:
            total -= len(merged[0]) + 1
            merged.pop(0)

        if not merged:
            return False

        content = "[运行笔记] 已归档步骤的关键结论（自动提炼，防剪枝失忆）:\n" + "\n".join(merged)
        changed = notes_idx is None or session.messages[notes_idx].content != content
        if notes_idx is not None:
            del session.messages[notes_idx]
        session.messages.append(
            Message(
                role=Role.SYSTEM,
                content=content,
                metadata={"type": self._NOTES_TYPE},
            )
        )
        return changed

    def get_compression_range(
        self, session: Session, min_total: int | None = None
    ) -> tuple[int, int] | None:
        """获取需要压缩的消息范围 [start, end).

        保留最近 keep_recent 条消息，压缩其余的。

        min_total（2026-09-05）：允许 token 超预算触发的压缩在消息数不足
        ``compress_threshold`` 时也生效——长链任务（GUI 自动化等单指令 60 步）
        通常到不了 80 条消息就早已远超 token 预算；若仍死守 80 条门槛，
        token 预算压缩形同虚设。默认仍为 ``compress_threshold``（按条数触发场景）。
        """
        # ★ 2026-09-14（视图分离）：区间在「视图」上计算（视图 = 已应用摘要与
        # 工具裁剪的有效消息）。返回的 [start, end) 为**视图下标**，调用方
        # compress 亦在视图上取段，无需映射回真相下标。
        msgs = self.build_llm_view(session, apply_tool_pruning=False)
        total = len(msgs)
        if total < (min_total if min_total is not None else self.compress_threshold):
            return None

        # 找到【开头的】system 消息段之后的第一条消息。
        # ★ 2026-09-09：原实现 break 在第一条 SYSTEM —— 但运行笔记是 SYSTEM 且
        # 被挂在列表末尾，导致 start ≈ total，回合内压缩/里程碑压缩在有笔记后
        # 永久 no-op（token 只涨不降）。改为只跳过【前导】SYSTEM 段。
        start = 0
        for i, m in enumerate(msgs):
            if m.role == Role.SYSTEM:
                start = i + 1
            else:
                break

        end = total - self.keep_recent
        # ★ 2026-09-09：边界不得切开 tool_call 批次 —— end 处若是 TOOL 消息，
        # 其发起的 assistant(tool_calls) 已在压缩区间内，保留它们会变成孤儿
        # tool 结果（API 400）。把整批划入压缩区间。
        while end < total and msgs[end].role == Role.TOOL:
            end += 1
        if end <= start:
            return None

        return (start, end)

    async def compress(
        self,
        session: Session,
        llm=None,
        memory_flush: Any | None = None,
        min_total: int | None = None,
    ) -> dict[str, Any]:
        """压缩会话 — 将旧消息替换为 LLM 生成的摘要.

        Args:
            session: 待压缩的会话。
            llm: 可选 LLM（用于生成摘要与 memory_flush 的结构化抽取）。
            memory_flush: 可选 ``MemoryFlush`` —— 压缩前先把将被替换的
                旧消息段抽取为长期记忆，防止压缩摘要丢失关键信息（2026-08-27）。
            min_total: 可选 —— 显式放宽触发压缩的最小消息数门槛
                （2026-09-06 里程碑压缩场景：长链任务消息不足 compress_threshold
                时也需按步数做阶段摘要；默认 None 走原有策略）。
        """
        info = {"compressed": False, "removed": 0, "summary": "", "flushed": False}

        # ★ 2026-09-14（视图分离）：不再在此剪枝真相 —— 工具裁剪已由
        # build_llm_view 在视图层完成（真相保持完整）。

        # 触发压缩的最小消息数门槛：
        # - min_total 显式传入时以调用方为准（2026-09-06 里程碑压缩）；
        # - token 超预算时放宽（默认 80 条）：门槛取 keep_recent+prune_batch+2 与
        #   compress_threshold 一半的较大者，保证至少能压缩出"保留最近 N 条"之外
        #   的一段有效消息，避免白调 LLM 摘要；
        # - 其余场景仍为 compress_threshold（按条数触发）。
        _min_total = self.compress_threshold
        if min_total is not None:
            _min_total = max(self.keep_recent + 3, int(min_total))
        elif self._over_budget(session):
            _min_total = max(
                self.keep_recent + self.prune_batch + 2,
                int(self.compress_threshold * 0.5),
            )

        rng = self.get_compression_range(session, min_total=_min_total)
        if not rng:
            return info

        start, end = rng
        _view = self.build_llm_view(session, apply_tool_pruning=False)
        old_messages = _view[start:end]

        # 压缩前记忆 flush（E4 闭环，2026-08-27）：先抽取将被替换的旧消息段，
        # 再把压缩摘要写入 —— 两路并行，保证关键信息不随压缩丢失。
        if memory_flush is not None:
            try:
                flushed = await memory_flush.flush(session, messages=old_messages)
                info["flushed"] = bool(flushed)
            except Exception:
                info["flushed"] = False

        # ★ 2026-09-15（用户方案）：把被压缩的**原文**分块归档到记忆库，
        # 使其可被 memory_search（embedding 语义 + FTS 关键词）随时召回。
        # 有了这个兜底，压缩才敢"压得狠"——细节不再是单向丢失。
        if memory_flush is not None:
            try:
                info["archived"] = await self._archive_to_memory(
                    getattr(memory_flush, "memory_store", None), session, old_messages
                )
            except Exception:  # noqa: BLE001
                info["archived"] = 0

        if llm:
            # 用 LLM 生成摘要
            summary = await self._llm_summarize(old_messages, llm)
        else:
            # 简单截断 — 提取关键信息
            summary = self._simple_summarize(old_messages)

        # ★ 2026-09-15：告知 agent「更早的原文可检索」，避免它凭摘要猜测或重复劳动
        if info.get("archived"):
            summary += (
                "\n\n（提示：被压缩的这段对话**原文已归档**到长期记忆，"
                "需要其中的具体细节时，请用 memory_search 工具检索召回，不要凭猜测复述。）"
            )

        # ★ 2026-09-14：原文快照 —— 摘要替换后原文此前**不可找回**，用户可见
        # 历史中段"凭空消失"（反馈「对话历史经常丢数据」）。此处把被替换的原文
        # 追加进 session.extra（有界保留），使「上下文用摘要省 token」与
        # 「真相可追溯」并存；session 详情 API 会把它作为 compressed_history
        # 返回，前端可展示"已摘要的 N 条原文"。归档表（messages_archive）同时
        # 保留一份，互为兜底。
        try:
            _snap = session.extra.setdefault("compressed_history", [])
            for _m in old_messages:
                _snap.append({
                    "role": getattr(getattr(_m, "role", None), "value", str(getattr(_m, "role", ""))),
                    "content": _m.content or "",
                    "timestamp": (
                        _m.timestamp.isoformat()
                        if hasattr(getattr(_m, "timestamp", None), "isoformat")
                        else str(getattr(_m, "timestamp", ""))
                    ),
                })
            _SNAP_MAX = 400
            if len(_snap) > _SNAP_MAX:  # 有界：防 extra 无限膨胀
                del _snap[: len(_snap) - _SNAP_MAX]
        except Exception:  # noqa: BLE001 — 快照失败不影响压缩主流程
            pass

        # ★ 2026-09-14（P0 视图分离）：不再构造"压缩后的 messages"去覆盖真相。
        # 摘要与锚点写入 session.extra['summaries']，由 build_llm_view 在构造
        # API 消息时应用；session.messages 保持完整 → 用户可见历史不再"中段消失"。
        _anchors = [list(self._anchor_of(m)) for m in old_messages]
        try:
            _sums = session.extra.setdefault("summaries", [])
            _sums.append({
                "anchors": _anchors,
                "summary": summary,
                # ★ 2026-09-15（分层记忆 · 情节层）：同时持久化结构化字段，
                # 便于后续按字段检索/排序/复用（summary 文本只用于给模型阅读）。
                "structured": getattr(self, "_last_episode", None),
                "count": len(old_messages),
                "created_at": datetime.now().isoformat(),
            })
            # ★ 2026-09-19：界从 20 提到 200（可用 SCOUT_MAX_SUMMARY_SEGS 覆盖）。
            # 旧逻辑删最旧段时会连带删掉它的 anchors → **那段原文重新回到视图
            # 全量重发**（压缩成果瞬间作废、上下文暴涨）。而视图里渲染几条摘要
            # 已由 build_llm_view ①.5 独立收敛（默认 3 条），保留全部 anchors
            # 并不会增加上下文，只占 session.extra 存储（单段 ~1KB，200 段 ≈ 200KB）。
            # 结论：宁可多存，不可丢 anchors。
            _SEG_MAX = self.max_summary_segs
            if len(_sums) > _SEG_MAX:
                del _sums[: len(_sums) - _SEG_MAX]
        except Exception:  # noqa: BLE001 — 摘要写入失败不影响主流程
            pass
        # 旧的"压缩后补 user 防 API 400"逻辑已上移到 build_llm_view（视图内保证
        # 至少一条 USER，等价语义）。

        session.lineage_id = f"{session.lineage_id}→compressed" if session.lineage_id else "compressed"
        # ★ 2026-09-14：把被摘要替换掉的原文回传给调用方归档。此前压缩**不做归档**
        # → 原文永久丢失：用户可见历史中段"凭空消失"，只剩 600 字摘要且不可恢复
        # （用户反馈「对话历史经常丢数据」的直接根因之一）。归档需 session_store，
        # ContextManager 不持有，故由调用方（Agent._context_govern）负责落归档表。
        info["replaced_messages"] = old_messages

        info["compressed"] = True
        info["removed"] = len(old_messages)  # 语义：本次移出视图的消息条数
        info["summary"] = summary
        return info

    def _extract_tool_meta(self, messages: list[Message]) -> str:
        """从工具消息中提取可复用元数据（来源 URL / 图片 URL / 工具名）。

        2026-08-20 新增：压缩摘要本身会丢失 URL、数字等细节，导致 LLM 事后
        需要重新搜索/抓取同一来源。压缩时把元数据单独附在摘要后，让"来源链接、
        图片链接"这类关键信息不被丢失。
        """
        lines: list[str] = []
        seen: set[str] = set()
        for m in messages:
            if m.role != Role.TOOL:
                continue
            name = m.metadata.get("tool_name", "unknown")
            content = m.content or ""
            urls: list[str] = []
            for u in _URL_RE.findall(content):
                u = u.rstrip(".,;)}]")
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
            if not urls:
                continue
            imgs = [u for u in urls if _IMG_RE.match(u)]
            non_imgs = [u for u in urls if not _IMG_RE.match(u)]
            if imgs:
                lines.append(f"- [图片·{name}] " + "; ".join(imgs))
            if non_imgs:
                lines.append(f"- [来源·{name}] " + "; ".join(non_imgs[:3]))
        return "\n".join(lines)

    async def _archive_to_memory(self, memory_store: Any, session: Any, messages: list) -> int:
        """把被压缩的**原文**分块归档进记忆库，使其可被 embedding / FTS 检索召回.

        ★ 2026-09-15（用户方案：低频压缩 + 每次压得狠 + 靠 embedding/grep 召回）：
        现有 ``memory_flush`` 走的是 LLM **提炼**（要点/偏好），细节仍会随压缩丢失；
        ``compressed_history`` 虽留了原文快照，却**没有索引、没有检索入口**，等于
        压在库里取不出来。

        这里把原文按约 800 字符分块写进 ``memories``（``category="session_history"``），
        直接复用 ``MemoryStore.search`` 的 RRF 混合检索（向量语义 + FTS5/LIKE 关键词）
        —— 也就是既支持 embedding 语义召回，也支持关键词精确召回。于是压缩可以
        放心压狠，需要细节时让 agent 用 ``memory_search`` 随时取回，而不是靠摘要里
        那几百字硬撑。

        返回成功归档的块数（任何异常都只记 0，绝不影响压缩主流程）。
        """
        if memory_store is None or not messages:
            return 0
        try:
            # ── 分块（保留角色前缀，便于召回后理解对话上下文）──
            chunks: list[str] = []
            cur: list[str] = []
            size = 0
            for m in messages:
                role = getattr(getattr(m, "role", None), "value", None) or str(getattr(m, "role", ""))
                line = f"{role}: {(getattr(m, 'content', '') or '')[:1200]}"
                if size + len(line) > 800 and cur:
                    chunks.append("\n".join(cur))
                    cur, size = [], 0
                cur.append(line)
                size += len(line) + 1
            if cur:
                chunks.append("\n".join(cur))
            if not chunks:
                return 0

            embedder = getattr(memory_store, "_embedding_provider", None)
            sid = str(getattr(session, "id", "") or "")
            n = 0
            for i, ch in enumerate(chunks):
                text = (
                    f"[会话历史归档 {sid[:8]} 第 {i + 1}/{len(chunks)} 块，共 {len(messages)} 条消息]\n{ch}"
                )
                emb = None
                if embedder is not None:
                    try:
                        _r = embedder.embed(ch)
                        emb = await _r if hasattr(_r, "__await__") else _r
                    except Exception:  # noqa: BLE001 — 无向量时退化为纯文本检索
                        emb = None
                try:
                    mid = memory_store.add(
                        text,
                        category="session_history",
                        importance=0.35,
                        embedding=emb,
                        source_session=sid,
                        source_msg_count=len(messages),
                    )
                    if isinstance(mid, int) and mid > 0:
                        n += 1
                except Exception:  # noqa: BLE001
                    continue
            return n
        except Exception:  # noqa: BLE001 — 归档失败绝不影响压缩
            return 0

    async def _llm_summarize(self, messages: list[Message], llm) -> str:
        """用 LLM 生成**结构化**情节摘要，失败时回退自然语言 / 本地截断.

        ★ 2026-09-15（分层记忆 · 情节层）：原实现只产出一段自然语言摘要，
        细节不可检索、字段不可复用。现在要求模型输出结构化 JSON
        （goal/constraints/decisions/actions/results/failures/open_loops/
        user_preferences/important_quotes/next_steps/importance），
        —— 这是"情节记忆"该有的形态：可检索、可排序、可复用。

        返回渲染后的文本（供摘要锚点使用）；结构化 dict 暂存
        ``self._last_episode``，由 ``compress`` 一并持久化（同一协程内读取，
        不存在跨会话串扰）。解析失败则退回自然语言原文，绝不因格式问题
        丢失压缩能力。
        """
        self._last_episode = None
        dialog = "\n".join(
            f"{msg.role.value}: {(msg.content or '')[:500]}" for msg in messages
        )
        meta = self._extract_tool_meta(messages)

        prompt = _EPISODE_SUMMARY_PROMPT.format(dialog=dialog)
        if meta:
            prompt += (
                "\n\n以下是从工具结果中提取的来源/图片链接清单，"
                "压缩后的 results 中必须完整保留这些链接（逐条列出，不要省略、不要改写）：\n"
                f"{meta}"
            )

        try:
            resp = await llm.complete([{"role": "user", "content": prompt}])
            raw = (resp.content or "").strip()
            structured = _parse_episode_json(raw)
            if structured is not None:
                self._last_episode = structured
                return _render_episode(structured)
            # 模型没按 JSON 输出 → 原样当摘要使用（不丢信息）
            return raw or self._simple_summarize(messages)
        except Exception:  # noqa: BLE001
            return self._simple_summarize(messages)

    def _simple_summarize(self, messages: list[Message]) -> str:
        """简单截断摘要 — 不调用 LLM."""
        user_msgs = [m for m in messages if m.role == Role.USER]
        tool_msgs = [m for m in messages if m.role == Role.TOOL]

        parts = [f"共 {len(messages)} 条消息，{len(user_msgs)} 条用户消息，{len(tool_msgs)} 条工具输出。"]
        for m in user_msgs[-3:]:
            parts.append(f"用户: {m.content[:100]}")
        meta = self._extract_tool_meta(messages)
        if meta:
            parts.append("来源/图片链接清单（须保留）：\n" + meta)
        return " ".join(parts)