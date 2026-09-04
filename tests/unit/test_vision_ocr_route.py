# -*- coding: utf-8 -*-
"""vision 工具路由决策 + OCR 本地兜底测试.

2026-09-04：vision 工具新增 resolve_mode 路由 ——
配置了专属视觉模型（vision_model ≠ 主 model）→ VL；否则本地 RapidOCR 兜底。
纯函数路由单测不依赖网络；OCR 路径用临时生成的小图实测（需要已安装
rapidocr-onnxruntime，否则跳过）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from scout.tools.builtin import vision as vision_mod
from scout.tools.builtin.vision import VisionTool


# ── resolve_mode 路由决策（纯函数，不触网）────────────────────

@pytest.mark.unit
def test_resolve_mode_no_api_key_ocr():
    cfg = SimpleNamespace(api_key="", model="gpt-4o", vision_model="gpt-4o")
    assert vision_mod.resolve_mode(cfg) == "ocr"


@pytest.mark.unit
def test_resolve_mode_no_vision_model_ocr():
    """主模型没有显式配置专属视觉模型 → 本地 OCR，不拿纯文本主模型硬发图."""
    cfg = SimpleNamespace(api_key="sk-x", model="qwen3.8-27b", vision_model="")
    assert vision_mod.resolve_mode(cfg) == "ocr"


@pytest.mark.unit
def test_resolve_mode_vision_equals_main_ocr():
    """视觉模型字段误填成纯文本主模型(qwen3.8-27b) → 视同未配置 → OCR."""
    cfg = SimpleNamespace(api_key="sk-x", model="qwen3.8-27b", vision_model="qwen3.8-27b")
    assert vision_mod.resolve_mode(cfg) == "ocr"


@pytest.mark.unit
def test_resolve_mode_dedicated_vision_vl():
    """显式配置了与主模型不同的专属视觉模型 → 走 VL."""
    cfg = SimpleNamespace(api_key="sk-x", model="qwen3.8-27b", vision_model="qwen-vl-plus")
    assert vision_mod.resolve_mode(cfg) == "vl"


# ── OCR 本地路径（真实引擎，无 RapidOCR 时跳过）────────────────

def _has_rapidocr() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _make_test_image(text: str) -> str:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (420, 110), "white")
    d = ImageDraw.Draw(img)
    d.text((15, 30), text, fill="black")
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    img.save(path)
    return path


@pytest.mark.unit
@pytest.mark.skipif(not _has_rapidocr(), reason="rapidocr-onnxruntime 未安装")
def test_ocr_extracts_text_from_local_image():
    path = _make_test_image("OCR ROUTE 2026")
    try:
        texts = asyncio.run(vision_mod._run_ocr(path))
        assert any("2026" in t for t in texts), f"OCR 结果: {texts}"
    finally:
        os.unlink(path)


@pytest.mark.unit
@pytest.mark.skipif(not _has_rapidocr(), reason="rapidocr-onnxruntime 未安装")
def test_vision_tool_ocr_mode_no_vision_model(monkeypatch):
    """未配置专属视觉模型 → execute 走 OCR 成功返回文字."""
    cfg = SimpleNamespace(
        api_key="sk-x", model="qwen3.8-27b", vision_model="qwen3.8-27b",
        vision_provider="", provider="openai", base_url="https://x.example.com/v1",
    )
    monkeypatch.setattr("scout.config.ConfigManager.load", lambda self: cfg)
    monkeypatch.setattr("scout.config.ConfigManager.get_provider_credentials",
                        lambda self, p: ("", ""))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")

    path = _make_test_image("VISION OCR MODE")
    try:
        obs = asyncio.run(VisionTool().execute(image=path, question="extract text"))
        assert obs.success
        assert "VISION" in obs.output.upper() or "OCR" in obs.output.upper()
    finally:
        os.unlink(path)
