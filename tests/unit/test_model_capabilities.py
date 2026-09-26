"""模型能力用户可配（上下文长度 / 思考强度 / 视觉）—— 单元 + 接口测试.

2026-09-24：三项能力此前全是"系统推断/写死"，用户改不了（未收录模型的上下文
窗口一律回退 128000、思考只有布尔开关、视觉由预设目录定死）。这里覆盖：
  ① 统一档位 → 各家思考参数的翻译（发错参数会 400）
  ② 用户覆盖优先级（手填 > 预设 > 名称推断 > 默认值）
  ③ /api/models/capabilities 与 /api/config 保存链路
"""
import pytest

from scout.adapters.web.routes.config import (
    build_thinking_extra,
    capability_key,
    resolve_model_capabilities,
    resolve_model_context_length,
    resolve_model_vision,
    resolve_thinking_style,
)


@pytest.mark.unit
class TestThinkingStyle:
    @pytest.mark.parametrize(
        "provider,model,expected",
        [
            ("dashscope", "qwen3-max", "qwen"),
            ("openai", "gpt-4o", "openai"),
            ("openai", "o3-mini", "openai"),
            ("openai", "gpt-5", "openai"),
            ("claude", "claude-sonnet-4-20250514", "anthropic"),
            ("openrouter", "anthropic/claude-sonnet-4", "openrouter"),
            ("gemini", "gemini-2.5-pro", "gemini"),
            ("deepseek", "deepseek-reasoner", "bool_only"),
            ("zhipu", "glm-5.2", "bool_only"),
            ("moonshot", "kimi-k2", "bool_only"),
        ],
    )
    def test_style_detection(self, provider, model, expected):
        assert resolve_thinking_style(provider, model) == expected


@pytest.mark.unit
class TestThinkingExtra:
    def test_auto_injects_nothing(self):
        extra, note = build_thinking_extra("qwen", "auto")
        assert extra == {}

    def test_qwen_budget(self):
        extra, _ = build_thinking_extra("qwen", "medium")
        assert extra == {"enable_thinking": True, "thinking_budget": 8192}

    def test_qwen_off(self):
        extra, _ = build_thinking_extra("qwen", "off")
        assert extra == {"enable_thinking": False}

    def test_openai_uses_reasoning_effort(self):
        """o 系列/ GPT-5 不接受 enable_thinking，必须只发 reasoning_effort."""
        extra, _ = build_thinking_extra("openai", "high")
        assert extra == {"reasoning_effort": "high"}
        assert "enable_thinking" not in extra

    def test_openai_off_falls_back_to_low(self):
        """o 系列无法完全关闭推理 → off 按 low 发，并给出说明."""
        extra, note = build_thinking_extra("openai", "off")
        assert extra == {"reasoning_effort": "low"}
        assert "low" in note

    def test_claude_budget_tokens(self):
        extra, _ = build_thinking_extra("anthropic", "high")
        assert extra == {"thinking": {"type": "enabled", "budget_tokens": 32768}}

    def test_openrouter_reasoning(self):
        extra, _ = build_thinking_extra("openrouter", "low")
        assert extra == {"reasoning": {"effort": "low"}}

    def test_bool_only_model_no_budget(self):
        """DeepSeek/GLM 只有开关，不能发 thinking_budget."""
        extra, note = build_thinking_extra("bool_only", "high")
        assert extra == {"enable_thinking": True}
        assert "分档" in note


@pytest.mark.unit
class TestContextLength:
    def test_preset_hit(self):
        assert resolve_model_context_length("dashscope", "qwen-long") == 10000000

    def test_name_inference(self):
        assert resolve_model_context_length("volcano", "doubao-pro-32k") == 32000

    def test_param_size_not_mistaken_for_window(self):
        """qwen3.8-27b 的 27b 是参数量，不能被当成 27000 窗口."""
        assert resolve_model_context_length("openai", "qwen3.8-27b") == 0

    def test_unknown_model(self):
        assert resolve_model_context_length("openai", "my-custom-model") == 0


