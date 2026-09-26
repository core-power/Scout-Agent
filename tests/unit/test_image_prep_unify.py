"""图片预处理统一（V2）测试：scout/llm/image_prep + 三条送模型路径的接线.

必须钉住的行为（2026-09-26 实测驱动）：
1. 附件内联与兜底识图此前**完全不做降采样**（只有 vision 工具做），实测 4 张常见
   附件请求体 10.5 MB；
2. 磁盘 >5MB 的图此前被**静默跳过**，模型不知道有图 → 降采样后本可送达；
3. 优化绝不能让图片**变大**或**消失**（扁平 UI 截图原图 59.6 KB，降到 2048 重编码
   反而 93.2 KB —— 这种图必须保留原图）；
4. 结论要缓存：`_build_api_messages` 每个 ReAct 迭代都会重建消息，单张降采样实测
   0.3~2.1 s，不缓存就是每轮重复付费。
"""

from __future__ import annotations

import base64
import glob
import os

import pytest
from PIL import Image

from scout.llm.image_prep import build_image_part, prepare_image

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_cache(tmp_path, monkeypatch):
    """把缓存目录指到 tmp，并保证用例之间互不污染."""
    cache = tmp_path / "image_cache"
    cache.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("scout.llm.image_prep._cache_dir", lambda: str(cache))
    yield cache


