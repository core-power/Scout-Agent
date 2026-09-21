#!/usr/bin/env python3
"""Scout Agent 绿色版桌面程序启动器.

特性:
  - 免安装: 双击即用，不写注册表、不做文件关联、不设开机自启
  - 数据跟随盘符: 数据目录 = 程序所在盘符根目录/.scout（如 D:\\.scout），不落 C 盘
  - 内嵌 Web 服务: 本地启动 uvicorn，原生 WinForms + WebView2 窗口加载
  - 配置跟随: exe 旁 config/.env 或 .env 可覆盖默认配置
  - 端口自适应: 8848 被占用时自动 +1 探测
  - 兜底降级: 无 WebView2 运行时/程序集时自动打开系统浏览器

用法:
    python desktop/launcher.py            # 图形窗口（Windows / 有 GUI 环境）
    python desktop/launcher.py --no-gui   # 仅启动服务（测试 / 无 GUI 环境）
    python desktop/launcher.py --port 9000 --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from datetime import datetime  # 数据迁移路径写 marker/备份名使用（此前缺失被外层 except 静默吞掉）
import threading
import time
from pathlib import Path


# ─────────────────────────────────────────────────────────────
# 路径解析
# ─────────────────────────────────────────────────────────────
def _is_frozen() -> bool:
    """PyInstaller 打包后 sys.frozen 为 True."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序主目录: 打包后 = exe 所在目录; 开发模式 = 项目根."""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """数据目录（2026-09-04 按用户要求改回盘符根 .scout 为工作目录）:
    - Windows 主目录: <exe 所在盘符>\\.scout（如 D:\\.scout）——历史工作目录，
      用户要求沿用；盘符根不可写时回退 %APPDATA%\\Scout（始终可写）。
    - 回退: exe 旁 data/，再回退用户目录 ~/.scout
    """
    if os.name == "nt":
        candidates = []
        try:
            anchor = Path(sys.executable if _is_frozen() else __file__).resolve().anchor
            if anchor:
                candidates.append(Path(anchor) / ".scout")
        except OSError:
            pass
        try:
            base = os.getenv("APPDATA") or str(Path.home())
            if base:
                candidates.append(Path(base) / "Scout")
        except OSError:
            pass
        for d in candidates:
            try:
                d.mkdir(parents=True, exist_ok=True)
                probe = d / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                return d
            except OSError:
                continue
    d = app_dir() / "data"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return d
    except OSError:
        return Path.home() / ".scout"


def _migrate_appdata_back(ddir: Path) -> None:
    """一次性反向迁移（2026-09-04）：%APPDATA%\\Scout → 盘符根 .scout.

    背景：8/31 曾把数据目录迁到 APPDATA，用户要求工作目录回到 D:\\.scout。
    APPDATA 是较新的活跃数据（config/记忆/会话），必须合并回 .scout 而非
    沿用旧文件。幂等：成功后写 marker + APPDATA 目录改名备份。
    """
    if os.name != "nt":
        return
    try:
        base = os.getenv("APPDATA") or ""
        if not base:
            return
        src = Path(base) / "Scout"
        if not src.is_dir() or not (src / "config.json").exists():
            return
        if ddir.resolve() == src.resolve():
            return
        if (ddir / ".migrated_from_appdata").exists():
            return
        import shutil

        ddir.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.name in ("launcher.log", ".write_probe", "webview2"):
                continue  # 日志/探测/浏览器缓存不搬
            target = ddir / item.name
            try:
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            except Exception:  # noqa: BLE001 — 单项失败不阻断整体
                print(f"[migrate-back] skip {item.name}: {type(item).__name__} error")
        (ddir / ".migrated_from_appdata").write_text(
            datetime.now().isoformat(), encoding="utf-8"
        )
        # 备份改名（同盘原子；失败不影响，marker 已保证幂等）
        try:
            src.rename(src.parent / f"Scout__migrated_{datetime.now():%Y%m%d_%H%M%S}")
        except OSError:
            pass
        print(f"[migrate-back] {src} -> {ddir}")
    except Exception as e:  # noqa: BLE001
        print(f"[migrate-back] failed: {e}")


