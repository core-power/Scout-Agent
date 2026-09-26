"""桌面自动化工具 — 操控本机 GUI 应用（窗口/控件/键鼠/截图/剪贴板/拖拽）.

定位（2026-09-03）：补齐 Agent 能力版图的 L2 层——shell 管命令行、browser 管
网页、PTY 管终端，本工具管**真实桌面 GUI**（微信/QQ/任意 Win32/UWP 窗口）。

技术路线（全走 pywinauto，无 pyautogui 依赖）：
- 窗口枚举/查找/激活/关闭：UIA 后端（UWP/新版应用可见）
- 控件读写：UIA 树 descendants 定位（按 标题/自动化ID/控件类型）
- 键盘：pywinauto.keyboard.send_keys —— 底层 SendInput(KEYEVENTF_UNICODE)，
  **中文输入可用**（pyautogui.typewrite 仅 ASCII，故弃用）
- 鼠标坐标：pywinauto.mouse（click/double/right/scroll，真实事件）
- 截图：PIL.ImageGrab 全屏 / wrapper.capture_as_image() 单窗口
- 剪贴板（2026-09-04 新增）：ctypes 直写 CF_UNICODETEXT/CF_HDROP——
  聊天应用"发文件"官方支持通道 = 文件入剪贴板 + Ctrl+V，比拖拽/点自绘
  上传按钮都稳；clip_read 让 Agent 能感知用户刚复制的内容
- 拖拽（2026-09-04 新增）：mouse press → 分步 move → release，步进产生
  真实 WM_MOUSEMOVE（目标应用靠 move 事件做 Hit-Test，瞬移会被丢弃）

安全边界：
- launch 仅 os.startfile（无命令行拼接，杜绝注入）
- close_window 走 WM_CLOSE 温和关闭（不强杀进程）
- 写操作（click/type/press_key/close）会作用于真实桌面——全局审批由
  policy.needs_approval / auto_approve 语义兜底；工具级 read 操作（list/
  find/read_controls/screenshot/clip_read）无副作用。
"""

from __future__ import annotations

import asyncio
import ctypes.wintypes as wt  # noqa: F401  —  供 _paste_text 使用
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import (
    ToolDefinition,
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_INTERNAL,
    ERROR_TIMEOUT,
)
from scout.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ★ 2026-09-14 平台守卫：desktop 仅 Windows（pywinauto/Win32 UIA）。
# 非导入即炸（pywinauto 均为函数内惰性导入），但注册与执行都按此开关。
_IS_WINDOWS = sys.platform == "win32"

# 写操作名单（便于上层策略/审计识别；工具内仅作日志标记）
_WRITE_ACTIONS = {
    "activate", "launch", "close_window", "click", "double_click",
    "right_click", "click_control", "type_control", "type_text",
    "press_key", "scroll", "drag", "click_type", "copy_file", "macro",
    "set_date",
}

_READ_ACTIONS = {
    "list_windows", "find_window", "active_window", "read_controls",
    "screenshot", "wait", "clip_read", "probe", "locate",
}

# 支持 find=<自然语言目标> 的点击类动作（内部截图→VL 定位→注入坐标点击）
_FIND_CLICK_ACTIONS = {"click", "double_click", "right_click", "click_type"}

_ALL_ACTIONS = sorted(_READ_ACTIONS | _WRITE_ACTIONS)

# read_controls / list_windows 输出上限（防爆屏）
_MAX_WINDOWS = 40
_MAX_CONTROLS = 60
_CTRL_TEXT_LEN = 80

# macro（确定性序列宏）上限——防 LLM 一次塞爆/失控
_MACRO_MAX_STEPS = 30
_MACRO_TOTAL_TIMEOUT = 300.0  # 整体墙钟预算（秒）
_MACRO_STEP_LOG_LEN = 240  # 每步回执单行截断长度

# 截图目录（数据目录下，与 browser 截图同区）
_SHOT_DIR = Path(_SCOUT_DATA_DIR) / "screenshots"

# ── 屏幕变化检测（2026-09-08）：与同范围上一张做像素 diff，几乎未变化则短路 ──
# 背景：GUI 任务高频出现"操作→截图→读图"循环；界面实际未变（等加载/点击无效）
# 时重复截图+vision 是纯浪费（VL 实测 16-21s/次 + 一轮 LLM 往返）。
# - 写操作后的第一张截图永不跳过（"操作未生效"是关键信息），仅附警示；
# - 之后（纯状态轮询）未变化 → 直接返回上一张路径，连新文件都不落盘；
# - force=true 强制新截图；SCOUT_SHOT_SKIP_UNCHANGED=0 一键回滚。
_SHOT_SIG_SIZE = (160, 100)  # 对比用统一缩放尺寸（与 scale 参数无关，跨 scale 稳定）
_SHOT_DIFF_TH = 2.0          # 平均像素差阈值（0-255）：光标闪烁/抗锯齿噪声远低于此
_SHOT_SKIP_ENABLED = os.getenv("SCOUT_SHOT_SKIP_UNCHANGED", "1") != "0"
_SHOT_LAST: dict[str, dict] = {}  # scope -> {"sig": Image, "path": str}，每范围仅留最近一张
_SHOT_LAST_MAX = 8                # 防长会话窗口句柄累积
_SHOT_EXPECT_CHANGE = False       # 写操作成功后置 True：下一张截图必须真实落盘


def _shot_signature(img):
    """固定小尺寸灰度签名图（PIL），供相邻帧像素 diff."""
    return img.convert("L").resize(_SHOT_SIG_SIZE)


def _img_mean_diff(a, b) -> float:
    """两签名图的平均像素差（0-255）；尺寸不一致返回 255（视为已变化）."""
    if a.size != b.size:
        return 255.0
    from PIL import ImageChops

    hist = ImageChops.difference(a, b).histogram()
    total = sum(hist)
    if not total:
        return 255.0
    return sum(i * c for i, c in enumerate(hist)) / total


# ── VL 定位（2026-09-08）：find=<自然语言目标> → 截图→VL→坐标→(点击) ──
# 定位决策从 screenshot→vision→click 三次工具调用 2~3 轮 LLM 往返压缩为一次调用。
# grounding 交给 VL 专用模型（与"chat 模型像素 grounding 不可靠"结论不冲突）；
# snap 吸附仍生效——T1 应用点击自动校正到控件中心。
_LOCATE_PROMPT = (
    "在截图中定位目标元素: {target}\n"
    "找到后返回该元素**中心点**在图片内的像素坐标。只输出一行 JSON，格式严格为:\n"
    '{{"found": true, "x": <中心x>, "y": <中心y>}}\n'
    '找不到或不确定时只输出: {{"found": false, "x": 0, "y": 0}}\n'
    "不要输出任何其他文字。"
)


# ── UIA 兜底定位：自然语言目标 → 控件名匹配词提取（2026-09-10）──
_UIA_STOPWORDS = (
    "按钮", "输入框", "文本框", "下拉框", "复选框", "单选框", "图标", "控件",
    "菜单项", "菜单", "选项", "标签页", "标签", "区域", "位置", "那个", "这个", "一个",
)
_UIA_ADJWORDS = (
    "红色", "蓝色", "绿色", "黄色", "白色", "黑色", "灰色", "紫色",
    "大的", "小的", "顶部", "底部", "左侧", "右侧", "上方", "下方",
)


def _extract_locate_needles(find: str) -> list[str]:
    """从自然语言目标提取控件名匹配词（UIA 兜底定位用）.

    "红色提交按钮" → ["提交", "红色提交"]（先试去形容词的干净词，命中率高）
    """
    s = (find or "").strip()
    for w in _UIA_STOPWORDS:
        s = s.replace(w, "")
    needles: list[str] = []
    stripped = s
    for w in _UIA_ADJWORDS:
        stripped = stripped.replace(w, "")
    if len(stripped) >= 2:
        needles.append(stripped)
    if s and s != stripped and len(s) >= 2:
        needles.append(s)
    return needles


def _grab_per_monitor(virtual_rect: tuple[int, int, int, int]):
    """逐显示器抓屏并按物理坐标拼接（混合 DPI 多屏兜底，2026-09-11）.

    ImageGrab.grab(all_screens=True) 在**混合缩放多屏**（如主屏 150% +
    副屏 100%）上可能被系统 DISPLAY DC 按主屏 DPI 拉伸副屏区域，截图像素
    与虚拟屏 rect 不对齐 → 坐标换算错位。逐屏抓取（bbox=该屏物理 rect，
    DISPLAY DC 原生虚拟屏坐标）后拼到虚拟屏画布，绕开整屏 DC 的拉伸问题。
    返回 PIL Image 或 None。
    """
    try:
        import ctypes

        from PIL import Image, ImageGrab

        u32 = ctypes.windll.user32
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_long * 4), ctypes.c_void_p,
        )
        mons: list[tuple[int, int, int, int]] = []

        def _cb(hmon, hdc, lprect, _):
            r = lprect.contents
            mons.append((int(r[0]), int(r[1]), int(r[2]), int(r[3])))
            return True

        u32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
        if not mons:
            return None
        vl, vt, vr, vb = virtual_rect
        canvas = Image.new("RGB", (vr - vl, vb - vt))
        pasted = 0
        for l, t, r, b in mons:
            try:
                shot = ImageGrab.grab(bbox=(l, t, r, b))
                if shot.size != (r - l, b - t):
                    # 该屏 DC 尺寸与物理 rect 不符（DPI 拉伸）→ 对齐缩放保底
                    shot = shot.resize((r - l, b - t))
                canvas.paste(shot, (l - vl, t - vt))
                pasted += 1
            except Exception:  # noqa: BLE001 — 单屏失败继续其余屏
                continue
        return canvas if pasted == len(mons) else None
    except Exception:  # noqa: BLE001
        return None


# ── 粗→细两段定位（2026-09-11）：弱 VL 模型在局部小图上定位成功率显著更高 ──
_COARSE_PROMPT = (
    "在截图中找到 {target} 所在的大致区域。"
    "只输出一行 JSON，格式严格为: "
    '{{"found": true, "left": <0-100>, "top": <0-100>, "right": <0-100>, "bottom": <0-100>}}'
    "（数值为相对截图宽高的百分比，框要略大于目标本身）。"
    "找不到或不确定时只输出: "
    '{{"found": false}}'
    "不要输出任何其他文字。"
)


def _parse_coarse_answer(text: str) -> tuple[float, float, float, float] | None:
    """解析粗定位回答 → (left, top, right, bottom) 0~1 比例；无效返回 None."""
    if not text:
        return None
    m = re.search(r"\{[^}]+\}", text, re.S)
    if not m:
        return None
    try:
        import json

        d = json.loads(m.group(0))
        if not d.get("found", True):
            return None
        l = max(0.0, min(100.0, float(d["left"]))) / 100
        t = max(0.0, min(100.0, float(d["top"]))) / 100
        r = max(0.0, min(100.0, float(d["right"]))) / 100
        b = max(0.0, min(100.0, float(d["bottom"]))) / 100
        if r <= l or b <= t:
            return None
        if (r - l) < 0.03 or (b - t) < 0.03:
            return None  # 区域过小（可疑）
        if (r - l) > 0.98 and (b - t) > 0.98:
            return None  # 全图 = 无信息量
        return (l, t, min(r, 1.0), min(b, 1.0))
    except Exception:  # noqa: BLE001
        return None


def _crop_by_region(src: Path, region: tuple[float, float, float, float]) -> Path | None:
    """按 0~1 比例区域裁剪截图并落盘到源图旁（粗→细定位用）."""
    try:
        from PIL import Image

        img = Image.open(src)
        w, h = img.size
        l, t, r, b = region
        box = (max(0, int(l * w)), max(0, int(t * h)), min(w, int(r * w)), min(h, int(b * h)))
        if box[2] - box[0] < 10 or box[3] - box[1] < 10:
            return None
        out = src.with_name(src.stem + "_crop.png")
        img.crop(box).save(str(out))
        return out
    except Exception:  # noqa: BLE001
        return None


# ── OCR 文本锚定兜底（2026-09-10，T3 自绘应用定位）──
# 2026-09-07 曾移除 RapidOCR 依赖（减负 160MB，用户决策）；此处为"定位"场景
# 动态导入：源码环境有依赖则启用（文本目标按 OCR 框中心点击，比 VL 像素
# 定位可靠），发布包无依赖则自动禁用——importlib 动态加载不会被
# PyInstaller 静态分析收编，包体积零变化。
_OCR_ENGINE: object | None = None


def _get_ocr_engine():
    """惰性加载 RapidOCR 引擎（模块级单例；不可用返回 None）."""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            import importlib

            mod = importlib.import_module("rapidocr_onnxruntime")
            _OCR_ENGINE = mod.RapidOCR()
        except Exception:  # noqa: BLE001 — 依赖缺失时静默禁用
            _OCR_ENGINE = False
    return _OCR_ENGINE or None


def _parse_locate_answer(text: str) -> tuple[int, int] | None:
    """从 VL 回答解析目标图片内坐标 (x, y).

    兼容 JSON / x=..y=.. / 裸数字对；明确 found:false 或解析失败返回 None。
    """
    if not text:
        return None
    low = text.lower()
    if re.search(r'["\']?found["\']?\s*[:=]\s*false', low):
        return None
    best: tuple[int, int] | None = None
    for m in re.finditer(r"\{[^{}]*\}", text):
        s = m.group(0)
        mx = re.search(r'["\']x["\']\s*[:=]\s*(\d+)', s)
        my = re.search(r'["\']y["\']\s*[:=]\s*(\d+)', s)
        if mx and my:
            best = (int(mx.group(1)), int(my.group(1)))
    if best:
        return best
    mx = re.search(r"\bx\s*[=＝:：]\s*\(?\s*(\d{1,5})", text, re.I)
    my = re.search(r"\by\s*[=＝:：]\s*\(?\s*(\d{1,5})", text, re.I)
    if mx and my:
        return int(mx.group(1)), int(my.group(1))
    pairs = re.findall(r"(?<![\d.])(\d{1,5})\s*[,，]\s*(\d{1,5})(?![\d.])", text)
    if pairs:
        x, y = pairs[-1]
        return int(x), int(y)
    return None


