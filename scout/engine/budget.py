"""迭代预算追踪 — 借鉴 Hermes 的 IterationBudget.

2026-09-20 追加 AdaptiveBudget：把"固定大步数上限"换成"小基准 + 有进展续期"。

背景（usage.db 实测）：用户把 max_turns 从 60 调到 500 只为"别让长任务半途被切"，
代价是简单任务也获得 500 步的空转空间——某会话 454 步里绝大多数是无效重试，
累计烧掉 751 万 prompt token。步数上限成了唯一护栏，既太松（防不住死循环）
又太笨（真需要步数时它是硬墙）。

新模型：
- 起步只给 base 步（默认 25），不给空转空间；
- 每观察到实质进展就在临近耗尽时续期 extend 步，直到 hard_max（默认 120），
  → 真在推进的任务能拿到足够步数（完成任务优先）；
- 连续 stall_limit 步无实质进展（全失败 / 输出指纹重复）直接判死循环终止，
  → 不必等 hard_max，也不依赖"看门狗提示 2 次"的间接路径。
"""

from __future__ import annotations

import hashlib
import os


def _env_int(name: str, default: int, lo: int = 1, hi: int = 10000) -> int:
    try:
        v = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


class IterationBudget:
    """迭代预算追踪（固定上限，原实现）."""

    def __init__(self, max_turns: int = 60):
        self.max_turns = max_turns
        self.current = 0

    def tick(self) -> None:
        self.current += 1

    @property
    def exhausted(self) -> bool:
        return self.current >= self.max_turns

    @property
    def remaining(self) -> int:
        return self.max_turns - self.current

    @property
    def percentage(self) -> float:
        return (self.current / self.max_turns) * 100 if self.max_turns > 0 else 0


