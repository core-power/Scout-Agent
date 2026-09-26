"""活跃会话注册表（上下文圆环实时性，2026-09-23）单测.

背景：/api/context/stats 此前读磁盘 session——生成期间消息只 append 进
内存对象、回合结束才落盘，导致整轮生成中圆环数值不动。修复：agent 维护
_active_sessions 注册表，stats 优先读内存版。
"""

import asyncio
import copy

from scout.core.types import Message, Role, Session
from scout.engine.agent import Agent


def _bare_agent() -> Agent:
    """绕过重 init（LLM key 等），只装注册表相关的最小属性."""
    a = Agent.__new__(Agent)
    a._active_sessions = {}
    return a


def test_register_and_lookup():
    a = _bare_agent()
    s = Session(id="sid-1")
    a._register_active_session(s)
    assert a._active_sessions["sid-1"] is s


def test_register_overwrites_same_sid():
    a = _bare_agent()
    s1, s2 = Session(id="sid-1"), Session(id="sid-1")
    a._register_active_session(s1)
    a._register_active_session(s2)
    assert a._active_sessions["sid-1"] is s2
    assert len(a._active_sessions) == 1


def test_register_evicts_oldest_beyond_8():
    a = _bare_agent()
    for i in range(10):
        a._register_active_session(Session(id=f"s{i}"))
    assert len(a._active_sessions) == 8
    assert "s0" not in a._active_sessions and "s1" not in a._active_sessions
    assert "s9" in a._active_sessions


def test_register_survives_bad_session():
    a = _bare_agent()
    a._register_active_session(None)  # type: ignore[arg-type]
    a._register_active_session(Session(id="ok"))
    assert len(a._active_sessions) == 1


def test_shallow_copy_shares_registry():
    """ws 端点用 copy.copy(agent) 换 callbacks —— 副本注册必须对原始 agent 可见."""
    orig = _bare_agent()
    a2 = copy.copy(orig)
    s = Session(id="shared-sid")
    a2._register_active_session(s)
    # 同一个 dict 对象（浅拷贝只复制引用，不重绑）
    assert orig._active_sessions is a2._active_sessions
    assert orig._active_sessions.get("shared-sid") is s


def test_stats_prefers_active_over_disk():
    """活跃 session 的实时消息（未落盘）应可被 stats 读取路径看到."""
    a = _bare_agent()
    s = Session(id="live-sid")
    s.messages.append(Message(role=Role.USER, content="hello"))
    a._register_active_session(s)
    # 模拟 server.py 的优先级判断
    active_sessions = getattr(a, "_active_sessions", None) or {}
    got = active_sessions.get("live-sid") if "live-sid" in active_sessions else None
    assert got is s
    assert [getattr(m.role, "value", m.role) for m in got.messages] == ["user"]


def test_concurrent_register_no_loop_starvation():
    """循环内连续注册不应抛错（生成中每步都可能注册/覆盖）."""
    a = _bare_agent()
    a._active_sessions = {}
    asyncio.run(asyncio.sleep(0))
    for i in range(100):
        a._register_active_session(Session(id=f"sid-{i % 20}"))
    assert len(a._active_sessions) == 8