def _poll_control_hit(w, needle: str) -> tuple[bool, list[str]]:
    """wait 事件轮询辅助：任一控件文本包含 needle 即命中.

    返回 (是否命中, 用于超时提示的最近控件文本样例≤12)."""
    needle_l = (needle or "").lower()
    names: list[str] = []
    try:
        ctrls = w.descendants()
    except Exception:  # noqa: BLE001 — 自绘 UI/树读取失败按未命中处理
        return False, []
    for c in ctrls:
        try:
            txt = (c.window_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not txt:
            continue
        if needle_l in txt.lower():
            return True, [txt]
        if txt not in names and len(names) < 12:
            names.append(txt)
    return False, names

_DPI_AWARE_DONE = False

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _has_cjk(text: str) -> bool:
    """含 CJK 字符（微信等 Qt/Chromium 应用对 SendInput unicode 输入"只显示不触发"，须走剪贴板）."""
    return bool(_CJK_RE.search(text or ""))


def _ensure_dpi_aware() -> None:
    """声明进程 DPI 感知（PER_MONITOR_AWARE_V2），统一物理像素坐标系.

    根因（2026-09-03 实测）：4K 屏 + 150% 缩放机器上，未感知进程的鼠标坐标会被
    Windows 虚拟化（3840x2160 物理 → 2560x1440 逻辑），而 PIL 截图始终是物理
    像素 → 视觉模型按截图返回的坐标经 mouse.click 后整体偏移（150% 时点哪儿
    偏哪儿），表现为"根本操控不到"。声明感知后三套坐标（截图/UIA rect/鼠标）
    全部统一为物理像素。进程级一次性设置，成功后不可撤销（无需撤销）。
    """
    global _DPI_AWARE_DONE
    if _DPI_AWARE_DONE:
        return
    _DPI_AWARE_DONE = True
    try:
        import ctypes

        u32 = ctypes.windll.user32
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if not u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            u32.SetProcessDPIAware()  # Win8.1 以下降级
        # ★ 2026-09-11 成功性验证：exe 壳进程可能因 CLR/已有窗口抢先而设置失败，
        # 默默继续会让鼠标/窗口坐标被虚拟化（150% 缩放下全错位）——显式告警。
        val = ctypes.c_int(-1)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        hr = ctypes.windll.shcore.GetProcessDpiAwareness(ctypes.c_void_p(h), ctypes.byref(val))
        if hr == 0 and val.value < 2:
            logger.warning(
                "DPI 感知未生效（状态=%s）——高 DPI/多屏环境下坐标将被虚拟化，"
                "点击会整体偏移！请检查 launcher 启动日志。",
                val.value,
            )
    except Exception:  # noqa: BLE001 — 设置失败不阻断工具，仅坐标可能受影响
        logger.warning("DPI awareness 设置失败（高 DPI 屏上坐标可能偏移）", exc_info=True)


def _uia_desktop():
    """UIA 后端 Desktop（惰性导入，缺包时给出可读错误）."""
    try:
        from pywinauto import Desktop
    except ImportError as e:
        raise RuntimeError(
            "pywinauto 未安装 — 请执行: pip install pywinauto Pillow"
        ) from e
    return Desktop(backend="uia")


_PID_NAME_CACHE: dict[int, str] = {}


def _proc_name(pid) -> str:
    """pid → 进程名（UIAElementInfo 无 process_name，需反查；带缓存）."""
    if not pid:
        return ""
    pid = int(pid)
    if pid not in _PID_NAME_CACHE:
        try:
            import psutil

            _PID_NAME_CACHE[pid] = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 — 进程已退出/权限不足
            _PID_NAME_CACHE[pid] = ""
    return _PID_NAME_CACHE[pid]


def _paste_text(text: str) -> bool:
    """文本经剪贴板粘贴（SetClipboardData + Ctrl+V）.

    背景（2026-09-03 实测）：微信 4.x 自定义搜索框对 SendInput 注入的
    unicode 字符"只显示不触发"（字进去了但搜索逻辑不跑——Qt 应用监听
    keydown/IME 而非 WM_CHAR）。剪贴板粘贴是完整事件链，实测可靠触发。
    """
    try:
        import ctypes

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        # 64 位指针必须显式声明（否则句柄被截断为 32 位 → GlobalLock 返回 0）
        k32.GlobalAlloc.restype = ctypes.c_void_p
        k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        k32.GlobalLock.restype = ctypes.c_void_p
        k32.GlobalLock.argtypes = [ctypes.c_void_p]
        k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        k32.GlobalFree.argtypes = [ctypes.c_void_p]

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        if not u32.OpenClipboard(0):
            return False
        try:
            u32.EmptyClipboard()
            n = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            h = k32.GlobalAlloc(GMEM_MOVEABLE, n)
            if not h:
                return False
            p = k32.GlobalLock(ctypes.c_void_p(h))
            if not p:
                k32.GlobalFree(ctypes.c_void_p(h))
                return False
            ctypes.memmove(p, ctypes.create_unicode_buffer(text), n)
            k32.GlobalUnlock(ctypes.c_void_p(h))
            if not u32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h)):
                k32.GlobalFree(ctypes.c_void_p(h))
                return False
        finally:
            u32.CloseClipboard()
        # 粘贴路径优先级（2026-09-03 实测）：
        # Chromium/Electron 类（飞书/微信 4.x/VS Code）：**只认 SendInput ^v**，
        #   不处理 WM_PASTE（SendMessage 不会抛异常→不能据此判断成功，
        #   实测因此常"假成功"漏走 ^v）
        # 标准 Win32 Edit/RichEdit：WM_PASTE 直接（比 ^v 更稳）
        # 检测焦点控件窗口类名分流
        pasted = False
        try:
            u32.GetGUIThreadInfo.argtypes = [wt.DWORD, ctypes.c_void_p]
            u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
            u32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            u32.GetClassNameW.restype = ctypes.c_int

            class _GUITHREADINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD),
                            ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                            ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND),
                            ("hwndMoveSize", wt.HWND), ("hwndCaret", wt.HWND),
                            ("rcCaret", ctypes.c_int * 4)]

            fg = u32.GetForegroundWindow()
            tid = u32.GetWindowThreadProcessId(fg, None)
            gti = _GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
            focus_hwnd = gti.hwndFocus or gti.hwndActive or fg if u32.GetGUIThreadInfo(tid, ctypes.byref(gti)) else fg
            cls = ctypes.create_unicode_buffer(128)
            u32.GetClassNameW(ctypes.c_void_p(focus_hwnd), cls, 128)
            class_name = (cls.value or "").lower()
            # 标准 Win32 输入控件类名 → WM_PASTE 直发
            is_standard = class_name in ("edit", "richedit", "richedit20w", "richedit20a")
            # Chromium/Electron/通用兜底：Chrome_WidgetWin_1/Chrome_RenderWidgetHostHWND/Qt 等
            if is_standard:
                u32.SendMessageW(ctypes.c_void_p(focus_hwnd), 0x0302, 0, 0)  # WM_PASTE
                pasted = True
            else:
                # Chromium/通用路径用 SendInput ^v（Chromium 只认这个）
                from pywinauto.keyboard import send_keys as _keys

                _keys("^v")
                pasted = True
        except Exception:  # noqa: BLE001
            pass
        if not pasted:
            try:
                from pywinauto.keyboard import send_keys

                send_keys("^v")
                pasted = True
            except Exception:  # noqa: BLE001
                pass
        return pasted
    except Exception:  # noqa: BLE001
        return False


def _copy_files_to_clipboard(paths: list[str]) -> bool:
    """文件列表写入剪贴板（CF_HDROP）——聊天应用"发文件"的官方支持通道.

    背景（2026-09-04）：微信/QQ/飞书发文件只剩两条难路——上传按钮是自绘控件
    （UIA 不可达），SendInput 合成拖拽过 Hit-Test 不稳。把文件放进剪贴板再
    Ctrl+V 是三大 IM 全部官方支持的路径，实测最稳。
    实现：DROPFILES 结构（fWide=1 宽字符）+ 双 \\0 结尾路径表 → CF_HDROP。
    """
    import ctypes

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalAlloc.restype = ctypes.c_void_p
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    k32.GlobalFree.argtypes = [ctypes.c_void_p]

    class _DROPFILES(ctypes.Structure):
        _fields_ = [("pFiles", ctypes.c_uint32), ("pt", ctypes.c_int32 * 2),
                    ("fNC", ctypes.c_uint32), ("fWide", ctypes.c_uint32)]

    files: list[str] = []
    for p in paths or []:
        try:
            ap = str(Path(p).expanduser().resolve())
        except OSError:  # 非法路径字符
            return False
        if not Path(ap).exists():
            return False
        files.append(ap)
    if not files:
        return False

    raw = ("\0".join(files) + "\0\0").encode("utf-16-le")
    size = ctypes.sizeof(_DROPFILES) + len(raw)
    if not u32.OpenClipboard(0):
        return False
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
        if not h:
            return False
        p = k32.GlobalLock(ctypes.c_void_p(h))
        if not p:
            k32.GlobalFree(ctypes.c_void_p(h))
            return False
        try:
            df = _DROPFILES()
            df.pFiles = ctypes.sizeof(_DROPFILES)
            df.fWide = 1
            ctypes.memmove(p, ctypes.byref(df), ctypes.sizeof(_DROPFILES))
            ctypes.memmove(ctypes.c_void_p(p + ctypes.sizeof(_DROPFILES)), raw, len(raw))
        finally:
            k32.GlobalUnlock(ctypes.c_void_p(h))
        if not u32.SetClipboardData(15, ctypes.c_void_p(h)):  # CF_HDROP=15
            k32.GlobalFree(ctypes.c_void_p(h))
            return False
        return True
    finally:
        u32.CloseClipboard()


def _read_clipboard() -> tuple[str, list[str]]:
    """读取剪贴板：返回 (文本, 文件路径列表)，二者至多一个非空.

    文件优先（CF_HDROP 更结构化）：用户在资源管理器 Ctrl+C 文件后，
    Agent 可直接拿到路径列表用于"发文件/打包/分析"等任务。
    """
    import ctypes

    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    # 64 位进程必须显式声明指针宽返回值（默认 int 会截断句柄 → DragQueryFileW 拿坏句柄返回 0）
    u32.GetClipboardData.restype = ctypes.c_void_p
    u32.GetClipboardData.argtypes = [ctypes.c_uint]
    shell32 = ctypes.windll.shell32
    shell32.DragQueryFileW.restype = ctypes.c_uint
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]

    if not u32.OpenClipboard(0):
        return "", []
    try:
        if u32.IsClipboardFormatAvailable(15):  # CF_HDROP
            h = u32.GetClipboardData(15)
            if h:
                n = shell32.DragQueryFileW(ctypes.c_void_p(h), 0xFFFFFFFF, None, 0)
                files = []
                for i in range(int(n)):
                    ln = shell32.DragQueryFileW(ctypes.c_void_p(h), i, None, 0)
                    buf = ctypes.create_unicode_buffer(int(ln) + 1)
                    shell32.DragQueryFileW(ctypes.c_void_p(h), i, buf, int(ln) + 1)
                    files.append(buf.value)
                if files:
                    return "", files
        if u32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
            h = u32.GetClipboardData(13)
            if h:
                p = k32.GlobalLock(ctypes.c_void_p(h))
                if p:
                    try:
                        return ctypes.wstring_at(p), []
                    finally:
                        k32.GlobalUnlock(ctypes.c_void_p(h))
        return "", []
    finally:
        u32.CloseClipboard()


def _force_foreground(hwnd: int, settle_ms: int = 600) -> bool:
    """Win32 API 强制窗口前台（带轮询防抢；已在前台时零成本短路）.

    背景（2026-09-03 实测）：
    1) pywinauto set_focus 对 Electron/Chromium 多进程窗口（飞书/微信）报告成功但
       GetForegroundWindow 不是它——其他进程窗口抢走了前台
    2) 单次 SetForegroundWindow 后焦点会被其他窗口抢走——必须轮询守住
    3) 性能（2026-09-04 实测）：已在前台时全流程仍耗 404ms——每次 rel 点击
       白付。先查前台归属，命中直接返回（~0ms）。
    """
    try:
        import ctypes

        u32 = ctypes.windll.user32
        # ★ 前台短路：目标已在前台 → 无需任何强抢动作
        if u32.GetForegroundWindow() == hwnd:
            return True
        u32.AllowSetForegroundWindow(-1)
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE（最小化时恢复）
        # 多次强抢：BringWindowToTop + SetForegroundWindow + SwitchToThisWindow
        for _ in range(5):
            u32.BringWindowToTop(hwnd)
            if u32.SetForegroundWindow(hwnd):
                u32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.08)
        # 稳定化轮询：检查前台是否还是它（防别的窗口抢走）
        deadline = time.time() + settle_ms / 1000.0
        last_ok = False
        while time.time() < deadline:
            cur = u32.GetForegroundWindow()
            if cur == hwnd:
                last_ok = True
                break
            # 重新抢一次
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
        # 最终状态快照
        final = u32.GetForegroundWindow() == hwnd
        return last_ok or final
    except Exception:  # noqa: BLE001
        return False


def _printwindow_capture(hwnd: int):
    """PrintWindow(PW_RENDERFULLCONTENT) 抓指定窗口 → PIL Image；失败返回 None.

    背景（2026-09-03 实测）：本机存在系统级屏幕 DC 拦截（BitBlt/GetDC(0) 返回
    ACCESS_DENIED，企业 DLP/安全软件典型行为），PIL ImageGrab 与 pywinauto
    capture_as_image 均依赖屏幕 DC 而失败。PrintWindow 走 WM_PRINT 消息让
    窗口自绘到我们的 DC，不碰屏幕 DC，实测畅通且 Chromium/DirectUI 窗口
    （PW_RENDERFULLCONTENT=2）内容完整。
    """
    try:
        import ctypes
        from PIL import Image

        class _RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                        ("r", ctypes.c_long), ("b", ctypes.c_long)]

        class _BMIH(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]

        u32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        rc = _RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rc))
        w, h = rc.r - rc.l, rc.b - rc.t
        if w <= 0 or h <= 0:
            return None
        hdc = u32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        try:
            ok = u32.PrintWindow(hwnd, mdc, 2) or u32.PrintWindow(hwnd, mdc, 0)
            if not ok:
                return None
            bmi = _BMIH()
            bmi.biSize = ctypes.sizeof(_BMIH)
            bmi.biWidth, bmi.biHeight = w, -h  # top-down
            bmi.biPlanes, bmi.biBitCount = 1, 32
            bmi.biCompression = 0  # BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            if not gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
                return None
            return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            u32.ReleaseDC(hwnd, hdc)
    except Exception:  # noqa: BLE001
        return None


def _win_pid(w) -> int:
    try:
        return int(w.process_id())
    except Exception:  # noqa: BLE001
        try:
            return int(getattr(w.element_info, "process_id", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0


def _img_to_png_bytes(img) -> bytes:
    """PIL Image → PNG 字节（供空屏守卫测编码体积）."""
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _focus_summary() -> str:
    """当前焦点控件摘要（类名+文本）——点击后回读供 agent 校验落点.

    准确率增强（2026-09-04）：坐标点击是否命中预期控件，靠截图验证要一轮
    vision（5-60s）；回读焦点控件类名/文本 ~1ms，agent 立即可判断（如点击
    搜索框后焦点应在 Edit 类控件上，仍 Pane 则说明点偏了）。
    """
    try:
        import ctypes

        u32 = ctypes.windll.user32

        class _GTI(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint),
                        ("hwndActive", ctypes.c_void_p), ("hwndFocus", ctypes.c_void_p),
                        ("hwndCapture", ctypes.c_void_p), ("hwndMenu", ctypes.c_void_p),
                        ("hwndMoveSize", ctypes.c_void_p), ("hwndCaret", ctypes.c_void_p),
                        ("rc", ctypes.c_int * 4)]

        gti = _GTI()
        gti.cbSize = ctypes.sizeof(_GTI)
        if not u32.GetGUIThreadInfo(0, ctypes.byref(gti)) or not gti.hwndFocus:
            return ""
        cls = ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(ctypes.c_void_p(gti.hwndFocus), cls, 128)
        txt = ctypes.create_unicode_buffer(128)
        u32.GetWindowTextW(ctypes.c_void_p(gti.hwndFocus), txt, 128)
        return f"focus={cls.value or '?'}" + (f"\"{txt.value[:40]}\"" if txt.value else "")
    except Exception:  # noqa: BLE001
        return ""


def _enum_top_windows_by_process(process: str) -> list:
    """EnumWindows 深度枚举（含隐藏/托盘化窗口）按进程名匹配顶层窗口.

    背景（2026-09-03 实测）：Desktop.windows() 只返回可见顶层窗口——微信最小化
    到托盘后主窗口 visible=False 且不在其返回列表里，导致 process 匹配永远
    失败。本函数用 Win32 EnumWindows 枚举全部顶层窗口（含隐藏），按 pid→
    进程名匹配，返回 pywinauto UIA wrapper 列表（托盘窗口在其中，可恢复）。
    """
    try:
        import ctypes

        import psutil
        from pywinauto import Desktop

        pl = process.lower()
        pids = {
            p.pid for p in psutil.process_iter(["name"])
            if pl in (p.info.get("name") or "").lower()
        }
        if not pids:
            return []
        u32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _):
            pid = ctypes.c_ulong()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and not u32.GetParent(hwnd):
                # 过滤消息辅助窗口（微信的 PowerMessageWindow/TrayIcon 等）
                cls = ctypes.create_unicode_buffer(128)
                u32.GetClassNameW(hwnd, cls, 128)
                cn = cls.value or ""
                if any(k in cn for k in ("PowerMessageWindow", "TrayIcon", "SystemMessageWindow")):
                    return True
                found.append(hwnd)
            return True

        u32.EnumWindows(_cb, 0)
        if not found:
            return []
        d = Desktop(backend="uia")
        return [d.window(handle=h) for h in found]
    except Exception:  # noqa: BLE001
        return []


# 窗口 wrapper 短 TTL 缓存（2026-09-04 性能实测：find_wrapper 155~833ms/次，
# 同窗口连续操作（点击→输入→回车）每次重枚举是纯浪费）。
# key=(process.lower(), title, index) → (wrapper, hwnd, expires_at)。
# IsWindow 实时校验 hwnd 有效性——窗口销毁/重建立即失效，TTL 只影响"新窗口出现"
# 的感知延迟（2s 内可接受）。
_WIN_CACHE: dict[tuple, tuple] = {}
_WIN_CACHE_TTL = 2.0

# 启动后等待窗口出现的上限（秒）★ 2026-09-14：把 wait 内联进 launch 结果，
# 省去模型「launch → 未就绪 → wait → activate」的一次往返（GUI 任务每次往返
# 都是一个 LLM 步，且常伴随截图/vision 重观察）。
_LAUNCH_WAIT_SECONDS = 6.0


# ── UIA 控件树缓存（2026-09-15，性能）──────────────────────────────
# 背景：read_controls 每次都执行 w.descendants() 从根遍历整棵控件树，再对**全部**
# 控件排序后只取前 N 个。飞书等自绘应用的树很大，而 UIA 取属性是跨进程 COM 调用，
# 于是"读一次飞书"的开销极高，且短时间内重复读取（模型连续几步都在看同一窗口）
# 完全是重复劳动——用户反馈"UIA 全量重建、无缓存、耗时且重复"。
# 设计：按 (hwnd, depth, control, control_type) 缓存**已渲染好的文本行**（不缓存
# 跨进程控件对象，避免悬挂引用）；用窗口"轻量指纹"（标题 + 矩形）校验是否仍然
# 对应当前界面，TTL 内直接复用。需要最新结构时传 force_refresh=true 绕过缓存。
_UIA_TREE_CACHE: dict[tuple, tuple] = {}
_UIA_TREE_TTL = 2.5


def _win_light_fingerprint(hwnd: int) -> tuple:
    """窗口轻量指纹：标题 + 屏幕矩形（不触碰控件树，开销可忽略）."""
    try:
        import ctypes

        u = ctypes.windll.user32
        h = ctypes.c_void_p(hwnd)
        if not u.IsWindow(h):
            return ()
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(h, buf, 512)

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        r = _RECT()
        u.GetWindowRect(h, ctypes.byref(r))
        return (buf.value, r.left, r.top, r.right, r.bottom)
    except Exception:  # noqa: BLE001
        return ()


def _uia_tree_get(ck: tuple, fp: tuple) -> list[str] | None:
    """命中且指纹一致、未过期 → 返回缓存的控件行；否则 None（并清理失效项）."""
    if not fp:
        return None
    ent = _UIA_TREE_CACHE.get(ck)
    if not ent:
        return None
    _fp, lines, exp = ent
    if _fp != fp or time.time() > exp:
        _UIA_TREE_CACHE.pop(ck, None)
        return None
    return lines


