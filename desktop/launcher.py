#!/usr/bin/env python3
"""Scout Agent 绿色版桌面程序启动器.

特性:
  - 免安装: 双击即用，不写注册表、不做文件关联、不设开机自启
  - 数据随程序: 便携模式数据目录 = exe 旁 data/（拷到 U 盘即可带走）
  - 内嵌 Web 服务: 本地启动 uvicorn，pywebview 窗口加载（Windows 用 WebView2）
  - 配置跟随: exe 旁 config/.env 或 .env 可覆盖默认配置
  - 端口自适应: 8848 被占用时自动 +1 探测
  - 兜底降级: 无 pywebview 时自动打开系统浏览器

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
    """便携数据目录: exe 旁 data/，不可写时回退用户目录 ~/.scout."""
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
def _open_gui(url: str, port: int) -> None:
    try:
        import webview
    except ImportError:
        # 降级: 打开系统浏览器
        import webbrowser

        webbrowser.open(url)
        print(f"[ok] 未安装 pywebview，已打开系统浏览器: {url}")
        return

    window = webview.create_window(
        "Scout Agent",
        url,
        width=1280,
        height=820,
        min_size=(960, 640),
        background_color="#0f172a",
        confirm_close=True,  # 关闭时确认，防误关导致服务退出
    )
    # 阻塞直到窗口关闭
    webview.start()
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

    # ── 环境准备（必须在导入 scout 之前） ──
    ddir = data_dir()
    os.environ.setdefault("SCOUT_DATA_DIR", str(ddir))
    load_env_files()

    host = args.host
    port = args.port or pick_port()

    app = build_app()
    t = threading.Thread(target=_run_server, args=(app, host, port), daemon=True)
    t.start()

    if not wait_ready(host, port):
        print(f"[error] 服务启动失败（{host}:{port}），请查看日志", file=sys.stderr)
        return 1

    url = f"http://{host}:{port}/chat"
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
