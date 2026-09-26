"""两阶段工具按需加载（load_tools + catalog）单元测试.

覆盖：
- ToolRegistry.catalog / schema_for / _visible_names
- load_tools 元工具：注入活跃集、去重、跨轮持久、未知/逗号串/截断降级
- Agent._select_progressive_tools：懒加载核心集更小、关键词仍生效、
  懒加载模式不做关键词单调累积、并回 lazy_loaded
"""

from __future__ import annotations

from types import SimpleNamespace

from scout.engine.agent import Agent
from scout.tools.builtin.load_tools import LoadToolsTool, _MAX_LOAD_PER_CALL
from scout.tools.registry import ToolRegistry


def _mk_schema(*names: str) -> list[dict]:
    """构造最小可用的 OpenAI function schema（不依赖真实工具注册）."""
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} 工具用途说明",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


class _FakeAgent(Agent):
    """跳过重型 __init__ 的 Agent 子类，仅注入 _select_progressive_tools 所需属性."""

    def __init__(self, tool_schemas: list[dict], lazy: bool):
        self._tool_schemas = tool_schemas
        self._tool_lazy_load = lazy
        self._exclude_tools: set[str] = set()
        self.allow_tools = None


# ── registry: catalog / schema_for ──────────────────────────────────


def test_catalog_returns_name_and_description():
    ToolRegistry.discover()
    cat = ToolRegistry.catalog()
    assert cat, "catalog 不应为空"
    for entry in cat:
        assert set(entry) == {"name", "description"}
        assert isinstance(entry["name"], str) and entry["name"]
        assert isinstance(entry["description"], str)
    # load_tools 自身也应在目录中（它是已注册工具）
    assert any(e["name"] == "load_tools" for e in cat)


def test_catalog_sorted_and_stable():
    ToolRegistry.discover()
    a = [e["name"] for e in ToolRegistry.catalog()]
    b = [e["name"] for e in ToolRegistry.catalog()]
    assert a == b == sorted(a), "目录须按名排序且稳定（前缀缓存契约）"


def test_catalog_respects_exclude():
    ToolRegistry.discover()
    cat = ToolRegistry.catalog(exclude={"shell"})
    assert all(e["name"] != "shell" for e in cat)


def test_schema_for_known_and_unknown():
    ToolRegistry.discover()
    s = ToolRegistry.schema_for("load_tools", compact=True)
    assert s is not None
    assert s["function"]["name"] == "load_tools"
    assert "parameters" in s["function"]
    assert ToolRegistry.schema_for("__definitely_not_a_tool__") is None


def test_catalog_much_smaller_than_full_schemas():
    """核心收益断言：目录体积应显著小于全量 compact schema."""
    import json

    ToolRegistry.discover()
    cat = json.dumps(ToolRegistry.catalog(), ensure_ascii=False)
    full = json.dumps(ToolRegistry.schemas(compact=True), ensure_ascii=False)
    assert len(cat) < len(full) / 2, "目录应至少比全量 compact 小一半"


# ── load_tools 元工具 ────────────────────────────────────────────────


async def test_load_tools_injects_and_persists(monkeypatch):
    pool = _mk_schema("file", "shell", "desktop", "vision")
    session = SimpleNamespace(extra={})
    agent = SimpleNamespace(
        _tool_schemas=pool,
        _active_tool_schemas=_mk_schema("file", "shell"),  # 核心集已在场
        _current_session=session,
    )
    monkeypatch.setattr(ToolRegistry, "_main_agent", agent, raising=False)

    obs = await LoadToolsTool().execute(names=["desktop"])
    assert obs.success
    active_names = {s["function"]["name"] for s in agent._active_tool_schemas}
    assert "desktop" in active_names, "load 后应进入活跃集"
    assert session.extra["lazy_loaded"] == ["desktop"], "应写入会话 lazy_loaded 跨轮持久"
    assert "desktop" in obs.output, "输出应回显已加载工具"


async def test_load_tools_dedupes_active(monkeypatch):
    pool = _mk_schema("file", "desktop")
    agent = SimpleNamespace(
        _tool_schemas=pool,
        _active_tool_schemas=_mk_schema("file", "desktop"),
        _current_session=SimpleNamespace(extra={}),
    )
    monkeypatch.setattr(ToolRegistry, "_main_agent", agent, raising=False)

    await LoadToolsTool().execute(names=["desktop"])
    names = [s["function"]["name"] for s in agent._active_tool_schemas]
    assert names.count("desktop") == 1, "重复加载不应产生重复条目"