@pytest.mark.unit
class TestCapabilitiesAggregate:
    def test_user_override_wins(self):
        cap = resolve_model_capabilities(
            "openai",
            "qwen3.8-27b",
            context_overrides={"openai:qwen3.8-27b": 64000},
            vision_overrides={"openai:qwen3.8-27b": True},
            effort="high",
        )
        assert cap["context_length"] == 64000
        assert cap["context_source"] == "user"
        assert cap["vision"] is True
        assert cap["vision_source"] == "user"
        # provider 标为 openai → 按 OpenAI 风格发 reasoning_effort（不是 enable_thinking）
        assert cap["thinking_style"] == "openai"
        assert cap["thinking_extra"] == {"reasoning_effort": "high"}

    def test_auto_source_when_no_override(self):
        cap = resolve_model_capabilities("volcano", "doubao-pro-32k")
        assert cap["context_length"] == 32000
        assert cap["context_source"] == "name"
        assert cap["vision_source"] in ("preset", "name", "")

    def test_vision_from_preset(self):
        assert resolve_model_vision("claude", "claude-sonnet-4-20250514") == (True, "preset")

    def test_capability_key(self):
        assert capability_key("OpenAI", "gpt-4o") == "openai:gpt-4o"
        assert capability_key("", "my-model") == "*:my-model"


@pytest.mark.unit
class TestContextStatsUsesOverride:
    """手填的上下文窗口必须成为圆环分母（否则 128k 模型仍显示 33k）."""

    def test_stats_limit_from_user_override(self, tmp_path, monkeypatch):
        from scout.config import manager as config_manager_mod

        monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
        # 先写一份带覆盖的配置
        from scout.config.manager import ConfigManager

        cm = ConfigManager()
        cfg = cm.load()
        cfg.provider = "openai"
        cfg.model = "qwen3.8-27b"
        cfg.model_context_overrides = {"openai:qwen3.8-27b": 64000}
        cm.save(cfg)

        from fastapi.testclient import TestClient
        from scout.web.server import create_web_app

        client = TestClient(create_web_app())
        r = client.get("/api/context/stats", params={"provider": "openai", "model": "qwen3.8-27b"})
        assert r.status_code == 200
        body = r.json()
        assert body["limit"] == 64000
        assert body["limit_source"] == "user"

    def test_stats_falls_back_to_default(self, tmp_path, monkeypatch):
        from scout.config import manager as config_manager_mod

        monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
        from fastapi.testclient import TestClient
        from scout.web.server import create_web_app

        client = TestClient(create_web_app())
        r = client.get("/api/context/stats", params={"provider": "openai", "model": "totally-unknown"})
        assert r.status_code == 200
        assert r.json()["limit"] == 128000