def _photo(tmp_path, name, w, h, fmt="JPEG"):
    """造一张"有内容"的图：渐变 + 噪声块（纯色会被压得极小，测不出真实体积）."""
    import random

    rnd = random.Random(11)
    im = Image.new("RGB", (w, h))
    px = im.load()
    step = max(1, min(w, h) // 200)
    for y in range(0, h, step):
        for x in range(0, w, step):
            c = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
            for yy in range(y, min(y + step, h)):
                for xx in range(x, min(x + step, w)):
                    px[xx, yy] = c
    p = tmp_path / name
    im.save(p, fmt, quality=90)
    return p


# ── prepare_image 本体 ───────────────────────────────────────────


def test_small_image_passes_through_untouched(tmp_path):
    p = _photo(tmp_path, "small.png", 640, 400, "PNG")
    r = prepare_image(str(p))
    assert r.usable and r.resized is False
    assert r.path == str(p), "够小够矮的图不该被重编码（重编码会让小字发虚）"


def test_big_photo_is_shrunk(tmp_path):
    p = _photo(tmp_path, "phone.jpg", 3000, 2200)
    orig = os.path.getsize(p)
    r = prepare_image(str(p), max_edge=1024, budget=200_000)
    assert r.resized is True
    assert max(r.width, r.height) <= 1024
    assert r.nbytes < orig, "降采样后必须真的变小"
    assert os.path.dirname(r.path) == os.path.dirname(p) or "image_cache" in r.path


def test_result_is_never_larger_than_original(tmp_path):
    """扁平 UI 截图：PNG 已极小，重编码反而变大 → 必须保留原图."""
    im = Image.new("RGB", (2560, 1440), (245, 246, 248))
    p = tmp_path / "flat.png"
    im.save(p, "PNG")
    orig = os.path.getsize(p)
    r = prepare_image(str(p), max_edge=1024, budget=orig * 10)
    assert r.nbytes <= orig, f"优化后变大：{orig} -> {r.nbytes}"


def test_repeated_calls_hit_cache(tmp_path):
    p = _photo(tmp_path, "cache.jpg", 2600, 1800)
    r1 = prepare_image(str(p))
    files = glob.glob(os.path.join(os.path.dirname(r1.path), "*"))
    r2 = prepare_image(str(p))
    assert r2.path == r1.path, "同图同参数必须复用同一份结果"
    assert glob.glob(os.path.join(os.path.dirname(r1.path), "*")) == files


def test_missing_file_reports_reason(tmp_path):
    r = prepare_image(str(tmp_path / "nope.png"))
    assert not r.usable and "不存在" in (r.skipped or "")


def test_corrupt_file_fails_open(tmp_path):
    p = tmp_path / "corrupt.png"
    p.write_bytes(b"definitely not a png")
    r = prepare_image(str(p))
    assert r.path == str(p), "解码失败必须放行原图，绝不让图片消失"


def test_alpha_png_kept_or_flattened(tmp_path):
    p = tmp_path / "icon.png"
    Image.new("RGBA", (1600, 1600), (10, 200, 30, 128)).save(p, "PNG")
    r = prepare_image(str(p), max_edge=512, budget=400_000)
    assert r.resized is False or max(r.width, r.height) <= 512
    assert r.mime in ("image/png", "image/jpeg")


def test_hard_ceiling_skips_absurd_file(tmp_path, monkeypatch):
    p = tmp_path / "huge.png"
    p.write_bytes(b"\x00" * (3 * 1024 * 1024))
    monkeypatch.setattr("scout.llm.image_prep.HARD_SKIP_BYTES", 1024 * 1024)
    r = prepare_image(str(p))
    assert not r.usable and "硬上限" in (r.skipped or "")


def test_build_image_part_shape(tmp_path):
    p = _photo(tmp_path, "part.png", 800, 600, "PNG")
    part, prep = build_image_part(str(p))
    assert part and part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/")
    assert base64.b64decode(url.split(",", 1)[1])[:4] in (b"\x89PNG", b"\xff\xd8")
    missing, prep2 = build_image_part(str(tmp_path / "none.png"))
    assert missing is None and not prep2.usable


# ── 接线：聊天附件内联 ───────────────────────────────────────────


def _agent_stub():
    from unittest.mock import MagicMock

    from scout.engine.agent import Agent

    a = Agent(MagicMock())
    a.vision_input = True  # 强制走"主模型直收"，本组用例只测附件构造
    return a


def test_inline_parts_use_prepared_bytes(tmp_path):
    a = _agent_stub()
    p = _photo(tmp_path, "big.jpg", 3000, 2200)
    parts = a._image_content_parts(
        [{"name": "big.jpg", "path": str(p), "type": "image/jpeg"}], "看看这张图"
    )
    imgs = [x for x in parts if x.get("type") == "image_url"]
    assert len(imgs) == 1
    payload = len(imgs[0]["image_url"]["url"])
    assert payload < os.path.getsize(p) * 1.34 * 0.9, (
        f"内联请求体没有比原图 base64 更小（{payload}），降采样未生效"
    )
    assert any(x.get("type") == "text" and "看看这张图" in x["text"] for x in parts)


def test_over_count_images_are_reported_not_silently_dropped(tmp_path):
    from scout.engine.agent import _IMAGE_MAX_COUNT

    a = _agent_stub()
    atts = []
    for i in range(_IMAGE_MAX_COUNT + 2):
        atts.append({"name": f"i{i}.png",
                     "path": str(_photo(tmp_path, f"i{i}.png", 400, 300, "PNG")),
                     "type": "image/png"})
    parts = a._image_content_parts(atts, "多张图")
    imgs = [x for x in parts if x.get("type") == "image_url"]
    notes = [x["text"] for x in parts if x.get("type") == "text" and "附件提示" in x["text"]]
    assert len(imgs) == _IMAGE_MAX_COUNT
    assert notes and "未送达" in notes[0], "超出张数必须明确告知模型"


def test_missing_attachment_is_reported(tmp_path):
    a = _agent_stub()
    parts = a._image_content_parts(
        [{"name": "gone.png", "path": str(tmp_path / "gone.png"), "type": "image/png"}],
        "图呢",
    )
    text = " ".join(x.get("text", "") for x in parts if x.get("type") == "text")
    assert "未送达" in text and "gone.png" in text


# ── 接线：vision 工具与兜底识图 ──────────────────────────────────


def test_vision_downscale_wrapper_passthrough_and_no_user_dir_pollution(tmp_path):
    from scout.tools.builtin.vision import _downscale_for_vision

    assert _downscale_for_vision("https://x/a.png") == "https://x/a.png"
    assert _downscale_for_vision("data:image/png;base64,xx").startswith("data:")
    assert _downscale_for_vision(str(tmp_path / "missing.png")) == str(tmp_path / "missing.png")

    p = _photo(tmp_path, "shot.png", 2600, 1700, "PNG")
    out = _downscale_for_vision(str(p), max_edge=800)
    assert out != str(p)
    assert "_vision_ds_" not in os.path.basename(out), "不该再往用户目录写临时副本"
    assert list(tmp_path.glob("_vision_ds_*")) == []


async def test_fallback_description_sends_prepared_image(tmp_path, monkeypatch):
    """兜底识图路径以前把**原始路径**交给 VL（绕过降采样）—— 现在必须送预处理后的图."""
    import asyncio
    from types import SimpleNamespace

    from scout.engine import context_inject as ci

    p = _photo(tmp_path, "big2.jpg", 3000, 2200)
    seen: list[str] = []

    async def fake_call(api_key, base_url, model, image, question, crop=""):
        seen.append(image)
        return SimpleNamespace(success=True, output="描述文本")

    monkeypatch.setattr("scout.tools.builtin.vision._call_vision", fake_call)
    monkeypatch.setattr("scout.tools.builtin.vision.get_vl_config",
                        lambda: ("sk-x", "https://x/v1", "vl-model", None))

    agent = _agent_stub()
    agent.vision_input = None
    agent.model_provider = "deepseek"
    agent.llm = SimpleNamespace(model="deepseek-chat")
    import scout.config as cfg_pkg

    monkeypatch.setattr(cfg_pkg.ConfigManager, "load", lambda self: SimpleNamespace(
        provider="deepseek", model="deepseek-chat", base_url="", vision_provider="",
        vision_model="", model_vision_overrides={}, model_vision_mode={},
        model_vision_probe={}, vision_disabled=False,
    ))
    # deepseek 无厂商兜底 → 路由判 none，不会走识图；这里显式造一个有兜底的厂商
    monkeypatch.setattr(cfg_pkg.ConfigManager, "load", lambda self: SimpleNamespace(
        provider="dashscope", model="qwen3.8-27b", base_url="", vision_provider="",
        vision_model="", model_vision_overrides={}, model_vision_mode={},
        model_vision_probe={}, vision_disabled=False,
    ))
    agent.model_provider = "dashscope"
    agent.llm = SimpleNamespace(model="qwen3.8-27b")

    out = await ci.ContextInjectMixin._describe_images_via_fallback(
        agent, [{"name": "big2.jpg", "path": str(p), "type": "image/jpeg"}]
    )
    assert seen, "应发起一次识图调用"
    assert "<image_descriptions" in out
    assert seen[0] != str(p), "送给 VL 的必须是降采样后的文件，而不是原图"
