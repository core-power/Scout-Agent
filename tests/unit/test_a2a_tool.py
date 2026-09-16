# -*- coding: utf-8 -*-
"""A2A 工具单元测试（mock 远程端，不触网）."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from scout.tools.builtin.a2a import A2ATool
from scout.tools.registry import ToolRegistry


class FakeClient:
    def __init__(self, answer="done", fail=False, hang=False):
        self.answer = answer
        self.fail = fail
        self.hang = hang
        self.received = None
        self.url = "http://example.com"

    async def send_task(self, message: str, task_id=None):
        self.received = message
        if self.hang:
            await asyncio.sleep(999)
        return SimpleNamespace(
            id=task_id or "tid-1",
            messages=[
                SimpleNamespace(role="user", parts=[SimpleNamespace(text=message)]),
                SimpleNamespace(role="agent", parts=[SimpleNamespace(text=self.answer)]),
            ],
            status=SimpleNamespace(
                state="failed" if self.fail else "completed",
                message="boom" if self.fail else None,
            ),
        )

    async def get_task(self, task_id: str):
        return SimpleNamespace(id=task_id, status=SimpleNamespace(state="working"))


class FakeManager:
    def __init__(self):
        self.clients = {}

    def list_agents(self):
        return [{"name": n, "url": getattr(c, "url", "?")} for n, c in self.clients.items()]

    def add_agent(self, name, url):
        c = FakeClient()
        c.url = url
        self.clients[name] = c

    def remove_agent(self, name):
        return self.clients.pop(name, None) is not None


@pytest.fixture
def tm(monkeypatch):
    tool = A2ATool()
    mgr = FakeManager()
    monkeypatch.setattr(ToolRegistry, "_main_agent", SimpleNamespace(a2a_manager=mgr), raising=False)
    return tool, mgr


def run(coro):
    return asyncio.run(coro)


def test_registered_in_registry():
    ToolRegistry.discover()
    assert ToolRegistry.get_tool("a2a") is not None


def test_list_empty_and_add(tm):
    tool, mgr = tm
    assert "尚未注册" in run(tool.execute(action="list_agents")).output
    obs = run(tool.execute(action="add_agent", name="b", url="http://example.com:8848"))
    assert obs.success and "b" in mgr.clients


def test_add_agent_requires_args(tm):
    tool, _ = tm
    assert not run(tool.execute(action="add_agent", name="only-name")).success


def test_send_task_returns_answer(tm):
    tool, mgr = tm
    mgr.add_agent("b", "http://example.com")
    obs = run(tool.execute(action="send_task", name="b", message="do it", timeout=5))
    assert obs.success and "done" in obs.output
    assert mgr.clients["b"].received == "do it"


def test_send_task_unknown_agent(tm):
    tool, _ = tm
    obs = run(tool.execute(action="send_task", name="ghost", message="x"))
    assert not obs.success and "未注册" in obs.output


def test_send_task_remote_failure(tm):
    tool, mgr = tm
    mgr.add_agent("bad", "http://example.com")
    mgr.clients["bad"].fail = True
    obs = run(tool.execute(action="send_task", name="bad", message="x", timeout=5))
    assert not obs.success and "失败" in obs.output


def test_send_task_timeout(tm):
    tool, mgr = tm
    mgr.add_agent("slow", "http://example.com")
    mgr.clients["slow"].hang = True
    obs = run(tool.execute(action="send_task", name="slow", message="x", timeout=0.3))
    assert not obs.success
