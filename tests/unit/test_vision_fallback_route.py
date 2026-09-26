"""三级视觉兜底路由测试（2026-09-24）.

覆盖 resolve_vision_route 六级判定、capabilities 的 vision_route 字段、
resolve_mode 兼容映射。
"""

from types import SimpleNamespace

import pytest

from scout.adapters.web.routes.config import (
    resolve_model_capabilities,
    resolve_vision_fallback,
    resolve_vision_route,
)
from scout.tools.builtin.vision import resolve_mode


def _cfg(provider="dashscope", model="qwen3.8-27b", vision_model="",
         overrides=None, vision_provider="", base_url=""):
    return SimpleNamespace(
        vision_provider=vision_provider,
        provider=provider,
        model=model,
        base_url=base_url,
        vision_model=vision_model,
        model_vision_overrides=overrides or {},
    )


def _real_route(agent):
    """让测试替身走真实路由判定（2026-09-26 收敛后的唯一决策入口）.

    `Agent._vision_route()` 只依赖 `agent.llm.model` / `agent.model_provider` 与
    配置对象，替身提供这三样即可复用生产逻辑 —— 不 stub 答案，才测得到收敛本身。
    """
    from scout.config import ConfigManager
    from scout.llm.vision_route import route_for_agent

    return route_for_agent(agent, ConfigManager().load())


@pytest.mark.unit
class TestResolveVisionRoute:
    def test_explicit_vision_model_wins(self):
        """① 旧显式 vision_model → fallback(user)，即使主模型本身支持视觉."""
        r = resolve_vision_route(_cfg(model="qwen3.7-plus", vision_model="qwen-vl-max"))
        assert r["path"] == "fallback"
        assert r["model"] == "qwen-vl-max"
        assert r["source"] == "user"

    def test_override_true_main(self):
        """② 显式开启 → main（即便厂商无兜底，如 deepseek）."""
        r = resolve_vision_route(_cfg(
            provider="deepseek", model="deepseek-v4-pro",
            overrides={"deepseek:deepseek-v4-pro": True}))
        assert r["path"] == "main"

    def test_override_false_none(self):
        """③ 显式关闭 → none（尊重用户，不做兜底）."""
        r = resolve_vision_route(_cfg(overrides={"dashscope:qwen3.8-27b": False}))
        assert r["path"] == "none"

    def test_preset_vision_main(self):
        """④ 未设置 + 预设标注支持 → main."""
        r = resolve_vision_route(_cfg(provider="dashscope", model="qwen3.7-plus"))
        assert r["path"] == "main"
        assert r["source"] == "preset"

    def test_name_inference_main(self):
        """④ 未设置 + 名称推断支持（gpt-4o）→ main."""
        r = resolve_vision_route(_cfg(provider="openai", model="gpt-4o"))
        assert r["path"] == "main"

    def test_auto_fallback(self):
        """⑤ 未设置 + 主模型纯文本 + 厂商有推荐 → fallback(推荐模型)."""
        r = resolve_vision_route(_cfg(provider="dashscope", model="qwen3.8-27b"))
        assert r["path"] == "fallback"
        assert r["model"] == "qwen-vl-max"
        assert r["source"] == "auto-fallback"

    def test_no_fallback_provider_none(self):
        """⑥ deepseek 无视觉 API → none."""
        r = resolve_vision_route(_cfg(provider="deepseek", model="deepseek-v4-pro"))
        assert r["path"] == "none"

    def test_fallback_uses_main_base_url(self):
        """兜底沿用主厂商 base_url（同厂商原则）."""
        r = resolve_vision_route(_cfg(base_url="https://xx.example/v1"))
        assert r["path"] == "fallback"
        assert r["base_url"] == "https://xx.example/v1"


@pytest.mark.unit
class TestVisionFallbackTable:
    def test_known_providers(self):
        assert resolve_vision_fallback("dashscope") == "qwen-vl-max"
        assert resolve_vision_fallback("openai") == "gpt-4o-mini"
        assert resolve_vision_fallback("volcano") == "doubao-1.5-vision-pro-32k"

    def test_unknown_provider_empty(self):
        assert resolve_vision_fallback("deepseek") == ""
        assert resolve_vision_fallback("") == ""
        assert resolve_vision_fallback("no-such") == ""


