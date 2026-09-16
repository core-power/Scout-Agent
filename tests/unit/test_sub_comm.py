# -*- coding: utf-8 -*-
"""Subagent comm tools tests (2026-09-07)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from scout.multiagent.broker import SubReport
from scout.multiagent.runtime import get_broker
from scout.tools.builtin.delegate import SharedDataTool, SubReportTool
from scout.tools.registry import ToolRegistry


class FakeTaggedCallbacks:
    def __init__(self, delegation_id, agent_name):
        self.delegation_id = delegation_id
        self.agent_name = agent_name


class FakeAgent:
    def __init__(self, delegation_id, agent_name):
        self.callbacks = FakeTaggedCallbacks(delegation_id, agent_name)


@pytest.fixture
def sub_a(monkeypatch):
    a = FakeAgent("dl_test1", "sub-A")
    monkeypatch.setattr(ToolRegistry, "_main_agent_holder", SimpleNamespace(agent=a), raising=False)
    return a


def run(coro):
    return asyncio.run(coro)


def test_report_publishes_to_broker(sub_a):
    obs = run(SubReportTool().execute(kind="finding", content="found the API format"))
    assert obs.success
    reports = get_broker().drain("dl_test1")
    assert len(reports) == 1
    assert reports[0].sender == "sub-A"
    assert reports[0].kind == "finding"


def test_report_rejected_outside_delegation(monkeypatch):
    a = FakeAgent(None, "main")
    monkeypatch.setattr(ToolRegistry, "_main_agent_holder", SimpleNamespace(agent=a), raising=False)
    obs = run(SubReportTool().execute(kind="progress", content="x"))
    assert not obs.success


def test_shared_data_exchange(sub_a, monkeypatch):
    run(SharedDataTool().execute(action="set", key="urls", value='["a.com","b.com"]'))
    b = FakeAgent("dl_test1", "sub-B")
    monkeypatch.setattr(ToolRegistry, "_main_agent_holder", SimpleNamespace(agent=b), raising=False)
    obs = run(SharedDataTool().execute(action="get", key="urls"))
    assert obs.success and "a.com" in obs.output


def test_digest_contains_reports(sub_a):
    get_broker().publish(SubReport(delegation_id="dl_x", sender="s1", kind="blocker", content="need input"))
    from scout.multiagent.broker import digest_reports

    d = digest_reports(get_broker().drain("dl_x"))
    assert "need input" in d
