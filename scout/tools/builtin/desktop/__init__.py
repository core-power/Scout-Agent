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

import ctypes.wintypes as wt  # noqa: F401  —  供 _paste_text 使用
import logging
import os
import re
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

# 写操作名单（便于上层策略/审计识别；工具内仅作日志标记）
_WRITE_ACTIONS = {
    "activate", "launch", "close_window", "click", "double_click",
    "right_click", "click_control", "type_control", "type_text",
    "press_key", "scroll", "drag", "click_type", "copy_file",
}

_READ_ACTIONS = {
    "list_windows", "find_window", "active_window", "read_controls",
    "screenshot", "wait", "clip_read",
}

_ALL_ACTIONS = sorted(_READ_ACTIONS | _WRITE_ACTIONS)

# read_controls / list_windows 输出上限（防爆屏）
_MAX_WINDOWS = 40
_MAX_CONTROLS = 60
_CTRL_TEXT_LEN = 80

# 截图目录（数据目录下，与 browser 截图同区）
_SHOT_DIR = Path(_SCOUT_DATA_DIR) / "screenshots"

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


def _ctrl_line(c) -> str:
    try:
        t = c.window_text() or ""
        line = (
            f"- {c.element_info.control_type} | name=\"{t[:_CTRL_TEXT_LEN]}\" "
            f"| auto_id=\"{c.element_info.automation_id or ''}\""
        )
        # 物理矩形（DPI 感知后 = 真实屏幕像素，可直接用于 click x/y）
        try:
            r = c.rectangle()
            line += f" | rect=({r.left},{r.top},{r.width()},{r.height()})"
        except Exception:  # noqa: BLE001
            pass
        # 输入类控件：读实际内容（ValuePattern 优先；UWP 应用 texts() 只回 Name）
        ctype = (c.element_info.control_type or "").lower()
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


