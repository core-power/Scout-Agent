"""视觉路由 2.0：两轴语义（模型事实 / 谁来看图）与收敛不变量.

对应设计稿《Scout Agent — 视觉能力配置重构设计》。核心要钉住的三件事：

1. 「视觉模型」填成与主模型同名 = 显式声明原生多模态（自定义模型最常见的表达），
   不再像旧实现那样"非空即用"把原生多模态模型降级成外挂识图（缺陷 D1）；
2. 聊天附件与 vision 工具/desktop 共用**同一个**决策入口
   `Agent._vision_route()` —— 旧实现里附件只问模型能力、从不问路由，同一轮可以
   给出两个不同答案（缺陷 D2）；
3. 无任何可用路径时不再静默，而是明确告知模型"图片未识读"（缺陷 D5）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scout.llm.vision_route import (
    capability_key,
    native_vision,
    resolve_vision_route,
    route_for_agent,
)

pytestmark = pytest.mark.unit


def cfg(**kw):
    base = dict(
        provider="openai", model="gpt-4o", base_url="https://api.test/v1",
        vision_provider="", vision_model="",
        model_vision_overrides={}, model_vision_mode={}, model_vision_probe={},
        vision_disabled=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── 轴 B：谁来看图 ─────────────────────────────────────────────────


def test_aux_same_as_main_declares_native():
    """外挂字段与主模型同名 → 按原生直收（不再降级成"别的模型先识图成文字"）."""
    r = resolve_vision_route(cfg(provider="custom", model="my-vl-7b", vision_model="my-vl-7b"))
    assert r["path"] == "main" and r["source"] == "self" and r["native"] is True
    # 大小写/空格差异不应影响等价判断
    r2 = resolve_vision_route(cfg(provider="custom", model="My-VL-7b", vision_model=" my-vl-7b "))
    assert r2["path"] == "main", r2


def test_aux_different_from_main_is_respected():
    """填了别的模型 = 用户显式指定外挂，不擅自改成原生（保留旧行为）."""
    r = resolve_vision_route(cfg(model="gpt-4o", vision_model="qwen-vl-max"))
    assert r["path"] == "fallback" and r["model"] == "qwen-vl-max" and r["native"] is True


def test_blank_aux_uses_native_when_capable():
    r = resolve_vision_route(cfg())
    assert r["path"] == "main" and r["source"] == "preset"


def test_blank_aux_falls_back_to_vendor_recommendation():
    r = resolve_vision_route(cfg(provider="dashscope", model="qwen3.8-27b"))
    assert r["path"] == "fallback" and r["source"] == "auto-fallback"
    assert "qwen-vl-max" in r["model"]


def test_no_path_returns_none_with_reason():
    r = resolve_vision_route(cfg(provider="deepseek", model="deepseek-chat"))
    assert r["path"] == "none" and r["reason"]


def test_global_switch_kills_everything():
    r = resolve_vision_route(cfg(vision_disabled=True))
    assert r["path"] == "none" and r["source"] == "off"


# ── 轴 A：模型事实（探测 > 声明 > 预设 > 名称猜测）──────────────────


def test_probe_beats_preset_and_name_guess():
    """名称像多模态但实测不支持 → 以探测为准（规则表追不完新模型）."""
    c = cfg(model="gpt-9-vision-ultra", model_vision_probe={"openai:gpt-9-vision-ultra": False})
    assert native_vision("openai", "gpt-9-vision-ultra", c) == (False, "probe")
    assert resolve_vision_route(c)["path"] == "fallback"  # 转用厂商推荐外挂


def test_probe_enables_unknown_custom_model():
    m = "agent-x-20260926"
    c = cfg(provider="custom", model=m, model_vision_probe={capability_key("custom", m): True})
    assert native_vision("custom", m, c) == (True, "probe")
    assert resolve_vision_route(c)["path"] == "main"


def test_mode_off_beats_native_capability():
    c = cfg(model_vision_mode={"openai:gpt-4o": "off"})
    r = resolve_vision_route(c)
    assert r["path"] == "none" and r["source"] == "off"
    assert r["native"] is True, "事实(native)与偏好(path)必须分开表达"


def test_preference_modes_never_fake_the_fact():
    """no_main / off 是偏好，不能把"其实能看图"的事实抹成 False（否则 UI 误报）."""
    for mode in ("no_main", "off"):
        c = cfg(model_vision_mode={capability_key("openai", "gpt-4o"): mode})
        assert native_vision("openai", "gpt-4o", c)[0] is True, f"mode={mode} 抹掉了事实"
        r = resolve_vision_route(c)
        assert r["native"] is True and r["path"] != "main", f"mode={mode} 结果异常: {r}"


def test_mode_no_main_allows_aux_but_not_main():
    """「别把图塞进主模型，但可以让外挂识图」——旧 UI 无法表达的意图."""
    c = cfg(provider="dashscope", model="qwen3.8-27b",
            model_vision_mode={"dashscope:qwen3.8-27b": "no_main"})
    r = resolve_vision_route(c)
    assert r["path"] == "fallback" and r["source"] == "override"
    # 没有任何可用外挂时才是 none
    c2 = cfg(provider="deepseek", model="deepseek-chat",
             model_vision_mode={capability_key("deepseek", "deepseek-chat"): "no_main"})
    assert resolve_vision_route(c2)["path"] == "none"


def test_mode_native_overrides_probe_negative():
    c = cfg(model="deepseek-chat", provider="deepseek",
            model_vision_mode={capability_key("deepseek", "deepseek-chat"): "native"})
    assert resolve_vision_route(c)["path"] == "main"


def test_legacy_false_keeps_behavior_but_asks():
    """旧版「关闭图片处理」语义歧义：行为保持不变，但打上 needs_choice 请用户确认."""
    c = cfg(provider="dashscope", model="qwen3.8-27b",
            model_vision_overrides={capability_key("dashscope", "qwen3.8-27b"): False})
    r = resolve_vision_route(c)
    assert r["path"] == "none" and r["needs_choice"] is True
    assert r.get("would_be"), "应告知用户本可启用哪个外挂"


def test_legacy_true_still_forces_native():
    c = cfg(provider="deepseek", model="deepseek-chat",
            model_vision_overrides={capability_key("deepseek", "deepseek-chat"): True})
    assert resolve_vision_route(c)["path"] == "main"


# ── 键规则：provider 大小写不敏感（修 D8）──────────────────────────


def test_capability_key_is_case_insensitive_on_provider():
    assert capability_key("OpenAI", "gpt-4o") == capability_key("openai", "gpt-4o")
    assert capability_key("", "m") == capability_key("  ", "m") == "*:m"


@pytest.mark.parametrize("prov", ["DashScope", "dashscope", "DASHSCOPE"])
def test_route_hits_override_regardless_of_case(prov):
    c = cfg(provider=prov, model="qwen3.8-27b",
            model_vision_mode={capability_key("dashscope", "qwen3.8-27b"): "off"})
    assert resolve_vision_route(c)["path"] == "none"


def test_legacy_provider_empty_key_still_readable():
    """老配置可能写成 ":model"（旧实现 provider 为空时的形态）→ 仍可命中."""
    c = cfg(provider="", model="m1", model_vision_overrides={":m1": True})
    assert resolve_vision_route(c)["path"] == "main"


# ── 收敛不变量：附件与工具必须同一答案 ─────────────────────────────


class _FakeAgent:
    """最小 Agent 替身：只提供 route_for_agent 需要的三个属性."""

    def __init__(self, provider, model):
        self.model_provider = provider
        self.llm = SimpleNamespace(model=model)
        self.vision_input = None


def test_runtime_model_beats_config_model():
    """聊天中途切模型 → 路由跟随**运行时**模型（旧实现按 config.model 会过期）."""
    c = cfg(provider="openai", model="gpt-4o")  # 配置里还停在 gpt-4o
    agent = _FakeAgent("dashscope", "qwen3.7-plus")  # 运行时已切到带 vision 的模型
    r = route_for_agent(agent, c)
    assert r["model"] == "qwen3.7-plus" and r["path"] == "main", r
    # 反向：运行时切成纯文本模型，配置的 gpt-4o 不该继续让它直收图片
    agent2 = _FakeAgent("deepseek", "deepseek-chat")
    r2 = route_for_agent(agent2, c)
    assert r2["native"] is False and r2["path"] != "main", r2


def test_attachment_and_tool_share_one_decision():
    """同一次判定同时服务附件内联门禁与 vision 工具（D2 的回归钉）."""
    agent = _FakeAgent("dashscope", "qwen3.8-27b")
    c = cfg(provider="dashscope", model="qwen3.8-27b")
    route = route_for_agent(agent, c)
    from scout.tools.builtin.vision import resolve_mode

    assert route["path"] == "fallback"
    assert resolve_mode(c) == "vl", "工具侧必须与附件侧同意"


def test_vision_input_still_forces_for_callers():
    """`Agent.vision_input` 作为调用方显式覆盖仍是最高优先（保留既有注入点语义）."""
    from unittest.mock import MagicMock

    from scout.engine.agent import Agent
    import scout.config as cfg_pkg

    a = Agent(MagicMock())
    a.model_provider = "dashscope"
    a.llm = SimpleNamespace(model="qwen3.8-27b")
    orig = cfg_pkg.ConfigManager.load
    try:
        cfg_pkg.ConfigManager.load = lambda self: cfg(
            provider="dashscope", model="qwen3.8-27b",
            model_vision_mode={capability_key("dashscope", "qwen3.8-27b"): "off"},
        )
        # 配置说"关闭视觉"，但调用方显式强制 True → 以强制值为准
        a.vision_input = True
        r = a._vision_route()
        assert r["path"] == "main" and r["source"] == "forced"
        assert a._vision_enabled() is True
        a.vision_input = False
        assert a._vision_route()["path"] == "none"
        a.vision_input = None
        assert a._vision_route()["source"] == "off", "取消强制后回到配置判定"
    finally:
        cfg_pkg.ConfigManager.load = orig


def test_agent_vision_route_and_enabled_agree():
    """`Agent._vision_enabled()` 现在等价于「路由判 main」."""
    from unittest.mock import MagicMock

    from scout.engine.agent import Agent

    a = Agent(MagicMock())
    a.model_provider = "deepseek"
    a.llm = SimpleNamespace(model="deepseek-chat")
    a.vision_input = None
    import scout.config as cfg_pkg

    orig = cfg_pkg.ConfigManager.load
    try:
        cfg_pkg.ConfigManager.load = lambda self: cfg(
            provider="deepseek", model="deepseek-chat", vision_model="qwen-vl-max"
        )
        assert a._vision_route()["path"] == "fallback"
        assert a._vision_enabled() is False, "走外挂时不能再把图片内联进主模型"
    finally:
        cfg_pkg.ConfigManager.load = orig
