"""新增能力单测（2026-08-27）：工具契约 / 测试反馈 / DAG 循环 / 持久会话 / SPI."""

from __future__ import annotations

import asyncio

import pytest

from scout.core.types import Observation, ToolCall
from scout.engine.test_feedback import build_test_feedback, extract_pytest_failures
from scout.plugins.spi import register_provider, unregister_provider
from scout.tools.base import ToolDefinition


# ── 工具契约 ──────────────────────────────────────────────

class _SimpleTool(ToolDefinition):
    name = "simple_tool"

    async def execute(self, path: str, count: int = 3, **kwargs):
        return Observation(tool_name=self.name, success=True, output=f"{path}:{count}")


def test_contract_schema_derivation():
    """无手写 parameters 的工具应从 execute 签名推导 schema."""
    tool = _SimpleTool()
    schema = tool.ensure_schema()
    assert schema["type"] == "object"
    assert "path" in schema["properties"]
    assert schema["required"] == ["path"]
    assert schema["properties"]["count"].get("default") == 3


def test_contract_validate_args_missing_required():
    tool = _SimpleTool()
    cleaned, err = tool.validate_args({})
    assert err and "path" in err


def test_contract_validate_args_coercion():
    tool = _SimpleTool()
    cleaned, err = tool.validate_args({"path": "/tmp/x", "count": "5"})
    assert err == ""
    assert cleaned["count"] == 5  # 字符串数字被纠正为 int


def test_registry_error_codes():
    """注册表兜底错误码：未知工具 / 内部异常."""
    from scout.tools.registry import ToolRegistry

    async def run():
        obs = await ToolRegistry.execute(ToolCall(name="no_such_tool_xyz", arguments={}))
        return obs

    obs = asyncio.run(run())
    assert obs.error_code == "UNKNOWN_TOOL"


# ── 测试反馈闭环 ─────────────────────────────────────────

PYTEST_SAMPLE = """
============================= test session starts =============================
tests/test_demo.py:3: in test_add
    assert add(1, 2) == 4
E   assert 3 == 4
_______________________________ test_other ________________________________
tests/test_demo.py:9: in test_other
    return x / 0
E   ZeroDivisionError: division by zero
1 failed, 1 passed in 0.50s
"""


def test_extract_pytest_failures():
    failures = extract_pytest_failures(PYTEST_SAMPLE)
    assert len(failures) >= 1
    f = failures[0]
    assert "test_other" in f.test_id or "test_add" in f.test_id
    assert "test_demo.py" in f.file
    assert f.error  # 有断言/异常信息


def test_build_test_feedback_empty_when_passed():
    from scout.engine.test_feedback import TestRunResult

    r = TestRunResult(passed=True, failures=[])
    assert build_test_feedback(r) == ""


# ── DAG 循环 ─────────────────────────────────────────────

class _MockCallbacks:
    async def on_status(self, s): pass
    async def on_tool_progress(self, *a, **k): pass


class _StubAgent:
    max_turns = 30
    callbacks = _MockCallbacks()

    def __init__(self, responses):
        self._responses = iter(responses)
        self.llm = type("LLM", (), {"complete": self._complete})()
        self._cnt = 0

    async def _complete(self, messages=None, **kw):
        content = next(self._responses)
        return type("R", (), {"content": content})()

    async def _run_react(self, msg, session, att=None):
        self._cnt += 1
        return {"response": f"step{self._cnt}", "session": session, "steps": 1}


def test_dag_loop_topological_execution():
    from scout.core.types import Session
    from scout.engine.loops import DAGLoop

    agent = _StubAgent([
        '[{"id":"a","description":"研究","depends_on":[]},'
        '{"id":"b","description":"总结","depends_on":["a"]}]',
        "汇总结果",
    ])
    loop = DAGLoop(agent)
    assert DAGLoop._topo_order(
        [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}]
    ) == ["a", "b"]

    async def run():
        return await loop.run("目标", Session(id="s1"))

    res = asyncio.run(run())
    assert res["response"] == "汇总结果"
    assert agent._cnt == 2  # 两个子会话


# ── 持久 shell 会话 ──────────────────────────────────────

@pytest.mark.asyncio
async def test_persistent_shell_session_state():
    from scout.tools.builtin.shell.session import ShellSession

    s = ShellSession("/tmp")
    await s.start()
    try:
        out, code = await s.run("cd /tmp && pwd")
        assert code == 0
        out, code = await s.run("export SCOUT_TEST=1; echo ok")
        assert "ok" in out
        out, code = await s.run("echo $SCOUT_TEST")
        assert "1" in out  # 环境变量跨调用保留
    finally:
        await s.close()


# ── 插件 SPI ─────────────────────────────────────────────

def test_spi_registry_and_llm_factory():
    from scout.llm.base import LLMClientFactory
    from scout.plugins.spi import SPI_KIND_LLM, get_provider

    class FakeLLM:
        def __init__(self, **kw):
            self.kw = kw

    try:
        register_provider(SPI_KIND_LLM, FakeLLM, source="test")
        assert get_provider(SPI_KIND_LLM) is FakeLLM

        async def run():
            return await LLMClientFactory.create("spi", model="test-model")

        llm = asyncio.run(run())
        assert isinstance(llm, FakeLLM)
        assert llm.kw["model"] == "test-model"
    finally:
        unregister_provider(SPI_KIND_LLM)


# ── SPI 扩展（cache / session / memory，2026-08-27）──────

def test_spi_cache_backend_factory():
    from scout.plugins.spi import SPI_KIND_CACHE
    from scout.storage.factory import get_cache_backend

    class FakeCache:
        def __init__(self, **kw):
            self.kw = kw

        async def connect(self):
            pass

    try:
        register_provider(SPI_KIND_CACHE, FakeCache, source="test")
        cache = get_cache_backend("spi", host="spi-host")
        assert isinstance(cache, FakeCache)
        assert cache.kw["host"] == "spi-host"
    finally:
        unregister_provider(SPI_KIND_CACHE)


def test_spi_session_store_factory():
    from scout.plugins.spi import SPI_KIND_SESSION
    from scout.session.store import get_session_store

    class FakeSessionStore:
        def __init__(self, **kw):
            self.kw = kw

        def save(self, *a, **kw):
            return "fake-saved"

    try:
        register_provider(SPI_KIND_SESSION, FakeSessionStore, source="test")
        store = get_session_store("spi", db_path="spi.db")
        assert isinstance(store, FakeSessionStore)
        assert store.save("x") == "fake-saved"
    finally:
        unregister_provider(SPI_KIND_SESSION)


def test_spi_memory_store_factory():
    from scout.memory.store import get_memory_store
    from scout.plugins.spi import SPI_KIND_MEMORY

    class FakeMemoryStore:
        def __init__(self, **kw):
            self.kw = kw

        def add(self, *a, **kw):
            return {"ok": True}

    try:
        register_provider(SPI_KIND_MEMORY, FakeMemoryStore, source="test")
        store = get_memory_store("spi", memory_file="spi.json")
        assert isinstance(store, FakeMemoryStore)
        assert store.add("m")["ok"]
    finally:
        unregister_provider(SPI_KIND_MEMORY)


def test_spi_unregistered_raises():
    """未注册时 backend='spi' 应明确报错（防止静默回退到内置实现）."""
    from scout.plugins.spi import SPI_KIND_CACHE
    from scout.storage.factory import get_cache_backend

    with pytest.raises(ValueError, match="SPI 未注册"):
        get_cache_backend("spi")