class DesktopTool(ToolDefinition):
    """本机桌面 GUI 自动化（Windows）."""

    name = "desktop"
    description = (
        "Control desktop GUI apps on Windows — THE tool for any GUI task (WeChat/QQ/Feishu/any "
        "window: launch, click, type, read screens, send keys). Never drive GUIs via shell.\n"
        "ROUTE: GUI app/window → this tool; CLI tasks → shell; reminders/AI timers → scheduler; "
        "absent-user/boot tasks → shell schtasks (current-user tasks need no admin; NEVER /RU "
        "SYSTEM for GUI).\n"
        "FLOW: list_windows/find_window → activate → read_controls (UIA names+rects) or "
        "screenshot→vision for coordinates → click_control/type_control by name. Custom-drawn UIs "
        "(WeChat 4.x exposes NO UIA controls): rel_x/rel_y or x/y clicks + type_text/press_key.\n"
        "SPEED: (a) click_type = click+type+keys in ONE call — prefer over separate calls; "
        "(b) verify_screenshot=true attaches post-action screenshot, saving a call; "
        "(c) screenshots default 0.5 downscale — multiply vision coords by 2; "
        "(d) vision only at decision points, otherwise trust tool results. "
        "Repeated ops on the same window auto-hit a 2s handle cache (no need to re-find).\n"
        "CLICK ACCURACY: click/click_type results include focus=<ClassName> of the control "
        "that received the click — sanity-check it (search box click → focus should be an "
        "Edit/Input class; still a Pane means you MISSED → adjust coords and retry, don't "
        "type blindly). Vision-read coords may be off by a few px; prefer rel_x/rel_y on "
        "fixed-layout apps.\n"
        "WINDOWS: prefer process= over title= (WeChat: 'Weixin'; title = current chat name, "
        "changes constantly). [min] windows have off-screen rects — activate first. One DPI-aware "
        "physical-pixel system: screenshot coords = control rects = mouse coords.\n"
        "FIELD NOTES: UAC prompts can NOT be automated — if launch/click hangs, tell the user to "
        "approve. IME: wait 0.3s between CJK typing and {ENTER}, or paste=true bypasses IME. "
        "Send files in chat apps: copy_file → click input → ^v → {ENTER} (NEVER drag). "
        "clip_read when user references just-copied content. Open/Save dialogs: type_control the "
        "full absolute path + {ENTER} — don't click Browse. drag: duration >=0.6s (Hit-Test needs "
        "real move events; instant moves discarded).\n"
        "WECHAT 4.x (fixed layout → use rel coords, NEVER vision pixel coords — chat models are "
        "unreliable at pixel grounding): search box ≈(0.085,0.055), no caret shown after click but "
        "keys register; first result row ≈rel_y 0.12–0.20 (vision reads text only); input box "
        "≈(0.60,0.925); send ≈(0.46,0.94). Send message: launch Weixin → click_type rel(0.085,0.055) "
        "text=<contact> → wait 1s → screenshot+vision pick first entry → click it → click_type "
        "rel(0.60,0.925) text=<msg> → {ENTER} → screenshot verify."
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
            "scale": {
                "type": "number",
                "description": "screenshot downscale, default 0.5 (multiply vision coords by 1/scale). 1.0 = 1:1.",
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
            "x": {"type": "integer", "description": "X screen coordinate (click/scroll/drag start)."},
            "y": {"type": "integer", "description": "Y screen coordinate (click/scroll/drag start)."},
            "x2": {
                "type": "integer",
                "description": "drag: end X (screen absolute).",
            },
            "y2": {
                "type": "integer",
                "description": "drag: end Y (screen absolute).",
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
                "description": "Click at fraction (0~1) of window WIDTH (0.085 = 8.5% from left). "
                "Preferred over vision pixel coords for fixed-layout apps.",
            },
            "rel_y": {
                "type": "number",
                "description": "Click at fraction (0~1) of window HEIGHT.",
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
            "timeout": {
                "type": "integer",
                "description": "Seconds for wait/find retry (default 10).",
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
        action = str(kwargs.get("action") or "").strip()
        if action not in _ALL_ACTIONS:
            return self._err(
                ERROR_INVALID_ARGS,
                f"未知 action: {action}（可用: {', '.join(_ALL_ACTIONS)}）",
            )
        # 任何操作前先统一坐标系（高 DPI 屏防点击偏移）
        _ensure_dpi_aware()
        if action in _WRITE_ACTIONS:
            logger.info("desktop tool write action: %s args=%s", action, kwargs)

        try:
            handler = getattr(self, f"_do_{action}")
            obs = await handler(**kwargs)
        except RuntimeError as e:
            # 依赖缺失等可读错误
            return self._err(ERROR_INTERNAL, str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception("desktop tool error (%s)", action)
            return self._err(ERROR_INTERNAL, f"{type(e).__name__}: {e}")

        # 写操作执行成功且请求了 verify_screenshot → 自动附带窗口截图
        # （省一轮独立的 screenshot 工具调用 = 省一次 LLM 往返）
        if (
            obs.success
            and action in _WRITE_ACTIONS
            and str(kwargs.get("verify_screenshot", "")).lower() in ("1", "true", "yes")
        ):
            try:
                shot = await self._do_screenshot(**kwargs)
                if shot.success:
                    first_line = shot.output.splitlines()[0]
                    obs = self._ok(
                        obs.output + f"\n[verify_screenshot] {first_line}",
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
        ctrls = sorted(ctrls, key=_key)
        lines = []
        for c in ctrls:
            try:
                ctype = c.element_info.control_type or ""
            except Exception:  # noqa: BLE001
                continue
            if control_type and ctype.lower() != control_type.lower():
                continue
            if control and control not in (c.window_text() or ""):
                continue
            lines.append(_ctrl_line(c))
            if len(lines) >= _MAX_CONTROLS:
                lines.append(f"...（超过 {_MAX_CONTROLS} 个控件，已截断；可用 control/control_type/depth 过滤）")
                break
        return self._ok(
            f"窗口 \"{w.window_text()}\" 控件 {len(lines)} 个:\n" + ("\n".join(lines) or "（无匹配控件）")
        )

    async def _do_screenshot(
        self, title: str = "", title_re: bool = False, index: int = 0,
        window_only: bool = False, scale: float = 0.0, **kw,
    ) -> Observation:
        _SHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
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

            try:
                img = ImageGrab.grab(all_screens=True)
            except OSError:
                try:
                    img = ImageGrab.grab()
                except OSError:
                    return self._err(
                        ERROR_INTERNAL,
                        "全屏截图失败：本机屏幕 DC 被系统拦截（DLP/安全软件）。"
                        "请改用窗口截图：screenshot + process=<应用名> + window_only=true。",
                    )
        # 空屏守卫（2026-09-04）：窗口未渲染完成时 PrintWindow 会返回近乎纯色的
        # 小图（实测 3131 字节整）——vision 读它只能得到空描述，误导 agent 决策。
        # 检测：PNG 编码体积异常小 + 图像色彩单一 → 重抓一次（多数情况第二次已渲染完）。
        try:
            import io

            png_size = len(_img_to_png_bytes(img))
            w_px, h_px = img.size
            colors = len(img.convert("RGB").getcolors(maxcolors=256) or []) if (w_px * h_px) else 0
            if png_size < 8000 and colors <= 8 and w_px * h_px > 50000:
                time.sleep(1.2)
                retry = _printwindow_capture(w.handle) if (window_only or title or (kw.get("process") or "").strip()) else None
                if retry is not None:
                    img = retry
        except Exception:  # noqa: BLE001 — 守卫失败不影响正常截图流程
            pass
        # 降采样（默认 0.5：vision API 对 1888x1150 级大图单次推理实测 5~60s，
        # 减半后体积/推理时间约降 60-70%，按钮级定位精度不受影响）
        try:
            s = float(scale) if scale else 0.5
        except (TypeError, ValueError):
            s = 0.5
        if 0 < s < 1.0:
            img = img.resize((max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))))
            note = f"（已降采样 {s:.0%}，坐标需按比例换算：原坐标 = 截图坐标 / {s:.2f}）"
        else:
            note = "（1:1 原始像素，可直接用于 click x/y）"
        img.save(str(path))
        return self._ok(
            f"截图已保存: {path}（{img.size[0]}x{img.size[1]}）{note}\n"
            f"提示: 可用 vision 工具读取该图片让模型理解界面并返回目标坐标。",
            {"path": str(path), "width": img.size[0], "height": img.size[1]},
        )

    async def _do_wait(
        self, title: str = "", title_re: bool = False, state: str = "appear",
        timeout: int = 10, **kw,
    ) -> Observation:
        if not title and not (kw.get("process") or "").strip():
            return self._err(ERROR_INVALID_ARGS, "缺少 title 或 process 参数")
        t0 = time.time()
        if state == "vanish":
            while time.time() - t0 < timeout:
                if _find_wrapper(title, title_re, timeout=0.0, process=kw.get("process", "")) is None:
                    label = title or (kw.get("process") or "")
                    return self._ok(f"窗口已消失: {label!r}（耗时 {time.time()-t0:.1f}s）")
                time.sleep(0.5)
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
            return self._ok(f"已启动: {resolved}")
        # 解析不到（Store 应用不注册 App Paths）→ 回退 os.startfile 原样启动，
        # 由 Windows Shell 解析（支持 Store 别名 notepad/mspaint、PATH、文件关联）。
        try:
            os.startfile(target)  # noqa: S606
            return self._ok(f"已启动: {target}（由 Windows Shell 解析）")
        except OSError as e:
            return self._err(
                ERROR_NOT_FOUND,
                f"无法启动 {target!r}: {e}。请提供完整路径，"
                "或先用 shell 的 where/注册表查询安装位置。",
            )

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
            return self._ok(f"已点击控件: {_ctrl_line(target)}")
        except Exception as e:  # noqa: BLE001
            # 兜底：控件中心物理坐标点击（自绘 UI/无 Click 模式时 click_input 会失败）
            try:
                from pywinauto import mouse

                r = target.rectangle()
                cx, cy = r.left + r.width() // 2, r.top + r.height() // 2
                mouse.click(button="left", coords=(cx, cy))
                return self._ok(
                    f"已点击控件（坐标兜底 {cx},{cy}）: {_ctrl_line(target)}；"
                    f"click_input 失败原因: {type(e).__name__}"
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
        return self._ok(f"已输入文本到 {_ctrl_line(target)}")

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

    def _resolve_click_point(self, kwargs: dict) -> tuple[int, int] | None:
        """解析点击坐标：支持绝对 x/y 与窗口相对比例 rel_x/rel_y（0~1）.

        rel 模式（2026-09-03）：vision 对话模型返回的像素坐标实测不可靠（偏差
        达窗口尺寸量级），而微信等应用窗口布局固定——搜索框恒在左上 ≈(0.085,
        0.055)。按窗口矩形比例计算物理坐标，稳定可靠。
        """
        x = kwargs.get("x")
        y = kwargs.get("y")
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            return int(x), int(y)
        rx = kwargs.get("rel_x")
        ry = kwargs.get("rel_y")
        if rx is None or ry is None:
            return None
        w = _find_wrapper(
            kwargs.get("title", ""), kwargs.get("title_re", False),
            kwargs.get("index", 0), timeout=kwargs.get("timeout", 5) or 5,
            process=kwargs.get("process", ""),
        )
        if w is None:
            return None
        # 强制前台：pywinauto set_focus 对微信等自绘窗口不可靠，强行抢前台
        _force_foreground(w.handle)
        r = w.rectangle()
        return (
            r.left + int(float(rx) * r.width()),
            r.top + int(float(ry) * r.height()),
        )

    async def _do_click_type(
        self, x: int = -1, y: int = -1, text: str = "", keys: str = "",
        click_delay: float = 0.3, **kw,
    ) -> Observation:
        """复合动作：坐标点击 → 等待 → 输入文本 → 可选按键（如 {ENTER}）.

        一次工具调用完成"点输入框+打字+回车"，省 2 轮 LLM 往返（实测每轮
        ReAct 循环 2-5s LLM + 可能的截图/vision 验证 5-60s）。
        支持相对坐标 rel_x/rel_y（按目标窗口矩形比例，微信等固定布局首选）。
        """
        point = self._resolve_click_point({"x": x, "y": y, **kw})
        if point is None:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 绝对坐标 或 rel_x/rel_y 窗口相对比例）")
        if not text and not keys:
            return self._err(ERROR_INVALID_ARGS, "缺少 text（输入内容）或 keys（按键）参数")
        from pywinauto import mouse
        from pywinauto.keyboard import send_keys

        px, py = point
        mouse.click(button="left", coords=(px, py))
        time.sleep(max(0.05, float(click_delay or 0.3)))
        parts = [f"({px},{py})"]
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
        point = self._resolve_click_point({"x": x, "y": y, **kw})
        if point is None:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 或 rel_x/rel_y）")
        from pywinauto import mouse

        mouse.click(button="left", coords=point)
        time.sleep(0.08)
        fs = _focus_summary()
        return self._ok(f"已左键点击 ({point[0]},{point[1]})" + (f"；{fs}" if fs else ""))

    async def _do_double_click(self, x: int = -1, y: int = -1, **kw) -> Observation:
        point = self._resolve_click_point({"x": x, "y": y, **kw})
        if point is None:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 或 rel_x/rel_y）")
        from pywinauto import mouse

        mouse.double_click(button="left", coords=point)
        return self._ok(f"已双击 ({point[0]},{point[1]})")

    async def _do_right_click(self, x: int = -1, y: int = -1, **kw) -> Observation:
        point = self._resolve_click_point({"x": x, "y": y, **kw})
        if point is None:
            return self._err(ERROR_INVALID_ARGS, "缺少坐标（x/y 或 rel_x/rel_y）")
        from pywinauto import mouse

        mouse.right_click(coords=point)
        return self._ok(f"已右键点击 ({point[0]},{point[1]})")

    async def _do_scroll(
        self, x: int = -1, y: int = -1, scroll: str = "down", amount: int = 3, **kw,
    ) -> Observation:
        from pywinauto import mouse

        # 坐标缺省 = 鼠标当前位置
        coords = (int(x), int(y)) if (x >= 0 and y >= 0) else None
        delta = abs(int(amount)) if amount else 3
        mouse.scroll(coords=coords, wheel_dist=delta if scroll != "up" else -delta)
        return self._ok(f"已滚动 {scroll} {delta} 格" + (f" @({x},{y})" if coords else "（鼠标当前位置）"))

    async def _do_drag(
        self, x: int = -1, y: int = -1, x2: int = -1, y2: int = -1,
        duration: float = 0.6, steps: int = 24, **kw,
    ) -> Observation:
        """真实拖拽：按下 → 分步移动 → 释放.

        背景（2026-09-04）：目标应用（滑块/画布/拖文件入窗）靠 WM_MOUSEMOVE
        事件流做 Hit-Test——瞬移（一次 move 到终点）会被判定为非拖拽而丢弃。
        分步 move + 每步 sleep 产生真实事件流；duration 建议 >=0.6s。
        起点支持 rel_x/rel_y（相对窗口比例），终点用屏幕绝对坐标 x2/y2。
        """
        start = self._resolve_click_point({"x": x, "y": y, **kw})
        if start is None:
            return self._err(ERROR_INVALID_ARGS, "缺少起点坐标（x/y 或 rel_x/rel_y）")
        if int(x2) < 0 or int(y2) < 0:
            return self._err(ERROR_INVALID_ARGS, "缺少终点坐标 x2/y2（屏幕绝对坐标）")
        from pywinauto import mouse

        n = max(4, int(steps))
        dur = max(0.1, float(duration or 0.6))
        mouse.press(button="left", coords=start)
        time.sleep(0.15)  # 让目标注册按下状态
        for i in range(1, n + 1):
            xi = start[0] + (int(x2) - start[0]) * i // n
            yi = start[1] + (int(y2) - start[1]) * i // n
            mouse.move(coords=(xi, yi))
            time.sleep(dur / n)
        time.sleep(0.1)
        mouse.release(button="left", coords=(int(x2), int(y2)))
        return self._ok(
            f"已拖拽 ({start[0]},{start[1]}) → ({int(x2)},{int(y2)})（{n} 步 / {dur:.1f}s）"
        )

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

    # ── 内部 ──────────────────────────────────────────────

    @staticmethod
    def _locate_control(w, control: str, control_type: str, control_index: int):
        """在窗口内按 name/type 过滤定位控件，返回第 control_index 个匹配."""
        try:
            ctrls = w.descendants()
        except Exception:  # noqa: BLE001
            return None
        matched = []
        for c in ctrls:
            try:
                if control and control not in (c.window_text() or ""):
                    continue
                if control_type and (c.element_info.control_type or "").lower() != control_type.lower():
                    continue
            except Exception:  # noqa: BLE001
                continue
            matched.append(c)
            if len(matched) > max(50, control_index + 1):
                break
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
ToolRegistry.register(DesktopTool())
