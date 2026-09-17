# -*- coding: utf-8 -*-
"""API 请求前缀稳定性测试（2026-09-17，事故②复盘）.

背景：LLM API 无状态，回合内第 N 步会重发前 N-1 步消息。若消息序列前缀
在步与步之间发生任何字节级变化，厂商的 context/prefix cache 全部失效
（自部署 vLLM / PAI-EAS 需开启 prefix caching 才生效），实测单个 GUI 任务
烧掉 107K token，绝大部分是逐步重发的相同前缀。

固定三条合约：
1. 回合内逐步追加（GUI 任务 20+ 步的主战场），前缀必须逐字节稳定；
2. 跨回合追加新消息，旧序列仍须原样成为新序列的前缀；
3. runtime_context 只注入最后一条带它的 user 消息（早期注入被有意
   丢弃以防重复付费——设计行为，锁定防回归）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from scout.core.types import Message, Role, Session
from scout.engine.agent import Agent


def _agent() -> Agent:
    """最小化 Agent：关闭全部可选子系统，只测消息构建纯函数路径."""
    return Agent(
        llm=MagicMock(),
        system_prompt="SYS",
        enable_context=False,
        enable_persistence=False,
        enable_memory=False,
        enable_security=False,
        enable_skills=False,
        enable_workspace=False,
        enable_bus=False,
        auto_approve=True,
        deep_thinking=False,
    )


def _sess() -> Session:
    return Session(
        id="s-prefix",
        messages=[
            Message(role=Role.USER, content="第一问",
                    metadata={"runtime_context": "[技能] x\n[记忆] y"}),
            Message(role=Role.ASSISTANT, content="答1"),
            Message(role=Role.USER, content="第二问", metadata={}),
        ],
    )


def test_prefix_stable_within_a_single_turn():
    """合约 1（核心）：回合内逐步追加，前缀逐字节稳定——缓存命中的主战场。"""
    agent = _agent()
    s = Session(id="s", messages=[
        Message(role=Role.USER, content="任务",
                metadata={"runtime_context": "[技能] x"}),
    ])
    m_prev = agent._build_api_messages(s)
    for i in range(3):
        s.messages.append(Message(
            role=Role.ASSISTANT, content="",
            metadata={"tool_calls": [
                {"name": "desktop", "call_id": f"call_{i}", "arguments": {}},
            ]}))
        s.messages.append(Message(
            role=Role.TOOL, content=f"工具输出 {i}",
            metadata={"call_id": f"call_{i}", "tool_name": "desktop",
                      "success": True}))
        m_now = agent._build_api_messages(s)
        assert m_now[: len(m_prev)] == m_prev, (
            f"回合内第 {i + 1} 步后前缀被改变 → context cache 全部失效")
        m_prev = m_now


def test_prefix_stable_when_appending_rounds():
    """合约 2：跨回合追加新消息，旧序列仍须原样成为新序列的前缀。"""
    agent = _agent()
    s = _sess()
    m1 = agent._build_api_messages(s)

    s.messages.append(Message(
        role=Role.ASSISTANT, content="",
        metadata={"tool_calls": [
            {"name": "web_search", "call_id": "call_1", "arguments": {"query": "q"}},
        ]}))
    s.messages.append(Message(
        role=Role.TOOL, content="结果",
        metadata={"call_id": "call_1", "tool_name": "web_search", "success": True}))
    s.messages.append(Message(role=Role.ASSISTANT, content="答2"))
    m2 = agent._build_api_messages(s)

    assert len(m2) > len(m1)
    assert m2[: len(m1)] == m1, "新增一轮后旧消息序列被改变 → 前缀缓存失效"


def test_runtime_context_only_on_latest_user():
    """合约 3：runtime_context 只注入最后一条带它的 user（早期注入被有意丢弃）。"""
    agent = _agent()
    s = _sess()
    s.messages.append(Message(role=Role.USER, content="第三问",
                              metadata={"runtime_context": "[技能] z"}))
    m2 = agent._build_api_messages(s)

    users = [m for m in m2 if m["role"] == "user"]
    assert len(users) == 3
    assert users[-1]["content"].startswith("第三问")
    assert "[技能] z" in users[-1]["content"]
    # 设计行为：早期 user 的注入被丢弃（避免多份 runtime_context 重复计费）
    assert "[技能] x" not in users[0]["content"]
