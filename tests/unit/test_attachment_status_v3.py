"""V3：附件送达状态回传与前端徽标的数据契约测试.

后端把"图片到底有没有被模型看到"写进用户消息 metadata（`attachments_status`），
经 WS `done` 事件与会话历史接口透传，前端据此在用户气泡上打徽标。本文件钉住
状态结构本身（前端渲染依赖这些字段），以及"绝不让用户以为图被读了"这条底线。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from scout.engine.context_inject import ContextInjectMixin

pytestmark = pytest.mark.unit


def make_agent(monkeypatch, provider="dashscope", model="qwen3.8-27b", **cfg_kw):
    """只测 mixin：把 Agent 上的 `_vision_route` 绑到实例上（生产中二者总是组合使用）.

    ★ 配置打桩必须走 monkeypatch.setattr —— 直接赋值 `ConfigManager.load = ...`
    会污染整个测试会话（我第一次写就踩了，67 个后续用例集体变红）。
    """
    from scout.engine.agent import Agent

    a = ContextInjectMixin.__new__(ContextInjectMixin)
    a.model_provider = provider
    a.llm = SimpleNamespace(model=model)
    a.vision_input = None
    a._vision_route = lambda: Agent._vision_route(a)
    cfg = SimpleNamespace(
        provider=provider, model=model, base_url="", vision_provider="",
        vision_model="", model_vision_overrides={}, model_vision_mode={},
        model_vision_probe={}, **cfg_kw,
    )
    import scout.config as cfg_pkg

    monkeypatch.setattr(cfg_pkg.ConfigManager, "load", lambda self: cfg)
    return a




def img(tmp_path, name="a.png"):
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", (300, 200), (200, 30, 30)).save(p, "PNG")
    return {"name": name, "path": str(p), "type": "image/png"}


# ── 状态结构 ─────────────────────────────────────────────────────


def test_main_route_reports_all_delivered(tmp_path, monkeypatch):
    a = make_agent(monkeypatch, model="gpt-4o", provider="openai")
    atts = [img(tmp_path, "x.png")]
    st = a._image_delivery_status(atts, {"path": "main", "reason": "支持", "needs_choice": False})
    assert st["route"] == "main" and st["total"] == 1 and st["delivered"] == 1
    assert st["unseen"] == [] and st["degraded"] is False


def test_none_route_marks_every_image_unseen(tmp_path, monkeypatch):
    a = make_agent(monkeypatch, provider="deepseek", model="deepseek-chat")
    atts = [img(tmp_path, "x.png"), img(tmp_path, "y.png")]
    st = a._image_delivery_status(
        atts, {"path": "none", "reason": "主模型不支持图片输入，且该厂商没有可用视觉模型",
               "needs_choice": False}
    )
    assert st["delivered"] == 0 and len(st["unseen"]) == 2
    assert all(u["reason"] for u in st["unseen"]), "每条未送达都必须有原因"
    assert st["degraded"] is True


def test_missing_file_is_reported_not_silently_dropped(tmp_path, monkeypatch):
    a = make_agent(monkeypatch, model="gpt-4o", provider="openai")
    atts = [{"name": "gone.png", "path": str(tmp_path / "gone.png"), "type": "image/png"}]
    st = a._image_delivery_status(atts, {"path": "main", "reason": "", "needs_choice": False})
    assert st["delivered"] == 0
    assert st["unseen"][0]["name"] == "gone.png" and "不存在" in st["unseen"][0]["reason"]


def test_over_count_images_listed_individually(tmp_path, monkeypatch):
    from scout.engine.agent import _IMAGE_MAX_COUNT

    a = make_agent(monkeypatch, model="gpt-4o", provider="openai")
    atts = [img(tmp_path, f"i{i}.png") for i in range(_IMAGE_MAX_COUNT + 2)]
    st = a._image_delivery_status(atts, {"path": "main", "reason": "", "needs_choice": False})
    assert st["delivered"] == _IMAGE_MAX_COUNT
    assert len(st["unseen"]) == 2
    assert all("上限" in u["reason"] for u in st["unseen"])


# ── 兜底识图路径：状态由识图循环产出 ─────────────────────────────


async def test_fallback_route_reports_described_count(tmp_path, monkeypatch):
    async def fake_call(api_key, base_url, model, image, question, crop=""):
        return SimpleNamespace(success=True, output="一张红色方块")

    monkeypatch.setattr("scout.tools.builtin.vision._call_vision", fake_call)
    monkeypatch.setattr("scout.tools.builtin.vision.get_vl_config",
                        lambda: ("sk-x", "https://x/v1", "qwen-vl-max", None))
    a = make_agent(monkeypatch, provider="dashscope", model="qwen3.8-27b")
    box: dict = {}
    out = await a._describe_images_via_fallback([img(tmp_path)], status_out=box)
    st = box["status"]
    assert "<image_descriptions" in out
    assert st["route"] == "fallback" and st["delivered"] == 1
    assert st["degraded"] is True, "识图成文字属于降级，前端必须显示提示"


async def test_fallback_vl_failure_is_counted_as_unseen(tmp_path, monkeypatch):
    async def fail(api_key, base_url, model, image, question, crop=""):
        return SimpleNamespace(success=False, output="403 AccessDenied")

    monkeypatch.setattr("scout.tools.builtin.vision._call_vision", fail)
    monkeypatch.setattr("scout.tools.builtin.vision.get_vl_config",
                        lambda: ("sk-x", "https://x/v1", "qwen-vl-max", None))
    a = make_agent(monkeypatch, provider="dashscope", model="qwen3.8-27b")
    box: dict = {}
    await a._describe_images_via_fallback([img(tmp_path)], status_out=box)
    st = box["status"]
    assert st["delivered"] == 0 and len(st["unseen"]) == 1
    assert "识图失败" in st["unseen"][0]["reason"]


async def test_none_route_still_tells_the_model_it_cannot_see(tmp_path, monkeypatch):
    a = make_agent(monkeypatch, provider="deepseek", model="deepseek-chat")
    box: dict = {}
    out = await a._describe_images_via_fallback([img(tmp_path)], status_out=box)
    assert "<attachments_unseen" in out
    assert "严禁描述或推测图片内容" in out
    assert box["status"]["delivered"] == 0


# ── 前端契约：字段名与已编译样式类 ───────────────────────────────

HTML = None


def _html():
    global HTML
    if HTML is None:
        from pathlib import Path

        HTML = (Path(__file__).resolve().parents[2] / "scout" / "web" / "static"
                / "index.html").read_text(encoding="utf-8")
    return HTML


def test_frontend_consumes_the_documented_fields():
    h = _html()
    assert "event.data.attachments" in h, "done 事件的 attachments 字段没被消费"
    assert "m.attachments_status" in h, "历史渲染没透传 attachments_status"
    for field in ("st.route", "st.unseen", "st.total"):
        assert field in h, f"前端未使用 {field}，字段改名会静默失效"


def test_badge_classes_are_already_compiled_into_app_css():
    """本项目 CSS 是预编译产物且本机无 node —— 写了新工具类不会进产物，表现是样式静默失效。

    所以徽标/提示用到的类必须逐个存在于 app.css。改样式时先查后写。
    """
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "scout" / "web" / "static" / "css"
           / "app.css").read_text(encoding="utf-8", errors="replace")

    def esc(cls: str) -> str:
        return "".join("\\" + ch if ch in '/[]().:%' else ch for ch in cls)

    used = ["text-tiny", "px-2", "py-0.5", "rounded-md", "border", "text-danger",
            "border-danger/30", "bg-danger/10", "transition-opacity", "hover:opacity-90",
            "text-warn", "border-warn/30", "bg-warn/10", "text-ink-3", "mt-1", "text-xs",
            "underline", "hidden", "w-full", "self-end"]
    missing = [c for c in used
               if not re.search(r"(?<![\w-])\." + re.escape(esc(c)) + r"(?![\w-])", css)]
    assert not missing, f"这些 Tailwind 类不在编译产物里，样式会静默失效: {missing}"


def test_sessions_api_exposes_attachments_status():
    """历史接口必须透传该字段，否则重开会话就看不出哪几轮图没被读过."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "scout" / "adapters" / "web"
           / "routes" / "sessions.py").read_text(encoding="utf-8")
    assert 'attachments_status' in src
