"""视觉能力实测探测（scout/llm/vision_probe + /api/models/probe-vision）测试.

设计要点必须被测试钉住：
1. HTTP 200 **不等于**能看图 —— 中转网关常静默丢弃 image_url 后正常回话。所以判据
   是"读出本机生成的随机数字图里的数字"，答对才算 supported。
2. 拿不到可信结论时**绝不落盘**（网络/鉴权/端点错、200 但读不出）：一次断网就把
   模型永久标成"不支持视觉"是不可接受的副作用。
3. 错误分类必须区分"参数报错"与"不收图片"：`temperature not supported` 若被判成
   不支持，会误伤一个其实能看图的模型。
"""

from __future__ import annotations

import io

import pytest

from scout.llm.vision_probe import (
    _extract_digits,
    classify_probe_error,
    make_digit_image,
    make_solid_image,
    probe_vision,
)

pytestmark = pytest.mark.unit


# ── 测试图 ───────────────────────────────────────────────────────


def test_digit_image_is_valid_png_and_deterministic():
    png1, ans1 = make_digit_image(seed=42)
    png2, ans2 = make_digit_image(seed=42)
    assert png1 == png2 and ans1 == ans2, "同种子必须可复现（测试与排查依赖这点）"
    assert png1[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(ans1) == 4 and ans1.isdigit()
    from PIL import Image

    with Image.open(io.BytesIO(png1)) as im:
        assert im.size[0] >= 200 and im.size[1] >= 60, "图太小 VL 会读不清"


def test_solid_image_answer_matches_palette():
    _, name = make_solid_image(seed=3)
    assert name in ("红", "蓝", "绿", "橙", "紫")


def test_digits_avoid_confusable_glyphs():
    """0/1 与 O/l 易混、笔画细，会造成假阴性 —— 字符集里不该出现。"""
    for _seed in range(30):
        _, ans = make_digit_image(seed=_seed)
        assert set(ans) <= set("23456789"), ans


@pytest.mark.parametrize(
    "reply,expect",
    [("图中数字是 4567。", "4567"), ("4567890", "4567"), ("**8899**", "8899"),
     ("没有数字", ""), ("", "")],
)
def test_extract_digits(reply, expect):
    assert _extract_digits(reply) == expect


# ── 错误分类：不能把参数报错当成"不收图片" ─────────────────────────


@pytest.mark.parametrize(
    "status,body",
    [(400, "this model does not support image input"),
     (415, "unsupported media type for image_url"),
     (422, "multimodal content is not allowed"),
     (400, "The requested model does not support vision modality")],
)
def test_classify_unsupported(status, body):
    assert classify_probe_error(status, body) == "unsupported"


@pytest.mark.parametrize(
    "status,body,expect",
    [(400, "temperature not supported", "unknown"),
     (400, "Invalid model name", "unknown"),
     (400, "max_tokens must be <= 4096", "unknown"),
     (429, "rate limited", "unknown"),
     (404, "model not found", "transport"),
     (401, "invalid api key", "auth"),
     (403, "permission denied", "auth")],
)
def test_classify_never_concludes_capability_from_unrelated_errors(status, body, expect):
    assert classify_probe_error(status, body) == expect


# ── probe_vision 决策流（打桩 _ask，不联网）───────────────────────


def _patch_ask(monkeypatch, replies):
    """按调用顺序返回 [(kind, content, status, err)]，并记录每次请求的问题."""
    calls: list[str] = []

    async def fake(api_key, base_url, model, png, question, timeout):
        calls.append(question)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    monkeypatch.setattr("scout.llm.vision_probe._ask", fake)
    return calls


async def test_probe_supported_when_digits_read_correctly(monkeypatch):
    _, ans = make_digit_image(seed=1234)
    _patch_ask(monkeypatch, [("ok", f"{ans}", 200, "")])
    r = await probe_vision("sk-x", "https://x/v1", "m", seed=1234)
    assert r["result"] == "supported" and r["verdict"] is True


async def test_probe_http200_but_blind_is_unverified_not_supported(monkeypatch):
    """★ 核心：网关吞掉图片、正常回一句客套话 → 不能判 supported。"""
    _patch_ask(monkeypatch, [("ok", "你好，我在的。", 200, ""), ("ok", "这是白色", 200, "")])
    r = await probe_vision("sk-x", "https://x/v1", "m", seed=1)
    assert r["result"] == "unverified"
    assert r["verdict"] is None, "读不出内容时不得落盘为「支持」"


async def test_probe_falls_back_to_color_round(monkeypatch):
    """数字认错但颜色答对 → 判 supported（区分"能看不识数"与"根本没看"）."""
    _, color = make_solid_image(seed=5)
    calls = _patch_ask(monkeypatch, [("ok", "0000", 200, ""), ("ok", f"这是{color}色", 200, "")])
    r = await probe_vision("sk-x", "https://x/v1", "m", seed=5)
    assert r["result"] == "supported" and r["verdict"] is True
    assert len(calls) == 2, "应补一轮纯色图再下结论"


async def test_probe_explicit_rejection_is_false(monkeypatch):
    _patch_ask(monkeypatch, [("error", "", 400, "this model does not support image input")])
    r = await probe_vision("sk-x", "https://x/v1", "m", seed=1)
    assert r["result"] == "unsupported" and r["verdict"] is False


@pytest.mark.parametrize(
    "status,err",
    [(0, "ConnectTimeout"), (401, "invalid api key"), (404, "model not found")],
)
async def test_probe_infra_failures_write_nothing(monkeypatch, status, err):
    _patch_ask(monkeypatch, [("error", "", status, err)])
    r = await probe_vision("sk-x", "https://x/v1", "m", seed=1)
    assert r["verdict"] is None and r["result"] in ("transport", "auth")


# ── 探测结果参与路由（探测优先级最高）─────────────────────────────


def test_probe_result_overrides_name_guess():
    from types import SimpleNamespace

    from scout.llm.vision_route import capability_key, native_vision

    m = "my-vision-like-4o"  # 名字像多模态
    c = SimpleNamespace(model_vision_probe={capability_key("custom", m): False},
                        model_vision_mode={}, model_vision_overrides={})
    assert native_vision("custom", m, c) == (False, "probe")


# ── 端点：只在有可信结论时落盘 ───────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from scout.config import manager as config_manager_mod

    monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(config_manager_mod, "CONFIG_DIR", tmp_path, raising=False)
    from fastapi.testclient import TestClient

    from scout.web.server import create_web_app

    return TestClient(create_web_app())


def _probe_result(verdict, result="supported", detail="x"):
    async def fake(api_key, base_url, model, timeout=25.0, seed=None):
        return {"result": result, "verdict": verdict, "detail": detail, "rounds": []}

    return fake


def test_probe_endpoint_persists_only_trusted_verdicts(client, monkeypatch):
    import scout.llm.vision_probe as vp

    # 可信：True → 落盘
    monkeypatch.setattr(vp, "probe_vision", _probe_result(True))
    r = client.post("/api/models/probe-vision",
                    json={"provider": "custom", "model": "m1", "api_key": "sk-real",
                          "base_url": "https://x/v1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["written"] is True and body["capabilities"]["vision_native"] is True
    assert body["capabilities"]["vision_source"] == "probe"
    from scout.config import ConfigManager

    assert ConfigManager().load().model_vision_probe[body["capability_key"]] is True

    # 不可信：None → 不落盘，且不得覆盖已有结论
    monkeypatch.setattr(vp, "probe_vision", _probe_result(None, "transport", "断网"))
    r2 = client.post("/api/models/probe-vision",
                     json={"provider": "custom", "model": "m1", "api_key": "sk-real",
                           "base_url": "https://x/v1"})
    assert r2.status_code == 200
    assert r2.json()["written"] is None
    assert ConfigManager().load().model_vision_probe["custom:m1"] is True, "旧结论被冲掉了"


def test_probe_endpoint_requires_key(client, monkeypatch):
    import scout.llm.vision_probe as vp

    called = {"n": 0}

    async def spy(*a, **k):
        called["n"] += 1
        return {"result": "supported", "verdict": True, "detail": "", "rounds": []}

    monkeypatch.setattr(vp, "probe_vision", spy)
    r = client.post("/api/models/probe-vision", json={"provider": "nosuch", "model": "m"})
    assert r.status_code == 400, r.text
    assert called["n"] == 0, "没有 Key 时不该发出任何真实请求"