def _migrate_old_data(new_dir: Path) -> None:
    """一次性迁移旧数据目录（2026-08-31）.

    旧版本可能把配置写在: <盘符>\\.scout、exe 旁 data/、~/.scout。
    仅当 new_dir 尚无 config.json 且旧目录存在 config.json 时迁移，
    避免覆盖新配置；迁移来源按优先级取第一个有效目录。
    """
    if os.name != "nt":
        return
    try:
        if (new_dir / "config.json").exists():
            return
    except OSError:
        return
    import shutil

    old_candidates = []
    try:
        anchor = Path(sys.executable if _is_frozen() else __file__).resolve().anchor
        if anchor:
            old_candidates.append(Path(anchor) / ".scout")
    except OSError:
        pass
    old_candidates.extend([app_dir() / "data", Path.home() / ".scout"])

    for old in old_candidates:
        try:
            if not old.is_dir() or not (old / "config.json").exists():
                continue
            new_dir.mkdir(parents=True, exist_ok=True)
            for item in old.iterdir():
                target = new_dir / item.name
                if item.name in ("launcher.log", ".write_probe"):
                    continue
                if target.exists():
                    continue
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
            print(f"[migrate] data dir migrated: {old} -> {new_dir}")
            return  # 只迁移一个来源，避免多份数据互相覆盖
        except OSError:
            continue


def load_env_files() -> None:
    """加载 exe 旁 config/.env 或 .env（仅补缺，不覆盖已有环境变量）."""
    for p in (app_dir() / "config" / ".env", app_dir() / ".env"):
        if not p.exists():
            continue
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and value and not os.environ.get(key):
                    os.environ[key] = value
        except OSError:
            pass
        break


# ─────────────────────────────────────────────────────────────
# 服务启动
# ─────────────────────────────────────────────────────────────
def pick_port(preferred: int = 8848, tries: int = 50) -> int:
    """从 preferred 起探测可用端口."""
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0  # 交给系统分配


def _run_server(app, host: str, port: int) -> None:
    """在独立线程中运行 uvicorn."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    _SERVER_STATE["server"] = server
    asyncio.run(server.serve())


_SERVER_STATE: dict = {"server": None}


def wait_ready(host: str, port: int, timeout: float = 60.0) -> bool:
    """轮询等待 HTTP 服务就绪."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def build_app():
    """构造 Web 应用（延迟导入，确保环境变量已设置）."""
    from scout.tools.registry import ToolRegistry
    from scout.web.server import create_web_app

    try:
        ToolRegistry.discover()
    except Exception:  # noqa: BLE001
        pass
    return create_web_app()


