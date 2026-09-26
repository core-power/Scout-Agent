"""IM 渠道交互式澄清（ChannelCallbacks + ask_user 哨兵）单元测试.

覆盖：澄清排版、on_clarify 发送并返回哨兵/失败降级、on_confirm 默认拒绝、
on_watchdog 默认继续，以及 ask_user 识别哨兵后「结束本轮等待回复」的行为。
"""

from __future__ import annotations

import pytest

from scout.adapters.channel_callbacks import ChannelCallbacks, format_clarify_message
from scout.core.callbacks import IM_CLARIFY_SENT
from scout.tools.builtin.ask_user import AskUserTool
from scout.tools.registry import ToolRegistry


# ── format_clarify_message ─────────────────────────────────────────


def test_format_with_options():
    text = format_clarify_message("要哪种格式？", ["PDF", "Word", "Markdown"])
    assert "要哪种格式？" in text
    assert "1. PDF" in text and "2. Word" in text and "3. Markdown" in text
    assert "回复编号" in text


def test_format_without_options():
    text = format_clarify_message("你的目标目录是？", None)
    assert text.strip() == "你的目标目录是？"
    assert "1." not in text


def test_format_caps_options_at_six():
    text = format_clarify_message("选一个", [f"opt{i}" for i in range(10)])
    assert "6. opt5" in text
    assert "opt6" not in text  # 第 7 项起被截断


# ── ChannelCallbacks ───────────────────────────────────────────────


async def test_on_clarify_sends_and_returns_sentinel():
    sent = []

    async def send_fn(cid, text, **kw):
        sent.append((cid, text, kw))
        return True

    cb = ChannelCallbacks(send_fn, channel_id="c1", user_id="u1", reply_to="m9")
    result = await cb.on_clarify("要哪种？", ["A", "B"])
    assert result == IM_CLARIFY_SENT
    assert len(sent) == 1
    cid, text, kw = sent[0]
    assert cid == "c1"
    assert "1. A" in text and "2. B" in text
    assert kw.get("reply_to") == "m9"


async def test_on_clarify_send_failure_degrades_to_empty():
    async def send_fn(cid, text, **kw):
        raise RuntimeError("network down")

    cb = ChannelCallbacks(send_fn, channel_id="c1")
    # 发送失败 → 返回空串（ask_user 退回旧的「说明假设后继续」降级）
    assert await cb.on_clarify("问题", ["A"]) == ""


async def test_on_clarify_empty_question_no_send():
    calls = []

    async def send_fn(cid, text, **kw):
        calls.append(text)
        return True

    cb = ChannelCallbacks(send_fn, channel_id="c1")
    assert await cb.on_clarify("   ", None) == ""
    assert calls == []


async def test_on_confirm_denies_and_notifies():
    sent = []

    async def send_fn(cid, text, **kw):
        sent.append(text)
        return True

    cb = ChannelCallbacks(send_fn, channel_id="c1")
    ok = await cb.on_confirm("req1", "shell", {"cmd": "rm"}, "危险命令")
    assert ok is False  # IM 默认拒绝危险操作
    assert sent and "shell" in sent[0] and "网页端" in sent[0]


async def test_on_watchdog_defaults_continue():
    async def send_fn(cid, text, **kw):
        return True

    cb = ChannelCallbacks(send_fn, channel_id="c1")
    assert await cb.on_watchdog("空转", {"steps": 3}) is True


# ── ask_user 识别 IM 哨兵 → 结束本轮 ───────────────────────────────


async def test_ask_user_returns_im_sent_observation(monkeypatch):
    """端到端：ask_user → ChannelCallbacks.on_clarify 发送 → 哨兵 → 结束本轮提示."""
    sent = []

    async def send_fn(cid, text, **kw):
        sent.append(text)
        return True

    class _FakeAgent:
        callbacks = ChannelCallbacks(send_fn, channel_id="c1", user_id="u1")

    monkeypatch.setattr(ToolRegistry, "_main_agent", _FakeAgent(), raising=False)
    tool = AskUserTool()
    assert tool is not None

    obs = await tool.execute(question="要哪种格式？", options=["PDF", "Word"])
    assert obs.success is True
    assert (obs.metadata or {}).get("im_clarify_sent") is True
    assert "结束本轮" in obs.output or "等待" in obs.output
    # 问题确实发到了渠道
    assert sent and "1. PDF" in sent[0]


async def test_ask_user_normal_answer_unaffected(monkeypatch):
    """非 IM（回调直接返回答案）时 ask_user 行为不变."""

    class _Cb:
        _ask_user_count = 0

        async def on_clarify(self, question, options=None):
            return "用户的回答"

    class _FakeAgent:
        callbacks = _Cb()

    monkeypatch.setattr(ToolRegistry, "_main_agent", _FakeAgent(), raising=False)
    tool = AskUserTool()
    obs = await tool.execute(question="确认一下？")
    assert obs.success is True
    assert "用户的回答" in obs.output
    assert (obs.metadata or {}).get("im_clarify_sent") is None
