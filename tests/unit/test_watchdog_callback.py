"""空转看门狗用户征询（on_watchdog）回调链路单元测试.

覆盖：NullCallbacks 默认继续、TaggedCallbacks 转发、WebCallbacks 的
future 注册/事件推送/继续与停止解析/取消清理。
"""

from __future__ import annotations

import asyncio

import pytest

from scout.adapters.web.callbacks import WebCallbacks
from scout.core.callbacks import NullCallbacks, TaggedCallbacks


async def test_null_callbacks_default_continue():
    """无 UI 通道 → 默认继续（保持旧的自动行为）."""
    assert await NullCallbacks().on_watchdog("空转了") is True


class _Inner:
    def __init__(self, ret):
        self._ret = ret
        self.calls = []

    async def on_watchdog(self, warning, meta=None):
        self.calls.append((warning, meta))
        return self._ret


async def test_tagged_callbacks_forwards_and_tags():
    inner = _Inner(False)
    tagged = TaggedCallbacks(inner, agent_role="main", agent_name="主代理")
    result = await tagged.on_watchdog("warn", {"steps": 2})
    assert result is False
    assert inner.calls and inner.calls[0][0] == "warn"
    # TaggedCallbacks 会注入 agent_role/agent_name
    assert inner.calls[0][1]["agent_role"] == "main"


async def test_tagged_callbacks_no_inner_defaults_true():
    class _NoWatchdog:
        pass
    tagged = TaggedCallbacks(_NoWatchdog())
    assert await tagged.on_watchdog("x") is True


async def _wait_registered(cb, tries=100):
    for _ in range(tries):
        if getattr(cb, "pending_watchdogs", None):
            return next(iter(cb.pending_watchdogs))
        await asyncio.sleep(0.02)
    raise AssertionError("on_watchdog 未注册 future")


async def test_web_on_watchdog_pushes_event_and_continues():
    cb = WebCallbacks()  # 无 ws → 事件进 events 队列
    task = asyncio.create_task(cb.on_watchdog("可能在空转", {"steps": 5, "tool_calls": 9, "ok": 0}))
    rid = await _wait_registered(cb)
    # 推送了 watchdog_request 事件
    ev = await asyncio.wait_for(cb.events.get(), timeout=2)
    assert ev["type"] == "watchdog_request"
    assert ev["data"]["request_id"] == rid
    assert ev["data"]["warning"] == "可能在空转"
    assert ev["data"]["meta"]["steps"] == 5
    assert ev["data"]["timeout"] >= 10
    # 用户点「继续」→ future True
    cb.pending_watchdogs[rid].set_result(True)
    assert await asyncio.wait_for(task, timeout=2) is True
    assert rid not in cb.pending_watchdogs  # 已清理


async def test_web_on_watchdog_stop_returns_false():
    cb = WebCallbacks()
    task = asyncio.create_task(cb.on_watchdog("空转", {"steps": 8}))
    rid = await _wait_registered(cb)
    await asyncio.wait_for(cb.events.get(), timeout=2)  # 消费 request 事件
    cb.pending_watchdogs[rid].set_result(False)  # 用户点「停止」
    assert await asyncio.wait_for(task, timeout=2) is False
    assert rid not in cb.pending_watchdogs


async def test_web_on_watchdog_cancel_cleans_up():
    cb = WebCallbacks()
    task = asyncio.create_task(cb.on_watchdog("空转", {}))
    rid = await _wait_registered(cb)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 取消后注册表须清理，避免长会话泄漏
    assert rid not in getattr(cb, "pending_watchdogs", {})


async def test_web_on_watchdog_syncs_adapter_table():
    """注册时同步写入 adapter 兼容表（SSE 等路径消费）."""
    cb = WebCallbacks()

    class _Adapter:
        def __init__(self):
            self._pending_watchdogs = {}

    cb._adapter = _Adapter()
    task = asyncio.create_task(cb.on_watchdog("空转", {}))
    rid = await _wait_registered(cb)
    assert rid in cb._adapter._pending_watchdogs
    cb.pending_watchdogs[rid].set_result(True)
    await asyncio.wait_for(task, timeout=2)
    assert rid not in cb._adapter._pending_watchdogs  # 两处都清理