@pytest.mark.unit
class TestCapabilitiesVisionRoute:
    def test_capabilities_contains_route(self):
        cap = resolve_model_capabilities("dashscope", "qwen3.8-27b")
        assert cap["vision_route"] == "fallback"
        assert cap["vision_route_model"] == "qwen-vl-max"

    def test_capabilities_none_route(self):
        cap = resolve_model_capabilities("deepseek", "deepseek-v4-pro")
        assert cap["vision_route"] == "none"
        assert cap["vision_route_model"] == ""

    def test_capabilities_override_off(self):
        cap = resolve_model_capabilities(
            "dashscope", "qwen3.8-27b",
            vision_overrides={"dashscope:qwen3.8-27b": False})
        assert cap["vision_route"] == "none"


@pytest.mark.unit
class TestResolveModeCompat:
    def test_mode_vl_for_fallback(self):
        assert resolve_mode(_cfg()) == "vl"

    def test_mode_vl_for_main(self):
        assert resolve_mode(_cfg(model="qwen3.7-plus")) == "vl"

    def test_mode_none(self):
        assert resolve_mode(_cfg(provider="deepseek", model="deepseek-v4-pro")) == "none"

    def test_mode_tolerates_missing_attrs(self):
        """cfg 缺属性（异常配置）不抛错，按 none 处理."""
        assert resolve_mode(SimpleNamespace(api_key="sk-x", model="whatever")) == "none"


@pytest.mark.unit
class TestAutoFallbackFailureGuidance:
    """自动兜底模型调用失败（未开通/无权限）→ 返回明确自救指引."""

    def test_vision_tool_failure_guidance(self, monkeypatch, tmp_path):
        import asyncio

        from scout.tools.builtin.vision import VisionTool

        cfg = SimpleNamespace(
            api_key="sk-x", model="qwen3.8-27b", vision_model="",
            vision_provider="", provider="dashscope", base_url="https://x/v1",
            model_vision_overrides={},
        )
        monkeypatch.setattr("scout.config.ConfigManager.load", lambda self: cfg)
        monkeypatch.setattr(
            "scout.tools.builtin.vision.get_vl_config",
            lambda: ("sk-x", "https://x/v1", "qwen-vl-max", cfg),
        )

        async def fake_fail(*a, **k):
            return SimpleNamespace(success=False, output="Error: model not exist")
        monkeypatch.setattr("scout.tools.builtin.vision._call_vision", fake_fail)
        monkeypatch.setattr(
            "scout.tools.builtin.vision._downscale_for_vision", lambda x: x
        )

        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG")
        obs = asyncio.run(VisionTool().execute(image=str(img), question="describe"))
        assert not obs.success
        assert "兜底视觉模型" in obs.output
        assert "手动填写" in obs.output

    def test_describe_failure_hint(self, monkeypatch, tmp_path):
        import asyncio

        from scout.engine.context_inject import ContextInjectMixin

        monkeypatch.setattr(
            "scout.config.ConfigManager.load",
            lambda self: SimpleNamespace(
                provider="dashscope", model="qwen3.8-27b", base_url="https://x/v1",
                vision_model="", vision_provider="", model_vision_overrides={},
            ),
        )

        class _Agent:
            model_provider = "dashscope"
            vision_input = None
            llm = SimpleNamespace(model="qwen3.8-27b")

            def _vision_route(self):
                return _real_route(self)

        async def fake_fail(*a, **k):
            return SimpleNamespace(success=False, output="403 AccessDenied")
        monkeypatch.setattr("scout.tools.builtin.vision._call_vision", fake_fail)
        monkeypatch.setattr(
            "scout.tools.builtin.vision.get_vl_config",
            lambda: ("sk-x", "https://x/v1", "qwen-vl-max", None),
        )

        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG")
        out = asyncio.run(
            ContextInjectMixin._describe_images_via_fallback(
                _Agent(), [{"name": "a.png", "path": str(img), "type": "image/png"}]
            )
        )
        assert "识别失败" in out
        assert "兜底视觉模型" in out  # 自救指引在场


