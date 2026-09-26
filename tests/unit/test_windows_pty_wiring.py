"""Windows PTY 交互式终端接通的接线守卫（2026-09-25）.

背景：`WindowsPtySession` 早已实现并有单测，但 shell 工具在 `interactive=true`
分支里按**操作系统**直接拒绝 Windows（"PTY 交互式终端仅支持 Linux/macOS"），
于是 ConPTY 实现在生产上根本走不到；打包产物的 PYZ 里 `winpty` 模块数为 0，
即便解除早退也会在桌面版失败。本文件把三处接线都钉住：

1. 工具守卫按**能力**（PTY_SUPPORTED）判断，不再按 OS 拒绝；
2. 挂起提示分平台（Windows 下 Ctrl-C 送不到前台子进程，中断＝重启会话）；
3. 桌面打包显式收集 winpty 的子模块与动态库。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHELL_TOOL = ROOT / "scout" / "tools" / "builtin" / "shell" / "__init__.py"
SPEC = ROOT / "desktop" / "scout_desktop.spec"


def _interactive_branch(src: str) -> str:
    """取 `if interactive:` 到下一个同级分支之间的代码块."""
    start = src.index("        if interactive:\n")
    m = re.search(r"\n        # \d", src[start + 20:])
    end = start + 20 + (m.start() if m else 4000)
    branch = src[start:end]
    assert 500 < len(branch) < 8000, f"分支切片疑似越界（{len(branch)} 字符），先修定位再断言"
    return branch


def test_no_os_based_refusal_of_interactive_pty():
    src = SHELL_TOOL.read_text(encoding="utf-8")
    branch = _interactive_branch(src)
    assert "仅支持 Linux/macOS" not in branch, "interactive 分支仍按操作系统拒绝 Windows"
    assert "PTY_SUPPORTED" in branch, "守卫应改为按能力（PTY_SUPPORTED）判断"
    assert "pip install pywinpty" in branch, "缺依赖时要给出可执行的安装指引"


def test_interrupt_hint_is_platform_aware():
    src = SHELL_TOOL.read_text(encoding="utf-8")
    branch = _interactive_branch(src)
    assert "IS_WINDOWS" in branch, "挂起提示需分平台"
    assert "重启会话" in branch, "Windows 侧要说明中断＝重启会话（丢失 cwd/环境变量）"
    # 不能再无条件承诺 Ctrl-C 可用
    assert branch.count("发送 Ctrl-C 中断") <= 1, "Ctrl-C 提示应只出现在非 Windows 分支"


def test_interactive_schema_description_matches_platform():
    src = SHELL_TOOL.read_text(encoding="utf-8")
    assert "Windows 走 ConPTY" in src, "interactive 的 schema 说明需如实反映 Windows 语义"


def test_desktop_spec_collects_winpty():
    assert SPEC.exists()
    src = SPEC.read_text(encoding="utf-8")
    assert 'find_spec("winpty")' in src, "spec 需带 winpty 可用性守卫"
    assert 'collect_submodules("winpty")' in src, "winpty 子模块需显式收集"
    assert "winpty-agent.exe" in src, "winpty-agent.exe 是 ConPTY 的必需可执行文件"
    assert "_pty_binaries" in src and "binaries += _pty_binaries" in src, "动态库需并入 binaries"
    assert "*_pty_hiddenimports" in src, "hiddenimports 需展开 winpty 子模块"


@pytest.mark.skipif(
    not (os.name == "nt"), reason="Windows ConPTY 端到端用例（非 Windows 无 cmd/ConPTY）"
)
async def test_interactive_tool_actually_runs_on_windows():
    """真正的接线验收：Windows 上 interactive=true 能拿到命令输出."""
    from scout.core.types import ToolCall
    from scout.tools.registry import ToolRegistry
    from scout.tools.builtin.shell.pty_session import PTY_SUPPORTED, WINPTY_AVAILABLE

    if not (PTY_SUPPORTED and WINPTY_AVAILABLE):
        pytest.skip("本机未安装 pywinpty（Windows PTY 依赖）")

    ToolRegistry.discover()
    from scout.tools.builtin.shell.pty_session import PtyShellSessionManager

    try:
        obs = await ToolRegistry.execute(
            ToolCall(name="shell", arguments={"command": "echo wiring-ok",
                                              "interactive": True, "timeout": 25})
        )
    finally:
        # 工具走的是管理器注册的 "default" 会话；不还就会污染后续
        # test_win_pty_manager_routes_and_reuses 的 alive_count()==0 断言
        # （本文件恰好先于它运行，于是真抓到了一次泄漏）。
        await PtyShellSessionManager.close("default")
    assert obs.success, f"Windows interactive 仍不可用: {obs.output[:200]}"
    assert "wiring-ok" in (obs.output or "")
    meta = obs.metadata or {}
    assert meta.get("interactive") is True
