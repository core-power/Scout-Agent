# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 — Scout Agent 绿色版桌面程序.

产物: dist/ScoutPortable/Scout.exe + 依赖文件夹（绿色便携，免安装）。

打包前需先生成图标:
    python tools/gen_pwa_icons.py
    python tools/gen_win_icon.py

可选瘦身: 注释掉 models 的 datas 收集，可减小体积约 90MB
（但首次运行需通过 download_model.py 获取本地嵌入模型）。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# ── 数据文件 ──────────────────────────────────────────────
# 注意: PyInstaller 的 datas 路径相对 spec 所在目录(desktop/)解析，
#       故项目根文件需加 ../ 前缀（2026-08-29 修复）。
datas = [
    ("../scout/web/static", "scout/web/static"),   # Web UI + PWA 资源
    ("../.env.example", "."),                       # 配置模板（随包携带）
    ("scout.ico", "."),                             # exe 图标（相对 spec 目录）
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
]

# ── 打包配置 ─────────────────────────────────────────────
a = Analysis(
    ["launcher.py"],  # 相对 spec 所在目录(desktop/)解析
    pathex=["."],
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
        "playwright",
        "torch",
        "transformers",  # 嵌入用 onnxruntime 直跑，无需 transformers
        "tkinter",       # 绿色版不依赖 tk 消息框
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Scout",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台便于查看启动日志（后续可改 False）
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
    name="ScoutPortable",
)
