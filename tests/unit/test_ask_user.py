"""ask_user 用户澄清链路单测（2026-09-23）.

覆盖：工具注册/schema、WebCallbacks future 消费与超时清理、取消清理、
每轮次数上限、无交互降级、旧签名回调兼容、TaggedCallbacks 转发。
"""

import asyncio

import pytest

from scout.adapters.web.callbacks import WebCallbacks
from scout.core.callbacks import NullCallbacks, TaggedCallbacks
from scout.tools.builtin.ask_user import (
    AskUserTool,
    _accepts_options,
    clarify_timeout_s,
)
from scout.tools.registry import ToolRegistry


@pytest.fixture
def tool() -> AskUserTool:
    return ToolRegistry.get_tool("ask_user")


# ── 注册与 schema ──────────────────────────────────────────


def test_registered_and_schema(tool):
    assert tool is not None
    schema = tool.to_schema()
    fn = schema["function"]
    assert fn["name"] == "ask_user"
    assert "question" in fn["parameters"]["properties"]
    assert "options" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["question"]


def test_timeout_env_override(monkeypatch):
    monkeypatch.setenv("SCOUT_CLARIFY_TIMEOUT", "120")
    assert clarify_timeout_s() == 120
    monkeypatch.setenv("SCOUT_CLARIFY_TIMEOUT", "5")  # 低于下限 → 钳到 30
    assert clarify_timeout_s() == 30
    monkeypatch.setenv("SCOUT_CLARIFY_TIMEOUT", "abc")  # 非法 → 默认 300
    assert clarify_timeout_s() == 300


# ── WebCallbacks.on_clarify：future 消费 / 超时 / 取消 ─────


async def test_web_clarify_answer_flow():
    cb = WebCallbacks(None)
    consumed = {}

    async def fake_wait_for(fut, timeout=None):
        # 模拟 WS 消费端：pop 注册表 + set_result（ws.py clarify_response 行为）
        for rid, f in list(cb.pending_clarifications.items()):
            if f is fut and not f.done():
                consumed[rid] = True
                cb.pending_clarifications.pop(rid)
                f.set_result("选方案B")
        return await fut

    import unittest.mock as mock

    with mock.patch.object(asyncio, "wait_for", fake_wait_for):
        ans = await cb.on_clarify("用哪个方案？", ["方案A", "方案B"])
    assert ans == "选方案B"
    assert not cb.pending_clarifications  # 已消费清理

    ev = await cb.events.get()
    assert ev["type"] == "clarify_request"
    assert ev["data"]["options"] == ["方案A", "方案B"]
    assert ev["data"]["question"] == "用哪个方案？"
    assert ev["data"]["request_id"]


async def test_web_clarify_timeout_cleans_and_notifies(monkeypatch):
    """超时 → 注册表清理 + 推 clarify_cancelled（前端据此关弹窗）."""
    import scout.tools.builtin.ask_user as ask_user_mod

    monkeypatch.setattr(ask_user_mod, "clarify_timeout_s", lambda: 0)

    cb = WebCallbacks(None)
    adapter = type("A", (), {"_pending_clarifications": {}})()
    cb._adapter = adapter

    ans = await cb.on_clarify("在吗？")
    assert ans == ""
    assert not cb.pending_clarifications
    assert not adapter._pending_clarifications  # adapter 兼容表也清理

    events = []
    while not cb.events.empty():
        events.append(cb.events.get_nowait())
    types = [e["type"] for e in events]
    assert "clarify_request" in types and "clarify_cancelled" in types


async def test_web_clarify_cancelled_cleans_registry():
    """用户点停止 → CancelledError 传播且注册表不泄漏."""
    cb = WebCallbacks(None)

    async def cancel_during_wait(fut, timeout=None):
        fut.cancel()
        return await fut

    import unittest.mock as mock

    with mock.patch.object(asyncio, "wait_for", cancel_during_wait):
        with pytest.raises(asyncio.CancelledError):
            await cb.on_clarify("q")
    assert not cb.pending_clarifications


