"""Reflexion — 反思与自我修正循环.

核心思想：每步工具执行后，用轻量 LLM 评估当前方向是否正确，
如果连续失败或偏离目标，触发策略调整建议注入上下文。

设计原则：
- 使用 executor_llm（便宜快速）生成反思
- 反思文本作为 system message 注入，引导下一步决策
- 连续失败时升级为"策略调整"，建议回退或换路
- 所有异常静默吞掉，反思是锦上添花，绝不阻断主循环
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Reflection:
    """单步反思结果."""
    step: int
    tool_name: str
    tool_success: bool
    assessment: str = ""           # 简短评估（1-2句）
    confidence: float = 0.5        # 0.0=完全迷失, 1.0=信心十足
    strategy_adjustment: str = ""  # 策略调整建议（空=无需调整）
    is_stuck: bool = False         # 是否陷入死循环/错误路径

    def to_context_hint(self) -> str:
        """转为注入上下文的简短提示."""
        parts = []
        if self.assessment:
            parts.append(f"[反思@步骤{self.step}] {self.assessment}")
        if self.strategy_adjustment:
            parts.append(f"策略调整: {self.strategy_adjustment}")
        if self.is_stuck:
            parts.append("⚠️ 当前方向可能有问题，请重新审视任务目标，考虑换一种方法。")
        return "\n".join(parts) if parts else ""


@dataclass
class ReflexionState:
    """一轮对话中的反思状态追踪."""
    reflections: list[Reflection] = field(default_factory=list)
    consecutive_failures: int = 0
    total_steps: int = 0
    last_adjustment_step: int = 0  # 上次策略调整的步数

    @property
    def success_rate(self) -> float:
        if self.total_steps == 0:
            return 0.0
        successes = sum(1 for r in self.reflections if r.tool_success)
        return successes / self.total_steps

    def recent_context(self, max_items: int = 3) -> str:
        """获取最近 N 条反思的上下文摘要."""
        recent = self.reflections[-max_items:]
        hints = [r.to_context_hint() for r in recent if r.to_context_hint()]
        return "\n".join(hints)


class ReflexionLoop:
    """反思循环 — 在 ReAct 循环的每步后评估方向.

    触发条件：
    - 每步工具执行后：轻量反思（快+便宜）
    - 连续失败 >= 2 次：深度反思 + 策略调整
    - 每 10 步：进度检查（且仅当任务步数>=10时）
    """

    def __init__(
        self,
        llm: Any,  # LLMClient — 用 executor_llm（便宜快）
        enable_deep_reflect: bool = True,
        failure_threshold: int = 2,   # 连续失败几次触发深度反思
        progress_interval: int = 10,  # 每几步做一次进度检查
        min_reflect_interval: int = 3,  # 两次反思间的最小步数间隔（节流，防连败后每步都反思）
    ):
        self.llm = llm
        self.enable_deep_reflect = enable_deep_reflect
        self.failure_threshold = failure_threshold
        self.progress_interval = progress_interval
        self.min_reflect_interval = min_reflect_interval

    def _should_reflect(self, state: ReflexionState, tool_success: bool) -> bool:
        """判断本轮是否需要反思.

        触发条件（减少过度反思）：
        - 达到连续失败阈值 → 深度反思（纠偏）
        - 达到进度检查间隔（步数较多时）→ 进度检查
        单次失败不立即反思，避免每次工具失败都触发 LLM 反思。
        """
        # 节流：距上次反思不足 min_reflect_interval 步时跳过，
        # 避免连续失败场景下每步失败都触发一次 LLM 反思（仍正常维护失败计数）
        if (
            state.last_adjustment_step
            and state.total_steps - state.last_adjustment_step < self.min_reflect_interval
        ):
            if not tool_success:
                state.consecutive_failures += 1
            else:
                state.consecutive_failures = 0
            return False

        # 连续失败达到阈值 → 必须反思（纠偏）
        if not tool_success:
            state.consecutive_failures += 1
            # 仅在达到阈值时反思；单次失败先不反思（随下一步自然纠偏）
            return state.consecutive_failures >= self.failure_threshold
        else:
            state.consecutive_failures = 0

        # 进度检查：放宽到 progress_interval（默认5→由调用方设），且仅当步数足够多时
        if state.total_steps >= 10 and state.total_steps % self.progress_interval == 0:
            return True

        return False

    async def reflect(
        self,
        state: ReflexionState,
        tool_name: str,
        tool_args: dict,
        tool_success: bool,
        tool_output: str,
        user_goal: str,
        step: int,
    ) -> Reflection | None:
        """执行一步反思.

        Args:
            state: 当前反思状态
            tool_name: 刚执行的工具名
            tool_args: 工具参数
            tool_success: 是否成功
            tool_output: 工具输出（截断）
            user_goal: 用户原始目标
            step: 当前步数

        Returns:
            Reflection 或 None（不需要反思时）
        """
        state.total_steps = step

        if not self._should_reflect(state, tool_success):
            return None

        if not self.llm:
            return None

        # 截断工具输出，控制 token
        output_excerpt = tool_output[:800] if tool_output else "(无输出)"
        args_brief = json.dumps(tool_args, ensure_ascii=False)[:300]

        # 构建反思 prompt
        is_deep = (
            state.consecutive_failures >= self.failure_threshold
            or (not tool_success and self.enable_deep_reflect)
        )

        if is_deep:
            prompt = self._build_deep_prompt(
                state, tool_name, args_brief, tool_success,
                output_excerpt, user_goal, step,
            )
        else:
            prompt = self._build_light_prompt(
                state, tool_name, args_brief, tool_success,
                output_excerpt, user_goal, step,
            )

        try:
            resp = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                _role="executor",
                extra_body={"enable_thinking": False},
            )
            reflection = self._parse_reflection(resp.content or "", step, tool_name, tool_success)
            if reflection:
                # 记录反思发生步数，供 _should_reflect 节流判断
                state.last_adjustment_step = step
            return reflection
        except Exception as e:
            logger.debug(f"Reflexion LLM call failed: {e}")
            return None

    def _build_light_prompt(
        self, state, tool_name, args_brief, success, output, goal, step
    ) -> str:
        return (
            "你是一个任务执行的自我监控模块。根据当前执行状态，用 1-2 句话评估方向是否正确。\n\n"
            f"用户目标: {goal[:300]}\n"
            f"当前步骤: {step}\n"
            f"刚执行: {tool_name}({args_brief})\n"
            f"结果: {'✅ 成功' if success else '❌ 失败'}\n"
            f"输出摘要: {output[:400]}\n"
        ) + (
            f"历史成功率: {state.success_rate:.0%}\n" if state.reflections else ""
        ) + (
            "\n请用 1-2 句中文评估当前方向，如果有问题指出怎么调整。不要编号、不要前缀。"
        )

    def _build_deep_prompt(
        self, state, tool_name, args_brief, success, output, goal, step
    ) -> str:
        recent_fails = [
            r for r in state.reflections[-5:] if not r.tool_success
        ]
        fail_summary = "\n".join(
            f"- 步骤{r.step}: {r.tool_name} 失败" for r in recent_fails
        ) if recent_fails else "(无)"

        return (
            "你是一个任务执行的策略顾问。当前执行遇到困难，需要评估是否要调整策略。\n\n"
            f"用户目标: {goal[:300]}\n"
            f"当前步骤: {step}\n"
            f"连续失败: {state.consecutive_failures} 次\n"
            f"整体成功率: {state.success_rate:.0%}\n\n"
            f"最近失败记录:\n{fail_summary}\n\n"
            f"刚执行: {tool_name}({args_brief})\n"
            f"结果: {'✅ 成功' if success else '❌ 失败'}\n"
            f"输出摘要: {output[:400]}\n\n"
            "请输出：\n"
            "1. 一句话评估当前状态\n"
            "2. 一句策略调整建议（如：换一种搜索关键词/先检查文件是否存在/回退到上一步等）\n\n"
            "格式：\n评估: ...\n调整: ..."
        )

    def _parse_reflection(
        self, text: str, step: int, tool_name: str, success: bool
    ) -> Reflection:
        """解析 LLM 的反思输出."""
        text = text.strip()
        reflection = Reflection(
            step=step,
            tool_name=tool_name,
            tool_success=success,
        )

        # 尝试解析 "评估: ... 调整: ..." 格式
        import re
        assess_match = re.search(r"(?:评估|状态|分析)[:：]\s*(.+?)(?:\n|$)", text)
        adjust_match = re.search(r"(?:调整|建议|策略|下一步)[:：]\s*(.+?)(?:\n|$)", text)

        if assess_match:
            reflection.assessment = assess_match.group(1).strip()
        else:
            # 取前两行作为评估
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            reflection.assessment = lines[0] if lines else text[:100]

        if adjust_match:
            reflection.strategy_adjustment = adjust_match.group(1).strip()

        # 判断是否陷入困境
        stuck_keywords = ["死循环", "无法", "失败", "错误", "偏离", "不对", "换一种", "回退", "放弃"]
        if any(kw in text for kw in stuck_keywords):
            reflection.is_stuck = True
            reflection.confidence = 0.2
        else:
            reflection.confidence = 0.7 if success else 0.4

        return reflection