# ─────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    """写启动日志到 data/launcher.log（windowed 模式无控制台，靠文件排障）."""
    try:
        with open(data_dir() / "launcher.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _redirect_stdio() -> None:
    """★ 修复 windowed 模式（PyInstaller console=False）服务起不来的根因。

    console=False 打包后 sys.stdout/sys.stderr 为 None，uvicorn 等库的 logging
    写 stderr 时抛 AttributeError，导致服务线程崩溃 → 端口永不就绪 → 超时退出。
    这里把 stdout/stderr 重定向到 data/launcher.log，既防崩溃又保留运行日志。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        logf = open(data_dir() / "launcher.log", "a", encoding="utf-8")
        sys.stdout = logf
        sys.stderr = logf
    except Exception:  # noqa: BLE001
        import os as _os

        devnull = open(_os.devnull, "w", encoding="utf-8")
        sys.stdout = devnull
        sys.stderr = devnull


def _enable_dpi_awareness() -> None:
    """启用 Windows 高 DPI 感知（Per-Monitor V2）。

    必须在创建任何窗口之前调用。否则 Windows 会把整个窗口按系统缩放
    位图拉伸，导致文字/界面模糊、布局错位、分辨率不适配。

    ★ 2026-09-11 加成功性验证：设置失败（被 CLR/已有 manifest 抢先等）
    时进程保持 DPI unaware，鼠标/窗口坐标会被虚拟化（150% 缩放下点哪儿
    偏哪儿），desktop 工具的坐标换算全部错位——必须显式留痕，不能默默失败。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        # Windows 10 1703+：DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        state = _verify_dpi_awareness()
        if not ok and state != 2:
            # 降级：旧系统 PROCESS_PER_MONITOR_DPI_AWARE = 2
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
                state = _verify_dpi_awareness()
            except Exception:  # noqa: BLE001
                pass
        if state == 2:
            _log("DPI: Per-Monitor V2 感知已启用（坐标=物理像素）")
        else:
            _log(f"⚠️ DPI 感知设置未生效（状态={state}）——高 DPI/多屏环境下鼠标坐标会被虚拟化，GUI 自动化将错位！")
    except Exception as _e:  # noqa: BLE001
        _log(f"⚠️ DPI 感知设置异常: {_e}")


def _verify_dpi_awareness() -> int:
    """查询本进程 DPI 感知状态（0=unaware 1=system 2=permonitor），失败 -1."""
    try:
        import ctypes

        val = ctypes.c_int(-1)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        hr = ctypes.windll.shcore.GetProcessDpiAwareness(ctypes.c_void_p(h), ctypes.byref(val))
        return val.value if hr == 0 else -1
    except Exception:  # noqa: BLE001
        return -1


def _workarea_size() -> tuple[int, int]:
    """主显示器工作区（扣除任务栏）的自适应窗口尺寸（物理像素）。"""
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        # SPI_GETWORKAREA = 0x0030
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w >= 960 and h >= 640:
            return int(w * 0.9), int(h * 0.88)
    except Exception:  # noqa: BLE001
        pass
    return 1280, 820


def _find_webview_dll(name: str) -> str | None:
    """定位 WebView2 的 .NET 程序集，兼容源码运行与 PyInstaller 打包。

    PyInstaller 打包后：dll 位于 _MEIPASS（onedir 的 _internal）根目录，
    由 hook-webview 从 webview/lib 收集而来；源码运行时位于 webview/lib。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    candidates: list[Path] = []
    if meipass:
        candidates.append(Path(meipass) / name)
    candidates.append(app_dir() / name)
    try:
        import webview.util  # pywebview 自带查找逻辑（含 runtimes 目录）

        candidates.append(Path(webview.util.interop_dll_path(name)))
    except Exception:  # noqa: BLE001
        pass
    candidates.append(Path(__file__).resolve().parent.parent / "webview" / "lib" / name)
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _find_icon_path() -> str | None:
    """定位窗口图标 scout.ico（打包后位于 _internal，源码位于项目根/desktop）."""
    meipass = getattr(sys, "_MEIPASS", None)
    for c in (
        [Path(meipass) / "scout.ico"] if meipass else []
    ) + [
        app_dir() / "scout.ico",
        app_dir() / "desktop" / "scout.ico",
        Path(__file__).resolve().parent / "scout.ico",
        Path(__file__).resolve().parent.parent / "desktop" / "scout.ico",
    ]:
        if c.exists():
            return str(c)
    return None


def _gui_state_path() -> Path:
    """桌面窗口状态（几何 / 退出确认偏好）落盘位置。"""
    return data_dir() / "gui_state.json"


def _load_gui_state() -> dict:
    try:
        p = _gui_state_path()
        if p.is_file():
            v = json.loads(p.read_text(encoding="utf-8"))
            return v if isinstance(v, dict) else {}
    except Exception as e:  # noqa: BLE001
        _log(f"gui state load failed (ignore): {e}")
    return {}


def _save_gui_state(patch: dict) -> None:
    try:
        st = _load_gui_state()
        st.update(patch)
        _gui_state_path().write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        _log(f"gui state save failed (ignore): {e}")


def _restore_geometry(st: dict) -> tuple[int, int, int, int, bool] | None:
    """把上次退出时的窗口几何还原出来。

    ★ 2026-09-19：此前每次启动都是「工作区 90%×88% + 居中」，用户手动调好的
      大小/位置/最大化状态一律不记，双屏或习惯小窗的用户每次都要重摆一遍。
      这里做了两道校验：尺寸下限，以及矩形必须仍与某块屏幕的工作区相交
      （拔掉副屏后窗口不能落在屏幕外，否则表现为「启动后看不见窗口」）。
    """
    try:
        x, y = int(st["x"]), int(st["y"])
        w, h = int(st["w"]), int(st["h"])
    except Exception:  # noqa: BLE001 - 字段缺失/类型不对就走默认
        return None
    if w < 640 or h < 480:
        return None
    try:
        from System.Windows.Forms import Screen

        for s in Screen.AllScreens:
            wa = s.WorkingArea
            # 只判断「相交」不够：剩 1px 也算相交，窗口照样等于看不见。
            # 要求可见部分至少 200×120，才认为这个几何还能用。
            ix1, ix2 = max(x, wa.X), min(x + w, wa.X + wa.Width)
            iy1, iy2 = max(y, wa.Y), min(y + h, wa.Y + wa.Height)
            if ix2 - ix1 >= 200 and iy2 - iy1 >= 120:
                return (x, y, w, h, bool(st.get("maximized")))
    except Exception as _e:  # noqa: BLE001 - Screen 不可用时仍按单屏放行
        _log(f"screen check failed (fallback accept): {_e}")
        return (x, y, w, h, bool(st.get("maximized")))
    return None


def _open_gui(url: str, port: int) -> None:
    """用原生 WinForms + WebView2 打开对话窗口。

    ★ 2026-08-29：不再使用 pywebview。pywebview 6.2.1 经 PyInstaller 打包后
    webview.start() 会卡死（窗口无法显示，进程空转），而手写 WebView2 窗口
    （pythonnet + WinForms + Microsoft.Web.WebView2.WinForms 控件）在打包环境
    已验证正常：窗口显示、内核导航、关闭确认全部可用。
    """
    if os.name != "nt":
        # 非 Windows: 降级打开系统浏览器
        import webbrowser

        webbrowser.open(url)
        print(f"[ok] 非 Windows 环境，已打开系统浏览器: {url}")
        return

    _log(f"_open_gui url={url}")
    core_dll = _find_webview_dll("Microsoft.Web.WebView2.Core.dll")
    winforms_dll = _find_webview_dll("Microsoft.Web.WebView2.WinForms.dll")
    _log(f"webview dll: core={core_dll} winforms={winforms_dll}")
    if not core_dll or not winforms_dll:
        import webbrowser

        webbrowser.open(url)
        print("[warn] 未找到 WebView2 程序集，已打开系统浏览器")
        return

    try:
        import clr

        clr.AddReference(core_dll)
        clr.AddReference(winforms_dll)
        _log("clr.AddReference OK")
        from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2
        # ★ Point 属于 System.Drawing（不是 System.Windows.Forms）。写在 WinForms 的
        # import 列表里在源码环境侥幸可用，打包后解析不到 -> GUI 初始化抛异常 ->
        # 回退成"打开系统浏览器"。2026-09-20 修正。
        from System.Drawing import Icon, Point, Size
        from System.Windows.Forms import (
            Application,
            Button,
            CheckBox,
            DialogResult,
            DockStyle,
            Form,
            FormBorderStyle,
            FormStartPosition,
            FormWindowState,
            Label,
            MessageBox,
            MessageBoxButtons,
            MessageBoxIcon,
        )
        from System.Threading import ApartmentState, Thread, ThreadStart
    except Exception as e:  # noqa: BLE001
        import webbrowser

        webbrowser.open(url)
        print(f"[warn] WebView2 初始化失败（{e}），已打开系统浏览器")
        return

    width, height = _workarea_size()
    wv_cache = data_dir() / "webview2"
    wv_cache.mkdir(parents=True, exist_ok=True)
    icon_path = _find_icon_path()
    gui_state = {"closed": False, "server": _SERVER_STATE.get("server")}

    def run_gui() -> None:
        try:
            _log("gui thread start")
            # ★ 2026-09-19：启用 WinForms 视觉样式。此前没调用，窗体边框/按钮/消息框
            #   会退回 Windows 经典（2000 年代）外观，在 Win10/11 上又灰又平，
            #   且不跟随系统主题 —— 是「桌面端看着不像原生应用」的直接原因。
            try:
                Application.EnableVisualStyles()
                Application.SetCompatibleTextRenderingDefault(False)
                _log("EnableVisualStyles OK")
            except Exception as _e:  # noqa: BLE001
                _log(f"EnableVisualStyles failed (ignore): {_e}")
            # 捕获 .NET 侧异常：pythonnet 事件异常不会传播到 Python try/except，
            # 会静默导致 Application.Run 消息循环退出（表现为窗口几秒后自动关闭）
            from System import AppDomain
            from System.Threading import ThreadExceptionEventArgs

            def _on_thread_exception(sender, e) -> None:
                _log(f"Application.ThreadException: {getattr(e, 'Exception', None)!r}")

            Application.ThreadException += _on_thread_exception

            def _on_unhandled(sender, e) -> None:
                _log(f"AppDomain.UnhandledException: {getattr(e, 'ExceptionObject', None)!r}")

            AppDomain.CurrentDomain.UnhandledException += _on_unhandled

            class MainForm(Form):
                def __init__(self) -> None:
                    super().__init__()
                    self.Text = "Scout Agent"
                    self.Width = width
                    self.Height = height
                    self.MinimumSize = Size(960, 640)
                    # ★ 窗口几何记忆：先按 Manual 定位，恢复失败再退回居中
                    self.StartPosition = FormStartPosition.Manual
                    _g = _restore_geometry(_load_gui_state())
                    if _g:
                        _x, _y, _w, _h, _max = _g
                        self.Location = Point(_x, _y)
                        self.Size = Size(_w, _h)
                        self.WindowState = FormWindowState.Maximized if _max else FormWindowState.Normal
                        _log(f"window geometry restored x={_x} y={_y} w={_w} h={_h} max={_max}")
                    else:
                        self.StartPosition = FormStartPosition.CenterScreen
                        _log(f"window geometry default w={width} h={height}")
                    if icon_path:
                        try:
                            self.Icon = Icon(icon_path)
                        except Exception:  # noqa: BLE001
                            pass
                    self.wv = WebView2()
                    props = CoreWebView2CreationProperties()
                    props.UserDataFolder = str(wv_cache)
                    self.wv.CreationProperties = props
                    self.wv.Dock = DockStyle.Fill
                    self.Controls.Add(self.wv)
                    self.FormClosing += self._on_closing
                    self.Closed += self._on_closed
                    self.Shown += self._on_shown
                    self.wv.CoreWebView2InitializationCompleted += self._on_ready
                    self.wv.EnsureCoreWebView2Async(None)

                def _on_ready(self, sender, args) -> None:
                    _log(f"webview ready IsSuccess={args.IsSuccess} InitException={getattr(args, 'InitializationException', None)}")
                    if args.IsSuccess:
                        cv2 = self.wv.CoreWebView2
                        # ★ 2026-09-04：WebView2 下载支持 —— 未配置默认下载文件夹/下载条时，
                        #   页面 <a download> / Content-Disposition: attachment 的下载会被静默丢弃
                        #   （表现为点下载"没反应"），后端 /api/files/download 实测 200 正常。
                        try:
                            _dl = Path(os.environ.get("USERPROFILE", "")) / "Downloads"
                            if not _dl.is_dir():
                                _dl = data_dir() / "downloads"
                                _dl.mkdir(parents=True, exist_ok=True)
                            cv2.Profile.DefaultDownloadFolderPath = str(_dl)
                            cv2.Settings.IsDefaultDownloadDialogEnabled = True
                            _log(f"webview download folder: {_dl}")
                        except Exception as _e:  # noqa: BLE001 - 下载配置失败不影响主流程
                            _log(f"webview download config failed (ignore): {_e}")

                        # ★ 2026-09-04：下载事件诊断 —— 每个下载打 URL/目标/状态/中断原因到
                        #   launcher.log，用于定位"弹窗提示下载不了"的失败环节（DownloadStarting
                        #   是否触发 -> StateChanged 最终状态 -> InterruptReason 具体原因）。
                        def _on_download_starting(_s, _e) -> None:
                            try:
                                op = _e.DownloadOperation
                                _log(f"DOWNLOAD start uri={getattr(op, 'Uri', '?')} "
                                     f"suggested={getattr(op, 'SuggestedFileName', '?')} "
                                     f"target={getattr(_e, 'ResultFilePath', '?')} "
                                     f"cancel={getattr(_e, 'Cancel', False)}")

                                def _on_dl_state(_s2, _e2) -> None:
                                    try:
                                        st = str(getattr(op, "State", ""))
                                        _log(f"DOWNLOAD state={st}")
                                        if st == "Interrupted":
                                            _log(f"DOWNLOAD interrupted reason={getattr(op, 'InterruptReason', '?')}")
                                        elif st == "Completed":
                                            _log(f"DOWNLOAD completed -> {getattr(op, 'ResultFilePath', '?')}")
                                    except Exception as _x:  # noqa: BLE001
                                        _log(f"DOWNLOAD state cb err: {_x}")

                                try:
                                    op.StateChanged += _on_dl_state
                                except Exception:
                                    op.add_StateChanged(_on_dl_state)
                            except Exception as _x:  # noqa: BLE001
                                _log(f"DOWNLOAD starting cb err: {_x}")

                        try:
                            cv2.DownloadStarting += _on_download_starting
                        except Exception:
                            cv2.add_DownloadStarting(_on_download_starting)
                        _log("webview download events wired")

                        # ★ 2026-09-19：关掉浏览器加速键。桌面应用里这些键全是坑：
                        #   F5 / Ctrl+R 重载页面（丢掉正在输入的草稿、断开 WS）；
                        #   Ctrl+P 打印、Ctrl+S 存网页、Ctrl+G/F3 继续查找、
                        #   Ctrl+ 加减号缩放（误触一次整界面就乱了且没有重置入口）。
                        #   关闭后 Ctrl+F 由前端会话内查找接管（前端只在桌面外壳下接管）。
                        try:
                            cv2.Settings.AreBrowserAcceleratorKeysEnabled = False
                            _log("settings: browser accelerator keys disabled")
                        except Exception as _e:  # noqa: BLE001
                            _log(f"settings accelerator failed (ignore): {_e}")
                        # Ctrl + 滚轮误缩放同理，一并关掉
                        try:
                            cv2.Settings.IsZoomControlEnabled = False
                        except Exception as _e:  # noqa: BLE001
                            _log(f"settings zoomcontrol failed (ignore): {_e}")
                        try:
                            cv2.Settings.IsStatusBarEnabled = False
                        except Exception as _e:  # noqa: BLE001
                            _log(f"settings statusbar failed (ignore): {_e}")

                        # ★ 2026-09-19：外链走系统默认浏览器。
                        #   默认行为下 target=_blank 会在控件内部开新窗口（或整页跳走），
                        #   而桌面壳没有后退/地址栏，用户点一次链接就「回不来了」。
                        def _on_new_window(_s, _e) -> None:
                            try:
                                uri = getattr(_e, "Uri", "") or ""
                                _e.Handled = True
                                _log(f"NewWindowRequested -> default browser: {uri}")
                                if uri and uri.startswith(("http://", "https://")):
                                    os.startfile(uri)
                            except Exception as _x:  # noqa: BLE001
                                _log(f"NewWindowRequested cb err: {_x}")

                        try:
                            cv2.NewWindowRequested += _on_new_window
                            _log("NewWindowRequested wired")
                        except Exception:
                            cv2.add_NewWindowRequested(_on_new_window)

                        cv2.Navigate(url)

                def _on_shown(self, sender, e) -> None:
                    _log("MainForm Shown")
                    # ★ 开箱即可打字：WinForms 默认把焦点给窗体而不是 WebView2，
                    #   窗口弹出后直接敲字没反应，用户以为卡住了。
                    try:
                        self.Activate()
                        self.wv.Focus()
                    except Exception as _e:  # noqa: BLE001
                        _log(f"focus webview failed (ignore): {_e}")

                def _on_closed(self, sender, e) -> None:
                    _log("MainForm Closed")

                def _confirm_exit(self) -> bool:
                    """退出确认（带「不再询问」）。

                    原来每次点 × 都弹一次模态消息框 —— 一天开关十几次就是十几次打扰。
                    这里换成带复选框的小窗口，勾了就写进 gui_state.json，之后直接退出。
                    """
                    st = _load_gui_state()
                    if st.get("exit_confirm") is False:
                        return True
                    try:
                        dlg = Form()
                        dlg.Text = "退出确认"
                        dlg.FormBorderStyle = FormBorderStyle.FixedDialog
                        dlg.StartPosition = FormStartPosition.CenterParent
                        dlg.MinimizeBox = False
                        dlg.MaximizeBox = False
                        dlg.ShowInTaskbar = False
                        dlg.Width = 396
                        dlg.Height = 186
                        if icon_path:
                            try:
                                dlg.Icon = Icon(icon_path)
                            except Exception:  # noqa: BLE001
                                pass
                        lbl = Label()
                        lbl.Text = "确定要退出 Scout Agent 吗？"
                        lbl.Left, lbl.Top, lbl.Width, lbl.Height = 20, 20, 340, 24
                        chk = CheckBox()
                        chk.Text = "不再询问，以后直接退出"
                        chk.Left, chk.Top, chk.Width, chk.Height = 20, 54, 260, 24
                        ok = Button()
                        ok.Text = "退出"
                        ok.Left, ok.Top, ok.Width, ok.Height = 172, 96, 88, 30
                        ok.DialogResult = DialogResult.Yes
                        cancel = Button()
                        cancel.Text = "取消"
                        cancel.Left, cancel.Top, cancel.Width, cancel.Height = 272, 96, 88, 30
                        cancel.DialogResult = DialogResult.No
                        dlg.AcceptButton = ok
                        dlg.CancelButton = cancel
                        for c in (lbl, chk, ok, cancel):
                            dlg.Controls.Add(c)
                        r = dlg.ShowDialog(self)
                        if r != DialogResult.Yes:
                            return False
                        if chk.Checked:
                            _save_gui_state({"exit_confirm": False})
                            _log("exit confirm disabled by user")
                        return True
                    except Exception as _e:  # noqa: BLE001 - 自绘失败退回消息框
                        _log(f"exit dialog failed, fallback MessageBox: {_e}")
                        r = MessageBox.Show(
                            "确定要退出 Scout Agent 吗？",
                            "退出确认",
                            MessageBoxButtons.YesNo,
                            MessageBoxIcon.Question,
                        )
                        return r == DialogResult.Yes

                def _on_closing(self, sender, e) -> None:
                    _log(f"FormClosing CloseReason={e.CloseReason}")
                    # 先把几何存下来（RestoreBounds 在最大化时给的是还原后的尺寸）
                    try:
                        rb = self.RestoreBounds
                        _save_gui_state({
                            "x": int(rb.X), "y": int(rb.Y),
                            "w": int(rb.Width), "h": int(rb.Height),
                            "maximized": self.WindowState == FormWindowState.Maximized,
                        })
                    except Exception as _e:  # noqa: BLE001
                        _log(f"save geometry failed (ignore): {_e}")
                    if not self._confirm_exit():
                        e.Cancel = True

            form = MainForm()
            _log("MainForm created, Application.Run...")
            Application.Run(form)
            _log("Application.Run exited")
        except Exception as e:  # noqa: BLE001
            import traceback

            _log(f"gui thread EXCEPTION: {e}")
            traceback.print_exc()
        finally:
            gui_state["closed"] = True
            _log("gui thread end")

    # WebView2 控件必须在 STA 线程创建（WinForms 消息循环）
    t = Thread(ThreadStart(run_gui))
    t.SetApartmentState(ApartmentState.STA)
    t.Start()
    while not gui_state["closed"]:
        t.Join(200)

    server = _SERVER_STATE.get("server")
    if server is not None:
        server.should_exit = True

    # ★ 2026-09-14：退出前直接落盘活跃会话（不依赖 uvicorn lifespan finally）——
    # server 跑在 daemon 线程上，should_exit 后主线程随即走完并 os._exit，
    # daemon 线程被直接终结，lifespan 的 finally（含 flush 钩子）可能来不及执行。
    # 这里在主线程同步执行一次，确保「未收尾回合 / 未到节流点的增量」不丢。
    #
    # ★ 2026-09-20：flush 真正生效依赖 get_session_store() 全局单例（store.py）——
    # 此前每次 new 实例，主线程这个 flush 跑在新实例上 _active_refs 恒空，落盘 0 条。
    # 现在主线程与 uvicorn 线程共享同一实例、同一 _active_refs，主线程 asyncio.run
    # 干净路径执行落盘。"cannot schedule new futures" 报错来自 lifespan 的 to_thread，
    # 已在 server.py 改为独立线程根治，与本处无关。
    try:
        from scout.session.store import get_session_store

        _n = get_session_store().flush_active()
        _log(f"退出 flush: {_n} 个活跃会话已落盘")
    except Exception as e:  # noqa: BLE001 — 退出路径，失败不阻断关闭
        _log(f"退出 flush 失败: {e}")


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
# ★ 2026-09-01：单实例互斥体句柄 — 模块级持有，防止被 GC 释放导致互斥失效
_single_instance_mutex = None


def _acquire_single_instance() -> bool:
    """GUI 模式单实例保护（Windows）.

    WebView2 的 userDataFolder 不允许多进程同时使用 —— 已有实例运行时
    再次双击 exe，新实例的 WebView2 会与旧实例争用缓存目录，导致新窗口
    白屏/渲染异常（且旧实例也可能受影响）。

    已有实例时：将已打开的窗口恢复并置前，返回 False（调用方退出）。
    """
    global _single_instance_mutex
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, "ScoutAgent_SingleInstance_Mutex_v1")
        # ERROR_ALREADY_EXISTS = 183
        if handle and ctypes.get_last_error() == 183:
            _log("single-instance: 检测到已有实例，激活已有窗口")
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.FindWindowW.restype = wintypes.HWND
                user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
                user32.IsIconic.restype = wintypes.BOOL
                user32.IsIconic.argtypes = [wintypes.HWND]
                user32.ShowWindow.restype = wintypes.BOOL
                user32.ShowWindow.argtypes = [wintypes.HWND, wintypes.INT]
                user32.SetForegroundWindow.restype = wintypes.BOOL
                user32.SetForegroundWindow.argtypes = [wintypes.HWND]
                hwnd = user32.FindWindowW(None, "Scout Agent")
                if hwnd:
                    if user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
            except Exception as _e:  # noqa: BLE001
                _log(f"single-instance: 激活旧窗口失败（忽略）: {_e}")
            return False
        _single_instance_mutex = handle
        return True
    except Exception as _e:  # noqa: BLE001 - 互斥不可用时保守放行
        _log(f"single-instance: 互斥检测不可用（放行）: {_e}")
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Scout Desktop", description="Scout Agent 绿色版桌面程序")
    parser.add_argument("--no-gui", action="store_true", help="仅启动 Web 服务（测试/无 GUI 环境）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=0, help="端口（默认自动探测，优先 8848）")
    args = parser.parse_args(argv)

    # ── Windows 高 DPI 感知（必须在任何窗口创建前调用） ──
    _enable_dpi_awareness()

    # ── windowed 模式 stdio 兜底（必须在导入 uvicorn/scout 之前） ──
    _redirect_stdio()

    # ── 环境准备（必须在导入 scout 之前） ──
    ddir = data_dir()
    # ★ 2026-09-04：反向迁移（APPDATA\Scout → 盘符根 .scout），必须在导入 scout 前
    _migrate_appdata_back(ddir)
    # ★ 2026-08-31：一次性迁移旧数据目录（盘符根 .scout / exe 旁 data / ~/.scout）
    #   必须在导入 scout 之前执行，保证 config.json/secret_key 落到新目录。
    _migrate_old_data(ddir)
    os.environ.setdefault("SCOUT_DATA_DIR", str(ddir))
    # ★ 2026-08-30：配置文件目录同样跟随 exe（config.json 不再写 C 盘 ~/.scout）
    os.environ.setdefault("SCOUT_CONFIG_DIR", str(ddir))
    # ★ 2026-08-30：标记桌面绿色版（更新检查横幅仅在桌面版显示）
    os.environ.setdefault("SCOUT_DESKTOP", "1")
    load_env_files()

    # ★ 2026-09-01：GUI 单实例保护（--no-gui 测试/服务模式不受限）。
    #   WebView2 的 userDataFolder 不允许多进程同时使用，二次启动 exe 会导致
    #   新窗口白屏/渲染异常（用户实测：已有实例时双击 exe，新窗口 UI 损坏）。
    #   已有实例时：激活已打开的窗口，新进程直接退出。
    if not args.no_gui and not _acquire_single_instance():
        return 0

    host = args.host
    port = args.port or pick_port()
    _log(f"main: host={host} port={port}")

    app = build_app()
    _log("main: app built")
    t = threading.Thread(target=_run_server, args=(app, host, port), daemon=True)
    t.start()

    if not wait_ready(host, port):
        _log(f"main: server NOT ready in {host}:{port}")
        print(f"[error] 服务启动失败（{host}:{port}），请查看日志", file=sys.stderr)
        return 1

    url = f"http://{host}:{port}/chat"
    _log(f"main: server ready -> {url}")
    print(f"[ok] Scout Agent 服务已就绪: {url}")
    print(f"[ok] 数据目录: {ddir}")

    if args.no_gui:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    _open_gui(url, port)
    return 0


if __name__ == "__main__":
    _rc = main()
    # ★ 2026-09-20：GUI 模式下窗口关闭后强制退出进程（--no-gui 已在 main 内自行返回，到不了这里）。
    # 此前正常 sys.exit 会卡在残留的非 daemon 第三方线程（uvicorn/pywinauto 等）上，
    # 进程不退出但仍持有单实例互斥 → 之后双击 exe 全部静默退出（僵尸进程，2026-09-20 实测）。
    # 会话落盘已在 _open_gui 的 finally 中完成，这里直接终止是安全的。
    import os as _os
    _os._exit(_rc)