@pytest.mark.unit
class TestCapabilitiesRoute:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from scout.config import manager as config_manager_mod

        monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
        from fastapi.testclient import TestClient
        from scout.web.server import create_web_app

        return TestClient(create_web_app())

    def test_get_capabilities(self, client):
        r = client.get("/api/models/capabilities", params={"provider": "dashscope", "model": "qwen3-max"})
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == "qwen3-max"
        assert body["thinking_style"] == "qwen"
        assert "thinking_applied" in body

    def test_save_then_read_back(self, client):
        payload = {
            "provider": "openai",
            "model": "my-model",
            "model_context_overrides": {"openai:my-model": 200000},
            "model_vision_overrides": {"openai:my-model": True},
            "reasoning_effort": "high",
        }
        r = client.post("/api/config", json=payload)
        assert r.status_code == 200
        assert r.json().get("status") == "ok"

        g = client.get("/api/models/capabilities", params={"provider": "openai", "model": "my-model"})
        body = g.json()
        assert body["context_length"] == 200000
        assert body["context_source"] == "user"
        assert body["vision"] is True
        assert body["vision_source"] == "user"
        assert body["thinking_effort"] == "high"

    def test_zero_clears_override(self, client):
        client.post("/api/config", json={
            "provider": "openai", "model": "m2",
            "model_context_overrides": {"openai:m2": 50000},
        })
        assert client.get("/api/models/capabilities", params={"provider": "openai", "model": "m2"}).json()["context_source"] == "user"
        # 传 0 = 清除覆盖
        client.post("/api/config", json={
            "provider": "openai", "model": "m2",
            "model_context_overrides": {"openai:m2": 0},
        })
        body = client.get("/api/models/capabilities", params={"provider": "openai", "model": "m2"}).json()
        assert body["context_source"] != "user"

    def test_invalid_effort_ignored(self, client):
        client.post("/api/config", json={"reasoning_effort": "ultra"})
        body = client.get("/api/models/capabilities", params={"provider": "openai", "model": "m3"}).json()
        assert body["thinking_effort"] in ("auto", "off", "low", "medium", "high")

    def test_saved_vision_fallback_reflected_in_route(self, client):
        """手动兜底模型（方案 A 下拉）保存后，capabilities 的视觉路由应显示它而非自动推荐."""
        # qwen3.8-27b 不支持视觉 → 未配兜底时自动推荐 qwen-vl-max
        body = client.get("/api/models/capabilities", params={"provider": "dashscope", "model": "qwen3.8-27b"}).json()
        assert body["vision_route"] == "fallback"
        assert body["vision_route_model"] == "qwen-vl-max"
        assert body["vision_route_source"] == "auto-fallback"
        # 保存手动兜底后 → 路由模型变为用户所选，source=user
        client.post("/api/config", json={"vision_model": "qwen-vl-plus"})
        body = client.get("/api/models/capabilities", params={"provider": "dashscope", "model": "qwen3.8-27b"}).json()
        assert body["vision_route"] == "fallback"
        assert body["vision_route_model"] == "qwen-vl-plus"
        assert body["vision_route_source"] == "user"
        # 清除 → 回到自动推荐
        client.post("/api/config", json={"vision_model": ""})
        body = client.get("/api/models/capabilities", params={"provider": "dashscope", "model": "qwen3.8-27b"}).json()
        assert body["vision_route_model"] == "qwen-vl-max"
        assert body["vision_route_source"] == "auto-fallback"


@pytest.mark.unit
class TestAgentThinkingWiring:
    def test_auto_keeps_deep_thinking_behaviour(self):
        """auto 档：行为必须与改造前一致（deep_thinking 布尔控制）."""
        from scout.engine.agent import Agent

        a = Agent.__new__(Agent)
        a.agent_mode = "react"
        a.deep_thinking = True
        a.reasoning_effort = "auto"
        a.model_provider = "dashscope"
        a.llm = type("L", (), {"model": "qwen3-max"})()
        assert a._thinking_extra() == {"enable_thinking": True}

        a2 = Agent.__new__(Agent)
        a2.agent_mode = "react"
        a2.deep_thinking = False
        a2.reasoning_effort = "auto"
        a2.model_provider = "dashscope"
        a2.llm = type("L", (), {"model": "qwen3-max"})()
        assert a2._thinking_extra() == {"enable_thinking": False}

    def test_multi_agent_always_off(self):
        from scout.engine.agent import Agent

        a = Agent.__new__(Agent)
        a.agent_mode = "multi_agent"
        a.deep_thinking = True
        a.reasoning_effort = "high"
        a.model_provider = "dashscope"
        a.llm = type("L", (), {"model": "qwen3-max"})()
        assert a._thinking_extra() == {"enable_thinking": False}

    def test_explicit_level_translated(self):
        from scout.engine.agent import Agent

        a = Agent.__new__(Agent)
        a.agent_mode = "react"
        a.deep_thinking = True
        a.reasoning_effort = "high"
        a.model_provider = "openai"
        a.llm = type("L", (), {"model": "o3"})()
        assert a._thinking_extra() == {"reasoning_effort": "high"}

    def test_vision_flag_forced(self):
        from scout.engine.agent import Agent

        a = Agent.__new__(Agent)
        a.vision_input = True
        a.llm = type("L", (), {"model": "whatever"})()
        a.model_provider = "openai"
        assert a._vision_enabled() is True
        a.vision_input = False
        assert a._vision_enabled() is False

    def test_vision_auto_by_model(self):
        from scout.engine.agent import Agent

        a = Agent.__new__(Agent)
        a.vision_input = None
        a.model_provider = "claude"
        a.llm = type("L", (), {"model": "claude-sonnet-4-20250514"})()
        assert a._vision_enabled() is True
