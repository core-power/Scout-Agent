"""用户反馈存储（scout.memory.feedback）单元测试.

覆盖：落盘/校验、列表过滤与排序、汇总统计、点踩→记忆的学习旁路。
所有用例把 DATA_DIR 指向 tmp_path，避免污染真实数据目录。
"""

from __future__ import annotations

import json

import pytest

from scout.memory import feedback as fb


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """把反馈落盘目录隔离到 tmp_path."""
    monkeypatch.setattr("scout.config.paths.DATA_DIR", tmp_path)
    yield tmp_path


class _FakeMemoryStore:
    def __init__(self, raise_on_add: bool = False):
        self.added: list[dict] = []
        self._raise = raise_on_add
        self._next_id = 100

    def add(self, content, category="general", importance=0.5, source_session="", **kw):
        if self._raise:
            raise RuntimeError("boom")
        self.added.append(
            {"content": content, "category": category, "importance": importance,
             "source_session": source_session}
        )
        self._next_id += 1
        return self._next_id


# ── record_feedback ────────────────────────────────────────────────


def test_record_up_and_down_persist(_isolate_data_dir):
    r1 = fb.record_feedback(rating="up", session_id="s1", message_id="m1")
    r2 = fb.record_feedback(rating="down", session_id="s1", reason="答非所问")
    assert r1["rating"] == "up" and r1["id"] and r1["ts"] > 0
    assert r2["rating"] == "down"
    path = _isolate_data_dir / "feedback.jsonl"
    assert path.exists()
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 2
    assert lines[0]["id"] == r1["id"]


def test_record_invalid_rating_raises():
    with pytest.raises(ValueError):
        fb.record_feedback(rating="sideways")
    with pytest.raises(ValueError):
        fb.record_feedback(rating="")


def test_record_truncates_long_fields():
    rec = fb.record_feedback(rating="down", comment="x" * 5000, answer="y" * 9000)
    assert len(rec["comment"]) <= 2000
    assert len(rec["answer"]) <= 4000


def test_record_normalizes_rating_case():
    rec = fb.record_feedback(rating="UP")
    assert rec["rating"] == "up"


# ── list_feedback ──────────────────────────────────────────────────


def test_list_order_newest_first_and_limit():
    for i in range(5):
        fb.record_feedback(rating="up", message_id=f"m{i}")
    out = fb.list_feedback(limit=3)
    assert len(out) == 3
    # 最新在前
    assert out[0]["message_id"] == "m4"
    assert out[-1]["message_id"] == "m2"


def test_list_filter_by_rating():
    fb.record_feedback(rating="up", message_id="u")
    fb.record_feedback(rating="down", message_id="d", reason="太慢")
    downs = fb.list_feedback(rating="down")
    assert len(downs) == 1 and downs[0]["message_id"] == "d"


def test_list_empty_when_no_file():
    assert fb.list_feedback() == []


def test_list_skips_corrupt_lines(_isolate_data_dir):
    fb.record_feedback(rating="up", message_id="ok")
    path = _isolate_data_dir / "feedback.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write("{not valid json}\n")
    fb.record_feedback(rating="down", message_id="ok2")
    out = fb.list_feedback()
    ids = {r["message_id"] for r in out}
    assert ids == {"ok", "ok2"}  # 损坏行被跳过


# ── stats ──────────────────────────────────────────────────────────


def test_stats_counts_and_reasons():
    fb.record_feedback(rating="up")
    fb.record_feedback(rating="up")
    fb.record_feedback(rating="down", reason="答非所问")
    fb.record_feedback(rating="down", reason="太慢")
    fb.record_feedback(rating="down", reason="太慢")
    s = fb.stats()
    assert s["up"] == 2 and s["down"] == 3 and s["total"] == 5
    assert s["satisfaction"] == round(2 / 5, 4)
    assert s["down_reasons"]["太慢"] == 2
    assert s["down_reasons"]["答非所问"] == 1


