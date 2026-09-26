# -*- coding: utf-8 -*-
"""vision 工具路由决策测试.

2026-09-07：移除本地 OCR 兜底（用户决策：未配置视觉模型时直接提示不可用，
为项目减负约 160MB 依赖）。路由简化为 —— vision_model 非空 → "vl"；否则
"none"。纯函数单测不依赖网络。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from scout.tools.builtin import vision as vision_mod
from scout.tools.builtin.vision import VisionTool


# ── resolve_mode 路由决策（纯函数，不触网）────────────────────

@pytest.mark.unit
def test_resolve_mode_no_vision_model_none():
    """未配置 vision_model → "none"（无 OCR 兜底，execute 将返回友好提示）."""
    cfg = SimpleNamespace(api_key="sk-x", model="qwen3.8-27b", vision_model="")
    assert vision_mod.resolve_mode(cfg) == "none"


@pytest.mark.unit
def test_resolve_mode_vision_equals_main_vl():
    """vision_model 与主 model 相同 → 仍走 VL（2026-09-06 规则保留）.

    同名也可能是多模态模型（实测 qwen3.8-27b 支持 image_url）。
    """
    cfg = SimpleNamespace(api_key="sk-x", model="qwen3.8-27b", vision_model="qwen3.8-27b")
    assert vision_mod.resolve_mode(cfg) == "vl"


@pytest.mark.unit
def test_resolve_mode_dedicated_vision_vl():
    """显式配置了与主模型不同的专属视觉模型 → 走 VL."""
    cfg = SimpleNamespace(api_key="sk-x", model="qwen3.8-27b", vision_model="qwen-vl-plus")
    assert vision_mod.resolve_mode(cfg) == "vl"


@pytest.mark.unit
def test_resolve_mode_missing_attrs_none():
    """cfg 缺 vision_model 等属性（异常配置）→ 不抛错；纯文本模型 → "none"."""
    cfg = SimpleNamespace(api_key="sk-x", model="deepseek-v4-pro")
    assert vision_mod.resolve_mode(cfg) == "none"


# ── execute：未配置视觉模型 → 友好提示（不抛错、不触网）────────

@pytest.mark.unit
def test_execute_unconfigured_returns_hint(monkeypatch):
    """无任何视觉路径（deepseek 无兜底模型）→ execute 返回"无法读取图片"提示."""
    cfg = SimpleNamespace(
        api_key="sk-x", model="deepseek-v4-pro", vision_model="",
        vision_provider="", provider="deepseek", base_url="https://x.example.com/v1",
        model_vision_overrides={},
    )
    monkeypatch.setattr("scout.config.ConfigManager.load", lambda self: cfg)
    monkeypatch.setattr(
        "scout.config.ConfigManager.get_provider_credentials", lambda self, p: ("", "")
    )
    obs = asyncio.run(VisionTool().execute(image="whatever.png", question="describe"))
    assert not obs.success
    assert "视觉模型" in obs.output


@pytest.mark.unit
def test_no_ocr_symbols():
    """OCR 相关符号已从模块移除（防止依赖回归）."""
    for name in ("_run_ocr", "_get_ocr_engine", "_ocr_sync", "_enhance_image"):
        assert not hasattr(vision_mod, name), f"{name} 不应存在"
