#!/usr/bin/env python3
"""构建 Scout Agent Windows 绿色版（zip 免安装包），全程在 Linux/macOS 完成。

原理（不依赖 Wine / PyInstaller / 交叉编译）:
  1. pip download --platform win_amd64 交叉下载所有依赖的 Windows wheel
  2. 下载 Windows embeddable Python 3.11.9（免安装运行时）
  3. 组装便携目录: python/ + scout/ + desktop/launcher.py + 启动Scout.bat + config
  4. 打包为 ScoutDesktop-win64.zip —— 拷到任意 Windows 机器解压，双击即用

用法:
    python3 tools/build_windows_portable.py [--out dist] [--wheels-dir /tmp/winwheels] [--no-zip]

注意:
    - 本地模型（scout/models，约 114MB）不打包，首次运行在界面内自动下载。
    - Windows 10/11 自带 WebView2，pywebview 直接可用；未装则自动降级系统浏览器。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY_EMBED_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
PY_EMBED_DIR = "python"          # 便携目录内 python 子目录
REQUIREMENTS = ROOT / "desktop" / "requirements-desktop.txt"
EXTRA_PACKAGES = ["pywebview"]

# 源码拷贝时排除的目录/文件
IGNORE_DIRS = {"__pycache__", ".git", "models", "tests", ".pytest_cache", "node_modules"}
IGNORE_FILES = {".DS_Store", "*.pyc", "*.pyo"}

BAT_TEMPLATE = r"""@echo off
rem Scout Agent 绿色版启动器 —— 免安装、免注册
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON=%~dp0python\python.exe"
if not exist "%PYTHON%" (
    echo [error] 未找到 %PYTHON%
    echo 请确认解压完整（python 目录缺失）。
    pause
    exit /b 1
)

rem 数据默认存于 %%APPDATA%%\Scout（升级覆盖程序不丢配置）；可用 SCOUT_DATA_DIR 覆盖
"%PYTHON%" desktop\launcher.py
if errorlevel 1 (
    echo.
    echo [error] Scout 启动失败，请查看上方日志。
    pause
)
endlocal
"""

README_TEMPLATE = """Scout Agent —— Windows 绿色版（免安装）
========================================

一、这是什么
  绿色版 = 免安装、免注册、不写注册表。整个程序在一个文件夹里，
  拷到 U 盘 / 任意 Windows 10/11 机器即可使用，数据跟着文件夹走。

二、使用方法
  1. 解压本压缩包到任意目录（路径建议不要含中文和空格）
  2. 双击「启动Scout.bat」
  3. 首次启动会自动打开程序窗口（WebView2 窗口）；稍等片刻即可对话
  4. 关闭窗口即退出；数据保存在 %APPDATA%\\Scout（Windows 用户数据目录，
     升级覆盖程序文件夹不会丢失配置）

三、常见问题
  Q: 双击后弹出黑色命令行窗口，但没有打开程序界面？
  A: 属正常现象（绿色版带控制台便于看日志）。请等待约 5-10 秒，
     若长时间无窗口，请查看控制台输出，或用浏览器访问 http://127.0.0.1:8848/chat
  Q: 界面提示需要下载模型？
  A: 首次使用需下载内置模型（约 114MB，仅一次）。
     下载完成后缓存在 %APPDATA%\Scout，之后可离线使用。
  Q: 杀毒软件报毒 / 拦截？
  A: 绿色版软件无签名，个别杀软可能误报。请添加信任或排除目录。
     本项目完全开源，可自行审查源码。
  Q: 浏览器打不开 / 提示连接失败？
  A: 检查 8848 端口是否被占用（程序会自动 +1 探测端口，日志会显示实际地址）。

四、数据目录
  %APPDATA%\Scout  程序数据（会话、配置、模型缓存）—— 升级覆盖程序不丢失，
                   换机时随程序文件夹一并拷贝；亦可用 SCOUT_DATA_DIR 指定其他位置
  config/.env     可选配置（把 .env.example 改名为 .env 后按需编辑）
  logs/           运行日志（若存在）

五、技术说明
  内置 Python 3.11.9 embeddable + 全部依赖已打包，无需安装任何环境。
  界面基于 WebView2（Windows 10/11 自带），未安装时自动降级为系统浏览器。
  源码与构建脚本：https://github.com/scout-agent/scout-agent（如有）
