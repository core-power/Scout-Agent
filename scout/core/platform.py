"""跨平台工具 — 抹平 Windows / Linux / macOS 差异.

核心目标：消除"命令执行失败"和"输出解码崩溃"两大类问题。
- IS_WINDOWS: 平台判断
- decode_output(): 鲁棒字节解码（UTF-8 → GBK → latin-1 兜底），中文 Windows 不再崩
- get_temp_dir(): 跨平台临时目录（替代硬编码 /tmp）
- get_python_cmd(): python / python3 自动选择
- get_platform_prompt(): 注入 system prompt，让 Agent 知道自己在哪个 OS、用什么 shell 语法
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def decode_output(data: bytes) -> str:
    """鲁棒解码子进程输出 — 中文 Windows 的 GBK 输出不再抛异常.

    依次尝试 UTF-8 → GBK → latin-1，全程不抛异常。
    """
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 终极兜底：替换无法解码的字节
    return data.decode("utf-8", errors="replace")


def get_temp_dir(sub: str = "") -> Path:
    """跨平台临时目录 — 替代硬编码的 /tmp.

    Windows: %TEMP%\\scout\\<sub>
    Linux/macOS: /tmp/scout/<sub>
    """
    base = Path(tempfile.gettempdir()) / "scout"
    if sub:
        base = base / sub
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_python_cmd() -> str:
    """返回当前平台的 Python 可执行命令."""
    return "python" if IS_WINDOWS else "python3"


def get_shell_name() -> str:
    """返回当前平台默认 shell 名称（用于提示 Agent）."""
    if IS_WINDOWS:
        return "cmd.exe（也可用 powershell -Command \"...\"）"
    if IS_MACOS:
        return "zsh / bash"
    return "bash / sh"


def get_platform_prompt() -> str:
    """生成注入 system prompt 的平台信息块.

    让 Agent 明确知道操作系统、shell 语法、Python 命令、路径分隔符，
    从源头避免生成 Linux 命令在 Windows 上反复失败。
    """
    py = get_python_cmd()
    if IS_WINDOWS:
        return (
            "## 运行环境（重要）\n"
            "你正在 **Windows** 上运行。shell 命令通过 cmd.exe 执行：\n"
            "- 使用 Windows 命令：`dir`(列出)、`type`(查看文件)、`copy`、`del`、`mkdir`、`findstr`(搜索)、`cd`\n"
            "- **不要使用** Linux 命令（ls/cat/grep/rm/wc/head/tail），它们不存在\n"
            "- 需要复杂操作时用 `powershell -Command \"...\"` 或 `powershell -File xxx.ps1`\n"
            f"- Python 用 `{py}`（不是 python3）\n"
            "- 命令链接用 `&&`（cmd 支持）；环境变量用 `%VAR%`（不是 $VAR）\n"
            "- 路径用反斜杠 `C:\\path` 或正斜杠均可，避免 Unix 风格 `/home/...`\n"
            "- 输出编码为 GBK/UTF-8，已自动处理，无需 chcp\n\n"
        )
    if IS_MACOS:
        return (
            "## 运行环境\n"
            f"你正在 **macOS** 上运行，shell 为 zsh/bash，Python 用 `{py}`，标准 Unix 命令可用。\n\n"
        )
    return (
        "## 运行环境\n"
        f"你正在 **Linux** 上运行，shell 为 bash，Python 用 `{py}`，标准 Unix 命令可用。\n\n"
    )
