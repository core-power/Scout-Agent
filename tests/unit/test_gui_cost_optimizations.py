"""GUI 成本优化（2026-09-08）三项改动的单元测试.

覆盖：
1. desktop.screenshot 屏幕变化检测（未变化短路 / force / 写操作后预期变化警示）
2. desktop.wait 事件等待（until_control 命中与超时、until_title_contains 命中）
3. vision.crop 区域裁剪（参数解析、裁剪落盘、meta 坐标偏移换算）

全部用合成图像/假窗口，不触碰真实桌面与视觉模型。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from scout.core.types import Observation
from scout.tools.builtin import desktop as dmod
from scout.tools.builtin import vision as vmod


async def _fake_call_vision(api_key, base_url, model, image, question, crop=""):
    """VL 定位桩：固定返回截图内坐标 (320, 240)."""
    return Observation(tool_name="vision", success=True, output='{"found": true, "x": 320, "y": 240}')


def _busy_image(w: int = 640, h: int = 480) -> Image.Image:
    """多色纹理图：避免触发空屏守卫（纯色小 PNG）。"""
    img = Image.new("RGB", (w, h))
    img.putdata(
        [
            ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256)
            for y in range(h)
            for x in range(w)
        ]
    )
    return img


@pytest.fixture()
def shot_env(tmp_path, monkeypatch):
    """隔离截图目录与变化检测缓存，屏蔽真实抓屏."""
    monkeypatch.setattr(dmod, "_SHOT_DIR", tmp_path)
    monkeypatch.setattr(dmod, "_SHOT_LAST", {})
    monkeypatch.setattr(dmod, "_SHOT_EXPECT_CHANGE", False)
    import PIL.ImageGrab as IG

    synth = _busy_image()
    monkeypatch.setattr(IG, "grab", lambda all_screens=False: synth.copy())
    return tool


@pytest.fixture()
def tool():
    return dmod.DesktopTool()


# ── ① 截图变化检测 ──────────────────────────────────────────


def test_screenshot_second_identical_short_circuits(tool, shot_env):
    obs1 = asyncio.run(tool._do_screenshot())
    assert obs1.success and "截图已保存" in obs1.output
    obs2 = asyncio.run(tool._do_screenshot())
    assert obs2.success
    assert obs2.metadata.get("unchanged") is True
    assert "屏幕未变化" in obs2.output
    # 复用路径 = 第一张的路径
    assert obs2.metadata["path"] == obs1.metadata["path"]


def test_screenshot_force_overrides_skip(tool, shot_env):
    asyncio.run(tool._do_screenshot())
    obs = asyncio.run(tool._do_screenshot(force=True))
    assert obs.success and obs.metadata.get("unchanged") is not True
    assert "截图已保存" in obs.output


def test_screenshot_expect_change_saves_and_warns(tool, shot_env, monkeypatch):
    asyncio.run(tool._do_screenshot())
    monkeypatch.setattr(dmod, "_SHOT_EXPECT_CHANGE", True)
    obs = asyncio.run(tool._do_screenshot())
    assert obs.success and "截图已保存" in obs.output
    assert "可能未生效" in obs.output
    # 消费后恢复轮询语义
    obs2 = asyncio.run(tool._do_screenshot())
    assert obs2.metadata.get("unchanged") is True


def test_screenshot_changed_content_saves_normally(tool, shot_env, monkeypatch):
    from PIL import ImageOps

    import PIL.ImageGrab as IG

    asyncio.run(tool._do_screenshot())
    inverted = ImageOps.invert(_busy_image())
    monkeypatch.setattr(IG, "grab", lambda all_screens=False: inverted.copy())
    obs = asyncio.run(tool._do_screenshot())
    assert obs.success and "截图已保存" in obs.output
    assert obs.metadata.get("unchanged") is not True


# ── ② wait 事件等待 ──────────────────────────────────────────


class _FakeCtrl:
    def __init__(self, text):
        self._text = text

    def window_text(self):
        return self._text


class _FakeWin:
    def __init__(self, texts):
        self._texts = texts

    def window_text(self):
        return "MainWindow"

    def descendants(self):
        return [_FakeCtrl(t) for t in self._texts]


def test_wait_until_control_hit(tool, monkeypatch):
    monkeypatch.setattr(
        dmod, "_find_wrapper", lambda *a, **k: _FakeWin(["文件", "发送成功", "取消"])
    )
    obs = asyncio.run(tool._do_wait(title="x", until_control="发送成功", timeout=3))
    assert obs.success and "控件已出现" in obs.output


def test_wait_until_control_timeout_with_samples(tool, monkeypatch):
    monkeypatch.setattr(dmod, "_find_wrapper", lambda *a, **k: _FakeWin(["文件", "编辑"]))
    monkeypatch.setattr(dmod, "_poll_control_hit", lambda w, n: (False, ["文件", "编辑"]))
    obs = asyncio.run(tool._do_wait(title="x", until_control="发送成功", timeout=1))
    assert not obs.success
    assert "超时" in obs.output and "文件" in obs.output


def test_wait_until_title_contains_hit(tool, monkeypatch):
    class _FakeDesktop:
        def windows(self):
            return [_FakeWin([]), _TitleWin("导出完成 - Excel")]

    class _TitleWin:
        def __init__(self, t):
            self._t = t

        def window_text(self):
            return self._t

    monkeypatch.setattr(dmod, "_uia_desktop", lambda: _FakeDesktop())
    obs = asyncio.run(tool._do_wait(until_title_contains="导出完成", timeout=2))
    assert obs.success and "窗口已出现" in obs.output


def test_poll_control_hit_real_walk(tool):
    hit, names = dmod._poll_control_hit(_FakeWin(["文件", "发送成功", "取消"]), "发送")
    assert hit and names == ["发送成功"]
    hit2, names2 = dmod._poll_control_hit(_FakeWin(["文件", "编辑", "视图"]), "发送")
    assert not hit2 and "文件" in names2 and len(names2) <= 12


# ── ③ vision crop ───────────────────────────────────────────


def test_parse_crop_variants():
    assert vmod._parse_crop("10,20,30,40", 200, 100) == (10, 20, 30, 40)
    assert vmod._parse_crop("5，6x7*8", 200, 100) == (5, 6, 7, 8)
    assert vmod._parse_crop("1,2,3", 200, 100) is None
    assert vmod._parse_crop("a,b,c,d", 200, 100) is None
    assert vmod._parse_crop("", 200, 100) is None
    # 越界裁剪
    assert vmod._parse_crop("190,90,50,50", 200, 100) == (190, 90, 10, 10)


def test_crop_local_image_with_meta(tmp_path):
    src = tmp_path / "shot.png"
    Image.new("RGB", (200, 100), (10, 20, 30)).save(src)
    meta = {"scale": 0.5, "win_left": 100, "win_top": 50, "shot_w": 200, "shot_h": 100}
    (tmp_path / "shot.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    out, err = vmod._crop_local_image(src, "20,30,50,40")
    assert err == "" and out is not None and out.exists()
    assert Image.open(out).size == (50, 40)
    m2 = json.loads((tmp_path / "shot_crop.meta.json").read_text(encoding="utf-8"))
    # 裁剪偏移按 scale 换算进窗口偏移：100 + 20/0.5 = 140；50 + 30/0.5 = 110
    assert m2["win_left"] == 140 and m2["win_top"] == 110
    assert m2["shot_w"] == 50 and m2["shot_h"] == 40


def test_crop_local_image_no_meta(tmp_path):
    src = tmp_path / "plain.png"
    Image.new("RGB", (80, 60), (255, 0, 0)).save(src)
    out, err = vmod._crop_local_image(src, "0,0,40,30")
    assert err == "" and out is not None and Image.open(out).size == (40, 30)
    # 无 meta 时不生成 meta（click 退化为绝对坐标）
    assert not (tmp_path / "plain_crop.meta.json").exists()


def test_crop_invalid_args_report_error(tmp_path):
    src = tmp_path / "img.png"
    Image.new("RGB", (50, 50), (0, 0, 0)).save(src)
    out, err = vmod._crop_local_image(src, "bad,input")
    assert out is None and err


def test_vision_schema_has_crop():
    props = vmod.VisionTool.parameters["properties"]
    assert "crop" in props
    # ★ 2026-09-15：image 改为可选 —— 留空即自动截屏（把「screenshot → vision」
    # 两次工具调用压缩为一次，GUI 长任务每步省一次 LLM 往返）
    assert vmod.VisionTool.parameters["required"] == ["question"]
    _props = vmod.VisionTool.parameters["properties"]
    assert "window" in _props and "process" in _props, "自动截屏需支持窗口/进程限定"
    assert "image" in _props


# ── ④ locate / find= 一体化定位（2026-09-08 缓做项）──────────────


def test_parse_locate_answer_variants():
    parse = dmod._parse_locate_answer
    assert parse('{"found": true, "x": 120, "y": 88}') == (120, 88)
    assert parse('blah blah\n{"x": 35, "y": 401}') == (35, 401)
    assert parse("x=120, y=88") == (120, 88)
    assert parse("坐标是 120, 88") == (120, 88)
    assert parse('{"found": false, "x": 0, "y": 0}') is None
    assert parse("") is None
    assert parse("完全找不到目标") is None


def test_locate_point_success_converts_coords(tool, shot_env, monkeypatch):
    """VL 返回截图内坐标 → 按 meta scale/偏移换算屏幕坐标."""
    monkeypatch.setattr(
        vmod, "get_vl_config",
        lambda: ("sk-x", "https://x/v1", "vl-model", SimpleNamespace(vision_model="vl-model")),
    )
    monkeypatch.setattr(
        vmod, "_call_vision",
        _fake_call_vision,
    )
    px, py, err = asyncio.run(tool._locate_point("提交按钮", {}))
    assert err is None
    # VL 给 (320, 240)，scale=0.5、无窗口偏移 → (640, 480)
    assert (px, py) == (640, 480)


def test_locate_point_vision_not_configured(tool, shot_env, monkeypatch):
    monkeypatch.setattr(
        vmod, "get_vl_config",
        lambda: ("", "", "", SimpleNamespace(vision_model="")),
    )
    px, py, err = asyncio.run(tool._locate_point("按钮", {}))
    assert (px, py) == (-1, -1) and err is not None
    assert "视觉模型" in err.output


def test_locate_point_not_found_fails_safe(tool, shot_env, monkeypatch):
    async def _nf(api_key, base_url, model, image, question, crop=""):
        return Observation(tool_name="vision", success=True, output='{"found": false, "x": 0, "y": 0}')

    monkeypatch.setattr(
        vmod, "get_vl_config",
        lambda: ("sk-x", "https://x/v1", "vl-model", SimpleNamespace(vision_model="vl-model")),
    )
    monkeypatch.setattr(vmod, "_call_vision", _nf)
    _px, _py, err = asyncio.run(tool._locate_point("不存在的按钮", {}))
    assert err is not None and "未能定位" in err.output


def test_locate_point_out_of_bounds_guard(tool, shot_env, monkeypatch):
    async def _oob(api_key, base_url, model, image, question, crop=""):
        return Observation(tool_name="vision", success=True, output='{"x": 9999, "y": 9999}')

    monkeypatch.setattr(
        vmod, "get_vl_config",
        lambda: ("sk-x", "https://x/v1", "vl-model", SimpleNamespace(vision_model="vl-model")),
    )
    monkeypatch.setattr(vmod, "_call_vision", _oob)
    _px, _py, err = asyncio.run(tool._locate_point("按钮", {}))
    assert err is not None and "超出截图范围" in err.output


def test_execute_find_injects_coords_into_click(tool, shot_env, monkeypatch):
    """click find= → 内部定位 → 坐标注入 x/y → 进入正常点击链路."""
    captured = {}

    async def _fake_locate(find, kwargs):
        return 111, 222, None

    async def _fake_click(**kw):
        captured.update(kw)
        return Observation(tool_name="desktop", success=True, output="clicked")

    monkeypatch.setattr(tool, "_locate_point", _fake_locate)
    monkeypatch.setattr(tool, "_do_click", _fake_click)
    obs = asyncio.run(
        tool.execute(action="click", find="红色提交按钮", rel_x=0.5, rel_y=0.5)
    )
    assert obs.success
    assert captured["x"] == 111 and captured["y"] == 222
    # find/rel 冲突参数已被清除，避免二次换算
    assert "find" not in captured and "rel_x" not in captured


def test_locate_action_requires_find(tool):
    obs = asyncio.run(tool._do_locate())
    assert not obs.success and "find" in obs.output


def test_locate_registered_as_read_action():
    assert "locate" in dmod._READ_ACTIONS
    assert "locate" in dmod._ALL_ACTIONS
    props = dmod.DesktopTool.parameters["properties"]
    assert "find" in props
