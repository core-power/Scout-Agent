"""ask_user — 有疑惑时向用户提问澄清（WorkBuddy AskUserQuestion 同款）.

设计动机（2026-09-23）：此前 agent 循环里 LLM 没有任何手段在任务中途向用户
提问——提示词写着"除非需要用户澄清"，但澄清通道根本不存在（on_clarify 回调
全代码库无人调用）。需求含糊时只能靠模型猜，猜错就整轮返工。

链路：LLM 调 ask_user(question, options)
  → ToolRegistry._main_agent.callbacks.on_clarify(question, options)
  → Web 控制台：前端弹澄清卡片，用户点选项或自由输入
  → clarify_response 回包 → future 唤醒 → 工具返回用户回答
  → LLM 拿到回答继续 ReAct 循环

降级行为（不抛错，保证 agent 能继续）：
- 无交互 UI（NullCallbacks / 无 _main_agent）→ 返回提示"当前环境不支持交互"
- 用户长时间未回应 / 关闭卡片 → 返回提示"用户未回应"，模型应说明假设后继续
- 单轮请求内最多问 3 次 → 超限返回提示，防 LLM 刷屏式追问把任务变成问答游戏
  （计数挂在 per-request 的 callbacks 对象上，每轮请求自然清零）

注意：ask_user 不暴露给子代理（DELEGATE_TOOLS 排除）——子代理的歧义应写进
结论交回主代理，由主代理统一决定是否向用户澄清，避免编排过程中弹窗语义混乱。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any

from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 单轮请求内澄清次数上限。防 LLM 反复追问（提示词已约束，这里是硬兜底）。
_MAX_ASKS_PER_TURN = 3


def clarify_timeout_s() -> int:
    """用户回答等待上限（秒），SCOUT_CLARIFY_TIMEOUT 可覆盖.

    统一入口：ask_user 工具与 WebCallbacks.on_clarify 共用，避免两处硬编码漂移
    （此前 300 写了两遍）。太短会在用户离开时频繁降级；太长会占住一个工具槽。
    """
    try:
        return max(30, int(os.getenv("SCOUT_CLARIFY_TIMEOUT", "300") or 300))
    except ValueError:
        return 300


def _accepts_options(fn) -> bool:
    """回调是否支持 (question, options) 新签名.

    旧实现只收 (question)——用签名精确判断，而不是 try/except TypeError 重试
    （TypeError 重试会把回调内部真实的类型错误吞成"旧签名"）。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True  # 拿不到签名（C 扩展等）→ 按新签名尝试
    if "options" in params:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())


class AskUserTool(ToolDefinition):
    """向用户提问澄清 — 需求含糊、多种做法难以取舍、缺关键信息时使用."""

    name = "ask_user"
    description = (
        "有疑惑时向用户提问澄清（等待用户回答后继续）。适用：需求存在歧义、"
        "有多种做法且选择影响结果、缺少关键信息（目标文件/路径/范围/格式等）。"
        "不要基于猜测一路做完——先问再做。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要向用户澄清的问题，一句话说清疑惑点",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：2-4 个候选项，用户可直接点选（也总能自由输入）",
            },
        },
        "required": ["question"],
    }
    # 交互工具：有副作用（阻塞等待用户）、绝不能与其他工具并行
    pure_read = False

    async def execute(self, question: str = "", options: list[str] | None = None, **kwargs: Any) -> Observation:
        question = (question or "").strip()
        if not question:
            return Observation(
                tool_name=self.name,
                success=False,
                output="",
                error="question 不能为空：请写清楚要澄清的问题",
                error_code="INVALID_ARGS",
            )

        # 选项规整：去空、去重、最多 4 个（与前端卡片布局一致）
        opts: list[str] = []
        for o in options or []:
            o = str(o).strip()
            if o and o not in opts:
                opts.append(o)
        opts = opts[:4]

        # 拿当前请求的 agent（ws/chat 路由在每轮请求里注入 _main_agent，
        # 其 callbacks 是带真实 UI 通道的 WebCallbacks/TaggedCallbacks）
        agent = getattr(ToolRegistry, "_main_agent", None)
        callbacks = getattr(agent, "callbacks", None) if agent else None
        on_clarify = getattr(callbacks, "on_clarify", None)
        if on_clarify is None:
            return Observation(
                tool_name=self.name,
                success=False,
                output=(
                    "当前环境不支持交互澄清（无用户回调）。"
                    "请基于现有信息选择最合理的做法，并在最终回复中说明你的假设。"
                ),
                error="no interactive channel",
                error_code="UNAUTHORIZED",
            )

        # 每轮请求次数上限：计数挂在 callbacks 对象上（每轮请求新建，自然清零；
        # 主/子代理经 TaggedCallbacks 各持一层，主代理计数落在最外层包装上）
        count = getattr(callbacks, "_ask_user_count", 0) + 1
        try:
            callbacks._ask_user_count = count
        except Exception:  # noqa: BLE001  # 不可 setattr 的回调实现 → 放弃硬限
            count = 0
        if count > _MAX_ASKS_PER_TURN:
            return Observation(
                tool_name=self.name,
                success=True,
                output=(
                    f"本轮澄清次数已达上限（{_MAX_ASKS_PER_TURN} 次）。"
                    "请基于已有信息与用户的历次回答继续完成任务；"
                    "剩余疑点在最终回复中列出，供用户一次性纠正。"
                ),
            )

        timeout = clarify_timeout_s()
        try:
            # on_clarify 会阻塞到用户回答 / 超时 / 请求被取消；内部自带超时，
            # 这里再兜一层（+5s 余量，让内部超时先触发以返回更准确的降级文案）
            coro = (
                on_clarify(question, opts)
                if _accepts_options(on_clarify)
                else on_clarify(question)
            )
            answer = await asyncio.wait_for(coro, timeout=timeout + 5)
        except asyncio.TimeoutError:
            answer = ""
        except Exception as e:  # noqa: BLE001
            # CancelledError 是 BaseException，不会被这里吞掉——取消应正常向上传播
            logger.warning(f"ask_user 澄清失败: {e}")
            answer = ""

        # IM 渠道（2026-09-24）：ChannelCallbacks 已把问题+选项发到聊天渠道并返回
        # 哨兵 → 让模型结束本轮、提示用户等待其回复（下一条消息在持久会话里续上）。
        from scout.core.callbacks import IM_CLARIFY_SENT
        if answer == IM_CLARIFY_SENT:
            return Observation(
                tool_name=self.name,
                success=True,
                output=(
                    "澄清问题已通过聊天渠道发送给用户。请立即结束本轮：用一句话告诉用户"
                    "你已把问题发过去、正在等待其回复；不要自行假设继续，也不要再调用其他工具。"
                    "用户的下一条消息会作为回答（同一会话已保留上下文）。"
                ),
                metadata={"im_clarify_sent": True},
            )

        answer = (answer or "").strip()
        if not answer:
            return Observation(
                tool_name=self.name,
                success=True,
                output=(
                    "用户未回应（超时或跳过了问题）。"
                    "请基于现有信息选择最合理的做法继续，并在回复中说明你的假设；"
                    "若该假设影响重大，明确告知用户可以纠正。"
                ),
            )

        return Observation(
            tool_name=self.name,
            success=True,
            output=f"用户回答：{answer}",
            metadata={"question": question, "options": opts},
        )


ToolRegistry.register(AskUserTool())