async def test_load_tools_unknown_name(monkeypatch):
    agent = SimpleNamespace(
        _tool_schemas=_mk_schema("file"),
        _active_tool_schemas=_mk_schema("file"),
        _current_session=SimpleNamespace(extra={}),
    )
    monkeypatch.setattr(ToolRegistry, "_main_agent", agent, raising=False)

    obs = await LoadToolsTool().execute(names=["__nope__"])
    assert not obs.success
    assert obs.metadata["unknown"] == ["__nope__"]


async def test_load_tools_accepts_comma_string(monkeypatch):
    pool = _mk_schema("file", "desktop", "vision")
    agent = SimpleNamespace(
        _tool_schemas=pool,
        _active_tool_schemas=_mk_schema("file"),
        _current_session=SimpleNamespace(extra={}),
    )
    monkeypatch.setattr(ToolRegistry, "_main_agent", agent, raising=False)

    obs = await LoadToolsTool().execute(names="desktop,vision")
    assert obs.success
    assert set(obs.metadata["loaded"]) == {"desktop", "vision"}


async def test_load_tools_truncates_over_limit(monkeypatch):
    names = [f"t{i}" for i in range(_MAX_LOAD_PER_CALL + 5)]
    pool = _mk_schema(*names)
    agent = SimpleNamespace(
        _tool_schemas=pool,
        _active_tool_schemas=[],
        _current_session=SimpleNamespace(extra={}),
    )
    monkeypatch.setattr(ToolRegistry, "_main_agent", agent, raising=False)

    obs = await LoadToolsTool().execute(names=names)
    assert obs.metadata["truncated"] is True
    assert len(obs.metadata["loaded"]) == _MAX_LOAD_PER_CALL


async def test_load_tools_empty_names_invalid():
    obs = await LoadToolsTool().execute(names=[])
    assert not obs.success
    assert obs.error_code == "INVALID_ARGS"


async def test_load_tools_without_agent_still_returns_schema(monkeypatch):
    """脱离 Agent（无 _main_agent）时不注入，但仍回显 schema 供本轮参考."""
    monkeypatch.setattr(ToolRegistry, "_main_agent", None, raising=False)
    ToolRegistry.discover()
    obs = await LoadToolsTool().execute(names=["load_tools"])
    # load_tools 一定已注册 → 能取到 schema
    assert obs.success
    assert "schemas" in obs.output


# ── _select_progressive_tools ────────────────────────────────────────


_NAMES = (
    "file", "shell", "execute_code", "web_search", "web_fetch",
    "memory_search", "memory_save", "memory_list", "send_file",
    "env_config_get", "env_config_save", "env_config_list", "env_config_delete",
    "ask_user", "load_tools", "desktop", "vision", "scheduler",
)


def _selected(lazy: bool, text: str, extra: dict | None = None) -> set[str]:
    agent = _FakeAgent(_mk_schema(*_NAMES), lazy=lazy)
    session = SimpleNamespace(extra=extra or {})
    out = agent._select_progressive_tools(text, session)
    return {s["function"]["name"] for s in out}


def test_lazy_core_smaller_than_full_core():
    lazy = _selected(lazy=True, text="你好")
    eager = _selected(lazy=False, text="你好")
    assert len(lazy) < len(eager), "懒加载常驻核心集应更小"
    assert "load_tools" in lazy, "懒加载模式必须常驻 load_tools"
    # 低频写操作在懒加载下移出核心集
    assert "memory_save" not in lazy
    assert "env_config_save" not in lazy


def test_keyword_still_expands_in_lazy_mode():
    sel = _selected(lazy=True, text="帮我截个图看看屏幕")
    assert "desktop" in sel
    assert "vision" in sel, "desktop 联动 vision"


def test_lazy_mode_no_keyword_monotonic_accumulation():
    """懒加载模式下，旧的 active_tools 累积不再生效（防膨胀）."""
    sel = _selected(
        lazy=True,
        text="你好",  # 无桌面关键词
        extra={"active_tools": ["desktop"]},
    )
    assert "desktop" not in sel


def test_lazy_mode_unions_lazy_loaded():
    """load_tools 显式加载过的工具，下一轮并回活跃集."""
    sel = _selected(lazy=True, text="你好", extra={"lazy_loaded": ["scheduler"]})
    assert "scheduler" in sel


def test_eager_mode_keeps_accumulation():
    """非懒加载模式保持旧行为：active_tools 单调累积仍生效."""
    sel = _selected(lazy=False, text="你好", extra={"active_tools": ["scheduler"]})
    assert "scheduler" in sel


def test_selection_always_non_empty():
    assert _selected(lazy=True, text="")
    assert _selected(lazy=False, text="")
