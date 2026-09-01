# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — Scout Agent 绿色版桌面程序.

产物: dist/ScoutDesktop/ScoutAgent.exe + 依赖文件夹（绿色便携，免安装）。

打包前需先生成图标:
    python tools/gen_pwa_icons.py
    python tools/gen_win_icon.py

可选瘦身: 注释掉 models 的 datas 收集，可减小体积约 90MB
（但首次运行需通过 download_model.py 获取本地嵌入模型）。
"""

import os
import sys

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# ── 数据文件 ──────────────────────────────────────────────
# 注意: PyInstaller 的 datas 路径相对 spec 所在目录(desktop/)解析，
#       故项目根文件需加 ../ 前缀（2026-08-29 修复）。
datas = [
    ("../scout/web/static", "scout/web/static"),   # Web UI + PWA 资源
    ("../.env.example", "."),                       # 配置模板（随包携带）
    ("../VERSION", "."),                            # 版本号（更新检查/版本显示用，2026-08-30）
    ("scout.ico", "."),                             # exe 图标（相对 spec 目录）
]

# ── WebView2 程序集（原生窗口方案，2026-08-29） ────────────
# launcher.py 直接 AddReference 加载，须放到 _internal 根目录；
# WebView2Loader.dll 为 native 加载器，.NET DllImport 按 exe 目录搜索。
_webview_lib = None
for _p in sys.path:
    _c = os.path.join(_p, "webview", "lib")
    if os.path.isdir(_c):
        _webview_lib = _c
        break
if _webview_lib:
    datas += [
        (os.path.join(_webview_lib, "Microsoft.Web.WebView2.Core.dll"), "."),
        (os.path.join(_webview_lib, "Microsoft.Web.WebView2.WinForms.dll"), "."),
        (os.path.join(_webview_lib, "runtimes", "win-x64", "native", "WebView2Loader.dll"), "."),
    ]

# 本地嵌入模型（bge-small-zh-v1.5 onnx）— 默认不打包（约 114MB 瘦身）。
# 如需开箱即用，取消下行注释；否则首次运行需在 Scout 界面下载模型。
# models_dir = ("scout/models", "scout/models")
# datas.append(models_dir)

# onnxruntime: 收集其数据文件与动态库（Windows 下为 onnxruntime.dll / providers）
datas += collect_data_files("onnxruntime")
binaries = collect_dynamic_libs("onnxruntime")

# ── 隐式导入（动态 import / 反射加载的模块） ─────────────
hiddenimports = [
    # 核心硬依赖（requirements 已含）
    "croniter",
    "dotenv",
    "click",
    # uvicorn 动态加载的循环/协议实现
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # Web / 事件流
    "sse_starlette",
    "websockets",
    "multipart",
    # scout 核心模块（含反射发现路径）
    "scout.web.server",
    "scout.tools.registry",
    "scout.tools.base",
    "scout.config.manager",
    "scout.config.settings",
    "scout.llm.providers.registry",
    "scout.llm.tracker",
    "scout.llm.prompt_cache",
    "scout.engine.agent",
    "scout.context.manager",
    "scout.context.memory_extract",
    "scout.context.context_assembler",
    "scout.context.memory_flush",
    "scout.memory.store",
    "scout.memory.embedder",
    "scout.memory.vector",
    "scout.session.store",
    "scout.bus",
    "scout.scheduler",
    "scout.security.secret",
    "scout.doctor",
    # 内置工具模块（2026-08-30 修复）：PyInstaller 打包后 iter_modules 无法
    # 枚举 PYZ 归档，须显式收集全部 builtin 工具，discover() 才有代码可导入。
    *collect_submodules("scout.tools.builtin"),
    # playwright CLI（首次运行自动安装 chromium 用，2026-08-30）
    "playwright.__main__",
    "playwright.async_api",
    "playwright.sync_api",
    "playwright._impl._driver",
]

# ── 打包配置 ─────────────────────────────────────────────
# 2026-09-01 修复: 在 desktop/ 下执行 PyInstaller 时 pathex=["."] 只含 desktop,
# Analysis 找不到项目根的 scout 包 → "missing module named scout" 静默跳过 →
# 运行时报 No module named 'scout.tools'。改为显式加入项目根。
_SRC_ROOT = os.path.dirname(os.path.abspath(SPECPATH))
a = Analysis(
    ["launcher.py"],  # 相对 spec 所在目录(desktop/)解析
    pathex=[_SRC_ROOT, "."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 削减体积（桌面端不启用 IM 渠道）
        "discord",
        "wechaty",
        "torch",
        "transformers",  # 嵌入用 onnxruntime 直跑，无需 transformers
        "tkinter",       # 绿色版不依赖 tk 消息框
        # 注: playwright 不再排除（2026-08-30）——浏览器工具开箱即用：
        # Python 包本体进包（~20MB），chromium 二进制首次使用时自动安装到用户目录。
        # 官方 hook 会自动收集 playwright/driver 下的 node 驱动。
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="ScoutAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # 无控制台窗口，双击直接弹出对话界面（如需调试日志可改 True）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="scout.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ScoutDesktop",
)