class AdaptiveBudget(IterationBudget):
    """自适应步数预算：小基准起步，有进展续期，无进展早停.

    对外保持 IterationBudget 的接口（max_turns / current / tick / exhausted /
    remaining / percentage），因此可直接替换而不改调用方。

    三个终止信号（调用方 break 后按 budget.stop_reason 出文案）：
    - "steps"    : 触到 hard_max（真跑满了，任务大概率极长）
    - "stalled"  : 连续 stall_limit 步无实质进展（死循环，最应该早停的）
    - None       : 尚未终止

    续期条件（全部满足才给）：
    1) 剩余步数 <= margin（临近耗尽，不是每步都续）
    2) 最近一步有实质进展
    3) 未到 hard_max
    """

    def __init__(
        self,
        base: int | None = None,
        extend: int | None = None,
        hard_max: int | None = None,
        stall_limit: int | None = None,
        margin: int = 5,
        max_turns: int | None = None,
        min_snippet: int = 4,
    ):
        """参数缺省读环境变量；显式传 max_turns 时退化为固定上限（兼容旧行为）."""
        if max_turns is not None:
            super().__init__(max_turns)
            self.base = max_turns
            self.extend = 0
            self.hard_max = max_turns
            self.stall_limit = 0
            self.margin = 0
            self.min_snippet = 0
        else:
            self.base = base if base is not None else _env_int("SCOUT_BUDGET_BASE", 25)
            self.extend = (
                extend if extend is not None else _env_int("SCOUT_BUDGET_EXTEND", 15)
            )
            self.hard_max = (
                hard_max
                if hard_max is not None
                else _env_int("SCOUT_BUDGET_HARD_MAX", 120)
            )
            self.stall_limit = (
                stall_limit
                if stall_limit is not None
                else _env_int("SCOUT_BUDGET_STALL", 8)
            )
            self.margin = max(0, margin)
            self.min_snippet = max(0, min_snippet)
            # 基准不得高于硬顶
            if self.base > self.hard_max:
                self.base = self.hard_max
            super().__init__(self.base)

        self.granted = 0          # 已续期次数
        self.idle = 0             # 当前连续"未调用任何工具"的步数
        self.stall = 0            # 当前连续无进展步数
        self.progress_steps = 0   # 有进展步数（供收尾文案/观测）
        self.stop_reason: str | None = None
        self._seen: set[str] = set()  # 本回合已见过的调用指纹

    # ── 观测 ──────────────────────────────────────────────────────────
    def observe(self, calls: list[tuple[str, bool, str]] | None) -> bool:
        """记录一步的工具执行结果，返回该步是否算"实质进展".

        calls: [(tool_name, success, output_snippet), ...]，空列表表示本步无工具调用
        （纯文本步）—— 单独走 idle 计数：连续极多步一个工具都不调，说明模型已
        脱离 ReAct 循环（自言自语），判死循环。阈值刻意放宽到 stall_limit 的 2 倍
        且不低于 12，避免误杀"先分析两步再动手"的正常节奏。

        进展判据（任一命中即进展）：
        - 至少一个调用成功，且其指纹（工具名 + 输出摘要）本回合没出现过；
        重复的成功调用（同工具同输出）不算进展 —— 这正是"假进展"空转的特征。
        """
        if not calls:
            self.idle += 1
            if self.stall_limit > 0 and self.idle >= max(12, self.stall_limit * 2):
                self.stop_reason = "stalled"
            return False

        self.idle = 0   # 本步至少还调了工具，脱离 idle 计数
        progressed = False
        has_fail = False      # 本步是否有失败调用（失败本身就是明确的"没推进"信号）
        has_content = False   # 本步是否有"有内容"的成功输出
        for name, ok, snippet in calls:
            if not ok:
                has_fail = True
                continue
            text = snippet or ""
            if len(text.strip()) >= self.min_snippet:
                has_content = True
            fp = self._fp(name, text)
            if fp in self._seen:
                continue
            self._seen.add(fp)
            progressed = True

        if progressed:
            self.stall = 0
            self.progress_steps += 1
            self._maybe_extend()
            return True

        if not (has_fail or has_content):
            # 中性信号：本步全部成功但输出为空/极短（如 grep 不同目录都"无匹配"）。
            # 这类探索确实在换位置，只是没命中——既不该记进展（否则无限续期），
            # 也不该记停滞（否则误杀正常排查）。stall 保持不变，最终由 base 步数自然收尾。
            return False

        self.stall += 1
        if self.stall_limit > 0 and self.stall >= self.stall_limit:
            self.stop_reason = "stalled"
        return False

    @staticmethod
    def _fp(name: str, snippet: str) -> str:
        raw = f"{name}|{(snippet or '')[:300]}"
        return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()

    # ── 续期 ──────────────────────────────────────────────────────────
    def _maybe_extend(self) -> bool:
        if self.extend <= 0 or self.max_turns >= self.hard_max:
            return False
        if self.current + self.margin < self.max_turns:
            return False
        self.max_turns = min(self.max_turns + self.extend, self.hard_max)
        self.granted += 1
        return True

    # ── 终止判定 ──────────────────────────────────────────────────────
    @property
    def stalled(self) -> bool:
        return self.stop_reason == "stalled"

    @property
    def exhausted(self) -> bool:
        if self.stop_reason == "stalled":
            return True
        if super().exhausted:
            if self.stop_reason is None:
                self.stop_reason = "steps"
            return True
        return False

    @property
    def display_max(self) -> int:
        """给 UI 的稳定分母 —— 用 hard_max，避免续期时进度条回退/跳变."""
        return self.hard_max

    @property
    def remaining(self) -> int:
        return max(0, self.max_turns - self.current)


def make_budget(
    max_turns: int,
    adaptive: bool = True,
    **kw,
) -> IterationBudget:
    """按开关构造预算对象.

    语义约定（2026-09-20）：开启自适应后，配置里的 ``max_turns`` 从"固定步数"
    变为**硬顶**——自适应预算在它之下运作（小基准起步、有进展续期、无进展早停）。
    这样"我配了 150 却在第 120 步被切"的困惑不会出现，同时 150 不再是默认起步量。

    adaptive=False → 完全退回旧的固定上限行为。
    """
    if not adaptive:
        return IterationBudget(max_turns)
    b = AdaptiveBudget(**kw)
    try:
        mt = int(max_turns or 0)
    except (TypeError, ValueError):
        mt = 0
    if mt > 0:
        b.hard_max = mt
        if b.base > b.hard_max:
            b.base = b.hard_max
        b.max_turns = b.base
    return b