def test_stats_empty():
    s = fb.stats()
    assert s == {"up": 0, "down": 0, "total": 0, "satisfaction": None, "down_reasons": {}}


def test_stats_down_without_reason_bucketed_as_other():
    fb.record_feedback(rating="down")
    assert fb.stats()["down_reasons"].get("其他") == 1


# ── learn_from_negative ────────────────────────────────────────────


def test_learn_writes_memory_on_down():
    store = _FakeMemoryStore()
    rec = fb.record_feedback(rating="down", session_id="s9", reason="工具用错", comment="该用A却用了B", question="帮我做X")
    mid = fb.learn_from_negative(rec, store)
    assert mid > 0
    assert len(store.added) == 1
    a = store.added[0]
    assert a["category"] == "feedback"
    assert a["importance"] == 0.35
    assert a["source_session"] == "s9"
    assert "工具用错" in a["content"] and "该用A却用了B" in a["content"] and "帮我做X" in a["content"]


def test_learn_skips_on_up():
    store = _FakeMemoryStore()
    rec = fb.record_feedback(rating="up")
    assert fb.learn_from_negative(rec, store) == -1
    assert store.added == []


def test_learn_none_store_safe():
    rec = fb.record_feedback(rating="down", reason="x")
    assert fb.learn_from_negative(rec, None) == -1


def test_learn_store_error_safe():
    store = _FakeMemoryStore(raise_on_add=True)
    rec = fb.record_feedback(rating="down", reason="x")
    # 记忆写入抛错时必须被吞掉并返回 -1（旁路不得影响主链路）
    assert fb.learn_from_negative(rec, store) == -1


# ── HTTP 路由（/api/feedback）──────────────────────────────────────


def _make_client(agent=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from scout.adapters.web.routes.feedback import FeedbackRoutes

    class _Host(FeedbackRoutes):
        def __init__(self, ag):
            self.app = FastAPI()
            self._agent = ag
            self._setup_feedback_routes()

    host = _Host(agent)
    return TestClient(host.app)


def test_route_post_up(_isolate_data_dir):
    client = _make_client()
    r = client.post("/api/feedback", json={"rating": "up", "session_id": "s1", "message_id": "m1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["id"]
    # 已落盘
    assert len(fb.list_feedback()) == 1


def test_route_post_down_wires_memory(_isolate_data_dir):
    """点踩经路由应触发 learn_from_negative → 写入 memory_store."""
    store = _FakeMemoryStore()
    agent = type("A", (), {"memory_store": store})()
    client = _make_client(agent=agent)
    r = client.post(
        "/api/feedback",
        json={"rating": "down", "reason": "未完成", "comment": "少了一半", "session_id": "s2"},
    )
    assert r.status_code == 200
    assert len(store.added) == 1
    assert store.added[0]["category"] == "feedback"
    assert "未完成" in store.added[0]["content"]


def test_route_post_invalid_rating_400(_isolate_data_dir):
    client = _make_client()
    r = client.post("/api/feedback", json={"rating": "meh"})
    assert r.status_code == 400


def test_route_down_without_agent_is_safe(_isolate_data_dir):
    """无 agent（memory_store 缺失）时点踩仍应成功落盘，不报错."""
    client = _make_client(agent=None)
    r = client.post("/api/feedback", json={"rating": "down", "reason": "太慢"})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_route_get_feedback_and_stats(_isolate_data_dir):
    client = _make_client()
    client.post("/api/feedback", json={"rating": "up"})
    client.post("/api/feedback", json={"rating": "down", "reason": "答非所问"})
    r = client.get("/api/feedback?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["up"] == 1 and body["stats"]["down"] == 1
    assert len(body["feedback"]) == 2
    # 过滤
    r2 = client.get("/api/feedback?rating=down")
    assert all(x["rating"] == "down" for x in r2.json()["feedback"])