def _uia_tree_put(ck: tuple, fp: tuple, lines: list[str]) -> None:
    """写入缓存（简单容量上限，避免长会话无界增长）."""
    if not fp or not lines:
        return
    try:
        if len(_UIA_TREE_CACHE) > 64:
            for k in list(_UIA_TREE_CACHE)[:32]:
                _UIA_TREE_CACHE.pop(k, None)
        _UIA_TREE_CACHE[ck] = (fp, lines, time.time() + _UIA_TREE_TTL)
    except Exception:  # noqa: BLE001
        pass


def _cached_wrapper(process: str, title: str, title_re: bool, index: int):
    """命中缓存且 hwnd 仍有效则返回 wrapper，否则 None."""
    if title_re or (not process and not title):
        return None
    key = ((process or "").lower(), title, index)
    ent = _WIN_CACHE.get(key)
    if not ent:
        return None
    w, hwnd, exp = ent
    if time.time() > exp:
        _WIN_CACHE.pop(key, None)
        return None
    try:
        import ctypes

        if not ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd)):
            _WIN_CACHE.pop(key, None)
            return None
    except Exception:  # noqa: BLE001
        _WIN_CACHE.pop(key, None)
        return None
    return w


def _cache_wrapper(w, process: str, title: str, index: int) -> None:
    try:
        _WIN_CACHE[((process or "").lower(), title, index)] = (w, w.handle, time.time() + _WIN_CACHE_TTL)
    except Exception:  # noqa: BLE001
        pass


def _load_shot_meta(img_path: str) -> dict | None:
    """读取截图元数据（截图工具保存的同名 .meta.json）.

    用于把视觉读数的截图内坐标自动换算为屏幕坐标：物理点 =
    (win_left, win_top) 屏幕偏移 + 截图坐标 / scale。
    无 meta（截图被清理/来自旧版/URL 图片）→ 返回 None，调用方按原坐标处理。
    """
    if not img_path:
        return None
    try:
        import json
        p = Path(img_path)
        mp = p.with_suffix(".meta.json")
        if not mp.exists():
            return None
        m = json.loads(mp.read_text(encoding="utf-8"))
        if not isinstance(m, dict):
            return None
        m.setdefault("scale", 1.0)
        m.setdefault("win_left", 0)
        m.setdefault("win_top", 0)
        m.setdefault("shot_w", -1)
        m.setdefault("shot_h", -1)
        return m
    except Exception:  # noqa: BLE001 — meta 损坏视为不存在
        return None


def _find_wrapper(
    title: str = "", title_re: bool = False, index: int = 0,
    timeout: float = 0.0, process: str = "",
):
    """按 标题/进程名 找窗口 wrapper；找不到返回 None（timeout 秒内重试）.

    - title_re=False: 标题精确匹配（推荐配 process 用）
    - title_re=True:  标题正则匹配
    - process: 进程名子串匹配（不区分大小写），如 "Weixin"/"WeChat"/"Feishu"。
      微信等应用标题随聊天对象变化，按进程找最稳；title 为空时仅按 process 过滤。
    - 2026-09-04 性能：先查 2s TTL 缓存（IsWindow 校验），命中省 155~833ms。
    """
    cached = _cached_wrapper(process, title, title_re, index)
    if cached is not None:
        return cached
    deadline = time.time() + max(0.0, timeout)
    while True:
        try:
            d = _uia_desktop()
            wins = d.windows()
            if process:
                pl = process.lower()
                wins = [x for x in wins if pl in _proc_name(_win_pid(x)).lower()]
                # 可见枚举无果 → 深度枚举（微信托盘化后 Desktop.windows() 不含它）
                if not wins:
                    wins = _enum_top_windows_by_process(process)
                    if wins:
                        # 托盘窗口：恢复第一个（SW_RESTORE + 前台）
                        try:
                            import ctypes

                            hwnd = wins[0].handle
                            ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            ctypes.windll.user32.SetForegroundWindow(hwnd)
                            time.sleep(0.4)
                        except Exception:  # noqa: BLE001
                            pass
            if title:
                if title_re:
                    matches = [x for x in wins if re.search(title, x.window_text() or "")]
                else:
                    matches = [x for x in wins if (x.window_text() or "") == title]
            else:
                matches = wins  # 仅按 process 找
            # 可见过滤（隐藏窗口不可交互），除非全部隐藏
            visible = []
            for x in matches:
                try:
                    if x.is_visible():
                        visible.append(x)
                except Exception:  # noqa: BLE001
                    visible.append(x)
            if visible:
                result = visible[min(index, len(visible) - 1)]
                _cache_wrapper(result, process, title, index)
                return result
            if matches:
                # 全部隐藏 = 最小化到托盘（微信/QQ 常见）→ 恢复第一个再返回
                try:
                    import ctypes

                    hwnd = matches[0].handle
                    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
                    time.sleep(0.3)
                    _cache_wrapper(matches[0], process, title, index)
                    return matches[0]
                except Exception:  # noqa: BLE001
                    return matches[0]
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001 — 枚举抖动（窗口正在销毁）视为未找到
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _win_summary(w) -> str:
    pid = ""
    pname = ""
    try:
        pid = w.process_id()
        pname = _proc_name(pid) or "?"
    except Exception:  # noqa: BLE001
        pass
    # 窗口状态标记（2026-09-04）：最小化窗口的 rect 是 (-32000,-32000)，
    # 对其做 rel 坐标点击会点到屏幕外——agent 看到 [min] 应先 activate 再操作
    state = ""
    try:
        if w.is_minimized():
            state = " [min]"
        elif w.is_maximized():
            state = " [max]"
    except Exception:  # noqa: BLE001
        pass
    return f"[{w.handle}] pid={pid} exe={pname}{state} \"{(w.window_text() or '')[:60]}\""


def _ctrl_line(c, with_center: bool = False) -> str:
    try:
        t = c.window_text() or ""
        ei = c.element_info
        # win32 经典树（backend="win32"）无 control_type/automation_id——降级取类名
        ctype = getattr(ei, "control_type", None) or ""
        auto_id = getattr(ei, "automation_id", None) or ""
        if not ctype:
            try:
                ctype = c.friendly_class_name()
            except Exception:  # noqa: BLE001
                ctype = ""
        line = (
            f"- {ctype} | name=\"{t[:_CTRL_TEXT_LEN]}\" "
            f"| auto_id=\"{auto_id}\""
        )
        # 物理矩形（DPI 感知后 = 真实屏幕像素，可直接用于 click x/y）
        try:
            r = c.rectangle()
            line += f" | rect=({r.left},{r.top},{r.width()},{r.height()})"
            # read_controls 输出带中心点：T1/T2 应用免截图免 vision 直接可点
            if with_center:
                line += f" | center=({r.left + r.width() // 2},{r.top + r.height() // 2})"
        except Exception:  # noqa: BLE001
            pass
        # 输入类控件：读实际内容（ValuePattern 优先；UWP 应用 texts() 只回 Name）
        ctype = ctype.lower()
        if ctype in ("edit", "document"):
            content = ""
            try:
                content = str(c.get_value() or "")  # ValuePattern（多数输入框支持）
            except Exception:  # noqa: BLE001
                pass
            if not content.strip():
                try:
                    vals = c.texts()
                    if vals and vals[0].strip():
                        content = str(vals[0])
                except Exception:  # noqa: BLE001
                    pass
            if content.strip():
                line += f" | text=\"{content[:_CTRL_TEXT_LEN]}\""
        return line
    except Exception:  # noqa: BLE001
        return "- <control gone>"


# UIA 容器型控件（坐标命中/吸附时跳过——点容器中心不是用户意图，容易误触）
_SNAP_SKIP_TYPES = {
    "pane", "window", "titlebar", "group", "list", "tree", "table",
    "document", "scrollbar", "menubar", "toolbar", "statusbar",
    "splitter", "custom",  # 自绘大区域（微信/腾讯会议整窗常为 Custom）
}

# win32 经典树（backend="win32"）类名级跳过的容器（无交互意义的巨区/装饰）
_SNAP_SKIP_W32 = {
    "#32770",  # 对话框本体
    "mdiclient", "scrollbar", "statusbar", "rebarwindow32",
    "tooltips_class32", "sysheader32", "grip",
    # 2026-09-08：Chromium 渲染子窗口（近整窗巨区）——微信 4.x/Electron 应用
    # UIA 空树时 win32 兜底会吸到它中心，离目标极远
    "chrome_widgetwin_1", "chrome_renderwidgethosthwnd",
}


def _win32_wrapper(w):
    """给 UIA 窗口建 win32 经典 backend wrapper（老式 Delphi/MFC/VB6 的控件树）.

    背景（2026-09-05 通用化）：很多老式 Windows 软件在 UIA 下是空树，
    但子窗口其实是标准 HWND（类名 Button/Edit/SysListView32 等），用
    pywinauto backend="win32" 才能看到。建失败返回 None（可能非老式）。
    """
    try:
        from pywinauto import Application

        hwnd = w.handle
        app = Application(backend="win32").connect(handle=hwnd, timeout=3)
        return app.window(handle=hwnd)
    except Exception:  # noqa: BLE001
        return None


def _hit_w32(w32, x: int, y: int):
    """win32 经典树命中：含点 (x,y) 的最小子窗口控件（老式应用 UIA 空树时用）.

    2026-09-08：补近整窗面积守卫（与 UIA 侧 _snap_control 的 0.6 对齐）——
    此前无守卫，含点的近整窗渲染子窗口会被选中并吸到其中心。
    """
    try:
        wr = w32.rectangle()
        w_area = (wr.right - wr.left) * (wr.bottom - wr.top)
    except Exception:  # noqa: BLE001
        w_area = 0
    try:
        ctrls = w32.descendants()
    except Exception:  # noqa: BLE001
        return None
    best = None
    best_area = None
    for c in ctrls:
        try:
            r = c.rectangle()
            if not (r.left <= x < r.right and r.top <= y < r.bottom):
                continue
            try:
                cls = (c.class_name() or "").lower()
            except Exception:  # noqa: BLE001
                cls = ""
            if cls in _SNAP_SKIP_W32:
                continue
        except Exception:  # noqa: BLE001
            continue
        area = (r.right - r.left) * (r.bottom - r.top)
        # 近整窗子窗口（渲染巨区）不参与吸附
        if w_area > 0 and area >= w_area * 0.6:
            continue
        if best is None or area < best_area:
            best, best_area = c, area
    return best


def _hit_control(w, x: int, y: int):
    """返回包含点 (x,y) 的最小可交互 UIA 控件；无则 None（点空/自绘区）.

    背景（2026-09-05）：vision/rel 目测坐标常差几个像素导致点错/点偏。
    对原生控件树用"矩形包含 + 面积最小"选中最深叶子控件，即可把落点
    吸附到控件中心（probe 先行确认，click snap=true 自动校正）。
    自绘应用（微信 4.x 等无控件树）遍历近乎空树，快速返回 None。
    """
    try:
        ctrls = w.descendants()
    except Exception:  # noqa: BLE001
        return None
    best = None
    best_area = None
    for c in ctrls:
        try:
            r = c.rectangle()
            if not (r.left <= x < r.right and r.top <= y < r.bottom):
                continue
            if (c.element_info.control_type or "").lower() in _SNAP_SKIP_TYPES:
                continue
            # 2026-09-08：矩形健全性 + 可见性 —— 最小化(-32000)/零尺寸/不可见
            # 控件（虚拟列表所有 item 同 rect、隐藏层）参与吸附会把点击拉偏
            if r.left <= -30000 or r.top <= -30000 or r.right <= r.left or r.bottom <= r.top:
                continue
            try:
                if not c.is_visible():
                    continue
            except Exception:  # noqa: BLE001 — 可见性判定失败不阻塞
                pass
        except Exception:  # noqa: BLE001
            continue
        area = (r.right - r.left) * (r.bottom - r.top)
        if best is None or area < best_area:
            best, best_area = c, area
    return best


def _window_at_point(px: int, py: int):
    """返回包含屏幕点的最小可见顶层窗口 wrapper.

    2026-09-08：无 title/process 上下文的坐标点击，此前 _find_wrapper("","")
    会返回"第一个可见窗口"（UIA 枚举序）——点视觉上落在 B 窗口上，吸附却
    发生在 A 窗口的控件上，实际点击落错窗口。改为按矩形包含选最小窗口。
    """
    try:
        wins = _uia_desktop().windows()
    except Exception:  # noqa: BLE001
        return None
    best = None
    best_area = None
    for w in wins:
        try:
            if not w.is_visible():
                continue
            r = w.rectangle()
            if r.left <= px < r.right and r.top <= py < r.bottom:
                area = (r.right - r.left) * (r.bottom - r.top)
                if best is None or area < best_area:
                    best, best_area = w, area
        except Exception:  # noqa: BLE001
            continue
    return best


def _snap_control(w, x: int, y: int, max_ratio: float = 0.6):
    """把 (x,y) 吸附到命中最深控件中心；返回 (cx, cy, control|None).

    None 表示未吸附（无控件树 / 点空白 / 命中控件过大如近整窗 Pane），
    此时调用方保持原坐标点击并如实报告。
    """
    c = _hit_control(w, x, y)
    if c is None:
        return x, y, None
    try:
        r = c.rectangle()
        wr = w.rectangle()
        w_area = (wr.right - wr.left) * (wr.bottom - wr.top)
        if w_area > 0 and (r.right - r.left) * (r.bottom - r.top) >= w_area * max_ratio:
            return x, y, None  # 控件近整窗（容器残留）——吸附无意义，不点中心
        return (
            r.left + (r.right - r.left) // 2,
            r.top + (r.bottom - r.top) // 2,
            c,
        )
    except Exception:  # noqa: BLE001
        return x, y, None