"""


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=True, **kw)


def download(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        log(f"已存在，跳过下载: {dest.name}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    log(f"完成: {dest.name} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")


def download_wheels(wheels_dir: Path) -> None:
    """交叉下载全部依赖的 win_amd64 wheel."""
    if not shutil.which("pip"):
        log("[error] 未找到 pip")
        sys.exit(1)
    wheels_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "download",
        "--platform", "win_amd64",
        "--python-version", "3.11",
        "--implementation", "cp",
        "--abi", "cp311",
        "--only-binary=:all:",
        "-d", str(wheels_dir),
        "-r", str(REQUIREMENTS),
        *EXTRA_PACKAGES,
    ]
    # 覆盖用户 pip.conf 里的失效 NVIDIA extra-index（避免每包 5 次 DNS 重试拖慢）
    env = dict(os.environ)
    env["PIP_EXTRA_INDEX_URL"] = ""
    env["PIP_INDEX_URL"] = "https://pypi.tuna.tsinghua.edu.cn/simple"
    env["PIP_TRUSTED_HOST"] = ""
    subprocess.run(cmd, check=True, env=env)


def unpack_wheels(wheels_dir: Path, site_packages: Path) -> None:
    """把所有 wheel 解包进 site-packages."""
    site_packages.mkdir(parents=True, exist_ok=True)
    n = 0
    for whl in sorted(wheels_dir.glob("*.whl")):
        with zipfile.ZipFile(whl) as z:
            for m in z.infolist():
                if m.is_dir():
                    continue
                name = m.filename
                # 跳过 RECORD 等元数据（保留 dist-info 目录其余文件）
                if name.startswith("RECORD") or name.endswith((".pyc", "RECORD.jws", "RECORD.pem")):
                    continue
                target = site_packages / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        n += 1
    log(f"解包 {n} 个 wheel 到 site-packages")


def setup_embeddable_python(portable: Path) -> Path:
    """下载并安置 embeddable Python，启用 site（site-packages 生效）."""
    work = portable / PY_EMBED_DIR
    if (work / "python.exe").exists():
        log("embeddable python 已存在")
        return work
    cache = ROOT / ".cache" / "python-3.11.9-embed-amd64.zip"
    download(PY_EMBED_URL, cache)
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(cache) as z:
        z.extractall(work)
    # 启用 site: import site 会加载 Lib/site-packages
    pth = work / "python311._pth"
    text = pth.read_text(encoding="utf-8")
    pth.write_text(text.replace("#import site", "import site"), encoding="utf-8")
    log("embeddable python 就绪（已启用 site）")
    return work


def copy_source(portable: Path) -> None:
    """拷贝 scout 源码与桌面入口（排除缓存/模型）."""
    def ignore(d: str, names: list[str]) -> set[str]:
        out = set()
        for n in names:
            p = Path(d) / n
            if p.is_dir():
                if n in IGNORE_DIRS:
                    out.add(n)
            elif n in IGNORE_FILES or n.endswith((".pyc", ".pyo")):
                out.add(n)
        return out

    shutil.copytree(ROOT / "scout", portable / "scout", ignore=ignore)
    (portable / "desktop").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "desktop" / "launcher.py", portable / "desktop" / "launcher.py")
    if (ROOT / ".env.example").exists():
        shutil.copy2(ROOT / ".env.example", portable / ".env.example")
    log("源码拷贝完成")


def write_aux(portable: Path) -> None:
    (portable / "启动Scout.bat").write_text(BAT_TEMPLATE, encoding="utf-8")
    (portable / "说明.txt").write_text(README_TEMPLATE, encoding="utf-8")


def make_zip(portable: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    zpath = out / "ScoutDesktop-win64.zip"
    if zpath.exists():
        zpath.unlink()
    log(f"打包 {zpath}")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(portable):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
            for f in files:
                full = Path(root) / f
                rel = full.relative_to(portable)
                z.write(full, arcname=f"ScoutDesktop/{rel}")
    log(f"完成: {zpath} ({zpath.stat().st_size / 1024 / 1024:.1f} MB)")
    return zpath


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 Scout Agent Windows 绿色版")
    ap.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 dist/）")
    ap.add_argument("--wheels-dir", default="/tmp/winwheels", help="wheel 缓存目录")
    ap.add_argument("--no-zip", action="store_true", help="只组装目录不打包 zip")
    args = ap.parse_args()

    out = Path(args.out)
    wheels_dir = Path(args.wheels_dir)
    portable = out / "ScoutDesktop"

    # 整体重建便携目录，避免上次构建残留（如旧版 croniter 6.x 目录）
    if portable.exists():
        log(f"清理旧构建目录: {portable}")
        shutil.rmtree(portable, ignore_errors=True)
    portable.mkdir(parents=True, exist_ok=True)

    log("=== 步骤 1/5: 交叉下载 win_amd64 wheels ===")
    download_wheels(wheels_dir)

    log("=== 步骤 2/5: 安置 embeddable Python ===")
    setup_embeddable_python(portable)

    log("=== 步骤 3/5: 拷贝源码 ===")
    copy_source(portable)

    log("=== 步骤 4/5: 解包 wheels + 辅助文件 ===")
    unpack_wheels(wheels_dir, portable / PY_EMBED_DIR / "Lib" / "site-packages")
    write_aux(portable)

    if args.no_zip:
        log(f"组装完成（未打包）: {portable}")
        return 0

    log("=== 步骤 5/5: 打包 zip ===")
    make_zip(portable, out)
    log("全部完成 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