@pytest.mark.unit
class TestDescribeViaFallback:
    """聊天图片兜底描述注入（mock VL 调用，不联网）."""

    def _agent_stub(self, tmp_path, provider="dashscope", model="qwen3.8-27b"):
        """构造最小 agent 代理：mixin 方法只用到这几个属性."""
        from scout.config.manager import ConfigManager
        from scout.config import paths as cfg_paths


        class _CM:
            def load(self):
                return SimpleNamespace(
                    provider=provider, model=model, base_url="https://x/v1",
                    vision_model="", vision_provider="",
                    model_vision_overrides={},
                )

            def get_provider_credentials(self, p):
                return ("", "")

        import scout.config as cfg_pkg

        class _Agent:
            model_provider = provider
            vision_input = None
            llm = SimpleNamespace(model=model)

            def _vision_route(self):
                return _real_route(self)

        return _Agent(), _CM

    def test_describe_injects_block(self, monkeypatch, tmp_path):
        import asyncio

        from scout.engine.context_inject import ContextInjectMixin

        agent, _ = self._agent_stub(tmp_path)
        monkeypatch.setattr(
            "scout.config.ConfigManager.load",
            lambda self: SimpleNamespace(
                provider="dashscope", model="qwen3.8-27b", base_url="https://x/v1",
                vision_model="", vision_provider="", model_vision_overrides={},
            ),
        )

        async def fake_call(api_key, base_url, model, image, question, crop=""):
            return SimpleNamespace(success=True, output="图中是一只猫")
        monkeypatch.setattr(
            "scout.tools.builtin.vision._call_vision", fake_call
        )
        monkeypatch.setattr(
            "scout.tools.builtin.vision.get_vl_config",
            lambda: ("sk-x", "https://x/v1", "qwen-vl-max", None),
        )

        import os as _os
        img = tmp_path / "cat.png"
        img.write_bytes(b"\x89PNG fake")

        atts = [{"name": "cat.png", "path": str(img), "type": "image/png"}]
        out = asyncio.run(
            ContextInjectMixin._describe_images_via_fallback(agent, atts)
        )
        assert "<image_descriptions" in out
        assert "图中是一只猫" in out
        assert "qwen-vl-max" not in out  # 模型名不暴露给主模型，只给描述

    def test_skipped_when_vision_enabled(self, tmp_path, monkeypatch):
        """主模型可直收（路由判 main）→ 不做兜底描述，由附件内联直发."""
        import asyncio

        from scout.engine.context_inject import ContextInjectMixin

        monkeypatch.setattr(
            "scout.config.ConfigManager.load",
            lambda self: SimpleNamespace(
                provider="dashscope", model="qwen3.7-plus", base_url="https://x/v1",
                vision_model="", vision_provider="", model_vision_overrides={},
                model_vision_mode={}, model_vision_probe={}, vision_disabled=False,
            ),
        )
        agent, _ = self._agent_stub(tmp_path, model="qwen3.7-plus")
        assert agent._vision_route()["path"] == "main", "前提：该模型应被判为可直收图片"
        out = asyncio.run(
            ContextInjectMixin._describe_images_via_fallback(
                agent, [{"name": "a.png", "path": "whatever", "type": "image/png"}]
            )
        )
        assert out == ""

    def test_unseen_notice_when_no_path(self, monkeypatch, tmp_path):
        """deepseek（无兜底厂商）→ 不静默：明确告知模型"图片无法识读"。

        ★ 2026-09-26 行为变更（D5）：旧实现返回空串，模型只看到落盘路径、不知道
        自己看不见图，最容易的失败模式就是对着不存在的图片内容编话。
        """
        import asyncio

        from scout.engine.context_inject import ContextInjectMixin

        agent, _ = self._agent_stub(tmp_path, provider="deepseek", model="deepseek-v4-pro")
        monkeypatch.setattr(
            "scout.config.ConfigManager.load",
            lambda self: SimpleNamespace(
                provider="deepseek", model="deepseek-v4-pro", base_url="",
                vision_model="", vision_provider="", model_vision_overrides={},
                model_vision_mode={}, model_vision_probe={}, vision_disabled=False,
            ),
        )
        out = asyncio.run(
            ContextInjectMixin._describe_images_via_fallback(
                agent, [{"name": "a.png", "path": "whatever", "type": "image/png"}]
            )
        )
        assert "<attachments_unseen" in out
        assert "严禁描述或推测图片内容" in out        # 防幻觉指令必须在
        assert "模型能力" in out                       # 给出自救路径
        assert "<image_descriptions" not in out        # 且没有伪造的识别结果