class DesktopTool(ToolDefinition):
    """本机桌面 GUI 自动化（Windows）."""

    name = "desktop"
    description = (
        "Control desktop GUI apps on Windows — THE tool for any GUI task. Never drive GUIs via shell.\n"
        "ROUTE: GUI → this tool; CLI → shell; reminders → scheduler; absent-user/boot → shell "
        "schtasks (current user needs no admin; NEVER /RU SYSTEM for GUI).\n"
        "FLOW: find_window/list_windows → activate → read_controls (UIA names+rects+center, click "
        "by center, no screenshot) or screenshot→vision→click(img=<path>) for self-drawn UIs "
        "(WeChat 4.x has NO UIA tree → rel_x/rel_y + type_text/press_key).\n"
        "SPEED: (a) click_type = click+type+keys in ONE call; (b) verify_screenshot=true attaches "
        "post-action shot; (c) vision-read coords → click img=<shot path> auto-converts (NEVER "
        "hand-multiply; screen=true if coords are already screen coords); (d) vision only at "
        "decision points, trust tool results; same-window ops hit a 2s cache; (e) wait "
        "until_control/until_title_contains replaces screenshot+vision for 'did it load' checks "
        "(zero tokens); (f) screenshot short-circuits when unchanged (reuses previous path; "
        "force=true override); after a write action '[变化检测] 几乎一致' warning = MISSED "
        "action, not success; (g) click find=<target> / locate = one-call VL grounding (use only "
        "when no UIA tree and you must look at the screen).\n"
        "CLICK ACCURACY: click-family auto-SNAPs to the UIA control center under the point "
        "(snap=false disables); drag start snaps, drag END is exact (rel_x2/rel_y2 same-window, "
        "x2/y2 cross-window); scroll takes rel too. probe classifies apps: [T1-UIA] → "
        "click_control/type_control by name or coords+snap; [T2-Win32 legacy (Delphi/MFC)] → "
        "same by-name tools auto-retry the win32 tree; [T3-self-drawn] → screenshot → rel → "
        "click snap=false → verify → macro. probe reports tier, control name+rect, rel≈, and "
        "warns if the point is outside the window rect. Cheap workflow: probe → confirm → click. "
        "After clicking, reply includes focus=<ClassName> (a Pane on a native app = MISSED → "
        "adjust, don't type blindly). Prefer rel_x/rel_y over vision pixel coords.\n"
        "WINDOWS: prefer process= over title= (WeChat process 'Weixin'; title = chat name, "
        "changes). [min] windows have off-screen rects — activate first. One DPI-aware "
        "physical-pixel system: screenshot coords = control rects = mouse coords.\n"
        "FIELD NOTES: UAC can NOT be automated — ask the user. IME: wait 0.3s between CJK typing "
        "and {ENTER}, or paste=true. Send files: copy_file → click input → ^v → {ENTER} (NEVER "
        "drag). Open/Save dialogs: type_control the full absolute path + {ENTER} — don't click "
        "Browse. drag duration >=0.6s (Hit-Test needs real move events).\n"
        "UIA SCRIPTING: the `uiautomation` package ships in-bundle — inside execute_code do "
        "`import uiautomation as auto` and walk raw UIA trees (auto.GetRootControl() → GetChildren(), "
        "read Name/ControlTypeName/BoundingRectangle) when read_controls comes up empty on "
        "self-drawn apps; a pure-Python tree dump costs zero vision tokens and often beats "
        "screenshot-guessing. Print the tree lines, pick the control, then act via click/rel coords.\n"
        "WECHAT 4.x (rel coords only — NEVER vision pixel coords): search ≈(0.085,0.055), first "
        "result row ≈rel_y 0.12–0.20, input ≈(0.60,0.925), send ≈(0.46,0.94). Send: click_type "
        "rel(0.085,0.055) text=<contact> → wait 1s → pick first entry → click_type rel(0.60,0.925) "
        "text=<msg> → click send button（微信4.x 合成 Enter 不发送，勿用 {ENTER}）→ verify.\n"
        "MACRO (anti-token #1): >=3 deterministic steps → ONE macro call, saves N-1 LLM "
        "round-trips. macro=<JSON> {\"steps\":[{\"action\":<action>, ...kwargs...}, ...], "
        "\"fail_fast\":true}; step vocabulary = the write/wait actions plus sleep "
        "({\"action\":\"sleep\",\"seconds\":1.0}); macro-level process/title/index/timeout are "
        "inherited by steps without their own. Values must be fully decided now — NEVER macro a "
        "step whose value depends on a previous step's live result (decide that as a normal "
        "step, then macro the deterministic tail). fail_fast=true stops at the first failure "
        "with a per-step log; verify_screenshot=true appends ONE final screenshot."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": _ALL_ACTIONS,
                "description": "Action to perform.",
            },
            "verify_screenshot": {
                "type": "boolean",
                "description": "Write actions: attach post-action screenshot to the result. Default false.",
            },
            "date": {
                "type": "string",
                "description": "set_date: 目标日期（2026-09-11 / 2026/9/11 / 2026年9月11日）。按 年→月→日 逐段键入纯数字（段满自动跳段），专治 QDateEdit/DateTimePicker 等分段控件——整串输入会被解析错乱。",
            },
            "find": {
                "type": "string",
                "description": "click/double_click/right_click/click_type & locate: natural-language "
                "target (e.g. '红色提交按钮' / '搜索输入框'). Internally screenshots + VL-locates + "
                "converts to screen coords, then clicks (snap still applies). One call replaces "
                "screenshot→vision→click. Requires vision model configured; fails safely to "
                "read_controls/rel fallback hints otherwise.",
            },
            "screen": {
                "type": "boolean",
                "description": "x/y 是屏幕绝对坐标：与 img= 同传但坐标并非截图内坐标时置 true，"
                "跳过截图坐标换算（默认 false=按 img 截图坐标自动换算）。",
            },
            "scale": {
                "type": "number",
                "description": "screenshot downscale, default 0.5. 降采样后点击请把截图路径传给 "
                "click 的 img= 参数让工具自动换算坐标，勿手算。1.0 = 1:1。",
            },
            "title": {
                "type": "string",
                "description": "Window title (exact match, or regex when title_re=true).",
            },
            "process": {
                "type": "string",
                "description": "Process name substring, e.g. 'Weixin'/'Feishu' — preferred over title "
                "(works alone or with title).",
            },
            "title_re": {
                "type": "boolean",
                "description": "Treat title as regex. Default false.",
            },
            "index": {
                "type": "integer",
                "description": "Which matching window when several match (default 0).",
            },
            "control": {
                "type": "string",
                "description": "Control name/text to locate (click_control/type_control).",
            },
            "control_type": {
                "type": "string",
                "description": "Filter controls by type, e.g. Button/Edit/ListItem (optional).",
            },
            "depth": {
                "type": "integer",
                "description": "read_controls: max tree depth (e.g. 2-3 for huge windows to speed up; default full tree).",
            },
            "control_index": {
                "type": "integer",
                "description": "Which matching control when several match (default 0).",
            },
            "text": {
                "type": "string",
                "description": "Text to type (type_control / type_text; Chinese supported).",
            },
            "keys": {
                "type": "string",
                "description": "Key sequence for press_key, pywinauto syntax: {ENTER} {ESC} ^a ^c ^v {TAB}.",
            },
            "x": {"type": "integer", "description": "X screen coordinate (click/scroll/drag start/probe point)."},
            "y": {"type": "integer", "description": "Y screen coordinate (click/scroll/drag start/probe point)."},
            "img": {
                "type": "string",
                "description": "坐标来源的截图路径（desktop screenshot 返回的 path）。当 x/y 是 vision "
                "读取该截图的像素坐标时传入，工具按截图 meta（scale + 窗口偏移）自动换算成屏幕坐标，"
                "杜绝手算降采样缩放出错。不传或坐标超出该截图范围 = 按屏幕绝对坐标处理。"
                "适用: click/double_click/right_click/click_type 的 x/y（rel_x/rel_y 不需要）。",
            },
            "x2": {
                "type": "integer",
                "description": "drag: end X (screen absolute; cross-window drag). "
                "Prefer rel_x2/rel_y2 when both ends are inside the same window.",
            },
            "y2": {
                "type": "integer",
                "description": "drag: end Y (screen absolute; cross-window drag).",
            },
            "rel_x2": {
                "type": "number",
                "description": "drag: end at fraction (0~1) of start window WIDTH — window move "
                "safe. Requires title/process (same window as start). Exact drop point, NOT snapped.",
            },
            "rel_y2": {
                "type": "number",
                "description": "drag: end at fraction (0~1) of start window HEIGHT.",
            },
            "duration": {
                "type": "number",
                "description": "drag: seconds for the move phase (default 0.6; keep >=0.6 so Hit-Test registers).",
            },
            "file": {
                "type": "string",
                "description": "copy_file: file absolute path(s) onto clipboard (';' separated). "
                "Then paste into chat input with ^v.",
            },
            "rel_x": {
                "type": "number",
                "description": "Fraction (0~1) of target window WIDTH (0.085 = 8.5% from left). "
                "Used by click-family / click_type / probe / drag start / scroll. "
                "Preferred over vision pixel coords for fixed-layout apps.",
            },
            "rel_y": {
                "type": "number",
                "description": "Fraction (0~1) of target window HEIGHT.",
            },
            "snap": {
                "type": "boolean",
                "description": "Snap the given point to the center of the UIA control under it "
                "(default true). Applies to click/double_click/right_click/click_type and drag "
                "START point — fixes vision/rel points a few px off and pulls points in list gaps "
                "onto a control (default click would rubber-band select). Self-drawn UIs keep raw "
                "coords and report no snap. drag END is an exact drop point: never snapped. "
                "snap=false = use the exact point.",
            },
            "scroll": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Wheel direction for scroll action.",
            },
            "amount": {
                "type": "integer",
                "description": "Wheel ticks for scroll (default 3).",
            },
            "target": {
                "type": "string",
                "description": "App path / file / URI to open (launch action, uses os.startfile, no args injection).",
            },
            "window_only": {
                "type": "boolean",
                "description": "screenshot: capture only the target window instead of full screen.",
            },
            "state": {
                "type": "string",
                "enum": ["appear", "vanish"],
                "description": "wait: wait for window to appear (default) or vanish.",
            },
            "until_control": {
                "type": "string",
                "description": "wait: poll until a control whose text contains this substring appears "
                "in the target window (UIA, zero token). Use INSTEAD of screenshot-then-vision for "
                "'did it load / send succeed' checks. Timeout reply includes recent control-text samples.",
            },
            "until_title_contains": {
                "type": "string",
                "description": "wait: poll until any top-level window title contains this substring "
                "(e.g. a new dialog). Zero token, no screenshot needed.",
            },
            "force": {
                "type": "boolean",
                "description": "screenshot: force a fresh capture even when the screen looks unchanged "
                "since the last shot of the same scope (default false = unchanged screen short-circuits "
                "and returns the previous path).",
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds for wait/find retry (default 10).",
            },
            "macro": {
                "type": "string",
                "description": "JSON string packing >=3 deterministic steps into ONE call (anti-token; "
                "see description MACRO section). {\"steps\":[{action + its kwargs}, ...], "
                "\"fail_fast\": true|false} — inherits top-level process/title/index/timeout.",
            },
        },
        "required": ["action"],
    }
    annotations = ToolAnnotations(
        read_only=False,
        destructive=False,
        idempotent=False,
        open_world=False,
        requires_approval=False,
    )
    # 仅 Windows（pywinauto/UIA）；其他平台 registry 自动隐藏
    platforms = ("windows",)

    async def execute(self, **kwargs) -> Observation:
        # ★ 2026-09-14 平台守卫：desktop 依赖 pywinauto/Win32（仅 Windows）。
        # import 是惰性的（非 Windows 不会在模块加载时炸），但执行时必须
        # 明确拒绝并告知原因，而不是让 pywinauto ImportError 裸抛。
        if not _IS_WINDOWS:
            return self._err(
                ERROR_INVALID_ARGS,
                "desktop 工具仅支持 Windows（依赖 pywinauto/Win32 UIA）。"
                "当前平台不可用——Linux 请用 shell（xdotool/wmctrl），"
                "macOS 请用 shell（osascript/AppleScript）。",
            )
        action = str(kwargs.get("action") or "").strip()
        if action not in _ALL_ACTIONS:
            return self._err(
                ERROR_INVALID_ARGS,
                f"未知 action: {action}（可用: {', '.join(_ALL_ACTIONS)}）",
            )
        # 任何操作前先统一坐标系（高 DPI 屏防点击偏移）
        _ensure_dpi_aware()
        # find=<自然语言目标>：点击类动作内部先 VL 定位再注入坐标（2026-09-08）
        # 定位+点击一次调用完成，省 2 次工具调用与 1~2 轮 LLM 往返
        if action in _FIND_CLICK_ACTIONS and str(kwargs.get("find") or "").strip():
            px, py, err = await self._locate_point(str(kwargs["find"]).strip(), kwargs)
            if err is not None:
                return err
            # ★ 2026-09-09：同时清掉 img/screen —— _locate_point 已把坐标换算成
            # 屏幕坐标写回 x/y，若残留 img=，点击路径会把屏幕坐标再当截图坐标
            # 换算一次（全屏 0.5 降采样时坐标直接减半 → 点错位置）
            kwargs = {
                k: v for k, v in kwargs.items()
                if k not in ("find", "rel_x", "rel_y", "x", "y", "img", "screen")
            }
            kwargs["x"], kwargs["y"] = px, py
        if action in _WRITE_ACTIONS:
            logger.info("desktop tool write action: %s args=%s", action, kwargs)
            # ★ 2026-09-11 四态回读验证：写操作前快照各范围最新截图签名，
            # verify_screenshot 时与操作后差分 → confirmed/partial/suspected_noop/unverifiable
            _pre_shots = {k: dict(v) for k, v in _SHOT_LAST.items()}

        try:
            handler = getattr(self, f"_do_{action}")
            obs = await handler(**kwargs)
        except RuntimeError as e:
            # 依赖缺失等可读错误（_uia_desktop 等惰性导入点的显式提示）
            return self._err(ERROR_INTERNAL, str(e))
        except ImportError as e:
            # ★ 2026-09-14 分类上报：依赖缺失（pywinauto/PIL 未装）与普通
            # 执行错误区分——此前混在通用 Exception 里，agent 无从判断
            # "装依赖能解决"还是"换个方法"。
            logger.exception("desktop tool dependency missing (%s)", action)
            return self._err(
                ERROR_INTERNAL,
                f"依赖缺失: {e} —— desktop 工具需要 pywinauto + Pillow，"
                "请执行 `pip install pywinauto Pillow` 后重试（不要重试当前操作）。",
            )
        except PermissionError as e:
            logger.exception("desktop tool permission denied (%s)", action)
            return self._err(
                ERROR_INTERNAL,
                f"权限不足: {e} —— 可能被安全软件/UIPI（以管理员运行的应用）拦截，"
                "尝试以相同权限运行 Scout 或换目标窗口。",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("desktop tool error (%s)", action)
            return self._err(ERROR_INTERNAL, f"{type(e).__name__}: {e}")

        # 写操作成功 → 置"预期变化"标记：下一张截图永不跳过（"操作未生效"是
        # 关键信息，不可被屏幕变化检测短路），最多附警示，见 _do_screenshot
        if obs.success and action in _WRITE_ACTIONS:
            global _SHOT_EXPECT_CHANGE
            _SHOT_EXPECT_CHANGE = True

        # 写操作执行成功且请求了 verify_screenshot → 自动附带窗口截图
        # （省一轮独立的 screenshot 工具调用 = 省一次 LLM 往返）
        # ★ 2026-09-11 升级为四态回读验证（对标 winhand-use）：
        #   confirmed（界面显著变化）/ partial（细微变化）/
        #   suspected_noop（几乎未变——不是失败，是"回去重看"）/ unverifiable
        if (
            obs.success
            and action in _WRITE_ACTIONS
            and str(kwargs.get("verify_screenshot", "")).lower() in ("1", "true", "yes")
        ):
            try:
                shot = await self._do_screenshot(**kwargs)
                if shot.success:
                    first_line = shot.output.splitlines()[0]
                    verify_note = ""
                    try:
                        _scope = (shot.metadata or {}).get("scope") or ""
                        _pre = _pre_shots.get(_scope) if _pre_shots else None
                        _cur = _SHOT_LAST.get(_scope)
                        if _pre is not None and _cur is not None and _scope:
                            _diff = _img_mean_diff(_pre["sig"], _cur["sig"])
                            if _diff >= _SHOT_DIFF_TH:
                                verify_note = (
                                    f"\n[验证 confirmed] 界面已变化（像素差 {_diff:.2f}）"
                                )
                            elif _diff >= 0.3:
                                verify_note = (
                                    f"\n[验证 partial] 界面细微变化（像素差 {_diff:.2f}）——结合上下文判断是否生效"
                                )
                            else:
                                verify_note = (
                                    f"\n[验证 suspected_noop] 界面几乎未变（像素差 {_diff:.2f}）——"
                                    "操作可能未生效，请重新观察界面（read_controls/probe/读图），勿盲目重试"
                                )
                        else:
                            verify_note = "\n[验证 unverifiable] 无操作前同范围截图可比对，请自行确认效果"
                    except Exception:  # noqa: BLE001 — 差分失败退化为原行为
                        verify_note = ""
                    obs = self._ok(
                        obs.output + f"\n[verify_screenshot] {first_line}{verify_note}",
                        {**(obs.metadata or {}), **(shot.metadata or {})},
                    )
            except Exception:  # noqa: BLE001 — 附带截图失败不影响主操作结果
                pass
        return obs

    # ── 读操作 ────────────────────────────────────────────

    async def _do_list_windows(self, **kw) -> Observation:
        d = _uia_desktop()
        lines = []
        for w in d.windows():
            try:
                if not w.is_visible():
                    continue
                lines.append(_win_summary(w))
            except Exception:  # noqa: BLE001
                continue
            if len(lines) >= _MAX_WINDOWS:
                lines.append(f"...（超过 {_MAX_WINDOWS} 个，已截断）")
                break
        return self._ok("\n".join(lines) or "（无可见顶层窗口）", {"count": len(lines)})

    async def _do_find_window(self, title: str = "", title_re: bool = False, **kw) -> Observation:
        process = kw.get("process", "")
        if not title and not process:
            return self._err(ERROR_INVALID_ARGS, "缺少 title 或 process 参数")
        d = _uia_desktop()
        wins = d.windows()
        if process:
            pl = process.lower()
            wins = [w for w in wins if pl in _proc_name(_win_pid(w)).lower()]
        if title:
            if title_re:
                matches = [w for w in wins if re.search(title, w.window_text() or "")]
            else:
                matches = [w for w in wins if title in (w.window_text() or "")]
        else:
            matches = wins
        if not matches:
            return self._err(ERROR_NOT_FOUND, f"未找到匹配窗口: {title!r}")
        body = "\n".join(_win_summary(w) for w in matches[:_MAX_WINDOWS])
        return self._ok(f"匹配 {len(matches)} 个窗口:\n{body}")

    async def _do_active_window(self, **kw) -> Observation:
        import ctypes

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
        return self._ok(f"前台窗口: [{hwnd}] \"{buf.value}\"")

    async def _do_read_controls(
        self, title: str = "", title_re: bool = False, index: int = 0, control: str = "",
        control_type: str = "", depth: int = 0, **kw,
    ) -> Observation:
        w = _find_wrapper(title or "", title_re, index, timeout=kw.get("timeout", 5) or 5, process=kw.get("process", ""))
        if w is None:
            return self._err(ERROR_NOT_FOUND, f"未找到窗口: {title or '(空标题)'}")
        # ★ 2026-09-15：控件树缓存命中则直接复用（省掉整棵树的跨进程遍历与排序）。
        # 过滤条件不同 → 结果不同，故一并进缓存键；force_refresh 可强制取最新。
        _hwnd = int(getattr(w, "handle", 0) or 0)
        _ck = (_hwnd, int(depth or 0), str(control or ""), str(control_type or "").lower())
        _fp = _win_light_fingerprint(_hwnd)
        if not kw.get("force_refresh"):
            _hit = _uia_tree_get(_ck, _fp)
            if _hit is not None:
                return self._ok(
                    f"窗口 \"{w.window_text()}\" 控件 {len(_hit)} 个（复用 2.5s 内的结构缓存，"
                    f"需要最新请传 force_refresh=true）:\n" + "\n".join(_hit)
                )
        try:
            ctrls = w.descendants(depth=depth) if depth and depth > 0 else w.descendants()
        except Exception as e:  # noqa: BLE001
            return self._err(ERROR_INTERNAL, f"控件树读取失败: {e}")
        # 有交互价值的控件优先（自绘 UI 常几十个 Pane，交互控件排前面不被截断挤掉）
        _prio = {"button": 0, "edit": 0, "listitem": 0, "menuitem": 0, "tabitem": 0,
                 "checkbox": 0, "radiobutton": 0, "combobox": 0, "hyperlink": 0,
                 "document": 1, "text": 2, "pane": 3}
        def _key(c):
            try:
                return _prio.get((c.element_info.control_type or "").lower(), 2)
            except Exception:  # noqa: BLE001
                return 2
        lines = []
        # ★ 2026-09-15：先按过滤条件筛出候选，**再**按交互价值排序取前 N。
        # 原实现对全量控件排序后才过滤取前 N —— 白排了大半棵树（UIA 取属性是
        # 跨进程调用，排序时的每次 key 计算都要过 COM）。
        _cands = []
        for c in ctrls:
            try:
                ctype = c.element_info.control_type or ""
            except Exception:  # noqa: BLE001
                continue
            if control_type and ctype.lower() != control_type.lower():
                continue
            if control and control not in (c.window_text() or ""):
                continue
            _cands.append((_key(c), c))
        _cands.sort(key=lambda x: x[0])
        for _k, c in _cands:
            lines.append(_ctrl_line(c, with_center=True))
            if len(lines) >= _MAX_CONTROLS:
                lines.append(f"...（超过 {_MAX_CONTROLS} 个控件，已截断；可用 control/control_type/depth 过滤）")
                break
        _uia_tree_put(_ck, _fp, lines)
        return self._ok(
            f"窗口 \"{w.window_text()}\" 控件 {len(lines)} 个:\n" + ("\n".join(lines) or "（无匹配控件）")
        )

    async def _locate_point(self, find: str, kwargs: dict) -> tuple[int, int, Observation | None]:
        """find= 定位公共实现：截图→VL→图片坐标→屏幕坐标（含 scale/偏移换算）.

        返回 (px, py, None) 成功；(-1, -1, err) 失败——err 已是可直接回传的失败
        Observation（未配置视觉模型/截图失败/VL 失败/未找到/坐标越界防幻觉）。
        """
        try:
            from scout.tools.builtin.vision import _call_vision, get_vl_config, resolve_mode
        except Exception as e:  # noqa: BLE001
            return -1, -1, self._err(ERROR_INTERNAL, f"vision 模块不可用: {e}")
        api_key, base_url, model, cfg_proxy = get_vl_config()
        mode = resolve_mode(cfg_proxy) if cfg_proxy is not None else ("vl" if api_key and model else "none")
        if mode != "vl" or not api_key:
            return -1, -1, self._err(
                ERROR_INVALID_ARGS,
                "find/locate 定位需要可用的视觉能力。请在「设置 → 模型配置 → 模型能力」"
                "开启当前模型的视觉能力（或单独配置视觉模型）；"
                "未开启前请改用 read_controls（T1/T2 应用）或 rel_x/rel_y 固定布局坐标。",
            )
        shot = await self._do_screenshot(**kwargs)
        if not shot.success:
            return -1, -1, shot
        shot_path = str((shot.metadata or {}).get("path") or "")
        meta = _load_shot_meta(shot_path) or {}
        obs = await _call_vision(
            api_key, base_url, model, shot_path, _LOCATE_PROMPT.format(target=find)
        )
        if not obs.success:
            # ★ 2026-09-11 失败统一出口：兜底链（UIA→OCR）→ VL 强化重试（1.0原图/粗→细）
            return await self._locate_failed_path(
                find, kwargs, shot_path, meta,
                self._err(ERROR_INTERNAL, f"VL 定位调用失败: {obs.output[:300]}"),
            )
        parsed = _parse_locate_answer(obs.output)
        if parsed is None:
            return await self._locate_failed_path(
                find, kwargs, shot_path, meta,
                self._err(
                    ERROR_NOT_FOUND,
                    f"VL 未能定位目标 {find!r}（回答: {obs.output[:200]}）。"
                    "可换更具体的描述重试，或改用 read_controls / rel_x/rel_y / screenshot+vision。",
                ),
            )
        ix, iy = parsed
        scale = float(meta.get("scale") or 1.0) or 1.0
        sw, sh = int(meta.get("shot_w") or 0), int(meta.get("shot_h") or 0)
        if sw and sh and not (0 <= ix <= sw and 0 <= iy <= sh):
            return await self._locate_failed_path(
                find, kwargs, shot_path, meta,
                self._err(
                    ERROR_NOT_FOUND,
                    f"VL 返回坐标 ({ix},{iy}) 超出截图范围 {sw}x{sh}，视为定位失败（防幻觉护栏）。",
                ),
            )
        px = int(meta.get("win_left", 0) or 0) + int(ix / scale + 0.5)
        py = int(meta.get("win_top", 0) or 0) + int(iy / scale + 0.5)
        return px, py, None

    def _ocr_fallback_locate(self, find: str, shot_path: str, meta: dict) -> tuple[int, int, str] | None:
        """OCR 文本锚定兜底：在截图上按文字框定位目标中心（T3 自绘应用）.

        UIA/win32 树为空的纯自绘应用（微信 4.x、腾讯会议等），按钮本质是
        文字标签——OCR 找到文字框中心即命中，确定性远高于 VL 像素定位。
        依赖 RapidOCR（动态加载，环境无依赖返回 None 不报错）。
        """
        engine = _get_ocr_engine()
        if engine is None or not shot_path:
            return None
        needles = _extract_locate_needles(find)
        if not needles:
            return None
        try:
            result, _ = engine(shot_path)
        except Exception:  # noqa: BLE001
            return None
        if not result:
            return None
        scale = float(meta.get("scale") or 1.0) or 1.0
        win_left = int(meta.get("win_left", 0) or 0)
        win_top = int(meta.get("win_top", 0) or 0)
        for needle in needles:
            for item in result:
                # RapidOCR 输出: [box(4点), text, score]
                try:
                    box, text = item[0], str(item[1] or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if len(text) < 2:
                    continue
                if needle in text or text in needle:
                    xs = [p[0] for p in box]
                    ys = [p[1] for p in box]
                    cx = sum(xs) / len(xs)
                    cy = sum(ys) / len(ys)
                    px = win_left + int(cx / scale + 0.5)
                    py = win_top + int(cy / scale + 0.5)
                    logger.info("OCR 兜底定位 %r → 文本 %r @(%d,%d)",
                                find, text[:20], px, py)
                    return px, py, text[:40]
        return None

    def _fallback_locate(self, find: str, kwargs: dict, shot_path: str, meta: dict) -> tuple[int, int, str] | None:
        """定位兜底链（2026-09-10）：UIA 控件树按名 → OCR 文本锚定."""
        fb = self._uia_fallback_locate(find, kwargs)
        if fb:
            return fb
        return self._ocr_fallback_locate(find, shot_path, meta)

    async def _vl_enhanced_retries(self, find: str, kwargs: dict) -> tuple[int, int] | None:
        """VL 强化重试链（2026-09-11，确定性兜底也失败后的 VL 侧最后手段）.

        ① scale=1.0 原图重试：默认 0.5 降采样会丢失小目标（小图标/小按钮），
           原图重试一次显著提升小目标命中率；
        ② 粗→细两段：先问"目标在哪个区域"（百分比框）→ 裁剪局部 → 精确定位。
           弱 VL 模型在局部小图上的定位成功率远高于全图直答。
        """
        try:
            from scout.tools.builtin.vision import _call_vision, get_vl_config, resolve_mode

            api_key, base_url, model, cfg_proxy = get_vl_config()
            mode = resolve_mode(cfg_proxy) if cfg_proxy is not None else ("vl" if api_key and model else "none")
            if mode != "vl" or not api_key:
                return None

            # ① scale=1.0 原图重试
            shot = await self._do_screenshot(**{**kwargs, "scale": 1.0, "force": "true"})
            sp = str((shot.metadata or {}).get("path") or "") if shot.success else ""
            if sp:
                meta = _load_shot_meta(sp) or {}
                obs = await _call_vision(api_key, base_url, model, sp, _LOCATE_PROMPT.format(target=find))
                if obs.success:
                    parsed = _parse_locate_answer(obs.output)
                    if parsed:
                        ix, iy = parsed
                        sw, sh = int(meta.get("shot_w") or 0), int(meta.get("shot_h") or 0)
                        if not sw or (0 <= ix <= sw and 0 <= iy <= sh):
                            px = int(meta.get("win_left", 0) or 0) + ix  # scale=1.0
                            py = int(meta.get("win_top", 0) or 0) + iy
                            logger.info("VL scale=1.0 重试命中 %r @(%d,%d)", find, px, py)
                            return px, py

                # ② 粗→细（基于 1.0 原图）
                coarse = await _call_vision(api_key, base_url, model, sp, _COARSE_PROMPT.format(target=find))
                if coarse.success:
                    region = _parse_coarse_answer(coarse.output)
                    if region:
                        cropped = _crop_by_region(Path(sp), region)
                        if cropped:
                            obs2 = await _call_vision(
                                api_key, base_url, model, str(cropped), _LOCATE_PROMPT.format(target=find)
                            )
                            if obs2.success:
                                p2 = _parse_locate_answer(obs2.output)
                                if p2:
                                    ix, iy = p2
                                    from PIL import Image

                                    cw, ch = Image.open(cropped).size
                                    if 0 <= ix <= cw and 0 <= iy <= ch:
                                        # 裁剪图内坐标 → 原截图坐标 → 屏幕坐标（scale=1.0）
                                        full = Image.open(sp).size
                                        off_x = int(region[0] * full[0])
                                        off_y = int(region[1] * full[1])
                                        px = int(meta.get("win_left", 0) or 0) + off_x + ix
                                        py = int(meta.get("win_top", 0) or 0) + off_y + iy
                                        logger.info("VL 粗→细命中 %r @(%d,%d)", find, px, py)
                                        return px, py
            return None
        except Exception:  # noqa: BLE001
            return None

    async def _locate_failed_path(
        self, find: str, kwargs: dict, shot_path: str, meta: dict, err: Observation
    ) -> tuple[int, int, Observation | None]:
        """定位失败统一出口（2026-09-11）：兜底链 → VL 强化重试 → 原错误."""
        fb = self._fallback_locate(find, kwargs, shot_path, meta)
        if fb:
            return fb[0], fb[1], None
        retry = await self._vl_enhanced_retries(find, kwargs)
        if retry:
            return retry[0], retry[1], None
        return -1, -1, err

    def _uia_fallback_locate(self, find: str, kwargs: dict) -> tuple[int, int, str] | None:
        """VL 定位失败后的控件树兜底：按目标关键词匹配控件名，返回屏幕中心坐标.

        ★ 2026-09-10：弱 VL 模型（空返回/拒答/坐标幻觉）场景下，多数应用的
        UIA/win32 控件树仍可用（腾讯会议实测 UIA 树完整）。"预定会议按钮"这类
        文本目标按名命中控件中心，比像素定位可靠且零成本。返回 (x, y, 控件名)
        或 None（无命中/窗口找不到）。
        """
        needles = _extract_locate_needles(find)
        if not needles:
            return None
        # ★ 2026-09-11 防护：无窗口定位上下文（title/process 均空）时不做
        # 控件树兜底——_find_wrapper 会抓任意窗口并遍历其全部控件树
        # （复杂窗口数千控件、秒级耗时），且"随便一个窗口"上的命中大概率
        # 不是目标窗口，坐标反而误导。此场景交由 OCR/VL 兜底。
        if not (kwargs.get("title") or "").strip() and not (kwargs.get("process") or "").strip():
            return None
        try:
            w = _find_wrapper(
                kwargs.get("title", ""), kwargs.get("title_re", False),
                kwargs.get("index", 0), timeout=0.0, process=kwargs.get("process", ""),
            )
            if w is None:
                return None
            for needle in needles:
                try:
                    ctrls = w.descendants()
                except Exception:  # noqa: BLE001
                    return None
                _interactive = (
                    "button", "edit", "listitem", "menuitem", "tabitem",
                    "checkbox", "radiobutton", "combobox", "hyperlink",
                )

                def _prio(c) -> int:
                    try:
                        ct = (c.element_info.control_type or "").lower()
                        return 0 if ct in _interactive else 1
                    except Exception:  # noqa: BLE001
                        return 1

                best = None
                for c in sorted(ctrls, key=_prio):
                    try:
                        text = (c.window_text() or "").strip()
                        if not text or len(text) < 2:
                            continue
                        if (needle in text or text in needle) and _prio(c) == 0:
                            best = c
                            break  # 交互控件命中即用
                        if best is None and _prio(c) == 1 and (needle in text or text in needle):
                            best = c  # 记住非交互命中，继续找交互控件
                    except Exception:  # noqa: BLE001
                        continue
                if best is not None:
                    rect = best.rectangle()
                    if rect.right > rect.left and rect.bottom > rect.top:
                        cx = (rect.left + rect.right) // 2
                        cy = (rect.top + rect.bottom) // 2
                        if cx >= 0 and cy >= 0:
                            logger.info("UIA 兜底定位 %r → 控件 %r @(%d,%d)",
                                        find, (best.window_text() or "")[:30], cx, cy)
                            return cx, cy, (best.window_text() or "")[:40]
            return None
        except Exception:  # noqa: BLE001
            return None

    async def _do_locate(self, find: str = "", **kw) -> Observation:
        """自然语言定位：返回目标屏幕坐标与命中的 UIA 控件（不点击）.

        与 click find= 的区别：locate 只读（拿到坐标后自行决策 click/click_control），
        click find= 一步到位。两者共用 _locate_point（截图→VL→坐标换算）。
        """
        if not find:
            return self._err(ERROR_INVALID_ARGS, "缺少 find 参数（目标描述，如 '红色提交按钮'）")
        px, py, err = await self._locate_point(find.strip(), kw)
        if err is not None:
            return err
        # 吸附预览：给出命中的 UIA 控件（T1 应用可直接 click_control 免坐标）
        snap_line = ""
        ctrl_name = ""
        cx, cy = px, py
        try:
            w = _find_wrapper(
                kw.get("title", ""), kw.get("title_re", False),
                kw.get("index", 0), timeout=0.0, process=kw.get("process", ""),
            )
            if w is not None:
                sx, sy, hit = self._apply_snap(w, "true", px, py)
                if hit is not None:
                    cx, cy = sx, sy
                    ctrl_name = (hit.window_text() or "")[:60]
                    snap_line = f"\nsnap 吸附: {hit.element_info.control_type or 'Control'} {ctrl_name!r} center=({sx},{sy}) → 建议 click_control"
        except Exception:  # noqa: BLE001 — 吸附预览失败不影响坐标返回
            pass
        meta = {
            "x": px, "y": py, "snap_x": cx, "snap_y": cy,
            "control": ctrl_name,
        }
        return self._ok(
            f"定位成功: {find!r} → 屏幕坐标 ({px},{py}){snap_line}\n"
            f"下一步: click action 用 x={px} y={py}（snap 默认开启会自动吸附控件中心），"
            "或按上面建议 click_control；确认落点可加 verify_screenshot=true。",
            meta,
        )

    async def _do_screenshot(
        self, title: str = "", title_re: bool = False, index: int = 0,
        window_only: bool = False, scale: float = 0.0, **kw,
    ) -> Observation:
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        _fs_warn = ""  # 全屏分支的坐标/范围警告（多显示器/DPI 自检）
        # 文件名带毫秒：防止同秒内多次截图（screenshot 与 vision 并行/verify 附带截图）
        # 互相覆盖 png/meta，造成坐标错配
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = _SHOT_DIR / f"desktop_{ts}.png"
        if window_only or (title or (kw.get("process") or "").strip()):
            w = _find_wrapper(title, title_re, index, timeout=kw.get("timeout", 5) or 5, process=kw.get("process", ""))
            if w is None:
                return self._err(ERROR_NOT_FOUND, f"未找到窗口: title={title!r} process={kw.get('process')!r}")
            # 抓取链：PrintWindow（不受屏幕 DC 拦截影响，DLP 环境唯一可靠路径）
            # → pywinauto capture_as_image（屏幕 DC）→ 报错
            img = _printwindow_capture(w.handle)
            if img is None:
                try:
                    img = w.capture_as_image()
                except Exception:  # noqa: BLE001
                    img = None
            if img is None:
                return self._err(
                    ERROR_INTERNAL,
                    f"窗口截图失败（PrintWindow 与屏幕 DC 均不可用）: {w.window_text()!r}",
                )
        else:
            from PIL import ImageGrab

            # ★ 2026-09-11 多显示器 origin 修复：ImageGrab.grab(all_screens=True)
            # 的图内 (0,0) = **虚拟屏左上角**（副屏在主屏左/上方时是负坐标），
            # 此前 meta 一律写 win_left=0 → 副屏目标坐标换算整体错位。
            # 记录虚拟屏 origin，img= 换算自动正确；同时自检尺寸防 DPI 虚拟化。
            _fs_origin = (0, 0)
            _fs_warn = ""
            try:
                import ctypes

                _u32 = ctypes.windll.user32
                _vs_l, _vs_t = _u32.GetSystemMetrics(76), _u32.GetSystemMetrics(77)
                _vs_w, _vs_h = _u32.GetSystemMetrics(78), _u32.GetSystemMetrics(79)
                _fs_origin = (_vs_l, _vs_t)
            except Exception:  # noqa: BLE001
                pass
            try:
                img = ImageGrab.grab(all_screens=True)
                # 自检：截图尺寸应等于虚拟屏物理尺寸；不符 = DPI 虚拟化（进程
                # 未成功声明感知）或混合 DPI 多屏拼接异常（PIL 已知问题）。
                try:
                    if (_vs_w, _vs_h) and img.size != (_vs_w, _vs_h):
                        # ★ 逐屏抓取拼接降级（混合 DPI 正确路径）：每屏独立
                        # bbox 抓取按物理坐标拼画布，成功则替代整屏 DC 结果。
                        patched = _grab_per_monitor((_vs_l, _vs_t, _vs_l + _vs_w, _vs_t + _vs_h))
                        if patched is not None and patched.size == (_vs_w, _vs_h):
                            img = patched
                            _fs_warn = (
                                "[多屏提示] 已用逐屏抓取拼接替代整屏截图"
                                "（混合缩放多屏下整屏 DC 会拉伸副屏）。"
                            )
                            logger.info("全屏截图走逐屏拼接路径（all_screens 尺寸异常已修正）")
                        else:
                            _fs_warn = (
                                f"[坐标警告] 全屏截图 {img.size[0]}x{img.size[1]} 与虚拟屏 "
                                f"{_vs_w}x{_vs_h} 不一致（DPI 虚拟化或混合缩放拼接失败）——"
                                "坐标换算可能错位！建议改用窗口截图（window_only=true）。"
                            )
                            logger.warning(_fs_warn)
                except NameError:
                    pass
            except OSError:
                try:
                    img = ImageGrab.grab()
                    _fs_origin = (0, 0)
                    _fs_warn = "[范围提示] all_screens 失败，已降级仅截主屏——副屏内容不在本图中。"
                except OSError:
                    return self._err(
                        ERROR_INTERNAL,
                        "全屏截图失败：本机屏幕 DC 被系统拦截（DLP/安全软件）。"
                        "请改用窗口截图：screenshot + process=<应用名> + window_only=true。",
                    )
        scope = (
            f"win:{w.handle}"
            if (window_only or title or (kw.get("process") or "").strip())
            else "fullscreen"
        )
        # 空屏守卫（2026-09-04）：窗口未渲染完成时 PrintWindow 会返回近乎纯色的
        # 小图（实测 3131 字节整）——vision 读它只能得到空描述，误导 agent 决策。
        # 检测：PNG 编码体积异常小 + 图像色彩单一 → 重抓一次（多数情况第二次已渲染完）。
        try:
            png_size = len(_img_to_png_bytes(img))
            w_px, h_px = img.size
            colors = len(img.convert("RGB").getcolors(maxcolors=256) or []) if (w_px * h_px) else 0
            if png_size < 8000 and colors <= 8 and w_px * h_px > 50000:
                # ★ 2026-09-25 Windows 性能修复：本工具所有动作都跑在事件循环协程里，time.sleep 会冻结整个服务（WS 推流、IM 渠道、其他会话全部排队），实测单处最长 1.2s（截图空屏守卫）、每步 0.05~0.5s → 一律改 await asyncio.sleep。
                await asyncio.sleep(1.2)
                retry = _printwindow_capture(w.handle) if (window_only or title or (kw.get("process") or "").strip()) else None
                if retry is not None:
                    img = retry
        except Exception:  # noqa: BLE001 — 守卫失败不影响正常截图流程
            pass
        # ── 屏幕变化检测（2026-09-08）：与同范围上一张像素 diff，几乎未变化则短路 ──
        global _SHOT_EXPECT_CHANGE
        expect_change = _SHOT_EXPECT_CHANGE
        _SHOT_EXPECT_CHANGE = False
        force = str(kw.get("force", "")).lower() in ("1", "true", "yes")
        warn_prefix = ""
        if _SHOT_SKIP_ENABLED and not force:
            last = _SHOT_LAST.get(scope)
            if last and Path(last["path"]).exists():
                try:
                    diff = _img_mean_diff(last["sig"], _shot_signature(img))
                except Exception:  # noqa: BLE001 — 对比失败按"已变化"处理
                    diff = 255.0
                if diff < _SHOT_DIFF_TH:
                    if expect_change:
                        warn_prefix = (
                            f"[变化检测] 屏幕与上一张几乎一致（平均像素差 {diff:.2f}）——"
                            "上一步操作可能未生效；请先用 read_controls/probe 确认，勿盲目重试。\n"
                        )
                    else:
                        return self._ok(
                            f"屏幕未变化（与上一张平均像素差 {diff:.2f}，已跳过重复截图）。\n"
                            f"读屏请直接复用上一张: vision image={last['path']}\n"
                            f"坐标仍有效，click 同样用 img={last['path']}；确需强制新截图加 force=true。",
                            {"unchanged": True, "path": last["path"], "img": last["path"]},
                        )
        # 降采样（默认 0.5：vision API 对 1888x1150 级大图单次推理实测 5~60s，
        # 减半后体积/推理时间约降 60-70%，按钮级定位精度不受影响）
        try:
            s = float(scale) if scale else 0.5
        except (TypeError, ValueError):
            s = 0.5
        if 0 < s < 1.0:
            img = img.resize((max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))))
        # 写截图元数据（降采样比例 + 窗口在屏幕上的偏移）——click 传 img=<此 path>
        # 即自动把视觉读数的截图坐标换算成屏幕坐标，缩放换算交给代码而非 LLM 手算
        try:
            import json
            is_window = bool(window_only or (title or (kw.get("process") or "").strip()))
            if is_window:
                wr = w.rectangle()
                win_left, win_top = wr.left, wr.top
            else:
                # ★ 2026-09-11：全屏截图 origin = 虚拟屏左上角（多显示器副屏
                # 在左/上方时为负坐标），img= 坐标换算据此自动对齐副屏。
                win_left, win_top = _fs_origin
            meta = {
                "path": str(path),
                "kind": "window" if is_window else "fullscreen",
                "scale": s if 0 < s <= 1.0 else 1.0,
                "shot_w": img.size[0],
                "shot_h": img.size[1],
                "win_left": int(win_left),
                "win_top": int(win_top),
                "scope": scope,  # ★ 2026-09-11 四态回读验证：操作前后同范围差分定位用
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            path.with_suffix(".meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 — meta 缺失不影响截图，点击退化为原坐标
            logger.debug("截图 meta 写入失败: %s", path, exc_info=True)
        img.save(str(path))
        if _SHOT_SKIP_ENABLED:
            try:
                if len(_SHOT_LAST) >= _SHOT_LAST_MAX and scope not in _SHOT_LAST:
                    _SHOT_LAST.pop(next(iter(_SHOT_LAST)))
                _SHOT_LAST[scope] = {"sig": _shot_signature(img), "path": str(path)}
            except Exception:  # noqa: BLE001
                pass
        note = (
            f"（已降采样 scale={s:.2f}，shot={img.size[0]}x{img.size[1]}）"
            if 0 < s < 1.0 else "（1:1 原始像素）"
        )
        return self._ok(
            f"{_fs_warn}{warn_prefix}截图已保存: {path}（{img.size[0]}x{img.size[1]}）{note}\n"
            f"坐标用法: 让 vision 读本图返回目标像素坐标后，把坐标和本截图路径一起传给 "
            f"desktop click 的 img=<此 path>（x/y 为该截图内坐标），工具会自动换算为屏幕坐标，"
            f"切勿手算 scale。",
            {
                "path": str(path),
                "img": str(path),
                "scale": s if 0 < s <= 1.0 else 1.0,
                "width": img.size[0],
                "height": img.size[1],
                # ★ 2026-09-11 四态回读验证：execute() 据此定位操作前基线签名
                "scope": scope,
            },
        )

    async def _do_wait(
        self, title: str = "", title_re: bool = False, state: str = "appear",
        timeout: int = 10, until_control: str = "", until_title_contains: str = "",
        **kw,
    ) -> Observation:
        process = kw.get("process", "")
        t0 = time.time()
        # ── 事件等待（2026-09-08）：文本级轮询代替"截图看加载"──
        # UIA 查询毫秒级、零 token；截图+vision 一轮实测 16-21s + 图像 token。
        if until_control:
            if not title and not process:
                return self._err(
                    ERROR_INVALID_ARGS, "until_control 需要配合 title 或 process 定位目标窗口"
                )
            deadline = t0 + max(1, timeout)
            sample: list[str] = []
            while True:
                w = _find_wrapper(title, title_re, timeout=0.0, process=process)
                if w is not None:
                    hit, sample = _poll_control_hit(w, until_control)
                    if hit:
                        return self._ok(
                            f"控件已出现: {sample[0]!r}（窗口 {w.window_text()!r}，"
                            f"耗时 {time.time()-t0:.1f}s）"
                        )
                if time.time() >= deadline:
                    break
                await asyncio.sleep(0.5)
            hint = (
                f"窗口内控件文本样例: {sample[:8]}"
                if sample
                else "未读到任何控件（可能是自绘 UI）——改用 screenshot+vision 或 rel 坐标"
            )
            return self._err(
                ERROR_TIMEOUT,
                f"等待超时（{timeout}s），未出现文本含 {until_control!r} 的控件。{hint}",
            )
        if until_title_contains:
            deadline = t0 + max(1, timeout)
            needle = until_title_contains.lower()
            while True:
                try:
                    for w in _uia_desktop().windows():
                        try:
                            t = w.window_text() or ""
                        except Exception:  # noqa: BLE001
                            continue
                        if needle in t.lower():
                            return self._ok(f"窗口已出现: {t!r}（耗时 {time.time()-t0:.1f}s）")
                except Exception:  # noqa: BLE001 — 枚举失败下一轮重试
                    pass
                if time.time() >= deadline:
                    break
                await asyncio.sleep(0.5)
            return self._err(
                ERROR_TIMEOUT, f"等待超时（{timeout}s），无窗口标题包含 {until_title_contains!r}"
            )
        if not title and not process:
            return self._err(ERROR_INVALID_ARGS, "缺少 title 或 process 参数")
        if state == "vanish":
            while time.time() - t0 < timeout:
                if _find_wrapper(title, title_re, timeout=0.0, process=kw.get("process", "")) is None:
                    label = title or (kw.get("process") or "")
                    return self._ok(f"窗口已消失: {label!r}（耗时 {time.time()-t0:.1f}s）")
                await asyncio.sleep(0.5)
            return self._err(ERROR_TIMEOUT, f"等待超时（{timeout}s），窗口仍存在: {title!r}")
        w = _find_wrapper(title, title_re, timeout=timeout, process=kw.get("process", ""))
        if w is None:
            return self._err(ERROR_TIMEOUT, f"等待超时（{timeout}s），窗口未出现: title={title!r} process={kw.get('process')!r}")
        return self._ok(f"窗口已出现: {w.window_text()!r}（耗时 {time.time()-t0:.1f}s）")

    # ── 写操作 ────────────────────────────────────────────

    async def _do_activate(
        self, title: str = "", title_re: bool = False, index: int = 0, **kw,
    ) -> Observation:
        w = _find_wrapper(title, title_re, index, timeout=kw.get("timeout", 5) or 5, process=kw.get("process", ""))
        if w is None:
            return self._err(ERROR_NOT_FOUND, f"未找到窗口: {title!r}")
        if not _force_foreground(w.handle):
            # 兜底：pywinauto set_focus（部分场景仍然有效）
            try:
                w.set_focus()
            except Exception:  # noqa: BLE001
                pass
        return self._ok(f"窗口已激活: \"{w.window_text()}\"")

    async def _do_launch(self, target: str = "", **kw) -> Observation:
        if not target:
            return self._err(ERROR_INVALID_ARGS, "缺少 target 参数（exe 路径/文件/URI）")
        # 裸名且应用已在运行 → 直接置前其窗口（不启动第二实例）
        base = Path(target).stem.lower()
        if base and not Path(target).exists():
            try:
                import psutil

                running = [
                    p for p in psutil.process_iter(["name"])
                    if (p.info.get("name") or "").lower().startswith(base)
                ]
            except Exception:  # noqa: BLE001
                running = []
            if running:
                w = _find_wrapper(process=running[0].info["name"])
                if w is not None:
                    try:
                        w.set_focus()
                        return self._ok(f"{target} 已在运行，已将其窗口置前: \"{w.window_text()}\"")
                    except Exception:  # noqa: BLE001
                        pass
        resolved = self._resolve_target(target)
        if resolved is not None:
            os.startfile(resolved)  # noqa: S606 — 受控打开，非 shell 执行
            return await self._launch_result(resolved, base)
        # 解析不到（Store 应用不注册 App Paths）→ 回退 os.startfile 原样启动，
        # 由 Windows Shell 解析（支持 Store 别名 notepad/mspaint、PATH、文件关联）。
        try:
            os.startfile(target)  # noqa: S606
            return await self._launch_result(target, base, note="（由 Windows Shell 解析）")
        except OSError as e:
            return self._err(
                ERROR_NOT_FOUND,
                f"无法启动 {target!r}: {e}。请提供完整路径，"
                "或先用 shell 的 where/注册表查询安装位置。",
            )

    async def _launch_result(self, shown: str, base: str, note: str = "") -> Observation:
        """启动结果：内联等待窗口出现，避免模型再补一次 wait/activate 往返.

        ★ 2026-09-14：此前 launch 立即返回「已启动」且不做任何验证 → 常见
        「窗口尚未就绪就执行下一步」→ 模型补 wait/activate/重试，GUI 任务步数
        成倍放大。现在启动后轮询窗口（最多 _LAUNCH_WAIT_SECONDS）：
        - 检测到窗口 → 直接给出标题（可立即 click/type，无需再 wait）
        - 未检测到   → 明确告知可能仍在加载（而非假装成功，让模型知道该等）
        """
        if base:
            deadline = time.monotonic() + _LAUNCH_WAIT_SECONDS
            while time.monotonic() < deadline:
                try:
                    w = _find_wrapper(process=base)
                except Exception:  # noqa: BLE001
                    w = None
                if w is not None:
                    try:
                        return self._ok(f"已启动{note}: {shown}；窗口已就绪: \"{w.window_text()}\"")
                    except Exception:  # noqa: BLE001
                        return self._ok(f"已启动{note}: {shown}；窗口已就绪")
                await asyncio.sleep(0.4)
            return self._ok(
                f"已启动{note}: {shown}；{_LAUNCH_WAIT_SECONDS:.0f}s 内未检测到窗口"
                "（可能仍在加载或已托盘化）——请用 wait process 等待，"
                "或激活后再操作；若长时间无窗口，改走其他路径（如对应网页版）。"
            )
        return self._ok(f"已启动{note}: {shown}")

    @staticmethod
    def _resolve_target(target: str) -> str | None:
        """解析启动目标：存在的路径直接用；裸名走 App Paths 注册表（.exe 补全）."""
        p = Path(target)
        try:
            if p.exists():
                return target
        except OSError:  # 非法路径字符
            return None
        # 裸名 → App Paths（微信/QQ 等安装时注册，如 Weixin.exe）
        try:
            import winreg

            for name in (target, f"{target}.exe" if not target.lower().endswith(".exe") else target):
                for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    try:
                        key = winreg.OpenKey(
                            root,
                            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}",
                        )
                        val, _ = winreg.QueryValueEx(key, None)
                        winreg.CloseKey(key)
                        if val and Path(val.strip('"')).exists():
                            return val.strip('"')
                    except OSError:
                        continue
        except ImportError:
            return None
        return None

    async def _do_close_window(
        self, title: str = "", title_re: bool = False, index: int = 0, **kw,
    ) -> Observation:
        w = _find_wrapper(title, title_re, index, timeout=kw.get("timeout", 5) or 5, process=kw.get("process", ""))
        if w is None:
            return self._err(ERROR_NOT_FOUND, f"未找到窗口: {title!r}")
        text = w.window_text()
        w.close()  # WM_CLOSE 温和关闭；应用可弹保存确认，由 Agent 继续决策
        return self._ok(
            f"已发送关闭请求: \"{text}\""
            "（若弹出保存确认框，用 click_control 点击『不保存』；"
            "若窗口未关闭（如标签页会话阻塞），可用 shell 工具 taskkill /PID <pid> /F 结束进程）"
        )

    async def _do_click_control(
        self, title: str = "", title_re: bool = False, index: int = 0, control: str = "",
        control_type: str = "", control_index: int = 0, **kw,
    ) -> Observation:
        if not ((title or kw.get("process")) and control):
            return self._err(ERROR_INVALID_ARGS, "需要窗口定位（title 或 process）与 control（控件）参数")
        w = _find_wrapper(title, title_re, index, timeout=kw.get("timeout", 5) or 5, process=kw.get("process", ""))
        if w is None:
            return self._err(ERROR_NOT_FOUND, f"未找到窗口: {title!r}")
        target = self._locate_control(w, control, control_type, control_index)
        if target is None:
            return self._err(
                ERROR_NOT_FOUND,
                f"窗口内未找到控件: control={control!r} type={control_type!r}",
            )
        # 主路：控件真实点击（点击前确保窗口前台）
        try:
            w.set_focus()
        except Exception:  # noqa: BLE001
            pass
        try:
            target.click_input()
            await asyncio.sleep(0.08)
            fs = _focus_summary()
            return self._ok(f"已点击控件: {_ctrl_line(target)}" + (f"；{fs}" if fs else ""))
        except Exception as e:  # noqa: BLE001
            # 兜底：控件中心物理坐标点击（自绘 UI/无 Click 模式时 click_input 会失败）
            try:
                from pywinauto import mouse

                r = target.rectangle()
                cx, cy = r.left + r.width() // 2, r.top + r.height() // 2
                mouse.click(button="left", coords=(cx, cy))
                fs = _focus_summary()
                return self._ok(
                    f"已点击控件（坐标兜底 {cx},{cy}）: {_ctrl_line(target)}；"
                    f"click_input 失败原因: {type(e).__name__}"
                    + (f"；{fs}" if fs else "")
                )
            except Exception as e2:  # noqa: BLE001
                return self._err(
                    ERROR_INTERNAL,
                    f"点击失败: click_input={type(e).__name__}; 兜底={type(e2).__name__}: {e2}",
                )

    async def _do_type_control(
        self, title: str = "", title_re: bool = False, index: int = 0, control: str = "",
        control_type: str = "", control_index: int = 0, text: str = "", **kw,
    ) -> Observation:
        if not ((title or kw.get("process")) and control):
            return self._err(ERROR_INVALID_ARGS, "需要窗口定位（title 或 process）、control（控件）、text（内容）参数")
        w = _find_wrapper(title, title_re, index, timeout=kw.get("timeout", 5) or 5, process=kw.get("process", ""))
        if w is None:
            return self._err(ERROR_NOT_FOUND, f"未找到窗口: {title!r}")
        target = self._locate_control(w, control, control_type, control_index)
        if target is None:
            return self._err(
                ERROR_NOT_FOUND,
                f"窗口内未找到控件: control={control!r} type={control_type!r}",
            )
        try:
            w.set_focus()
        except Exception:  # noqa: BLE001
            pass
        try:
            target.set_focus()
        except Exception:  # noqa: BLE001
            # 控件无法聚焦（自绘 UI）：点击中心后落到输入框再输入
            try:
                from pywinauto import mouse

                r = target.rectangle()
                mouse.click(button="left", coords=(r.left + r.width() // 2, r.top + r.height() // 2))
            except Exception:  # noqa: BLE001
                pass
        try:
            target.type_keys(text, with_spaces=True)
        except Exception:  # noqa: BLE001
            # 控件 type_keys 不可用：全局键盘输入兜底（焦点已在控件上）
            from pywinauto.keyboard import send_keys

            send_keys(text, with_spaces=True)
        fs = _focus_summary()
        return self._ok(f"已输入文本到 {_ctrl_line(target)}" + (f"；{fs}" if fs else ""))

    async def _do_type_text(self, text: str = "", paste: bool = False, **kw) -> Observation:
        """向当前焦点控件输入文本.

        路径选择（2026-09-03 实测修正）：
        - ASCII + 中文（无 paste）：**直接 SendInput unicode**（pywinauto send_keys）
          → Chromium/飞书/微信消息框都实测可用；比剪贴板粘贴更稳
        - paste=true：强制走剪贴板粘贴（用户显式要求时用）
        旧版"含中文自动粘贴"误判：飞书搜索框禁用了 Ctrl+V，改成 SendInput unicode 全过
        """
        if not text:
            return self._err(ERROR_INVALID_ARGS, "缺少 text 参数")
        if paste:
            if _paste_text(text):
                return self._ok(f"已向当前焦点粘贴输入 {len(text)} 字符（剪贴板模式）")
            return self._err(
                ERROR_INTERNAL,
                "剪贴板粘贴失败（粘贴通道被应用屏蔽，试试关闭 paste=true 直接输入）",
            )
        from pywinauto.keyboard import send_keys as _keys

        _keys(text, with_spaces=True)
        return self._ok(f"已向当前焦点输入 {len(text)} 字符（SendInput）")

    async def _do_press_key(self, keys: str = "", **kw) -> Observation:
        if not keys:
            return self._err(ERROR_INVALID_ARGS, "缺少 keys 参数（如 {ENTER} / ^a / {ESC}）")
        from pywinauto.keyboard import send_keys

        send_keys(keys)
        return self._ok(f"已发送按键: {keys}")

    async def _do_set_date(self, date: str = "", control: str = "", **kw) -> Observation:
        """分段日期控件专用设置（2026-09-10，腾讯会议 QDateEdit 实测教训）.

        QDateEdit / SysDateTimePick32 等分段控件：整串输入会被逐字符解析到
        各段导致日期错乱（实测 "2026/9/11" 被改成 2/2、4/2）。本 action 按
        年→月→日 逐段键入纯数字（段满自动跳段），从根上绕开该问题。
        """
        if not date:
            return self._err(ERROR_INVALID_ARGS, "缺少 date 参数（如 2026-09-11 / 2026/9/11 / 2026年9月11日）")
        m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$", str(date).strip())
        if not m:
            return self._err(ERROR_INVALID_ARGS, f"date 无法解析: {date!r}（支持 2026-09-11 / 2026/9/11 / 2026年9月11日）")
        y, mo, d = m.group(1), f"{int(m.group(2)):02d}", f"{int(m.group(3)):02d}"

        w = _find_wrapper(
            kw.get("title", ""), kw.get("title_re", False),
            kw.get("index", 0), timeout=kw.get("timeout", 5) or 5,
            process=kw.get("process", ""),
        )
        if w is None:
            return self._err(ERROR_NOT_FOUND, f"未找到窗口: {kw.get('title') or kw.get('process') or '(空)'}")

        # 定位日期控件：control 名优先 > 类名特征
        _date_classes = ("qdateedit", "sysdatetimepick32", "datetimepicker",
                         "dateedit", "radsdateedit", "calendar")
        ctrl = None
        try:
            ctrls = w.descendants()
        except Exception:  # noqa: BLE001
            ctrls = []
        for c in ctrls:
            try:
                if control and control in (c.window_text() or ""):
                    ctrl = c
                    break
                if not control:
                    cn = (getattr(c.element_info, "class_name", "") or "").lower()
                    if any(k in cn for k in _date_classes):
                        ctrl = c
                        break
            except Exception:  # noqa: BLE001
                continue
        if ctrl is None:
            hint = "或传 control= 按名定位" if not control else ""
            return self._err(
                ERROR_NOT_FOUND,
                f"未找到日期控件（QDateEdit/DateTimePicker 类）{hint}。"
                "备选路径：click 控件聚焦后用 type_text 逐段输入纯数字（年 4 位、月日各 2 位）。",
            )
        try:
            ctrl.set_focus()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.15)
        from pywinauto.keyboard import send_keys as _keys

        for seg in (y, mo, d):
            _keys(seg, with_spaces=True)
            await asyncio.sleep(0.2)
        return self._ok(
            f"已向日期控件分段键入 {y}-{mo}-{d}（年→月→日逐段，段满自动跳段）。"
            "注意：部分控件段序可能不同（美式为 月/日/年），完成后建议截图或 read_controls 确认实际值。"
        )

    def _click_point_with_win(self, kwargs: dict):
        """解析点击物理点并附带目标窗口；返回 (px, py, w|None).

        - x/y 绝对模式：直接用像素点；为支持 snap 吸附尽力找一次窗口
          （走 2s handle 缓存，通常无感；找不到窗口 = 点击桌面等，不吸附）。
        - rel_x/rel_y 模式（2026-09-03 起）：vision 模型返回像素坐标不可靠
          （偏差达窗口尺寸量级），固定布局应用按窗口矩形比例换算最稳。
        - 返回 (-1,-1,None) 表示参数不足/窗口未找到，调用方报错。
        """
        x = kwargs.get("x")
        y = kwargs.get("y")
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            px, py = int(x), int(y)
            # img=<截图 path>：视觉读数是截图内坐标（截图可能被降采样），
            # 换算逻辑放代码里自动完成（读同名 .meta.json），杜绝 LLM 手算 ×2 的
            # 系统性偏移；坐标超出该截图范围则视为已是屏幕坐标，原样使用。
            # ★ 2026-09-08：screen=true 显式声明"这是屏幕坐标"跳过换算 +
            #   回执注明换算过程 —— 全屏+0.5 降采样截图下，左上象限的屏幕坐标
            #   会落入截图尺寸范围被误 ×2（启发式歧义），透明化让模型可自查纠正。
            self._img_conv_note = ""
            _as_screen = str(kwargs.get("screen", "")).lower() in ("1", "true", "yes")
            meta = _load_shot_meta(str(kwargs.get("img") or ""))
            if (
                not _as_screen
                and meta is not None
                and 0 <= px <= meta.get("shot_w", -1) and 0 <= py <= meta.get("shot_h", -1)
            ):
                scale = float(meta.get("scale") or 1.0) or 1.0
                ox, oy = px, py
                px = int(meta.get("win_left", 0) or 0) + int(float(px) / scale + 0.5)
                py = int(meta.get("win_top", 0) or 0) + int(float(py) / scale + 0.5)
                self._img_conv_note = (
                    f"img换算: 截图坐标({ox},{oy})→屏幕({px},{py})（scale={scale:g}，"
                    "若你给的本就是屏幕坐标请改传 screen=true 重试）"
                )
            w = None
            if str(kwargs.get("snap", "true")).lower() not in ("0", "false", "no"):
                try:
                    if kwargs.get("title") or kwargs.get("process"):
                        w = _find_wrapper(
                            kwargs.get("title", ""), kwargs.get("title_re", False),
                            kwargs.get("index", 0), timeout=0.0,
                            process=kwargs.get("process", ""),
                        )
                    else:
                        # 2026-09-08：无窗口上下文 → 取包含该点的最小可见顶层
                        # 窗口（此前取"第一个可见窗口"会吸到无关窗口的控件）
                        w = _window_at_point(px, py)
                except Exception:  # noqa: BLE001 — 找不到窗口不影响纯坐标点击
                    w = None
            return px, py, w
        rx = kwargs.get("rel_x")
        ry = kwargs.get("rel_y")
        if rx is None or ry is None:
            return -1, -1, None
        w = _find_wrapper(
            kwargs.get("title", ""), kwargs.get("title_re", False),
            kwargs.get("index", 0), timeout=kwargs.get("timeout", 5) or 5,
            process=kwargs.get("process", ""),
        )
        if w is None:
            return -1, -1, None
        # 强制前台：pywinauto set_focus 对微信等自绘窗口不可靠，强行抢前台。
        # 2026-09-08 失败防护：前台化失败时坐标点击会落到该点最上层窗口
        # （可能是 Scout 自己），后续 type/press_key 全打进错误应用 → 直接报错
        if not _force_foreground(w.handle):
            return -1, -1, None
        r = w.rectangle()
        return (
            r.left + int(float(rx) * r.width()),
            r.top + int(float(ry) * r.height()),
            w,
        )

    def _apply_snap(self, w, snap: str, px: int, py: int):
        """snap!=false 且有窗口时把 (px,py) 吸附到命中最深控件中心.

        先 UIA 树；UIA 空树时自动兜底 win32 经典树（老式应用，2026-09-05）。
        返回 (cx, cy, hit)：吸附失败（自绘无树/命中近整窗/异常）时保持
        原坐标并返回 hit=None——调用方如实报告"吸附→"行即可，不影响落点。
        """
        if w is None or str(snap).lower() in ("0", "false", "no"):
            return px, py, None
        try:
            cx, cy, hit = _snap_control(w, px, py)
            if hit is not None:
                return cx, cy, hit
        except Exception:  # noqa: BLE001 — UIA 吸附失败继续尝试 win32 树
            pass
        try:
            w32 = _win32_wrapper(w)
            if w32 is not None:
                hit = _hit_w32(w32, px, py)
                if hit is not None:
                    try:
                        r = hit.rectangle()
                        return (
                            r.left + (r.right - r.left) // 2,
                            r.top + (r.bottom - r.top) // 2,
                            hit,
                        )
                    except Exception:  # noqa: BLE001
                        return px, py, None
        except Exception:  # noqa: BLE001 — 兜底失败不影响原坐标点击
            pass
        return px, py, None

    async def _do_click_type(
        self, x: int = -1, y: int = -1, text: str = "", keys: str = "",
        click_delay: float = 0.3, **kw,
    ) -> Observation:
        """复合动作：坐标点击 → 等待 → 输入文本 → 可选按键（如 {ENTER}）.

        一次工具调用完成"点输入框+打字+回车"，省 2 轮 LLM 往返（实测每轮
        ReAct 循环 2-5s LLM + 可能的截图/vision 验证 5-60s）。
        支持相对坐标 rel_x/rel_y（按目标窗口矩形比例，微信等固定布局首选）。
        """
        px, py, w = self._click_point_with_win({"x": x, "y": y, **kw})
        if px < 0 or py < 0:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 绝对坐标 或 rel_x/rel_y 窗口相对比例）")
        if not text and not keys:
            return self._err(ERROR_INVALID_ARGS, "缺少 text（输入内容）或 keys（按键）参数")
        from pywinauto import mouse
        from pywinauto.keyboard import send_keys

        cx, cy, hit = self._apply_snap(w, str(kw.get("snap", "true")), px, py)
        mouse.click(button="left", coords=(cx, cy))
        await asyncio.sleep(max(0.05, float(click_delay or 0.3)))
        parts = [f"({cx},{cy})"]
        if getattr(self, "_img_conv_note", ""):
            parts.append(self._img_conv_note)
        if hit is not None:
            parts.append("吸附→" + _ctrl_line(hit).lstrip("- "))
        fs = _focus_summary()
        if fs:
            parts.append(fs)
        if text:
            if str(kw.get("paste", "")).lower() in ("1", "true", "yes"):
                if _paste_text(text):
                    parts.append(f"粘贴输入 {len(text)} 字符")
                else:
                    parts.append(f"粘贴失败(text) | 输入 {len(text)} 字符")
                    send_keys(text, with_spaces=True)
            else:
                # 默认 SendInput unicode：Chromium/飞书/微信/记事本全实测可用
                send_keys(text, with_spaces=True)
                parts.append(f"输入 {len(text)} 字符")
        if keys:
            send_keys(keys)
            parts.append(f"按键 {keys}")
        return self._ok("已点击 " + "、".join(parts))

    async def _do_click(self, x: int = -1, y: int = -1, **kw) -> Observation:
        px, py, w = self._click_point_with_win({"x": x, "y": y, **kw})
        if px < 0 or py < 0:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 或 rel_x/rel_y）")
        from pywinauto import mouse

        cx, cy, hit = self._apply_snap(w, str(kw.get("snap", "true")), px, py)
        mouse.click(button="left", coords=(cx, cy))
        await asyncio.sleep(0.08)
        fs = _focus_summary()
        parts = [f"已左键点击 ({cx},{cy})"]
        if getattr(self, "_img_conv_note", ""):
            parts.append(self._img_conv_note)
        if hit is not None:
            parts.append("吸附→" + _ctrl_line(hit).lstrip("- "))
        if fs:
            parts.append(fs)
        elif hit is None:
            # ★ 2026-09-14：既未吸附到控件、焦点也无变化 → 很可能点在空白/自绘区，
            # 或坐标换算有偏差。此前仍返回「已点击」的成功语义，模型无法察觉动作
            # 未生效 → 反复点同一坐标直到看门狗介入（obs.success=True 也拦不住）。
            # 此处给出明确的「未确认命中」提示与替代路径建议。
            parts.append(
                "⚠️ 未吸附到控件且焦点未变化——可能未命中可交互元素（点偏或点在空白）。"
                "建议：改用 click control=<类名>，或 click find='目标描述'（VL 定位）；"
                "自绘界面可先 screenshot + vision 确认坐标再点"
            )
        return self._ok("；".join(parts))

    async def _do_double_click(self, x: int = -1, y: int = -1, **kw) -> Observation:
        px, py, w = self._click_point_with_win({"x": x, "y": y, **kw})
        if px < 0 or py < 0:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 或 rel_x/rel_y）")
        from pywinauto import mouse

        cx, cy, hit = self._apply_snap(w, str(kw.get("snap", "true")), px, py)
        mouse.double_click(button="left", coords=(cx, cy))
        await asyncio.sleep(0.08)
        parts = [f"已双击 ({cx},{cy})"]
        if hit is not None:
            parts.append("吸附→" + _ctrl_line(hit).lstrip("- "))
        fs = _focus_summary()
        if fs:
            parts.append(fs)
        return self._ok("；".join(parts))

    async def _do_right_click(self, x: int = -1, y: int = -1, **kw) -> Observation:
        px, py, w = self._click_point_with_win({"x": x, "y": y, **kw})
        if px < 0 or py < 0:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 或 rel_x/rel_y）")
        from pywinauto import mouse

        cx, cy, hit = self._apply_snap(w, str(kw.get("snap", "true")), px, py)
        mouse.right_click(coords=(cx, cy))
        await asyncio.sleep(0.08)
        parts = [f"已右键点击 ({cx},{cy})"]
        if hit is not None:
            parts.append("吸附→" + _ctrl_line(hit).lstrip("- "))
        fs = _focus_summary()
        if fs:
            parts.append(fs)
        return self._ok("；".join(parts))

    async def _do_scroll(
        self, x: int = -1, y: int = -1, scroll: str = "down", amount: int = 3, **kw,
    ) -> Observation:
        """滚动鼠标滚轮；坐标缺省 = 鼠标当前位置.

        坐标支持 rel_x/rel_y（2026-09-05，相对目标窗口矩形比例换算）——
        滚轮只要求落点在某区域内即可，用 rel 免 vision 绝对像素换算。
        """
        from pywinauto import mouse

        coords = None
        rx, ry = kw.get("rel_x"), kw.get("rel_y")
        if rx is not None and ry is not None:
            w = _find_wrapper(
                kw.get("title", ""), kw.get("title_re", False),
                kw.get("index", 0), timeout=kw.get("timeout", 5) or 5,
                process=kw.get("process", ""),
            )
            if w is None:
                return self._err(ERROR_NOT_FOUND, "未找到窗口（rel_x/rel_y 需要目标窗口 title/process）")
            r = w.rectangle()
            coords = (
                r.left + int(float(rx) * r.width()),
                r.top + int(float(ry) * r.height()),
            )
        elif x >= 0 and y >= 0:
            coords = (int(x), int(y))
        delta = abs(int(amount)) if amount else 3
        mouse.scroll(coords=coords, wheel_dist=delta if scroll != "up" else -delta)
        return self._ok(f"已滚动 {scroll} {delta} 格" + (f" @{coords}" if coords else "（鼠标当前位置）"))

    async def _do_drag(
        self, x: int = -1, y: int = -1, x2: int = -1, y2: int = -1,
        duration: float = 0.6, steps: int = 24, **kw,
    ) -> Observation:
        """真实拖拽：按下 → 分步移动 → 释放.

        背景（2026-09-04）：目标应用（滑块/画布/拖文件入窗）靠 WM_MOUSEMOVE
        事件流做 Hit-Test——瞬移（一次 move 到终点）会被判定为非拖拽而丢弃。
        分步 move + 每步 sleep 产生真实事件流；duration 建议 >=0.6s。
        坐标（2026-09-05 统一防点错）：
        - 起点：x/y 绝对 或 rel_x/rel_y；默认 snap 吸附到命中最深控件中心
          （起点若点偏到列表行间隙会变"框选"而非拖动——吸附兜底）。
        - 终点：rel_x2/rel_y2（相对起点同一窗口换算，窗口移动不失效）或
          x2/y2 屏幕绝对（跨窗口拖拽用）；终点是精确落点，不吸附。
        """
        px, py, w = self._click_point_with_win({"x": x, "y": y, **kw})
        if px < 0 or py < 0:
            return self._err(ERROR_INVALID_ARGS, "缺少起点坐标（x/y 或 rel_x/rel_y）")
        from pywinauto import mouse

        # 起点吸附：按下位置纠偏到命中最深控件上（失败保持原坐标）
        sx, sy, hit = self._apply_snap(w, str(kw.get("snap", "true")), px, py)
        # 终点：rel_x2/rel_y2 按起点同一窗口换算优先；否则 x2/y2 屏幕绝对
        rx2, ry2 = kw.get("rel_x2"), kw.get("rel_y2")
        if rx2 is not None and ry2 is not None:
            if w is None:
                return self._err(
                    ERROR_INVALID_ARGS,
                    "rel_x2/rel_y2 需要起点窗口上下文（title/process）——跨窗口拖拽请用 x2/y2 绝对坐标",
                )
            r = w.rectangle()
            ex = r.left + int(float(rx2) * r.width())
            ey = r.top + int(float(ry2) * r.height())
            end = f"rel({rx2},{ry2})→({ex},{ey})"
        elif int(x2) >= 0 and int(y2) >= 0:
            ex, ey = int(x2), int(y2)
            end = f"({ex},{ey})"
        else:
            return self._err(ERROR_INVALID_ARGS, "缺少终点坐标（rel_x2/rel_y2 或 x2/y2）")

        n = max(4, int(steps))
        dur = max(0.1, float(duration or 0.6))
        mouse.press(button="left", coords=(sx, sy))
        await asyncio.sleep(0.15)  # 让目标注册按下状态
        for i in range(1, n + 1):
            xi = sx + (ex - sx) * i // n
            yi = sy + (ey - sy) * i // n
            mouse.move(coords=(xi, yi))
            await asyncio.sleep(dur / n)
        await asyncio.sleep(0.1)
        mouse.release(button="left", coords=(ex, ey))
        parts = [f"已拖拽 ({sx},{sy}) → {end}（{n} 步 / {dur:.1f}s）"]
        if hit is not None:
            parts.append("吸附→" + _ctrl_line(hit).lstrip("- "))
        return self._ok("；".join(parts))

    async def _do_copy_file(self, file: str = "", **kw) -> Observation:
        """文件放入剪贴板（CF_HDROP）——聊天应用发文件的第一步.

        配套动作：click 聊天输入框 → press_key ^v → press_key {ENTER}。
        多文件用 ';' 分隔。详见 _copy_files_to_clipboard 注释。
        """
        if not (file or "").strip():
            return self._err(ERROR_INVALID_ARGS, "缺少 file 参数（要放入剪贴板的文件绝对路径，多个用 ';' 分隔）")
        files = [s.strip() for s in re.split(r"[;\n]", file) if s.strip()]
        if _copy_files_to_clipboard(files):
            return self._ok(
                f"已放入剪贴板 {len(files)} 个文件: " + "; ".join(files)
                + "\n下一步: click 聊天输入框 → press_key '^v' → press_key '{ENTER}' 即可发送"
            )
        missing = [p for p in files if not Path(p).exists()]
        return self._err(
            ERROR_INVALID_ARGS,
            "文件复制到剪贴板失败（剪贴板被占用或路径不存在: " + "; ".join(missing or files) + "）",
        )

    async def _do_macro(self, macro: str = "", **kw) -> Observation:
        """确定性序列宏——把 >=3 个无决策分支的原子动作压成一次工具调用.

        收益：正常 GUI 流程 N 个动作 = N 次 LLM 往返（每步还要截图→vision→决策），
        打包成宏后 = 1 次往返 + 可选的 1 张最终截图。步骤间不做任何 LLM/vision
        决策，因此每步取值必须在调用前完全确定（固定 rel 坐标/已知文本）。

        步骤格式（JSON 数组，逐项传给对应 _do_* 处理器）:
          {"steps": [
              {"action": "click_type", "process": "Weixin", "rel_x": 0.085, "rel_y": 0.055,
               "text": "媳妇"},
              {"action": "sleep", "seconds": 1.0},
              {"action": "press_key", "keys": "{ENTER}"},
              ...
          ], "fail_fast": true}
        允许的动作 = 写操作 + wait（read 操作含截图不参与，宏结束统一 verify）；
        macro 顶层 process/title/title_re/index/timeout 作为各步骤缺省窗口上下文。
        """
        import asyncio
        import json

        allowed = {
            "activate", "launch", "close_window", "click", "double_click",
            "right_click", "click_control", "type_control", "type_text",
            "press_key", "scroll", "drag", "click_type", "copy_file", "wait",
        }
        try:
            spec = json.loads((macro or "").strip() or "{}")
        except json.JSONDecodeError as e:
            return self._err(ERROR_INVALID_ARGS, f"macro 不是合法 JSON: {e}")
        if not isinstance(spec, dict):
            return self._err(ERROR_INVALID_ARGS, "macro 须为 JSON 对象，形如 {\"steps\": [...]}")
        steps = spec.get("steps")
        if not isinstance(steps, list) or not steps:
            return self._err(ERROR_INVALID_ARGS, "macro.steps 缺失或为空")
        if len(steps) > _MACRO_MAX_STEPS:
            return self._err(ERROR_INVALID_ARGS, f"macro 步数 {len(steps)} 超过上限 {_MACRO_MAX_STEPS}")
        fail_fast = bool(spec.get("fail_fast", True))

        # 宏级默认窗口上下文（步骤未显式指定时继承）
        defaults = {
            k: kw[k] for k in ("process", "title", "title_re", "index", "timeout")
            if kw.get(k) not in (None, "", False)
        }

        lines = [f"macro 开始（{len(steps)} 步, fail_fast={fail_fast}）"]
        t0 = time.monotonic()
        done = 0
        for i, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                lines.append(f"step{i}: <跳过非对象步骤 {step!r}>")
                continue
            action = str(step.get("action") or "").strip()
            if action == "sleep":
                secs = max(0.0, min(float(step.get("seconds", 0.5) or 0.5), 15.0))
                if secs > 0:
                    await asyncio.sleep(secs)
                lines.append(f"step{i} [sleep] OK: {secs}s")
                done = i
                continue
            if action == "macro" or action not in allowed:
                return self._err(
                    ERROR_INVALID_ARGS,
                    f"macro 第 {i} 步含非法动作: {action!r}"
                    f"（可用: {', '.join(sorted(allowed))}, sleep）",
                )
            if time.monotonic() - t0 > _MACRO_TOTAL_TIMEOUT:
                lines.append(f"step{i}: 整体超时（>{_MACRO_TOTAL_TIMEOUT:.0f}s），中止")
                break
            args = dict(step)
            args.pop("action", None)
            # 步骤内禁止单独截图（否则每步一张图，宏的意义就没了）；结束统一 verify
            args.pop("verify_screenshot", None)
            args.pop("verify", None)
            for k, v in defaults.items():
                args.setdefault(k, v)
            try:
                obs = await getattr(self, f"_do_{action}")(**args)
            except Exception as e:  # noqa: BLE001 — 步骤级兜底，失败可定位
                logger.exception("macro step %s (%s) 异常", i, action)
                lines.append(f"step{i} [{action}] EXCEPTION: {type(e).__name__}: {e}")
                if fail_fast:
                    return self._err(
                        ERROR_INTERNAL,
                        "\n".join(lines) + f"\n→ 宏在第 {i} 步中止（步骤抛异常）",
                    )
                continue
            done = i
            head = ""
            if obs and obs.output:
                head = obs.output.splitlines()[0]
                if len(head) > _MACRO_STEP_LOG_LEN:
                    head = head[:_MACRO_STEP_LOG_LEN] + "…"
            if obs and obs.success:
                lines.append(f"step{i} [{action}] OK: {head}")
            else:
                lines.append(f"step{i} [{action}] FAIL: {head or '<无输出>'}")
                if fail_fast:
                    return self._err(
                        ERROR_INTERNAL,
                        "\n".join(lines)
                        + f"\n→ 宏在第 {i} 步中止（fail_fast=true；按上文定位后重试，或整体换思路）",
                    )
        elapsed = time.monotonic() - t0
        lines.append(
            f"macro 完成: {done}/{len(steps)} 步成功，耗时 {elapsed:.1f}s"
            f"（1 次工具调用 ≈ 省去 {done - 1} 次 LLM 往返）"
        )
        return self._ok(
            "\n".join(lines),
            {
                "macro_steps_ok": done,
                "macro_steps_total": len(steps),
                "macro_elapsed": round(elapsed, 1),
            },
        )

    async def _do_clip_read(self, **kw) -> Observation:
        """读取剪贴板（文本/文件列表）——感知用户刚复制的内容."""
        text, files = _read_clipboard()
        if files:
            return self._ok(
                "剪贴板内容: 文件\n" + "\n".join(files),
                {"type": "files", "files": files},
            )
        if text:
            preview = text[:500] + ("…" if len(text) > 500 else "")
            return self._ok(
                f"剪贴板内容: 文本（{len(text)} 字符）\n{preview}",
                {"type": "text", "text_len": len(text)},
            )
        return self._ok("剪贴板为空或内容类型不支持（仅支持文本/文件）")

    async def _do_probe(self, x: int = -1, y: int = -1, **kw) -> Observation:
        """坐标命中检测（读操作）——点之前先看 (x,y) 处是什么.

        背景（2026-09-05）：vision/rel 目测坐标常偏几个像素导致点错。
        三档自动探测（2026-09-05 通用化，适用任意 Windows 软件）：先 UIA
        树按"矩形包含 + 面积最小"找最深控件；UIA 空树自动换 win32 经典树
        （老式 Delphi/MFC/VB6）；均无命中 = 自绘区。输出 [T1-UIA]/
        [T2-Win32]/[T3-自绘] 档位标记与对应策略、控件类型/名称/矩形、
        可吸附中心与窗口内 rel≈。命中 = 坐标方向正确；未命中 = 空白或
        自绘区，点前先按截图改 rel 再 probe。
        """
        px, py, w = self._click_point_with_win({"x": x, "y": y, **kw})
        if px < 0 or py < 0:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 或 rel_x/rel_y）")
        if w is None:
            return self._ok(
                f"probe ({px},{py}) → 无匹配窗口（点在桌面/其它窗口上，或窗口未找到）"
            )
        try:
            r = w.rectangle()
            wr = f"rect=({r.left},{r.top},{r.right - r.left}x{r.bottom - r.top})"
        except Exception:  # noqa: BLE001
            r = None
            wr = "rect=?"
        lines = [f"probe ({px},{py}) → {_win_summary(w)} {wr}"]
        # 坐标与窗口矩形关系诊断（2026-09-05）：区分"点错位置"与"坐标系错位"
        if r is not None:
            inside = r.left <= px < r.right and r.top <= py < r.bottom
            if not inside:
                lines.append(
                    f"警告: 点不在窗口矩形内（窗口左上=({r.left},{r.top})，"
                    f"相对窗口偏移=({px - r.left},{py - r.top})）"
                    "——若坐标取自窗口截图则是窗口内坐标，点击应改 rel_x/rel_y；"
                    "若取自全屏截图请重查窗口位置（窗口可能已移动）"
                )
            else:
                rw = max(1, r.right - r.left)
                rh = max(1, r.bottom - r.top)
                lines.append(
                    f"点在窗口内 rel≈({(px - r.left) / rw:.3f},{(py - r.top) / rh:.3f})"
                )
        c = _hit_control(w, px, py)
        if c is not None:
            lines.append("[T1-UIA] 命中: " + _ctrl_line(c))
            # 2026-09-08：吸附预告与 click 实际判定对齐 —— 此前 probe 直报命中
            # 控件中心并承诺"click 会点这里"，但 click 走 _snap_control 多了
            # 近整窗 0.6 面积剔除；同一坐标 probe 承诺的落点 click 并不会去
            try:
                sx, sy, sc = _snap_control(w, px, py)
                if sc is not None:
                    lines.append(f"可吸附中心 snap=({sx},{sy}) — click 会点这里")
                else:
                    lines.append("命中控件近整窗（容器残留），click 不会吸附——将点击原始坐标")
            except Exception:  # noqa: BLE001
                pass
            lines.append("策略: 控件名操作优先（click_control/type_control）；坐标点击用 rel + snap 吸附")
        else:
            # T2：UIA 空树 → win32 经典树再探（老式 Delphi/MFC 应用在 UIA 下常为空树）
            c2 = None
            w32 = _win32_wrapper(w)
            if w32 is not None:
                c2 = _hit_w32(w32, px, py)
            if c2 is not None:
                lines.append("[T2-Win32] UIA 无命中，经典树命中: " + _ctrl_line(c2))
                try:
                    cr = c2.rectangle()
                    cx = cr.left + (cr.right - cr.left) // 2
                    cy = cr.top + (cr.bottom - cr.top) // 2
                    lines.append(f"可吸附中心 snap=({cx},{cy}) — click 吸附已自动兜底 win32 树")
                except Exception:  # noqa: BLE001
                    pass
                lines.append("策略: 老式软件——click_control/type_control 按控件名最稳（已自动兜底 win32 树）")
            else:
                lines.append("[T3-自绘] UIA 与 Win32 经典树均无命中——该点是空白或自绘区，不要坐标盲点")
                lines.append("策略: 全屏截图 → 目测 rel_x/rel_y → probe 复检 → click(snap=false) → 截图验证 → 固化 macro")
        return self._ok("\n".join(lines))

    # ── 内部 ──────────────────────────────────────────────

    @staticmethod
    def _locate_control(w, control: str, control_type: str, control_index: int):
        """在窗口内按 name/type 过滤定位控件，返回第 control_index 个匹配.

        UIA 树找不到时自动兜底 win32 经典树（老式 Delphi/MFC 应用，2026-09-05），
        使 click_control/type_control 对老式软件同样可按控件名操作。
        """
        matched = []
        try:
            ctrls = w.descendants()
        except Exception:  # noqa: BLE001
            ctrls = []
        for c in ctrls:
            try:
                if control and control not in (c.window_text() or ""):
                    continue
                if control_type:
                    ctype = (getattr(c.element_info, "control_type", None) or "").lower()
                    if ctype != control_type.lower():
                        continue
            except Exception:  # noqa: BLE001
                continue
            matched.append(c)
            if len(matched) > max(50, control_index + 1):
                break
        if not matched:
            w32 = _win32_wrapper(w)
            if w32 is not None:
                try:
                    ctrls = w32.descendants()
                except Exception:  # noqa: BLE001
                    ctrls = []
                for c in ctrls:
                    try:
                        if control and control not in (c.window_text() or ""):
                            continue
                        if control_type:
                            try:
                                cls = (c.friendly_class_name() or "").lower()
                            except Exception:  # noqa: BLE001
                                cls = (c.class_name() or "").lower()
                            if cls != control_type.lower():
                                continue
                        matched.append(c)
                        if len(matched) > max(50, control_index + 1):
                            break
                    except Exception:  # noqa: BLE001
                        continue
        if not matched:
            return None
        return matched[min(control_index, len(matched) - 1)]

    def _err(self, code: str, msg: str) -> Observation:
        return Observation(tool_name=self.name, success=False, output=msg,
                           error=msg, error_code=code)

    def _ok(self, msg: str, metadata: dict[str, Any] | None = None) -> Observation:
        return Observation(tool_name=self.name, success=True, output=msg,
                           metadata=metadata or {})


# 模块顶层注册（registry.discover 导入本模块时生效）
# ★ 2026-09-14 平台条件注册：非 Windows 不注册 desktop 工具（模型看不到），
# 避免 builtin 工具发现链路在 Linux/macOS 上因 Win32 依赖异常。
# _IS_WINDOWS 在模块顶部定义；import 本身惰性安全（pywinauto 全部函数内导入）。
if _IS_WINDOWS:
    ToolRegistry.register(DesktopTool())
