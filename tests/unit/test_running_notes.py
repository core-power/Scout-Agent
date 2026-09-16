# -*- coding: utf-8 -*-
"""Running Notes + 预算告警注入的单元测试（2026-09-07）."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scout.context.manager import ContextManager
from scout.core.types import Message, Role


def make_session(n_tool: int = 0) -> SimpleNamespace:
    msgs = [Message(role=Role.SYSTEM, content="sys")]
    for i in range(n_tool):
        msgs.append(Message(role=Role.ASSISTANT, content=f"call {i}"))
        msgs.append(
            Message(
                role=Role.TOOL,
                content=f"output {i} " + "x" * 60,
                metadata={"tool_name": f"tool_{i}"},
            )
        )
    return SimpleNamespace(id="s1", messages=msgs, lineage_id="")


@pytest.fixture
def cm() -> ContextManager:
    return ContextManager(max_tokens=1000)


def tool_msg(text: str, name: str = "t") -> Message:
    return Message(role=Role.TOOL, content=text, metadata={"tool_name": name})


@pytest.mark.unit
def test_notes_created_on_prune(cm):
    s = make_session()
    removed = [
        tool_msg("https://a.com 标题AA 正文略", "web_fetch"),
        Message(role=Role.ASSISTANT, content="call"),
    ]
    assert cm.update_running_notes(s, removed) is True
    note = s.messages[-1]
    assert note.metadata["type"] == "running_notes"
    assert "[web_fetch]" in note.content
    assert "https://a.com" in note.content
    assert "call" not in note.content


@pytest.mark.unit
def test_notes_merge_and_position(cm):
    s = make_session()
    cm.update_running_notes(s, [tool_msg("alpha", "t1")])
    s.messages.append(Message(role=Role.USER, content="new user msg"))
    cm.update_running_notes(s, [tool_msg("beta", "t2")])
    note = s.messages[-1]
    assert note.metadata["type"] == "running_notes"
    assert "alpha" in note.content and "beta" in note.content
    before = note.content
    cm.update_running_notes(s, [tool_msg("beta", "t2")])
    assert s.messages[-1].content == before


@pytest.mark.unit
def test_notes_cap_drops_oldest(cm):
    cm._NOTES_MAX_ITEMS = 5
    s = make_session()
    for i in range(8):
        cm.update_running_notes(s, [tool_msg(f"item {i}", f"t{i}")])
    note = s.messages[-1].content
    assert "item 0" not in note and "item 1" not in note
    assert "item 7" in note


@pytest.mark.unit
def test_notes_not_created_when_nothing_removed(cm):
    s = make_session()
    assert cm.update_running_notes(s, []) is False
    assert all(m.metadata.get("type") != "running_notes" for m in s.messages)


@pytest.mark.unit
def test_no_budget_notice_symbol():
    """预算告警已按需求移除（2026-09-07）：确认不会回归引入."""
    assert not hasattr(cm, "ensure_budget_notice")
