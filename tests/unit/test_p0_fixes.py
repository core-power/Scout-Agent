# -*- coding: utf-8 -*-
"""P0 修复（2026-09-09）单元测试：剪枝配对完整性 / 压缩范围 / 委派上下文."""

import asyncio

import pytest

from scout.core.types import Message, Role
from scout.multiagent import runtime as rt
from scout.tools.builtin.delegate import _delegation_context


def _mk(role, content, **meta):
    return Message(role=role, content=content, metadata=meta)


class _FakeSession:
    def __init__(self, messages):
        self.messages = messages


@pytest.fixture()
def mgr():
    from scout.context.manager import ContextManager

    return ContextManager()


# ── 1. 剪枝不得产生孤儿 tool 消息（并行 tool_call 批次整批删除）──


def test_prune_keeps_tool_call_pairs(mgr):
    msgs = [_mk(Role.USER, "hi")]
    # 三轮，每轮 assistant 带 3 个并行 tool_call → 3 条 TOOL 结果
    for r in range(3):
        msgs.append(_mk(Role.ASSISTANT, "", tool_calls=[
            {"name": "t", "call_id": f"c{r}1", "arguments": {}},
            {"name": "t", "call_id": f"c{r}2", "arguments": {}},
            {"name": "t", "call_id": f"c{r}3", "arguments": {}},
        ]))
        for k in range(3):
            msgs.append(_mk(Role.TOOL, f"out{r}{k}" * 50, call_id=f"c{r}{k+1}"))
    sess = _FakeSession(list(msgs))
    mgr.max_tool_outputs = 4  # 强制剪枝
    mgr.prune_tool_outputs(sess)
    # 不变量：每条 TOOL 的前向最近 assistant 仍存在（同批未被拆散）
    ids = {m.metadata.get("call_id") for m in sess.messages if m.role == Role.TOOL}
    calls = set()
    for m in sess.messages:
        if m.role == Role.ASSISTANT and m.metadata.get("tool_calls"):
            calls |= {tc["call_id"] for tc in m.metadata["tool_calls"]}
    assert ids, "should keep some tools"
    assert ids <= calls, f"孤儿 tool 消息: {ids - calls}"


def test_token_prune_shrinks_in_place_not_delete(mgr):
    msgs = [_mk(Role.USER, "hi")]
    msgs.append(_mk(Role.ASSISTANT, "", tool_calls=[
        {"name": "t", "call_id": "c1", "arguments": {}},
        {"name": "t", "call_id": "c2", "arguments": {}},
    ]))
    msgs.append(_mk(Role.TOOL, "x" * 100000, call_id="c1"))
    msgs.append(_mk(Role.TOOL, "small", call_id="c2"))
    sess = _FakeSession(list(msgs))
    mgr.max_tokens = 2000  # 触发 token 维度瘦身
    mgr.prune_tool_outputs(sess)
    tool_ids = [m.metadata.get("call_id") for m in sess.messages if m.role == Role.TOOL]
    # 原地瘦身：两条 TOOL 都还在（配对完整），大输出被替换
    assert tool_ids == ["c1", "c2"]
    big = next(m for m in sess.messages if m.metadata.get("call_id") == "c1")
    assert len(big.content) < 200


# ── 2. 压缩范围：运行笔记在末尾时仍可压缩（start 不被末尾 SYSTEM 拉走）──


def test_compression_range_with_notes_at_end(mgr):
    msgs = [_mk(Role.USER, "q")] * 60
    msgs.append(_mk(Role.SYSTEM, "[运行笔记] 要点"))  # 末尾的运行笔记
    sess = _FakeSession(msgs)
    rng = mgr.get_compression_range(sess, min_total=10)
    assert rng is not None, "运行笔记在末尾不应导致压缩 no-op"
    start, end = rng
    assert start == 0  # 不跳到末尾 SYSTEM 之后


def test_compression_range_does_not_split_tool_batch(mgr):
    msgs = [_mk(Role.USER, "q")] * 50
    msgs.append(_mk(Role.ASSISTANT, "", tool_calls=[
        {"name": "t", "call_id": "b1", "arguments": {}},
        {"name": "t", "call_id": "b2", "arguments": {}},
    ]))
    msgs.append(_mk(Role.TOOL, "r1", call_id="b1"))
    msgs.append(_mk(Role.TOOL, "r2", call_id="b2"))
    # keep_recent 恰好切在第一条 TOOL 上
    sess = _FakeSession(list(msgs))
    total = len(msgs)
    sess_keep = mgr.keep_recent
    rng = None
    # 构造 end = total - keep_recent 正好落在 b1
    while sess_keep >= 1:
        mgr.keep_recent = sess_keep
        r = mgr.get_compression_range(sess, min_total=5)
        if r and r[1] < total and sess.messages[r[1]].role == Role.TOOL:
            rng = r
            break
        sess_keep -= 1
    if rng:
        start, end = rng
        # end 之后不得残留 TOOL（其 assistant 已在压缩区间）
        assert sess.messages[end].role != Role.TOOL
    mgr.keep_recent = sess_keep or mgr.keep_recent


# ── 3. 委派上下文 ContextVar ──


def test_delegation_context_none_by_default():
    assert _delegation_context()[0] is None


def test_delegation_context_set_and_reset():
    tok = rt.set_current_delegation("dl_abc", "子代理-测试")
    try:
        assert _delegation_context() == ("dl_abc", "子代理-测试")
    finally:
        rt.reset_current_delegation(tok)
    assert _delegation_context()[0] is None


def test_delegation_context_parallel_isolation():
    """并行任务各自持独立上下文（ContextVar 任务隔离）."""
    results = {}

    async def sub(name, did):
        tok = rt.set_current_delegation(did, name)
        try:
            await asyncio.sleep(0.02)
            results[name] = _delegation_context()
        finally:
            rt.reset_current_delegation(tok)

    async def main():
        await asyncio.gather(
            sub("A", "dl_1"),
            sub("B", "dl_2"),
        )

    asyncio.run(main())
    assert results["A"] == ("dl_1", "A")
    assert results["B"] == ("dl_2", "B")


# ── 4. broker pending() 不抛 TypeError ──


def test_broker_pending_missing_key():
    from scout.multiagent.broker import DelegateBroker

    b = DelegateBroker()
    assert b.pending("no_such_delegation") == 0