# ── ask_user 工具行为 ──────────────────────────────────────


class _FakeAgent:
    def __init__(self, cb):
        self.callbacks = cb


class _EchoCB:
    async def on_clarify(self, question, options=None):
        return f"已答:{question}|opts={options}"


class _OldSignatureCB:
    async def on_clarify(self, question):  # 旧签名
        return "旧签名回答"


async def test_tool_end_to_end(tool, monkeypatch):
    monkeypatch.setattr(ToolRegistry, "_main_agent", _FakeAgent(_EchoCB()), raising=False)
    obs = await tool.execute(question="目标文件是哪个？", options=["a.md", "b.md", "a.md", ""])
    assert obs.success
    assert "已答:目标文件是哪个？" in obs.output
    assert "opts=['a.md', 'b.md']" in obs.output  # 去重 + 去空
    assert obs.metadata["question"] == "目标文件是哪个？"


async def test_tool_old_signature_callback(tool, monkeypatch):
    monkeypatch.setattr(ToolRegistry, "_main_agent", _FakeAgent(_OldSignatureCB()), raising=False)
    obs = await tool.execute(question="q")
    assert obs.success and "旧签名回答" in obs.output


async def test_tool_no_interactive_degrade(tool, monkeypatch):
    monkeypatch.setattr(ToolRegistry, "_main_agent", None, raising=False)
    obs = await tool.execute(question="x")
    assert not obs.success
    assert obs.error_code == "UNAUTHORIZED"


async def test_tool_empty_question(tool):
    obs = await tool.execute(question="")
    assert obs.error_code == "INVALID_ARGS"


async def test_tool_per_turn_cap(tool, monkeypatch):
    """单轮 3 次上限：第 4 次返回提示而非再次弹窗."""
    monkeypatch.setattr(ToolRegistry, "_main_agent", _FakeAgent(_EchoCB()), raising=False)
    for i in range(3):
        obs = await tool.execute(question=f"q{i}")
        assert obs.success and "用户回答" in obs.output
    obs4 = await tool.execute(question="q4")
    assert obs4.success
    assert "上限" in obs4.output
    # 计数挂在 callbacks 上（per-request 对象），换一个 callbacks 即清零
    monkeypatch.setattr(ToolRegistry, "_main_agent", _FakeAgent(_EchoCB()), raising=False)
    obs_new = await tool.execute(question="新请求")
    assert "用户回答" in obs_new.output


async def test_tool_empty_answer_degrades(tool, monkeypatch):
    class _SilentCB:
        async def on_clarify(self, question, options=None):
            return "   "

    monkeypatch.setattr(ToolRegistry, "_main_agent", _FakeAgent(_SilentCB()), raising=False)
    obs = await tool.execute(question="q")
    assert obs.success and "用户未回应" in obs.output


# ── TaggedCallbacks 转发与签名判断 ─────────────────────────


async def test_tagged_forwards_to_null():
    tc = TaggedCallbacks(NullCallbacks(), agent_role="main", agent_name="主代理")
    assert await tc.on_clarify("q?", ["a"]) == ""


async def test_accepts_options_signature_check():
    assert _accepts_options(_EchoCB().on_clarify) is True
    assert _accepts_options(_OldSignatureCB().on_clarify) is False

    def _kwargs_only(question, **kw):  # VAR_KEYWORD 也算支持
        return question

    assert _accepts_options(_kwargs_only) is True


async def test_tagged_old_signature_inner():
    """Tagged 包装旧签名回调 → 自动降级单参调用，不靠 TypeError 重试."""
    tc = TaggedCallbacks(_OldSignatureCB(), agent_role="main", agent_name="主代理")
    assert await tc.on_clarify("q", ["a", "b"]) == "旧签名回答"
