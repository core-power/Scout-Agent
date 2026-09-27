"""E4 上下文/记忆工程化测试：跨会话关键记忆抽取 + 上下文组装 + agent 接入.

覆盖（docs/dsh-comparison.md E4，2026-08-27）：
- SessionMemoryExtractor：启发式抽取 / LLM 结构化抽取与降级 / 去重 / 会话标记 / 批量补抽取
- ContextAssembler：记忆块组装与预算截断 / 历史会话摘要 / 空输入兜底
- Agent 接入：可选项注入 / 会话结束自动抽取闭环
"""

from __future__ import annotations

import asyncio
import json

import pytest

from scout.context import (
    ContextAssembler,
    ContextManager,
    ExtractReport,
    MemoryFlush,
    SessionMemoryExtractor,
)
from scout.core.types import Delta, LLMResponse, Message, Role, Session
from scout.engine.agent import Agent
from scout.llm.base import LLMClient
from scout.memory.store import MemoryEntry, MemoryStore


# ── 假件 ──────────────────────────────────────────────────────────────

class FakeLLM(LLMClient):
    """满足 LLMClient 协议的最小假件."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        content = self.replies.pop(0) if self.replies else "ok"
        return LLMResponse(content=content)

    async def stream(self, messages, tools=None, **kwargs):
        yield Delta(text="ok", done=True)


class FakeSessionStore:
    """满足 SessionStore 异步接口的最小假件."""

    def __init__(self, sessions):
        self.sessions = {s["id"]: s for s in sessions}

    async def async_list(self, agent_id=None, limit=50, offset=0):
        rows = list(self.sessions.values())
        rows.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return rows[:limit]

    async def async_get(self, session_id):
        return self.sessions.get(session_id)

    async def async_update_extra(self, session_id, extra):
        if session_id in self.sessions:
            s = self.sessions[session_id]
            s["extra"] = {**(s.get("extra") or {}), **extra}


def _session(sid: str, msgs: list[tuple[Role, str, dict | None]] | None = None) -> Session:
    messages = []
    for role, content, meta in msgs or []:
        messages.append(
            Message(role=role, content=content, metadata=meta or {})
        )
    return Session(id=sid, messages=messages)


# ── 抽取器：启发式 ────────────────────────────────────────────────────

def test_heuristic_extract_kinds():
    s = _session("s1", [
        (Role.USER, "我希望以后都用简体中文回复", None),
        (Role.USER, "这个项目我们决定采用 FastAPI 方案", None),
        (Role.ASSISTANT, "结论：最终选择 httpx 作为 HTTP 客户端。", None),
    ])
    extractor = SessionMemoryExtractor()
    items = extractor._heuristic_extract(s)
    kinds = {i.kind for i in items}
    assert "preference" in kinds
    assert "decision" in kinds
    assert "conclusion" in kinds
    for it in items:
        assert it.importance >= 0.5


def test_heuristic_skips_compression_and_short_lines():
    s = _session("s1", [
        (Role.USER, "好的", None),  # 过短 → 跳过
        (Role.USER, "hi", None),
        (Role.USER, "这是历史会话摘要：blablabla 内容", {"type": "compression"}),
        (Role.ASSISTANT, "好的，我来帮你处理。", None),  # assistant 普通语句 → 跳过
    ])
    extractor = SessionMemoryExtractor()
    items = extractor._heuristic_extract(s)
    assert items == []


def test_heuristic_strips_runtime_tags():
    s = _session("s1", [
        (Role.USER, "以后都用 Python 写脚本。<runtime_context><current_time>2026-01-01</current_time></runtime_context>", None),
    ])
    extractor = SessionMemoryExtractor()
    items = extractor._heuristic_extract(s)
    assert items
    assert "<runtime_context>" not in items[0].content


# ── 抽取器：写入与去重 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_writes_to_store(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    s = _session("s1", [
        (Role.USER, "我希望以后都用中文回复", None),
    ])
    extractor = SessionMemoryExtractor(memory_store=store)
    report = await extractor.extract(s)
    assert isinstance(report, ExtractReport)
    assert len(report.added) == 1
    assert report.added[0].kind == "preference"
    hits = store.search("中文", limit=5)
    assert any(h.content == "我希望以后都用中文回复" for h in hits)


@pytest.mark.asyncio
async def test_extract_dedup(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    store.add("我希望以后都用中文回复", category="preference", importance=0.9)
    s = _session("s1", [
        (Role.USER, "我希望以后都用中文回复", None),
    ])
    extractor = SessionMemoryExtractor(memory_store=store)
    report = await extractor.extract(s)
    assert report.added == []
    assert len(report.skipped_duplicates) == 1


@pytest.mark.asyncio
async def test_extract_skips_low_importance(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    s = _session("s1", [
        (Role.USER, "帮我看看这个文件", None),  # fact，importance=0.6 < min 0.7
    ])
    extractor = SessionMemoryExtractor(memory_store=store, min_importance=0.7)
    report = await extractor.extract(s)
    assert report.added == []
    assert len(report.skipped_low_importance) == 1


@pytest.mark.asyncio
async def test_extract_empty_session(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    extractor = SessionMemoryExtractor(memory_store=store)
    report = await extractor.extract(_session("s0"))
    assert report.added == []
    assert report.summary().startswith("session=s0")


# ── 抽取器：LLM 结构化抽取与降级 ──────────────────────────────────────

def _llm_json_reply(items):
    return json.dumps({"memories": items}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_extract_llm_json(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    llm = FakeLLM(replies=[
        _llm_json_reply([
            {"content": "用户偏好中文", "kind": "preference", "importance": 0.9},
            {"content": "采用 FastAPI", "kind": "decision", "importance": 0.7},
        ])
    ])
    s = _session("s1", [(Role.USER, "我希望以后用中文", None)])
    extractor = SessionMemoryExtractor(memory_store=store, llm=llm)
    report = await extractor.extract(s)
    assert report.used_llm is True
    assert {i.kind for i in report.added} == {"preference", "decision"}
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_extract_llm_json_with_fence(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    raw = "```json\n" + _llm_json_reply([
        {"content": "记住用 httpx", "kind": "skill", "importance": 0.8},
    ]) + "\n```"
    llm = FakeLLM(replies=[raw])
    s = _session("s1", [(Role.USER, "以后都用 httpx", None)])
    extractor = SessionMemoryExtractor(memory_store=store, llm=llm)
    report = await extractor.extract(s)
    assert report.added and report.added[0].kind == "skill"


@pytest.mark.asyncio
async def test_extract_llm_fallback_on_bad_json(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    llm = FakeLLM(replies=["这不是 JSON"])
    s = _session("s1", [(Role.USER, "我希望以后都用中文回复", None)])
    extractor = SessionMemoryExtractor(memory_store=store, llm=llm)
    report = await extractor.extract(s)
    assert report.used_llm is False  # 降级启发式
    assert any(i.kind == "preference" for i in report.added)


# ── 抽取器：批量补抽取 ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_pending_sessions(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    sstore = FakeSessionStore([
        {"id": "done-1", "status": "done", "extra": {"title": "T1"}},
        {"id": "done-2", "status": "done",
         "extra": {"title": "T2", "memory_extracted_at": "2026-08-01T00:00:00"}},
        {"id": "active", "status": "idle", "extra": {}},
    ])
    extractor = SessionMemoryExtractor(memory_store=store)
    reports = await extractor.extract_pending_sessions(sstore, limit=10)
    assert len(reports) == 1  # 只有 done-1 未标记
    assert reports[0].session_id == "done-1"
    # 已标记：再次扫描不再抽取
    reports2 = await extractor.extract_pending_sessions(sstore, limit=10)
    assert reports2 == []


# ── 组装器 ────────────────────────────────────────────────────────────

class FakeMemoryStore:
    """仅暴露 search_async 的组装器假件."""

    def __init__(self, entries):
        self.entries = entries

    async def search_async(self, query, limit=10):
        return self.entries[:limit]


def _mem_entry(content, category="fact", importance=0.6):
    return MemoryEntry(id=None, content=content, category=category, importance=importance)


@pytest.mark.asyncio
async def test_build_memory_context():
    store = FakeMemoryStore([
        _mem_entry("用户偏好中文回复", "preference", 0.9),
        _mem_entry("项目采用 FastAPI", "decision", 0.7),
    ])
    assembler = ContextAssembler(memory_store=store)
    text = await assembler.build_memory_context("中文回复")
    assert "- [preference] 用户偏好中文回复" in text
    assert "- [decision] 项目采用 FastAPI" in text


@pytest.mark.asyncio
async def test_memory_context_budget():
    store = FakeMemoryStore([_mem_entry("A" * 500, "fact", 0.6)] * 3)
    assembler = ContextAssembler(memory_store=store, max_memory_chars=200)
    text = await assembler.build_memory_context("q")
    assert len(text) <= 200


@pytest.mark.asyncio
async def test_memory_context_ranks_by_importance():
    store = FakeMemoryStore([
        _mem_entry("低重要度记忆", "fact", 0.5),
        _mem_entry("高重要度记忆", "preference", 0.95),
    ])
    assembler = ContextAssembler(memory_store=store, memory_limit=1)
    text = await assembler.build_memory_context("q")
    assert "高重要度记忆" in text
    # ★ 2026-09-16：行为有意变更。原断言基于"全局 top-N 截断"语义（低重要度
    # 必然被挤出）。现改为**分层配额 + 事实层保底** —— 长期事实（居住地/忌口/
    # 约束）对回答正确性的影响远大于一条相关技能，故低重要度事实也可能保留。
    # 实测动机：全局截断下 preference/fact 被长条目挤掉，导致推荐了"川湘菜"
    # 给不吃辣的用户。
    assert "低重要度记忆" in text, "事实层保底应保留低重要度但属于 facts 层的记忆"


@pytest.mark.asyncio
async def test_build_session_summary():
    sstore = FakeSessionStore([
        {"id": "s1", "status": "done", "extra": {"title": "爬虫优化", "summary": "已选定 httpx 方案"}},
        {"id": "s2", "status": "idle", "extra": {}},
    ])
    assembler = ContextAssembler(session_store=sstore)
    text = await assembler.build_session_summary(exclude_session_id="s2")
    assert "爬虫优化" in text
    assert "httpx" in text
    assert "- 会话《爬虫优化》" in text


@pytest.mark.asyncio
async def test_assembler_empty_without_stores():
    assembler = ContextAssembler()
    mem, summ = await assembler.assemble("查询", exclude_session_id="x")
    assert mem == ""
    assert summ == ""


# ── Agent 接入 ────────────────────────────────────────────────────────

def _make_mini_agent(**overrides):
    defaults = dict(
        llm=FakeLLM(),
        enable_persistence=False,
        enable_memory=False,
        enable_security=False,
        enable_context=False,
        enable_skills=False,
        enable_bus=False,
        enable_self_heal=False,
        enable_hitl=False,
        enable_goal_manager=False,
        enable_reflexion=False,
        enable_observability=False,
        enable_workspace=False,
        max_turns=3,
    )
    defaults.update(overrides)
    return Agent(**defaults)


def test_agent_accepts_e4_components(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    extractor = SessionMemoryExtractor(memory_store=store)
    assembler = ContextAssembler(memory_store=store)
    agent = _make_mini_agent(memory_extractor=extractor, context_assembler=assembler)
    assert agent.memory_extractor is extractor
    assert agent.context_assembler is assembler


@pytest.mark.asyncio
async def test_agent_extracts_memory_on_conversation_end(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    extractor = SessionMemoryExtractor(memory_store=store)
    agent = _make_mini_agent(memory_extractor=extractor)
    res = await agent.run_conversation("我希望以后都用中文回复，请记住。")
    assert res["response"]
    # ★ 2026-09-16：记忆抽取已改为**后台任务**（不再阻塞用户响应 —— 实测让
    # 用户每次对话白等 10~25s 的一次 LLM 往返）。因此测试需要先等后台任务
    # 跑完再断言；原实现是同步 await，所以旧测试无需等待。
    _bg = getattr(agent, "_bg_tasks", None)
    if _bg:
        await asyncio.gather(*list(_bg), return_exceptions=True)
    hits = store.search("中文", limit=5)
    assert any("中文" in h.content for h in hits)


@pytest.mark.asyncio
async def test_agent_extract_failure_does_not_break(tmp_path):
    class BrokenExtractor:
        async def extract(self, session):
            raise RuntimeError("boom")

    agent = _make_mini_agent(memory_extractor=BrokenExtractor())
    res = await agent.run_conversation("你好")
    assert res["response"]  # 主流程不受抽取失败影响


# ── MemoryFlush 闭环（压缩前 flush）──────────────────────────────────

@pytest.mark.asyncio
async def test_memory_flush_extracts_and_writes(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    flush = MemoryFlush(memory_store=store)
    s = _session("s1", [
        (Role.USER, "我希望以后都用中文回复", None),
        (Role.ASSISTANT, "结论：方案定为 FastAPI。", None),
    ])
    text = await flush.flush(s)
    assert "[preference]" in text
    assert "[conclusion]" in text
    hits = store.search("中文", limit=5)
    assert any("中文回复" in h.content for h in hits)


@pytest.mark.asyncio
async def test_memory_flush_dedup(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    store.add("我希望以后都用中文回复", category="preference", importance=0.9)
    flush = MemoryFlush(memory_store=store)
    s = _session("s1", [(Role.USER, "我希望以后都用中文回复", None)])
    text = await flush.flush(s)
    assert text == ""  # 全部命中去重 → 无可写内容
    hits = store.search("中文", limit=10)
    assert len(hits) == 1  # 未新增重复


@pytest.mark.asyncio
async def test_compress_flushes_before_replace(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    flush = MemoryFlush(memory_store=store)
    cm = ContextManager(max_messages=6, compress_threshold=6, keep_recent=2)
    msgs = []
    for i in range(6):
        msgs.append(Message(role=Role.USER, content=f"我希望第 {i} 次都记住偏好", metadata={}))
        msgs.append(Message(role=Role.ASSISTANT, content=f"好的，第 {i} 次。", metadata={}))
    s = Session(id="s1", messages=msgs)
    info = await cm.compress(s, llm=None, memory_flush=flush)
    assert info["compressed"] is True
    assert info["flushed"] is True  # 压缩前已抽取
    hits = store.search("记住偏好", limit=10)
    assert len(hits) >= 1


def test_agent_auto_wraps_memory_flush(tmp_path):
    store = MemoryStore(db_path=tmp_path / "m.db")
    extractor = SessionMemoryExtractor(memory_store=store)
    agent = _make_mini_agent(memory_extractor=extractor)
    assert agent.memory_flush is not None  # 未显式注入也自动包装
    assert agent.memory_flush.extractor is extractor


# ── 前缀静态化契约（2026-09-27）：PromptBuilder 动静态分离 ────────────


class _FakeMemory:
    """最小记忆桩：search 返回固定条目，供注入断言用."""

    def __init__(self, content: str):
        self._content = content

    def search(self, _q: str, limit: int = 3):
        return [MemoryEntry(id=1, content=self._content, importance=0.5)]


def test_prompt_builder_system_prompt_is_static():
    """system prompt 必须跨轮逐字节一致，且不含任何动态内容."""
    from scout.context.prompt import PromptBuilder

    pb = PromptBuilder(system_prompt="你是 Scout。", memory_store=_FakeMemory("用户喜欢中文回复"))
    import time as _time

    p1 = pb.build(user_input="第一轮", current_step=0, max_steps=30)
    _time.sleep(1.1)  # 跨过一秒，旧实现的时间戳会变
    p2 = pb.build(user_input="第二轮内容完全不同", current_step=15, max_steps=30)

    assert p1 == p2, "system prompt 含动态内容，前缀缓存会每轮击穿"
    assert "当前时间" not in p1
    assert "用户喜欢中文回复" not in p1  # 记忆属于动态层


def test_prompt_builder_dynamic_content_goes_to_runtime_context():
    """时间戳/记忆/预算警告必须出现在 runtime_context（user 消息尾部），而非 system prompt."""
    from scout.context.prompt import PromptBuilder

    pb = PromptBuilder(
        system_prompt="你是 Scout。",
        memory_store=_FakeMemory("用户喜欢中文回复"),
        budget_warning_threshold=25,
    )
    rt = pb.build_runtime_context(user_input="帮我看看", current_step=10, max_steps=30)

    assert "当前时间" in rt
    assert "用户喜欢中文回复" in rt
    assert "剩余迭代次数" in rt
    # 无预算警告时不出现该行
    rt_far = pb.build_runtime_context(user_input="帮我看看", current_step=0, max_steps=1000)
    assert "剩余迭代次数" not in rt_far


# ── 前缀静态化契约（2026-09-27）：PromptBuilder 动静态分离 ────────────


class _FakeMemory:
    """最小记忆桩：search 返回固定条目，供注入断言用."""

    def __init__(self, content: str):
        self._content = content

    def search(self, _q: str, limit: int = 3):
        return [MemoryEntry(id=1, content=self._content, importance=0.5)]


def test_prompt_builder_system_prompt_is_static():
    """system prompt 必须跨轮逐字节一致，且不含任何动态内容."""
    from scout.context.prompt import PromptBuilder

    pb = PromptBuilder(system_prompt="你是 Scout。", memory_store=_FakeMemory("用户喜欢中文回复"))
    import time as _time

    p1 = pb.build(user_input="第一轮", current_step=0, max_steps=30)
    _time.sleep(1.1)  # 跨过一秒，旧实现的时间戳会变
    p2 = pb.build(user_input="第二轮内容完全不同", current_step=15, max_steps=30)

    assert p1 == p2, "system prompt 含动态内容，前缀缓存会每轮击穿"
    assert "当前时间" not in p1
    assert "用户喜欢中文回复" not in p1  # 记忆属于动态层


def test_prompt_builder_dynamic_content_goes_to_runtime_context():
    """时间戳/记忆/预算警告必须出现在 runtime_context（user 消息尾部），而非 system prompt."""
    from scout.context.prompt import PromptBuilder

    pb = PromptBuilder(
        system_prompt="你是 Scout。",
        memory_store=_FakeMemory("用户喜欢中文回复"),
        budget_warning_threshold=25,
    )
    rt = pb.build_runtime_context(user_input="帮我看看", current_step=10, max_steps=30)

    assert "当前时间" in rt
    assert "用户喜欢中文回复" in rt
    assert "剩余迭代次数" in rt
    # 无预算警告时不出现该行
    rt_far = pb.build_runtime_context(user_input="帮我看看", current_step=0, max_steps=1000)
    assert "剩余迭代次数" not in rt_far
