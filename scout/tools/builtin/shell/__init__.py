"""Shell 工具 — 安全增强的 Shell 命令执行.

安全策略 (2026-08-27 更新):
- 命令白名单（basename）+ 危险参数黑名单双重校验
- Shell 元字符策略（个人版放宽，见 SHELL_META 注释）：
  * 允许: 管道/重定向 (| > <) 与分号/逻辑符 (; &) 等正常用法（通过 bash -c 执行）
  * 拦截: 命令注入/编码攻击模式 — $(...)、反引号 `...`、${...}、curl|sh / wget|sh
- 危险命令硬拦截（DANGEROUS_PATTERNS）：rm -rf /、dd、mkfs、关机/重启、
  fork 炸弹、管道执行远程脚本、重启/停止 scout 服务、读取敏感系统文件/SSH 密钥/历史命令
- 默认使用 create_subprocess_exec（非 shell=True）防止 Shell 解析；仅当命令含
  元字符时才降级为 bash -c（此时参数中的注入模式已在校验阶段拦截）
- 超时保护，防止命令挂死
- 跨平台输出解码（UTF-8→GBK→latin-1）
- 路径遍历防护 + 系统目录访问拦截（SYSTEM_DIRS / ALLOWED_PATH_PREFIXES）
- 参数注入检测（INJECTION_PATTERNS，同时覆盖 command 与 args）
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.security.policy import ALLOWED_PATH_PREFIXES, SYSTEM_DIRS
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# 平台常量（2026-08-30 新增 Windows 适配）
IS_WINDOWS = os.name == "nt"

# Windows cmd.exe 内建命令（无对应 .exe，必须经 cmd /c 执行；
# 直接 create_subprocess_exec 会报"命令未找到"）
WIN_BUILTIN_CMDS = {
    "assoc", "break", "call", "cd", "chcp", "chdir", "cls", "color", "copy",
    "date", "del", "dir", "echo", "endlocal", "erase", "exit", "ftype",
    "goto", "if", "md", "mkdir", "move", "path", "pause", "popd", "prompt",
    "pushd", "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal",
    "shift", "start", "time", "title", "type", "ver", "verify", "vol",
}


def _win_quote(arg: str) -> str:
    """Windows cmd 参数引用：含空白/特殊字符时加双引号包裹，内部双引号转义为两个双引号.

    注意：cmd /c 模式 % 会做变量展开（%PATH% 等），未定义变量保持原样，符合用户预期；
    不使用 shlex.quote（其 POSIX 引号对 cmd 无效，且反斜杠会被当转义符）。
    """
    if arg and not re.search(r'[\s"&|<>^]', arg):
        return arg
    return '"' + arg.replace('"', '""') + '"'


_META_ONLY = re.compile(r'^[|><;&]+$')


def _build_proc_cmd(cmd_list: list[str]) -> list[str]:
    """跨平台子进程命令构造（Windows 适配核心）：
    - 单元素整串命令（command 原样，可能含空格参数/元字符）→ 整体交给 shell 解析，
      Windows 用 cmd.exe /d /s /c，Linux/macOS 用 bash -c（避免 create_subprocess_exec
      把 "where python" 当可执行文件名）。
    - 多元素（command + args 拆分）：含元字符 → shell 执行；Windows 的 cmd 内建命令
      （dir/type 等无 .exe）走 cmd /c；其余直接 exec。
    - 引号决策：纯元字符 token（|、&& 等）保持原样保留 shell 语义；
      混合内容 token（如 "a b&c"）加引号保护，避免 & 被误当命令分隔符。
    """
    _meta = re.compile(r'[|><;&]')
    if len(cmd_list) == 1:
        single = cmd_list[0].strip()
        if not single:
            return cmd_list
        if _meta.search(single) or re.search(r'\s', single):
            # 整串命令（含元字符或空格参数）：原样交给 shell 解析，不额外加引号
            if IS_WINDOWS:
                return ["cmd.exe", "/d", "/s", "/c", single]
            return ["bash", "-c", single]
        if IS_WINDOWS and os.path.basename(single).lower() in WIN_BUILTIN_CMDS:
            return ["cmd.exe", "/d", "/s", "/c", single]
        return cmd_list
    if any(_meta.search(a) for a in cmd_list):
        if IS_WINDOWS:
            _quoted = [a if _META_ONLY.match(a) else _win_quote(a) for a in cmd_list]
            return ["cmd.exe", "/d", "/s", "/c", " ".join(_quoted)]
        _quoted = [a if _META_ONLY.match(a) else shlex.quote(a) for a in cmd_list]
        return ["bash", "-c", " ".join(_quoted)]
    if IS_WINDOWS and os.path.basename(cmd_list[0]).lower() in WIN_BUILTIN_CMDS:
        return ["cmd.exe", "/d", "/s", "/c", " ".join(_win_quote(a) for a in cmd_list)]
    return cmd_list


# ── 跨平台解码 ──────────────────────────────────────────────
# 与 scout.security.sandbox._decode 共用同一实现，避免双份维护
from scout.security.sandbox import _decode as decode_output  # noqa: E402


# ── 安全策略 ────────────────────────────────────────────────
# 允许执行的命令白名单（basename 匹配）
SAFE_COMMANDS = {
    # 文件浏览
    "ls", "dir", "pwd", "cat", "type", "head", "tail", "wc", "grep",
    "find", "locate", "which", "whereis", "file", "stat", "du", "df",
    # 文件操作
    "mkdir", "touch", "cp", "mv", "rm", "ln", "chmod", "chown",
    # 文本处理
    "echo", "printf", "sort", "uniq", "cut", "awk", "sed", "tr",
    "diff", "comm", "paste", "fold", "column",
    # 系统信息
    "whoami", "uname", "hostname", "date", "uptime", "env", "printenv",
    "id", "groups", "ps", "top", "free", "lscpu", "lsblk",
    # 网络
    "ping", "curl", "wget", "dig", "nslookup", "traceroute", "ss", "netstat",
    # 开发工具
    "python3", "python", "pip", "pip3", "node", "npm", "npx",
    "git", "make", "cmake", "gcc", "g++", "cargo", "rustc",
    "java", "javac", "go", "ruby", "perl", "php",
    # 环境/服务管理（个人版常用）
    "bash", "sh", "source", "conda", "activate", "deactivate",
    "docker", "docker-compose", "systemctl", "service", "supervisorctl",
    "uvicorn", "gunicorn", "nohup", "kill", "pkill", "killall",
    "ssh", "scp", "rsync", "tmux", "screen", "vim", "nano", "less",
    "tar", "unzip",
    # 包管理
    "apt", "apt-get", "yum", "brew", "pnpm", "yarn",
    # 压缩
    "gzip", "gunzip", "zip", "unzip",
    # 其他
    "tree", "xargs", "tee", "basename", "dirname", "realpath", "readlink",
    "md5sum", "sha256sum", "sha1sum",
    # ── 常规命令放宽 (2026-08-25: 减少绕路) ──
    # shell 内建/导航
    "cd", "clear", "more", "man", "help", "alias", "unalias", "export",
    "unset", "set", "shopt", "jobs", "fg", "bg", "wait", "dirs", "pushd",
    "popd", "test", "true", "false", "history", "fc", "declare", "read",
    "readonly", "return", "shift", "builtin", "command", "umask", "ulimit",
    # 文本/数据
    "jq", "yq", "rg", "fd", "bat", "xxd", "hexdump", "od", "base64",
    "strings", "iconv", "dos2unix", "unix2dos", "numfmt", "fmt", "rev",
    "tac", "nl", "expand", "unexpand", "zcat", "bzcat", "xzcat",
    # 压缩/归档
    "xz", "unxz", "bzip2", "bunzip2", "zstd", "unzstd", "7z", "7za",
    "7zr", "lz4", "lzma", "unlzma", "cpio", "zipinfo", "zless", "zmore",
    # 系统诊断
    "who", "w", "last", "lastlog", "logname", "tty", "stty", "lsof",
    "fuser", "pgrep", "vmstat", "iostat", "mpstat", "pidstat", "htop",
    "btop", "ncdu", "lsusb", "lspci", "lsmod", "modinfo", "getent",
    "dmesg", "journalctl", "hostnamectl", "timedatectl", "localectl",
    "loginctl", "sysctl", "sync",
    # 网络（个人版调试常用）
    "ip", "ifconfig", "route", "arp", "host", "nc", "ncat", "socat",
    "telnet", "ftp", "sftp", "ssh-keygen", "ssh-copy-id", "aria2c",
    "axel", "http", "httpie", "ab", "wrk", "hey", "kubectl", "helm",
    "podman", "ctr", "nerdctl", "gcloud", "aws", "az", "doctl",
    "terraform", "ansible", "ansible-playbook", "vagrant",
    # 开发/编译/调试
    "clang", "clang++", "gdb", "lldb", "valgrind", "strace", "ltrace",
    "objdump", "nm", "readelf", "size", "ldd", "strip", "ar", "ranlib",
    "patchelf", "pkg-config", "ninja", "meson", "patch", "cmp", "openssl",
    "gpg", "sqlite3", "redis-cli", "psql", "mysql", "mongosh",
    "deno", "bun", "tsc", "ts-node", "lua", "luajit", "R", "Rscript",
    "tclsh", "wish", "mamba", "micromamba", "pipenv", "poetry", "uv", "uvx",
    "virtualenv", "pyenv", "expect",
    # 媒体/文档
    "ffmpeg", "ffprobe", "convert", "magick", "mogrify", "pdftotext",
    "pdfinfo", "pdftoppm", "pdftocairo", "gs", "exiftool", "mediainfo",
    "yt-dlp", "youtube-dl", "cwebp", "dwebp", "sox", "mutool", "qpdf",
    # 其他
    "watch", "seq", "yes", "sleep", "time", "timeout", "nproc", "arch",
    "getconf", "logger", "crontab",
    # ── Windows 常用命令 (2026-08-30 新增: 个人版 Windows 用户；Linux/macOS 无副作用) ──
    # cmd 内建（配合 WIN_BUILTIN_CMDS 经 cmd /c 执行）
    "chcp", "cls", "color", "title", "path", "prompt", "copy", "move",
    "del", "erase", "ren", "rename", "md", "rd", "rmdir", "vol", "ver",
    "verify", "assoc", "ftype", "start", "exit", "pause", "rem", "chdir",
    "setlocal", "endlocal",
    # Windows 外部命令
    "where", "tasklist", "taskkill", "ipconfig", "systeminfo", "schtasks",
    "reg", "wmic", "attrib", "findstr", "forfiles", "mklink", "setx",
    "xcopy", "robocopy", "mode", "compact", "driverquery", "netsh", "net",
    "cscript", "wscript", "msinfo32", "winver", "powershell", "pwsh",
    "cmd", "sfc", "takeown", "subst", "cipher", "fsutil", "powercfg",
    "gpupdate", "w32tm", "tzutil", "taskmgr", "chkdsk",
}

# 绝对禁止的参数模式（扩展版）
DANGEROUS_ARGS = [
    r"rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|(-[a-zA-Z]*f[a-zA-Z]*r))\s+/",  # rm -rf /
    r"rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|(-[a-zA-Z]*f[a-zA-Z]*r))\s+~",  # rm -rf ~
    r"rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|(-[a-zA-Z]*f[a-zA-Z]*r))\s+\*",  # rm -rf *
    r"--no-preserve-root",
    r"/dev/sd",
    r"/dev/nvme",
    r"/dev/zero",
    r"/dev/random",
    r"/dev/urandom",
    r"mkfs\.",
    r"dd\s+if=",
    r">\s*/dev/",
    r"chmod\s+-R\s+777\s+/",
    r"chmod\s+-R\s+777\s+~",
    r":\(\)\s*\{",  # fork bomb
    r"wget.*\|\s*sh",  # wget | sh
    r"curl.*\|\s*sh",  # curl | sh
    r"curl.*\|\s*bash",
    r"wget.*\|\s*bash",
    r"nc\s+-[a-zA-Z]*e",      # nc -e 反向 shell
    r"ncat\s+-[a-zA-Z]*e",    # ncat -e 反向 shell
    r"socat.*\bexec\b",       # socat exec 反向 shell
    # 个人版移除：python -c import os 等属正常用法，execute_code 工具已足够安全
    # r"python.*-c.*import\s+os",
    # r"python.*-c.*subprocess",
    # r"python.*-c.*exec\(",
    # r"python.*-c.*eval\(",
]

# Shell 元字符 — 个人版放宽：仅拦截命令注入/编码攻击模式，不拦截正常用法
# 原全面拦截(所有 |;&$` 等)误伤太严重，改为只针对性拦截
SHELL_META = re.compile(
    # 注: $'\xNN' 需写 \\x 匹配字面反斜杠+x；裸 \x 是残缺转义，Python 3.14 起 re.compile 直接报错
    r"\$\s*\(|`[^`]+`|\$\{|\$'\\x[0-9a-fA-F]{2}|\b(?:curl|wget)\s+.*\|\s*(?:sh|bash)"
)

# 参数注入检测 — 检测可能的命令注入模式
# 2026-08-27 放宽：移除 r'\$\w+'（$var 简单变量展开属 shell 常规用法，非注入攻击；
# 真正的注入面是 $()/`...`/${...} 命令替换与编码转义，仍保留拦截）
INJECTION_PATTERNS = [
    r'\$\(',  # $(command)
    r'`[^`]+`',  # `command`
    r'\$\{',  # ${var}
    r'\\x[0-9a-fA-F]{2}',  # \x hex escape
    r'\\[0-7]{1,3}',  # \octal escape
]


def _validate_command(command: str, args: list[str] | None = None) -> tuple[bool, str]:
    """三重安全校验：白名单 + 黑名单 + 注入检测.
    
    Returns:
        (is_safe, error_message)
    """
    if not command or not command.strip():
        return False, "命令为空"

    # 0. 危险命令硬拦截（强安全层，总是生效，不受 auto_approve 影响）
    # 个人版策略：危险操作不被动执行，而是引导用户到终端手动执行
    from scout.security.policy import DANGEROUS_PATTERNS as _policy_patterns
    full_cmd0 = command + " " + " ".join(args or [])
    for _pat, _desc in _policy_patterns:
        if re.search(_pat, full_cmd0, re.IGNORECASE):
            return False, (
                f"\u26d4 危险操作（{_desc}）已被安全保护拦截，我不会替你在后台执行。\n"
                f"如果你确实需要执行此操作，请自己在服务器的终端中手动运行以下命令：\n"
                f"    {full_cmd0}\n"
                f"请向我说明你需要执行的原因，或确认后由你亲自在终端完成。"
            )

    # 1. 检查 Shell 元字符（仅拦截注入/编码攻击模式；管道/重定向属正常用法，见 SHELL_META 注释）
    full_cmd = command + " " + " ".join(args or [])
    if SHELL_META.search(full_cmd):
        return False, "安全拦截: 命令包含命令注入/编码攻击模式（$(...)、反引号、${...}、curl|sh）"

    # 2. 白名单校验 — 提取基础命令名
    parts = command.strip().split()
    base_cmd = os.path.basename(parts[0])
    if base_cmd not in SAFE_COMMANDS:
        return False, f"安全拦截: 命令 '{base_cmd}' 不在白名单中。允许: {', '.join(sorted(SAFE_COMMANDS)[:20])}..."

    # 3. 危险参数模式检测
    for pattern in DANGEROUS_ARGS:
        if re.search(pattern, full_cmd, re.IGNORECASE):
            return False, "安全拦截: 检测到危险参数模式"

    # 4. 注入检测（同时覆盖 command 与 args）——2026-08-27 补强
    if "\n" in command or "\r" in command:
        return False, "安全拦截: 命令不能包含换行符（禁止多行命令走私）"

    for token in [command] + list(args or []):
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, token):
                return False, "安全拦截: 参数包含可疑的注入模式"

        # 检查路径遍历
        if ".." in token and ("/" in token or "\\" in token):
            return False, "安全拦截: 参数包含路径遍历 (..)"

        # 检查绝对路径中的敏感目录
        if token.startswith("/"):
            for sensitive in SYSTEM_DIRS:
                if token == sensitive or token.startswith(sensitive + "/"):
                    return False, f"安全拦截: 不允许访问系统目录 {sensitive}"

    return True, ""


class ShellTool(ToolDefinition):
    """安全 Shell 命令执行."""

    name = "shell"
    description = (
        "Execute a shell command in a restricted environment. "
        "Only whitelisted system utilities are allowed. "
        "Dangerous operations (recursive delete, disk format, piping to shell) are blocked. "
        "Prefer args parameter for arguments. System directories (/etc, /usr, /bin) are blocked."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The base command to execute (e.g. 'ls', 'grep', 'python3').",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command arguments as a list (avoids shell injection).",
                "default": [],
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30, max 120).",
                "default": 30,
            },
            "cwd": {
                "type": "string",
                "description": "Working directory for the command.",
                "default": ".",
            },
            "persistent": {
                "type": "boolean",
                "description": "持久会话（2026-08-27）：复用长驻 bash 进程，跨调用保留 cd/环境变量/后台任务。",
                "default": False,
            },
            "session_key": {
                "type": "string",
                "description": "持久会话标识（仅 persistent=True 时使用），同一 key 共享同一 bash 进程。",
                "default": "",
            },
            "interactive": {
                "type": "boolean",
                "description": "PTY 交互式终端（2026-08-27）：以伪终端运行命令，支持 vim/top/less 等交互式程序。"
                "超时后发送 Ctrl-C 而非杀进程，会话保留，可继续用 session_keys 注入按键。",
                "default": False,
            },
            "session_keys": {
                "type": "string",
                "description": "PTY 模式按键序列（仅 interactive=True 时使用）：命令执行完后注入，"
                "如 ':wq\\r' 保存退出 vim、'q' 退出 less、'jj' 移动光标。"
                "支持 \\r 回车、\\x03 Ctrl-C 等转义。",
                "default": "",
            },
        },
        "required": ["command"],
    }
    annotations = ToolAnnotations(
        title="Run Safe Shell Command",
        read_only=True,
        destructive=False,
    )

    async def execute(
        self,
        command: str,
        args: list[str] | None = None,
        timeout: int = 30,
        cwd: str = ".",
        persistent: bool = False,
        session_key: str = "",
        interactive: bool = False,
        session_keys: str = "",
        on_output: Any = None,
        sandbox: Any = None,  # Sandbox 实例（可选）
        **kwargs,
    ) -> Observation:
        # 0. 持久会话重置伪命令（先于安全校验，避免白名单拦截）
        if command == "__session_reset__":
            from scout.tools.builtin.shell.pty_session import PtyShellSessionManager
            from scout.tools.builtin.shell.session import ShellSessionManager

            key = session_key or "default"
            await ShellSessionManager.close(key)
            await PtyShellSessionManager.close(key)
            return Observation(
                tool_name=self.name,
                success=True,
                output="持久会话与 PTY 会话已重置",
            )

        # 1. 安全校验（PTY 纯按键注入场景：interactive + 空命令 + session_keys → 放行到 interactive 分支）
        is_safe, error = _validate_command(command, args)
        if not is_safe:
            if interactive and not command.strip() and session_keys:
                pass  # 走 interactive 分支处理按键注入
            else:
                # 复合命令自动拆分（2026-08-29）：命令含 && / ; 且被安全校验拦截时，
                # 尝试拆成单条序列逐条执行（每条仍走完整安全校验，不拆管道/重定向）。
                # 避免"整条命令被拦 → 反思"的无效循环。
                split_obs = await self._try_split_execute(command, args, timeout, cwd)
                if split_obs is not None:
                    return split_obs
                return Observation(tool_name=self.name, success=False, output=error)

        # 2. 限制超时
        timeout = min(max(timeout, 1), 120)

        # 2.1 持久会话（2026-08-27）：进程级 bash 长驻
        if persistent:
            from scout.tools.builtin.shell.session import ShellSessionManager

            work_dir = os.path.abspath(cwd) if os.path.isdir(os.path.abspath(cwd)) else "."
            sess = await ShellSessionManager.get(session_key or "default", cwd=work_dir)
            full_cmd = " ".join([command] + (args or []))
            output, code = await sess.run(full_cmd, timeout=timeout)
            if len(output) > 50000:
                output = output[:25000] + "\n... [输出截断] ...\n" + output[-25000:]
            if on_output and output:
                on_output(output[-3000:])
            return Observation(
                tool_name=self.name,
                success=code == 0,
                output=output or "(无输出)",
                metadata={"persistent": True, "session_key": session_key or "default", "exit_code": code},
            )

        # 2.2 PTY 交互式终端（2026-08-27）：伪终端会话，支持 vim/top 等交互程序
        if interactive:
            if IS_WINDOWS:
                # PTY 依赖 fcntl/termios/pty，均为 Unix 专属模块
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output="PTY 交互式终端仅支持 Linux/macOS（依赖 fcntl/termios/pty 模块）。"
                           "Windows 下请使用普通 shell 或 persistent 持久会话（cmd.exe）。",
                )
            from scout.tools.builtin.shell.pty_session import PtyShellSessionManager

            work_dir = os.path.abspath(cwd) if os.path.isdir(os.path.abspath(cwd)) else "."
            sess = await PtyShellSessionManager.get(session_key or "default", cwd=work_dir)
            parts: list[str] = []
            statuses: list[str] = []
            code: int | None = 0
            if command.strip():
                out, code, status = await sess.run(command.strip(), timeout=timeout)
                parts.append(out)
                statuses.append(status)
            if session_keys:
                keys = session_keys.encode().decode("unicode_escape", errors="replace")
                more, st = await sess.send_keys(keys, wait_sentinel=not command.strip())
                parts.append(more)
                statuses.append(st)
            output = "\n".join(p for p in parts if p).strip() or "(无输出)"
            if len(output) > 50000:
                output = output[:25000] + "\n... [输出截断] ...\n" + output[-25000:]
            # interactive 挂起（timeout）不算失败：会话仍在运行，可用 session_keys 继续
            hung = any(s == "timeout" for s in statuses) and command.strip()
            hint = (
                "\n\n[PTY] 命令挂起，交互程序仍在前台（会话保留，未中断）。"
                "可继续调用本工具（interactive=true, command='', session_keys='...'）注入按键，"
                "或用 session_keys='\\x03' 发送 Ctrl-C 中断。"
                if hung
                else ""
            )
            return Observation(
                tool_name=self.name,
                success=not hung and code in (None, 0),
                output=output + hint,
                metadata={
                    "interactive": True,
                    "session_key": session_key or "default",
                    "status": statuses,
                    "exit_code": code,
                    "suggest_keys": True if hung else None,
                },
            )

        # 3. 沙箱执行
        if sandbox and sandbox.is_docker:
            try:
                stdout, stderr, returncode = await sandbox.execute(
                    command, args=args, timeout=timeout, cwd=cwd
                )
                output = stdout
                if stderr:
                    output += f"\n[stderr]\n{stderr}"
                
                # 截断过长输出
                if len(output) > 50000:
                    output = output[:25000] + "\n... [输出截断] ...\n" + output[-25000:]
                
                return Observation(
                    tool_name=self.name,
                    success=returncode == 0,
                    output=output,
                )
            except Exception as e:
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=f"沙箱执行错误: {type(e).__name__}: {e}",
                )

        # 4. 本地执行（原有逻辑）
        cmd_list = [command] + (args or [])
        work_dir = os.path.abspath(cwd)
        if not os.path.isdir(work_dir):
            work_dir = "."
        # cwd 安全校验：阻止访问系统敏感目录（硬拦截，不受 auto_approve 影响）
        else:
            _home = os.path.expanduser("~")
            _work_ok = True
            for _sd in SYSTEM_DIRS:
                if work_dir == _sd or work_dir.startswith(_sd + os.sep):
                    _work_ok = False
                    break
            if not _work_ok:
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=f"安全拦截: 不允许在系统目录 {work_dir} 下执行命令",
                )
            # 允许主目录、临时目录、常见项目/数据目录（个人版放宽）
            if IS_WINDOWS:
                # Windows：允许主目录 + 任意盘符根下的工作目录（系统目录已在上面拦截）
                _drive, _ = os.path.splitdrive(work_dir)
                _allowed_prefixes = [_home + os.sep]
                if _drive:
                    _allowed_prefixes.append(_drive + os.sep)
            else:
                _allowed_prefixes = [_home + os.sep] + list(ALLOWED_PATH_PREFIXES)
            if not (work_dir == _home or any(work_dir.startswith(p) for p in _allowed_prefixes)):
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=f"安全拦截: cwd '{work_dir}' 不在允许范围内（主目录/临时目录/项目目录）",
                )

        try:
            # ── 跨平台执行构造（2026-08-30 Windows 适配）──
            # 元字符（| > ; & <）→ Linux/macOS 用 bash -c、Windows 用 cmd.exe /c；
            # Windows 的 cmd 内建命令（dir/type 等无 .exe）同样经 cmd /c。
            # 注意：元字符不能 quote，否则 shell 会当字面量；只 quote 普通参数。
            _proc_cmd = _build_proc_cmd(cmd_list)
            process = await asyncio.create_subprocess_exec(
                *_proc_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
            )

            output_lines = []
            try:
                async for line in process.stdout:
                    decoded = decode_output(line)
                    output_lines.append(decoded)
                    if on_output:
                        on_output(decoded)
            except Exception as e:
                logging.getLogger(__name__).warning("读取命令输出流异常: %s", e)

            # 等待进程结束，带超时
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=f"命令超时 ({timeout}s)，已终止。\n" + "".join(output_lines),
                )

            full_output = "".join(output_lines)
            # 截断过长输出
            if len(full_output) > 50000:
                full_output = full_output[:25000] + "\n... [输出截断] ...\n" + full_output[-25000:]

            return Observation(
                tool_name=self.name,
                success=process.returncode == 0,
                output=full_output,
            )

        except FileNotFoundError:
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"命令未找到: {command}",
            )
        except PermissionError:
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"权限不足: {command}",
            )
        except Exception as e:
            return Observation(
                tool_name=self.name,
                success=False,
                output=f"执行错误: {type(e).__name__}: {e}",
            )

    # ── 复合命令自动拆分（2026-08-29）──────────────────────────
    async def _try_split_execute(self, command: str, args: list[str] | None, timeout: int, cwd: str) -> Observation | None:
        """把被安全校验拦截的复合命令拆成单条序列逐条执行；无法安全拆分返回 None.

        拆分规则：
        - 只拆 && 与 ;（逻辑序列），引号内不拆（保护 python -c "a; b" 等）
        - 不拆 |（管道）——拆分会改变数据流语义，且可能绕过 curl|sh 等拦截
        - 提取 cd X && 前缀作为工作目录（否则拆分后 cd 失效）
        - 剥离子 shell 括号 ( cmd )
        - 每条重新走 _validate_command，任一不安全即整体放弃
        """
        full = " ".join([command] + (args or []))

        def _split_outside_quotes(text: str) -> list[str]:
            parts: list[str] = []
            buf: list[str] = []
            quote: str | None = None
            i, n = 0, len(text)
            while i < n:
                ch = text[i]
                if quote:
                    buf.append(ch)
                    if ch == quote and i > 0 and text[i - 1] != "\\":
                        quote = None
                    i += 1
                    continue
                if ch in "'\"`":
                    quote = ch
                    buf.append(ch)
                    i += 1
                    continue
                if ch == "&" and i + 1 < n and text[i + 1] == "&":
                    parts.append("".join(buf)); buf = []; i += 2; continue
                if ch == ";":
                    parts.append("".join(buf)); buf = []; i += 1; continue
                buf.append(ch)
                i += 1
            parts.append("".join(buf))
            return parts

        # 提取 cd X && 前缀
        m = re.match(r"^\s*cd\s+(\S+)\s*(?:&&|;)\s*(.+)$", full)
        split_cwd = m.group(1) if m else ""
        rest = m.group(2) if m else full

        parts = _split_outside_quotes(rest)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) <= 1:
            return None

        # 剥离子 shell 括号；任一条含管道则整体放弃（语义变化 + 可能绕过 curl|sh 拦截）
        cleaned: list[str] = []
        for p in parts:
            if p.startswith("(") and p.endswith(")"):
                p = p[1:-1].strip()
            if not p or "|" in p:
                return None
            cleaned.append(p)

        # 每条重新安全校验（command + args 拆开）
        for p in cleaned:
            tokens = shlex.split(p)
            if not tokens:
                return None
            ok, _ = _validate_command(tokens[0], tokens[1:] if len(tokens) > 1 else None)
            if not ok:
                return None

        # 逐条执行，合并输出
        work_dir = os.path.abspath(split_cwd if split_cwd else cwd)
        if not os.path.isdir(work_dir):
            return None
        results: list[str] = []
        overall_ok = True
        for p in cleaned:
            obs = await self._exec_local(p, timeout=timeout, cwd=work_dir)
            results.append(f"$ {p}\n{obs.output}")
            if not obs.success:
                overall_ok = False  # 模拟真实 shell 的 ; 语义：继续执行后续，但整体标记失败
        return Observation(
            tool_name=self.name,
            success=overall_ok,
            output="\n".join(results) or "(无输出)",
            metadata={"split": True, "parts": len(cleaned)},
        )

    async def _exec_local(self, command: str, timeout: int, cwd: str = ".") -> Observation:
        """单条命令本地执行（供复合命令拆分复用；不触发流式回调）."""
        work_dir = os.path.abspath(cwd)
        if not os.path.isdir(work_dir):
            return Observation(tool_name=self.name, success=False, output=f"目录不存在: {cwd}")
        try:
            cmd_list = shlex.split(command) if command else []
            if not cmd_list:
                return Observation(tool_name=self.name, success=False, output="命令为空")
            _proc_cmd = _build_proc_cmd(cmd_list)
            process = await asyncio.create_subprocess_exec(
                *_proc_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
            )
            output_lines = []
            try:
                async for line in process.stdout:
                    output_lines.append(decode_output(line))
            except Exception as e:
                logging.getLogger(__name__).warning("读取命令输出流异常: %s", e)
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=f"命令超时 ({timeout}s)，已终止。\n" + "".join(output_lines),
                )
            full_output = "".join(output_lines)
            if len(full_output) > 50000:
                full_output = full_output[:25000] + "\n... [输出截断] ...\n" + full_output[-25000:]
            return Observation(
                tool_name=self.name,
                success=process.returncode == 0,
                output=full_output,
            )
        except FileNotFoundError:
            return Observation(tool_name=self.name, success=False, output=f"命令未找到: {command}")
        except PermissionError:
            return Observation(tool_name=self.name, success=False, output=f"权限不足: {command}")
        except Exception as e:
            return Observation(tool_name=self.name, success=False, output=f"执行错误: {type(e).__name__}: {e}")


# import 时自动注册
ToolRegistry.register(ShellTool())
