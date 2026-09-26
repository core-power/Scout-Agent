"""IM 渠道回调 — 让 ask_user 澄清在 Telegram/Discord 等 IM 渠道可用（2026-09-24）.

背景：IM 渠道没有 WebSocket，且 ChannelManager 的 ``_run_channel`` 循环是**串行
阻塞**的 —— 一个回合处理（await agent）期间读不到下一条入站消息。因此无法像 Web
那样「弹窗阻塞等待用户即时作答」。

方案：契合 IM 天然的**多轮对话**模型。``on_clarify`` 把问题 + 编号选项**发送到
渠道**并返回哨兵 ``IM_CLARIFY_SENT``；ask_user 据此让模型结束本轮、提示用户「已
把问题发给你，等你回复」。因为 IM 会话按 (渠道,用户) 持久（handle_message 载入/
保存同一 session id），用户的下一条消息会在**保留上下文**的同一会话里自然续上，
作为对澄清的回答。

安全：``on_confirm``（危险操作确认）在 IM 上默认**拒绝**并提示改用网页端批准 ——
IM 无法可靠地做交互式批准，自动放行危险操作不可接受。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from scout.core.callbacks import IM_CLARIFY_SENT, Callbacks

logger = logging.getLogger(__name__)

# 发送函数签名：async (channel_id, text, **kwargs) -> bool
SendFn = Callable[..., Awaitable[Any]]


def format_clarify_message(question: str, options: list[str] | None = None) -> str:
    """把澄清问题 + 选项排版成适合 IM 的纯文本（编号列表 + 作答指引）."""
    lines = [(question or "").strip()]
    opts = [str(o).strip() for o in (options or []) if o and str(o).strip()]
    if opts:
        lines.append("")
        for i, o in enumerate(opts[:6], 1):  # 最多 6 项，避免刷屏
            lines.append(f"{i}. {o}")
        lines.append("")
        lines.append("回复编号，或直接把答案发给我。")
    return "\n".join(x for x in lines if x is not None)


class ChannelCallbacks(Callbacks):
    """IM 渠道回调：澄清走「发送 + 结束本轮 + 下一条消息作答」的多轮模型."""

    def __init__(
        self,
        send_fn: SendFn,
        channel_id: str,
        user_id: str = "",
        reply_to: str | None = None,
    ):
        self._send = send_fn
        self.channel_id = channel_id
        self.user_id = user_id
        self.reply_to = reply_to

    async def on_clarify(self, question: str, options: list[str] | None = None) -> str:
        """把澄清问题发到 IM 渠道；成功返回哨兵，失败返回空串（退回旧降级）."""
        text = format_clarify_message(question, options)
        if not text.strip():
            return ""
        try:
            await self._send(self.channel_id, text, reply_to=self.reply_to)
        except Exception:  # noqa: BLE001
            logger.debug("IM 澄清发送失败（退回降级）", exc_info=True)
            return ""
        return IM_CLARIFY_SENT

    async def on_confirm(
        self, request_id: str, tool_name: str, args: dict, reason: str
    ) -> bool:
        """IM 无法可靠地交互式批准危险操作 → 默认拒绝并提示改用网页端."""
        try:
            await self._send(
                self.channel_id,
                f"⚠️ 操作「{tool_name}」需要确认，但 IM 渠道暂不支持批准。"
                f"请改用网页端处理。（原因：{reason}）",
                reply_to=self.reply_to,
            )
        except Exception:  # noqa: BLE001
            pass
        return False

    async def on_watchdog(self, warning: str, meta: dict | None = None) -> bool:
        # IM 不做「继续/停止」弹窗；默认继续（模型已收到看门狗提示，2 次无进展强制收尾）
        return True

    # ── 其余回调：IM 无需过程可视化，空实现 ──
    async def on_tool_progress(self, tool_name: str, stage: str, message: str, metadata: dict | None = None) -> None:
        pass

    async def on_thinking(self, started: bool) -> None:
        pass

    async def on_reasoning(self, content: str) -> None:
        pass

    async def on_step(self, step: int, total_budget: int) -> None:
        pass

    async def on_stream_delta(self, text: str) -> None:
        pass

    async def on_tool_gen(self, tool_name: str, args: dict) -> None:
        pass

    async def on_status(self, status: str) -> None:
        pass

    async def on_reflection(self, hint: str) -> None:
        pass

    async def on_goals_extracted(self, goals: list[dict]) -> None:
        pass

    async def on_file(self, file_path: str, file_name: str = "", file_size: int = 0) -> None:
        pass
