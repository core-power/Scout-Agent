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
import os
import socket
import sys
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
    """数据目录:
    - Windows: 程序所在盘符根目录/.scout（如 D:\\.scout），不可写时回退 exe 旁 data/
    - 其他平台: exe 旁 data/，不可写时回退用户目录 ~/.scout
    """
    if os.name == "nt":
        anchor = Path(sys.executable if _is_frozen() else __file__).resolve().anchor
        if anchor:
            d = Path(anchor) / ".scout"
            try:
                d.mkdir(parents=True, exist_ok=True)
                probe = d / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                return d
            except OSError:
                pass
    d = app_dir() / "data"
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return d
    except OSError:
        return Path.home() / ".scout"


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
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        # Windows 10 1703+：DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:  # noqa: BLE001
        try:
            import ctypes

            # 旧系统：PROCESS_PER_MONITOR_DPI_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001
            pass


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
        from System.Drawing import Icon, Size
        from System.Windows.Forms import (
            Application,
            DialogResult,
            DockStyle,
            Form,
            FormStartPosition,
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
                    self.StartPosition = FormStartPosition.CenterScreen
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
                        self.wv.CoreWebView2.Navigate(url)

                def _on_shown(self, sender, e) -> None:
                    _log("MainForm Shown")

                def _on_closed(self, sender, e) -> None:
                    _log("MainForm Closed")

                def _on_closing(self, sender, e) -> None:
                    _log(f"FormClosing CloseReason={e.CloseReason}")
                    r = MessageBox.Show(
                        "确定要退出 Scout Agent 吗？",
                        "退出确认",
                        MessageBoxButtons.YesNo,
                        MessageBoxIcon.Question,
                    )
                    if r != DialogResult.Yes:
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


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
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
    os.environ.setdefault("SCOUT_DATA_DIR", str(ddir))
    # ★ 2026-08-30：配置文件目录同样跟随 exe（config.json 不再写 C 盘 ~/.scout）
    os.environ.setdefault("SCOUT_CONFIG_DIR", str(ddir))
    load_env_files()

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
    sys.exit(main())
