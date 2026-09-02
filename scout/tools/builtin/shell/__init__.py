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
- 解释器载荷深度检查（2026-08-31）：powershell/python/cmd 的 -Command/-c 参数是任意
  代码执行面，参数中出现"启动外部程序"载荷（Start-Process / subprocess / .exe 路径等）
  一律拦截，防止 agent 用解释器绕过白名单启动任意 exe。
- 跨平台命令平台化（2026-09-01）：白名单按系统过滤（Windows 剔除 POSIX 专用命令、
  Linux/macOS 剔除 Windows 专用命令），常用跨平台命令透明翻译（ls→dir、cat→type、
  grep→findstr、dir→ls、findstr→grep 等），参数不兼容时给出平台化提示——
  避免"过白名单但目标 shell 里命令未找到"的频繁操作报错。
- 应用启动后健康检查（2026-09-02）：已知应用（wemeetapp/wechat/dingtalk 等）经
  ShellExecuteW 启动后，轮询检测真实启动状态——检测到应用主窗口视为健康成功；
  检测到错误对话框（标准 #32770 对话框，静态文本含"找不到/网络路径/错误"等关键词）
  立即失败并返回弹窗完整文本；超时无新进程无窗口也判失败。杜绝"ShellExecuteW 返回
  成功但应用弹'找不到网络路径'错误框"的假成功反馈。
- 候选路径自动回退（2026-09-02）：健康检查失败（如某份安装损坏弹错误框）时，自动
  关闭错误对话框并尝试 KNOWN_APP_PATHS 中的下一个候选安装路径（如另一份可用安装），
  全部候选失败才返回失败。解决"机器上有损坏安装且排在前面，Scout 永远打到坏路径"
  的问题（真实案例：D:\tencent_meeting\WeMeet 损坏 → 自动回退 D:\tengxunhuiyi\WeMeet）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import subprocess  # noqa: F401 - DETACHED_PROCESS 用于 start 命令分离启动
import time
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.resources import no_window_kwargs
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
    # ★ 2026-09-01：.msc 管理单元（services.msc 等）无独立可执行文件，
    # 必须经 cmd /c 调用（cmd 会按文件关联打开 mmc 宿主）
    "services.msc", "devmgmt.msc", "diskmgmt.msc", "compmgmt.msc",
    "eventvwr.msc", "gpedit.msc", "secpol.msc", "certmgr.msc",
    "lusrmgr.msc", "perfmon.msc", "taskschd.msc", "wf.msc", "fsmgmt.msc",
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

# ★ 2026-09-01 修复「打开本地软件报错」——完整 PowerShell 解释器名单:
# powershell/pwsh 本身就是 shell,其 -Command 参数里的 | & ; 等元字符属于
# PowerShell 语法,必须原样传参直接 exec。若包一层 cmd.exe /c,Python
# subprocess 的参数重编码会造成双重引号解析,PowerShell 会把整条命令当成
# 字符串字面量回显(表现为:命令"看似执行"但程序未启动、$_ 等变量被展开
# 丢失、报"系统找不到文件/网络路径")。
_WIN_PS_EXES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_WIN_PS_CMD_RE = re.compile(
    r"""^(powershell(?:\.exe)?|pwsh(?:\.exe)?)\s+(-Command|-c|-command|--command)\s+(.+)$""",
    re.I | re.S,
)


def _win_split_args(command: str) -> list[str]:
    """Windows 引号感知的空白拆分（保留反斜杠）.

    shlex.split 是 POSIX 词法,会把 D:\\path 的反斜杠当转义符吃掉
    （D:\\Weixin\\Weixin.exe → D:WeixinWeixin.exe）,Windows 路径必须用本函数。
    引号内空格不拆分,引号本身剥离。
    """
    tokens: list[str] = []
    cur: list[str] = []
    in_q: str | None = None
    for ch in command:
        if in_q:
            if ch == in_q:
                in_q = None
            else:
                cur.append(ch)
        elif ch in ('"', "'"):
            in_q = ch
        elif ch.isspace():
            if cur:
                tokens.append(''.join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        tokens.append(''.join(cur))
    return tokens


def _needs_detached(cmd_list: list[str]) -> bool:
    """★ 2026-09-01 Windows：经 cmd.exe 执行 `start <程序>` 时需要 DETACHED。

    无控制台父进程（console=False 打包的 exe）下，CREATE_NO_WINDOW 的
    cmd.exe 执行内建 start 启动 GUI 程序，子进程会绑定到隐藏控制台并
    立即退出（实测矩阵：cmd /c start 不存活，+DETACHED_PROCESS 存活）。
    """
    if not cmd_list:
        return False
    base = os.path.basename(cmd_list[0]).lower()
    if base not in ("cmd.exe", "cmd"):
        return False
    # 形态1: [cmd.exe, /d, /s, /c, "start xxx ..."]  单串
    # 形态2: [cmd.exe, /d, /s, /c, "start", "xxx"]   拆分
    args = [a for a in cmd_list[1:] if a.lower() not in ("/d", "/s", "/c", "/k")]
    if not args:
        return False
    first = args[0].strip().lower()
    if first == "start" or first.startswith("start "):
        return True
    # 单串命令里 start 在最前
    if " " in args[0] and args[0].strip().split(None, 1)[0].lower() == "start":
        return True
    return False


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
    # ★ 2026-09-01 Windows 修复：PowerShell/pwsh 命令直接 exec,绕过 cmd.exe
    # 双重引号编码（详见 _WIN_PS_EXES 注释）——否则命令会被 PowerShell 当成
    # 字符串字面量回显而不执行,表现为"打开软件报错/程序没启动"。
    if IS_WINDOWS and cmd_list:
        first_raw = cmd_list[0].strip().lower()
        if len(cmd_list) > 1:
            # 多元素形态: cmd_list[0] 是纯命令名(可带路径),basename 提取
            is_ps_head = os.path.basename(first_raw) in _WIN_PS_EXES
        else:
            # 单元素形态: cmd_list[0] 是整串命令,取首个空白分隔 token。
            # 注意不能用 basename —— 整串含 Windows 路径时会被按 '\' 切割取到路径尾段
            _toks = first_raw.split()
            is_ps_head = bool(_toks) and _toks[0] in _WIN_PS_EXES
        if is_ps_head:
            if len(cmd_list) > 1:
                # 多元素形态 [powershell, -Command, <代码>]: 元字符属于 PS 语法,原样透传
                return cmd_list
            m = _WIN_PS_CMD_RE.match(cmd_list[0].strip())
            if m:
                code = m.group(3).strip()
                # 剥掉包裹代码的整体双引号（首尾配对时）
                if len(code) >= 2 and code[0] == '"' and code[-1] == '"':
                    code = code[1:-1]
                return [m.group(1), "-Command", code]
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
    # ── Windows 常用程序/系统工具 (2026-09-01 新增: 让"操作电脑"流畅 ——
    #    打开记事本/画图/计算器/资源管理器/控制面板等此前全被白名单拦截) ──
    # 附件程序
    "notepad", "calc", "mspaint", "write", "charmap", "snippingtool",
    "magnify", "osk", "winsat",
    # 资源管理器/控制面板/系统管理
    "explorer", "control", "regedit", "msconfig",
    "services.msc", "devmgmt.msc", "diskmgmt.msc", "compmgmt.msc",
    "eventvwr.msc", "gpedit.msc", "secpol.msc", "certmgr.msc",
    "lusrmgr.msc", "perfmon.msc", "taskschd.msc", "wf.msc", "fsmgmt.msc",
    # 网络/媒体
    "tracert", "pathping", "getmac", "netstat", "wmplayer", "mplayer2",
    # 其他常用
    "dxdiag", "resmon", "msra", "msdt", "optionalfeatures",
    # 终端
    "wt", "conhost",
}

# ── 平台化命令集（2026-09-01）──────────────────────────────
# 混合白名单的缺陷：ls/cat/grep 在 Windows cmd 下"过白名单但报命令未找到"，
# dir/findstr/tasklist 在 Linux/macOS 下同理 —— agent 反复重试 → 频繁操作报错。
# 三层方案：
#   1) 白名单按平台剔除另一平台专用命令（下面两个集合）；
#   2) 参数兼容的跨平台命令透明翻译（_WIN_ALIASES / _UNIX_ALIASES）；
#   3) 参数不兼容（如 ls -la、findstr /i）时给平台化提示（_platform_hint）。
_POSIX_ONLY_CMDS = {
    # 文件浏览
    "ls", "cat", "head", "tail", "wc", "grep", "find", "locate", "which",
    "whereis", "file", "stat", "du", "df",
    # 文件操作
    "touch", "cp", "mv", "rm", "ln", "chmod", "chown",
    # 文本处理
    "printf", "uniq", "cut", "awk", "sed", "tr", "diff", "comm", "paste",
    "fold", "column",
    # 系统信息
    "uname", "uptime", "env", "printenv", "id", "groups", "ps", "top",
    "free", "lscpu", "lsblk",
    # 网络
    "wget", "dig", "traceroute", "ss",
    # 环境/服务管理
    "bash", "sh", "source", "systemctl", "service", "supervisorctl",
    "uvicorn", "gunicorn", "nohup", "kill", "pkill", "killall", "rsync",
    "tmux", "screen", "vim", "nano", "less",
    # 包管理
    "apt", "apt-get", "yum", "brew",
    # 压缩
    "gzip", "gunzip", "zip",
    # 其他
    "xargs", "tee", "basename", "dirname", "realpath", "readlink",
    "md5sum", "sha256sum", "sha1sum",
    # shell 内建（cmd 无对应）
    "clear", "man", "alias", "unalias", "export", "unset", "shopt", "jobs",
    "fg", "bg", "wait", "dirs", "test", "true", "false", "history", "declare",
    "read", "readonly", "return", "shift", "builtin", "command", "umask",
    "ulimit",
    # 文本/数据
    "jq", "yq", "rg", "fd", "bat", "xxd", "hexdump", "od", "base64",
    "strings", "iconv", "dos2unix", "unix2dos", "numfmt", "fmt", "rev",
    "tac", "nl", "expand", "unexpand", "zcat", "bzcat", "xzcat",
    # 压缩/归档
    "xz", "unxz", "bzip2", "bunzip2", "zstd", "unzstd", "7za", "7zr", "lz4",
    "lzma", "unlzma", "cpio", "zipinfo", "zless", "zmore",
    # 系统诊断
    "who", "w", "last", "lastlog", "logname", "tty", "stty", "lsof",
    "fuser", "pgrep", "vmstat", "iostat", "mpstat", "pidstat", "htop",
    "btop", "ncdu", "lsusb", "lspci", "lsmod", "modinfo", "getent", "dmesg",
    "journalctl", "hostnamectl", "timedatectl", "localectl", "loginctl",
    "sysctl", "sync",
    # 网络
    "ip", "ifconfig", "route", "arp", "host", "nc", "ncat", "socat",
    "sftp", "ssh-keygen", "ssh-copy-id", "aria2c", "axel", "http", "httpie",
    "ab", "wrk", "hey", "kubectl", "helm", "podman", "ctr", "nerdctl",
    "gcloud", "aws", "az", "doctl", "terraform", "ansible",
    "ansible-playbook", "vagrant",
    # 开发/编译/调试
    "clang", "clang++", "gdb", "lldb", "valgrind", "strace", "ltrace",
    "objdump", "nm", "readelf", "size", "ldd", "strip", "ar", "ranlib",
    "patchelf", "pkg-config", "ninja", "meson", "patch", "cmp", "gpg",
    "sqlite3", "redis-cli", "psql", "mysql", "mongosh", "deno", "bun",
    "tsc", "ts-node", "lua", "luajit", "R", "Rscript", "tclsh", "wish",
    "mamba", "micromamba", "pipenv", "poetry", "uv", "uvx", "virtualenv",
    "pyenv", "expect",
    # 媒体/文档
    "ffmpeg", "ffprobe", "convert", "magick", "mogrify", "pdftotext",
    "pdfinfo", "pdftoppm", "pdftocairo", "gs", "exiftool", "mediainfo",
    "yt-dlp", "youtube-dl", "cwebp", "dwebp", "sox", "mutool", "qpdf",
    # 其他
    "watch", "seq", "yes", "sleep", "nproc", "arch", "getconf", "logger",
    "crontab",
}

_WIN_ONLY_CMDS = {
    # cmd 内建（bash 无对应）
    "chcp", "cls", "color", "title", "path", "prompt", "copy", "move",
    "del", "erase", "ren", "rename", "md", "rd", "vol", "ver", "verify",
    "assoc", "ftype", "start", "pause", "rem", "chdir", "setlocal",
    "endlocal",
    # Windows 外部命令
    "where", "tasklist", "taskkill", "ipconfig", "systeminfo", "schtasks",
    "reg", "wmic", "attrib", "findstr", "forfiles", "mklink", "setx",
    "xcopy", "robocopy", "mode", "compact", "driverquery", "netsh",
    "cscript", "wscript", "msinfo32", "winver", "cmd", "sfc", "takeown",
    "subst", "cipher", "fsutil", "powercfg", "gpupdate", "w32tm", "tzutil",
    "taskmgr", "chkdsk",
    # Windows 程序/系统工具
    "notepad", "calc", "mspaint", "charmap", "snippingtool", "magnify",
    "osk", "winsat", "explorer", "control", "regedit", "msconfig",
    "services.msc", "devmgmt.msc", "diskmgmt.msc", "compmgmt.msc",
    "eventvwr.msc", "gpedit.msc", "secpol.msc", "certmgr.msc",
    "lusrmgr.msc", "perfmon.msc", "taskschd.msc", "wf.msc", "fsmgmt.msc",
    "tracert", "pathping", "getmac", "wmplayer", "mplayer2", "dxdiag",
    "resmon", "msra", "msdt", "optionalfeatures", "wt", "conhost",
}

if IS_WINDOWS:
    SAFE_COMMANDS = SAFE_COMMANDS - _POSIX_ONLY_CMDS
else:
    SAFE_COMMANDS = SAFE_COMMANDS - _WIN_ONLY_CMDS

# 跨平台命令透明翻译表：值 (目标命令, 固定前置参数)。
# 仅覆盖语义等价、参数基本兼容的常用命令；参数带 - / / 开关时不翻译（走 _platform_hint）。
_WIN_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "ls": ("dir", ()),
    "cat": ("type", ()),
    "pwd": ("cd", ()),
    "which": ("where", ()),
    "grep": ("findstr", ()),
    "clear": ("cls", ()),
    "cp": ("copy", ()),
    "mv": ("move", ()),
    "rm": ("del", ()),
    "touch": ("type", ("nul", ">")),   # 创建空文件
    "uname": ("ver", ()),
    "diff": ("fc", ()),
    "unzip": ("tar", ("-xf",)),        # Win10 自带 tar 支持 zip
}

_UNIX_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "dir": ("ls", ()),
    "where": ("which", ()),
    "findstr": ("grep", ()),
    "cls": ("clear", ()),
    "copy": ("cp", ()),
    "move": ("mv", ()),
    "del": ("rm", ()),
    "erase": ("rm", ()),
    "ren": ("mv", ()),
    "rename": ("mv", ()),
    "md": ("mkdir", ()),
    "ver": ("uname", ("-a",)),
    "ipconfig": ("ip", ("addr",)),
    "tasklist": ("ps", ("aux",)),
    "taskkill": ("kill", ()),
}

_PLATFORM_HINT_EXAMPLES: dict[str, str] = {
    # Windows 侧（POSIX 命令 → 用法示例）
    "ls": "dir /a（含隐藏）、dir /s /b（递归）",
    "cat": "type file.txt",
    "grep": "findstr /i pattern file.txt",
    "which": "where python",
    "pwd": "cd（不带参数显示当前目录）",
    "clear": "cls",
    "cp": "copy src dst",
    "mv": "move src dst",
    "rm": "del file（删目录用 rmdir /s）",
    "touch": "type nul > newfile.txt",
    "diff": "fc file1 file2",
    "uname": "ver",
    "sleep": "powershell -Command \"Start-Sleep -Seconds 5\"",
    "head": "powershell -Command \"Get-Content file.txt -TotalCount 5\"",
    "tail": "powershell -Command \"Get-Content file.txt -Tail 5\"",
    # Unix 侧（Windows 命令 → 用法示例）
    "where": "which python",
    "findstr": "grep -i pattern file",
    "dir": "ls -la",
    "cls": "clear",
    "copy": "cp src dst",
    "move": "mv src dst",
    "del": "rm file",
    "ren": "mv old new",
    "md": "mkdir dir",
    "ver": "uname -a",
    "ipconfig": "ip addr 或 ipconfig 对应网卡信息用 ip link",
    "tasklist": "ps aux",
    "taskkill": "kill <pid>",
}


def _map_platform_command(command: str, args: list[str] | None) -> tuple[str, list[str] | None] | None:
    """把另一平台风格的命令翻译为当前平台等价命令（透明，LLM 无感知）.

    返回:
      - (new_command, new_args): 翻译成功或无需翻译，直接使用；
      - None: 命中跨平台命令但参数不兼容（带 - / / 开关），调用方应给平台化提示。
    """
    if not command or not command.strip():
        return command, args
    parts = command.strip().split(None, 1)
    if len(parts) > 1:
        # 整串命令（含空格/复合命令）：保留 shell 语义，不翻译
        return command, args
    base = os.path.basename(parts[0]).lower()
    table = _WIN_ALIASES if IS_WINDOWS else _UNIX_ALIASES
    entry = table.get(base)
    if entry is None:
        return command, args
    new_base, extra = entry
    if args:
        for a in args:
            if a.startswith("-") or a.startswith("/"):
                return None
    return new_base, list(extra) + list(args or [])


def _platform_hint(base_cmd: str) -> str:
    """跨平台命令被拦截时的平台化提示（另一平台命令 → 本平台等价命令 + 用法示例）."""
    base = base_cmd.lower()
    if IS_WINDOWS:
        entry = _WIN_ALIASES.get(base)
        if entry:
            target, _ = entry
            ex = _PLATFORM_HINT_EXAMPLES.get(base, f"请使用 {target} 对应功能")
            return (
                f"安全拦截: '{base_cmd}' 是 Linux/macOS 命令，当前 Windows 环境没有该命令。\n"
                f"💡 请改用 Windows 命令 '{target}'：{ex}"
            )
        if base in _POSIX_ONLY_CMDS:
            return (
                f"安全拦截: '{base_cmd}' 是 Linux/macOS 命令，当前 Windows 环境没有该命令。\n"
                f"💡 请改用 PowerShell 对应命令（如 Get-ChildItem / Get-Content / Select-String）。"
            )
        return ""
    entry = _UNIX_ALIASES.get(base)
    if entry:
        target, _ = entry
        ex = _PLATFORM_HINT_EXAMPLES.get(base, f"请使用 {target} 对应功能")
        return (
            f"安全拦截: '{base_cmd}' 是 Windows 命令，当前系统（Linux/macOS）没有该命令。\n"
            f"💡 请改用 '{target}'：{ex}"
        )
    if base in _WIN_ONLY_CMDS:
        return (
            f"安全拦截: '{base_cmd}' 是 Windows 命令，当前系统（Linux/macOS）没有该命令。\n"
            f"💡 请改用对应的 POSIX 命令。"
        )
    return ""

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
    r"start\s+.*\.exe",       # Windows start 启动可执行文件（2026-08-31：start 是白名单命令，
                              # 但 start any.exe 会启动任意程序，仅拦 .exe，不拦文档/网页）
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

# ── 解释器载荷深度检查（2026-08-31 补强）────────────────────
# 背景：白名单允许 powershell/python/cmd 等解释器，但其 -Command/-c 参数是任意
# 代码执行面，可用来绕过白名单启动任意外部程序。
# 真实案例：agent 用 powershell -Command "Start-Process 'D:\Weixin\Weixin.exe'"
# 绕过了对直接执行 Weixin.exe 的拦截，成功启动微信。
# 策略：解释器参数中出现"启动外部程序"载荷 → 拦截，引导用户到终端手动执行。
# 注意：bash/sh 不做深度检查（Linux 下 bash -c "python3 x.py" 属正常开发用法，
# 误伤面太大）；Windows 主要绕过面是 powershell/cmd/python，已全覆盖。
EXEC_LAUNCH_BYPASS: dict[str, tuple[str, ...]] = {
    "powershell": (
        r"-EncodedCommand",               # base64 编码命令不可审计，一律拦截
        r"Start-Process",
        r"Invoke-Expression", r"\biex\b",
        r"Invoke-Item", r"\bii\b",
        r"System\.Diagnostics\.Process",
        r"WScript\.Shell",
        r"Shell\.Application",
        r"['\"][^'\"]+\.exe['\"]",        # 引号内 .exe 路径（启动目标）
    ),
    "pwsh": (
        r"-EncodedCommand",
        r"Start-Process",
        r"Invoke-Expression", r"\biex\b",
        r"Invoke-Item", r"\bii\b",
        r"System\.Diagnostics\.Process",
        r"WScript\.Shell",
        r"Shell\.Application",
        r"['\"][^'\"]+\.exe['\"]",
    ),
    "cmd": (
        r"\bstart\b",                     # cmd 内建 start 启动程序
        r"powershell",                    # cmd 里再调 powershell 通常是绕过
        r"['\"][^'\"]+\.exe['\"]",        # 引号内 .exe 路径
    ),
    "python": (
        r"\bimport\s+subprocess\b",
        r"\bos\.system\b",
        r"\bos\.startfile\b",
        r"\bPopen\b",
        r"\beval\s*\(",
        r"\bexec\s*\(",
    ),
    "python3": (
        r"\bimport\s+subprocess\b",
        r"\bos\.system\b",
        r"\bos\.startfile\b",
        r"\bPopen\b",
        r"\beval\s*\(",
        r"\bexec\s*\(",
    ),
}


# ★ 2026-09-01：已知 Windows 应用 → 常见安装路径模板。
# 当 LLM 提交裸名 exe 被白名单拦截时,用本表解析真实绝对路径并给出正确启动姿势,
# 避免"打开腾讯会议/微信/钉钉报错"体验。allow_app_launch=True(Windows 个人版默认)
# 时 execute 层会自动重写为 `start "" "绝对路径"` 分离启动,保证 LLM 一次成功。
# 模板变量: {PF} {PF32} {LOCALAPPDATA} {APPDATA} {USERPROFILE} {DRIVE}
#   {DRIVE} 展开为所有文件系统盘根(C:\ D:\ E:\ ...),适配非标准安装路径。
KNOWN_APP_PATHS: dict[str, tuple[str, tuple[str, ...]]] = {
    # exe basename(小写) -> (应用显示名, (候选目录模板, ...))
    "wemeetapp.exe": ("腾讯会议", (
        r"{PF}\tencent\WeMeet", r"{PF32}\Tencent\WeMeet",
        r"{DRIVE}\tencent_meeting\WeMeet", r"{DRIVE}\tengxunhuiyi\WeMeet",
        r"{DRIVE}\Tencent\WeMeet", r"{APPDATA}\Tencent\WeMeet",
    )),
    "wemeet.exe": ("腾讯会议", (
        r"{PF}\tencent\WeMeet", r"{PF32}\Tencent\WeMeet",
        r"{DRIVE}\tencent_meeting\WeMeet", r"{DRIVE}\tengxunhuiyi\WeMeet",
        r"{DRIVE}\Tencent\WeMeet", r"{APPDATA}\Tencent\WeMeet",
    )),
    "wechat.exe": ("微信", (
        r"{PF}\Tencent\WeChat", r"{PF32}\Tencent\WeChat",
        r"{DRIVE}\WeChat", r"{DRIVE}\Program Files\Tencent\WeChat",
        r"{LOCALAPPDATA}\Programs\Tencent\WeChat", r"{APPDATA}\Tencent\WeChat",
    )),
    "weixin.exe": ("微信", (
        r"{PF}\Tencent\Weixin", r"{PF32}\Tencent\Weixin",
        r"{DRIVE}\Weixin", r"{LOCALAPPDATA}\Tencent\Weixin",
    )),
    "wxwork.exe": ("企业微信", (
        r"{PF32}\Tencent\WXWork", r"{PF}\Tencent\WXWork",
        r"{DRIVE}\WXWork", r"{APPDATA}\Tencent\WXWork",
    )),
    "dingtalk.exe": ("钉钉", (
        r"{PF32}\DingDing", r"{PF}\DingDing",
        r"{LOCALAPPDATA}\DingTalk", r"{LOCALAPPDATA}\Programs\DingTalk",
        r"{DRIVE}\DingDing", r"{DRIVE}\DingTalk",
    )),
    "feishu.exe": ("飞书", (
        r"{PF}\Feishu", r"{PF32}\Feishu",
        r"{LOCALAPPDATA}\Feishu", r"{LOCALAPPDATA}\Programs\Feishu",
        r"{DRIVE}\Feishu", r"{DRIVE}\Lark",
    )),
    "lark.exe": ("飞书(Lark)", (
        r"{PF}\Lark", r"{LOCALAPPDATA}\Lark", r"{DRIVE}\Lark",
    )),
    "qq.exe": ("QQ", (
        r"{PF32}\Tencent\QQ", r"{PF}\Tencent\QQ",
        r"{DRIVE}\QQ", r"{DRIVE}\Program Files\Tencent\QQ",
    )),
    "tim.exe": ("TIM", (
        r"{PF32}\Tencent\TIM", r"{PF}\Tencent\TIM",
    )),
    "chrome.exe": ("Google Chrome", (
        r"{PF}\Google\Chrome\Application", r"{PF32}\Google\Chrome\Application",
        r"{LOCALAPPDATA}\Google\Chrome\Application",
    )),
    "msedge.exe": ("Microsoft Edge", (
        r"{PF32}\Microsoft\Edge\Application", r"{PF}\Microsoft\Edge\Application",
    )),
    "firefox.exe": ("Mozilla Firefox", (
        r"{PF}\Mozilla Firefox", r"{PF32}\Mozilla Firefox",
    )),
    "code.exe": ("Visual Studio Code", (
        r"{LOCALAPPDATA}\Programs\Microsoft VS Code", r"{PF}\Microsoft VS Code",
    )),
    "winword.exe": ("Microsoft Word", (
        r"{PF}\Microsoft Office\root\Office16", r"{PF32}\Microsoft Office\root\Office16",
        r"{PF}\Microsoft Office\Office16",
    )),
    "excel.exe": ("Microsoft Excel", (
        r"{PF}\Microsoft Office\root\Office16", r"{PF32}\Microsoft Office\root\Office16",
        r"{PF}\Microsoft Office\Office16",
    )),
    "powerpnt.exe": ("Microsoft PowerPoint", (
        r"{PF}\Microsoft Office\root\Office16", r"{PF32}\Microsoft Office\root\Office16",
        r"{PF}\Microsoft Office\Office16",
    )),
    "wps.exe": ("WPS Office", (
        r"{PF}\Kingsoft\WPS Office", r"{PF32}\Kingsoft\WPS Office",
        r"{DRIVE}\Kingsoft\WPS Office",
    )),
    "et.exe": ("WPS 表格", (
        r"{PF}\Kingsoft\WPS Office", r"{PF32}\Kingsoft\WPS Office",
        r"{DRIVE}\Kingsoft\WPS Office",
    )),
    "wpp.exe": ("WPS 演示", (
        r"{PF}\Kingsoft\WPS Office", r"{PF32}\Kingsoft\WPS Office",
        r"{DRIVE}\Kingsoft\WPS Office",
    )),
    "cloudmusic.exe": ("网易云音乐", (
        r"{LOCALAPPDATA}\Netease\CloudMusic", r"{DRIVE}\Netease\CloudMusic",
    )),
    "qqmusic.exe": ("QQ音乐", (
        r"{PF32}\Tencent\QQMusic", r"{PF}\Tencent\QQMusic",
    )),
    "potplayermini64.exe": ("PotPlayer", (
        r"{DRIVE}\PotPlayer", r"{PF}\DAUM\PotPlayer",
    )),
    "potplayermini.exe": ("PotPlayer", (
        r"{DRIVE}\PotPlayer", r"{PF}\DAUM\PotPlayer",
    )),
    "vlc.exe": ("VLC 播放器", (r"{PF}\VideoLAN\VLC", r"{PF32}\VideoLAN\VLC")),
    "7zfm.exe": ("7-Zip 文件管理器", (r"{PF}\7-Zip", r"{PF32}\7-Zip")),
    "winrar.exe": ("WinRAR", (r"{PF}\WinRAR", r"{PF32}\WinRAR")),
    "steam.exe": ("Steam", (
        r"{PF32}\Steam", r"{PF}\Steam", r"{DRIVE}\Steam",
    )),
    "baidunetdisk.exe": ("百度网盘", (
        r"{PF}\Baidu\BaiduNetdisk", r"{LOCALAPPDATA}\Baidu\BaiduNetdisk",
        r"{DRIVE}\BaiduNetdisk",
    )),
    "thunder.exe": ("迅雷", (
        r"{PF}\Thunder Network\Thunder", r"{DRIVE}\Thunder Network\Thunder",
    )),
    "sunloginclient.exe": ("向日葵远程控制", (
        r"{PF}\Oray\SunLogin", r"{PF32}\Oray\SunLogin",
    )),
    "todesk.exe": ("ToDesk", (
        r"{PF}\ToDesk", r"{DRIVE}\ToDesk", r"{LOCALAPPDATA}\Programs\ToDesk",
    )),
    "typora.exe": ("Typora", (
        r"{LOCALAPPDATA}\Programs\Typora", r"{PF}\Typora",
    )),
    "obsidian.exe": ("Obsidian", (
        r"{LOCALAPPDATA}\Obsidian", r"{LOCALAPPDATA}\Programs\Obsidian",
    )),
    "wechatdevtools.exe": ("微信开发者工具", (
        r"{LOCALAPPDATA}\微信开发者工具", r"{LOCALAPPDATA}\Programs\微信开发者工具",
    )),
    "notepad++.exe": ("Notepad++", (r"{PF}\Notepad++", r"{PF32}\Notepad++")),
    "everything.exe": ("Everything", (r"{PF}\Everything", r"{PF32}\Everything")),
    "git-bash.exe": ("Git Bash", (r"{PF}\Git", r"{PF32}\Git")),
    "anki.exe": ("Anki", (r"{LOCALAPPDATA}\Programs\Anki",)),
}

# 应用目录模板变量展开值（环境变量缺失时回退系统默认路径）
_APP_DIR_ENV = (
    ("{PF32}", lambda: os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"),
    ("{PF}", lambda: os.environ.get("ProgramFiles") or r"C:\Program Files"),
    ("{LOCALAPPDATA}", lambda: os.environ.get("LOCALAPPDATA") or ""),
    ("{APPDATA}", lambda: os.environ.get("APPDATA") or ""),
    ("{USERPROFILE}", lambda: os.environ.get("USERPROFILE") or ""),
)

_KNOWN_APP_CANDIDATE_CACHE: dict[str, list[str]] = {}


def _app_drives() -> list[str]:
    """所有文件系统盘根（去重），优先 C 盘外的数据盘."""
    import string as _string

    roots: list[str] = []
    seen: set[str] = set()
    for _letter in _string.ascii_uppercase:
        root = f"{_letter}:\\"
        if os.path.exists(root) and root not in seen:
            seen.add(root)
            roots.append(root)
    if not roots:
        roots = ["C:\\"]
    return roots


def _expand_app_dirs(templates: tuple[str, ...]) -> list[str]:
    """展开路径模板 → 候选目录列表.

    {DRIVE} 前缀模板展开为每个盘根；其余 {VAR} 就地替换为环境变量值；
    目录必须真实存在才保留。
    """
    dirs: list[str] = []
    for tpl in templates:
        if tpl.startswith("{DRIVE}\\"):
            rel = tpl[len("{DRIVE}\\"):]
            for root in _app_drives():
                cand = os.path.join(root, rel)
                if os.path.isdir(cand):
                    dirs.append(cand)
            continue
        expanded = tpl
        for key, getter in _APP_DIR_ENV:
            val = getter()
            if val:
                expanded = expanded.replace(key, val)
        if expanded and os.path.isdir(expanded):
            dirs.append(expanded)
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _search_common_roots(exe_name: str) -> str | None:
    """在常见安装根目录下做有限深度搜索（≤3 层、限制目录数），返回首个匹配的绝对路径.

    作为 KNOWN_APP_PATHS 模板未命中的兜底（非标准安装、自定义盘符等场景）。
    """
    roots: list[str] = []
    for _key, getter in _APP_DIR_ENV:
        val = getter()
        if val and os.path.isdir(val):
            roots.append(val)
    roots.append(os.path.expanduser("~"))
    for drv in _app_drives():
        roots.append(drv)

    _name_l = exe_name.lower()
    _max_dirs = 6000  # 每根最多遍历目录数，避免全盘扫描卡顿
    for root in roots:
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            count += 1
            if count > _max_dirs:
                break
            depth = dirpath[len(root):].count(os.sep)
            if depth >= 3:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if not d.startswith("$") and not d.lower().startswith("windows")]
            for fn in filenames:
                if fn.lower() == _name_l:
                    return os.path.join(dirpath, fn)
    return None


def _resolve_known_app_candidates(exe_name: str) -> list[str]:
    """解析已知应用的全部候选绝对路径（Windows）。

    模板按优先级展开、逐个探测，返回【全部】命中路径（有序，首个为最高优先级）；
    模板全部未命中时，再兜底全盘搜索常见目录。结果缓存避免重复全盘扫描。

    ★ 2026-09-02：返回候选列表而非单个结果——启动后健康检查失败时可自动回退到
    下一个候选（如某份安装损坏弹"找不到网络路径"，回退到另一份可用安装）。
    """
    if not IS_WINDOWS or not exe_name:
        return []
    key = exe_name.strip().lower()
    if not key or not key.endswith(".exe"):
        return []
    if key in _KNOWN_APP_CANDIDATE_CACHE:
        return list(_KNOWN_APP_CANDIDATE_CACHE[key])

    result: list[str] = []
    entry = KNOWN_APP_PATHS.get(key)
    if entry:
        _display, templates = entry
        for d in _expand_app_dirs(templates):
            cand = os.path.join(d, exe_name)
            if os.path.isfile(cand):
                result.append(cand)
    if not result:
        found = _search_common_roots(exe_name)
        if found:
            result.append(found)
    _KNOWN_APP_CANDIDATE_CACHE[key] = list(result)
    return result


def _resolve_known_app(exe_name: str) -> str | None:
    """解析已知应用的绝对路径（Windows），返回最高优先级候选（=candidates[0]）。

    由白名单校验与 execute 层调用；启动时如需回退请使用
    _resolve_known_app_candidates 获取完整候选列表。
    """
    cands = _resolve_known_app_candidates(exe_name)
    return cands[0] if cands else None


def _win_enum_windows() -> list[tuple[int, str, str]]:
    """枚举可见顶层窗口 -> [(hwnd, title, class_name)]（Windows，ctypes，无第三方依赖）."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    out: list[tuple[int, str, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
            cls_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls_buf, 256)
            out.append((int(hwnd), title, cls_buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    return out


def _win_enum_procs() -> set[str]:
    """枚举当前所有进程 exe 名集合（Windows，ctypes，无第三方依赖）."""
    import ctypes
    from ctypes import wintypes
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    h = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    names: set[str] = set()
    if not h or h == ctypes.c_void_p(-1).value:
        return names
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(h, ctypes.byref(pe)):
            while True:
                names.add(pe.szExeFile.lower())
                if not kernel32.Process32NextW(h, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(h)
    return names


def _win_dialog_text(hwnd: int) -> str:
    """读取标准对话框（#32770）内全部 Static 文本，拼成错误详情."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    parts: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(child, lparam):
        length = user32.GetWindowTextLengthW(child)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(child, buf, length + 1)
            parts.append(buf.value)
        return True

    user32.EnumChildWindows(ctypes.c_void_p(hwnd), _cb, 0)
    return " | ".join(p for p in parts if p.strip())


def _win_close_window(hwnd: int) -> None:
    """向窗口发送 WM_CLOSE（0x0010），自动关闭（错误）对话框，避免残留.

    ★ 2026-09-02：候选路径自动回退时，先关掉损坏安装弹出的错误框，
    避免界面残留错误框、也避免其干扰后续候选的窗口/进程快照判断。
    """
    if not hwnd:
        return
    import ctypes
    try:
        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)
    except Exception:  # noqa: BLE001
        pass


# 错误对话框关键词（标题或静态文本命中即视为启动异常）
_ERR_DIALOG_KEYWORDS = (
    "找不到", "网络路径", "错误", "失败", "无法", "不能", "拒绝", "未响应",
    "已停止", "异常", "error", "failed", "not found", "cannot", "unavailable",
    "拒绝访问",
)


def _launch_app_windows(path: str, app_display: str = "", wait: float = 8.0) -> tuple[bool, str]:
    """用 Windows ShellExecuteW 启动本地应用 + 启动后健康检查（★ 2026-09-02）.

    ★ 2026-09-01 实测结论: 无控制台/打包 exe 环境下, `cmd /c start` 启动 GUI
    程序不可靠（cmd 内建 start 的 ShellExecute 语义在 DETACHED+DEVNULL 下会丢失），
    而 ShellExecuteW 走系统 Shell 语义 + 应用所在目录作 lpDirectory, 稳定拉起
    （腾讯会议/钉钉/飞书/QQ 均验证）。返回值 > 32 表示调用成功。

    ★ 2026-09-02 启动后健康检查：ShellExecuteW 返回成功 ≠ 应用真的起来了
    （wemeetapp 等 launcher 可能自己弹"找不到网络路径"错误框）。启动前记录
    窗口/进程快照，启动后轮询 wait 秒：
      1) 检测到应用主窗口（标题含 app_display）→ 健康成功；
      2) 检测到错误对话框（#32770 + 文本含错误关键词）→ 立即失败并返回弹窗全文；
      3) 超时但出现新进程 → 降级成功（进程已起，主窗口稍慢）；
      4) 超时无新进程无窗口 → 失败。

    Returns:
        (ok, detail) — ok=True 应用已运行；detail 为人类可读诊断/错误文本。
    """
    if not IS_WINDOWS:
        return True, ""
    import ctypes

    before_titles = {t for _, t, _ in _win_enum_windows()}
    before_procs = _win_enum_procs()
    # 应用已在运行（主窗口已存在）：ShellExecuteW 只会前置激活，直接判健康
    if app_display and any(app_display in t for t in before_titles):
        return True, "应用已在运行，已前置激活"

    try:
        res = ctypes.windll.shell32.ShellExecuteW(
            None, "open", path, None, os.path.dirname(path) or ".", 1,
        )
        if int(res) <= 32:
            return False, f"ShellExecuteW 调用失败（返回码 {int(res)}，<32 为 SE_ERR_*）"
    except Exception as e:  # noqa: BLE001
        return False, f"ShellExecuteW 调用异常: {e}"

    deadline = time.monotonic() + wait
    err_hint = ""
    got_new_proc = False
    while time.monotonic() < deadline:
        time.sleep(0.5)
        wins = _win_enum_windows()
        new_wins = [(h, t, c) for h, t, c in wins if t and t not in before_titles]
        # 1) 主窗口出现 → 健康
        if app_display:
            for _, t, _ in new_wins:
                if app_display in t:
                    return True, f"已检测到主窗口: {t!r}"
        # 2) 错误对话框 → 自动关闭并立即失败返回全文
        #    （自动关闭：避免残留错误框，也便于调用方回退下一个候选安装）
        for h, t, c in new_wins:
            if c == "#32770":
                body = _win_dialog_text(h)
                if any(k.lower() in (t + " " + body).lower() for k in _ERR_DIALOG_KEYWORDS):
                    _win_close_window(h)
                    return False, f"检测到错误对话框: 标题={t!r} 内容={body!r}"
            elif any(k.lower() in t.lower() for k in _ERR_DIALOG_KEYWORDS):
                _win_close_window(h)
                err_hint = err_hint or f"检测到错误窗口: {t!r}"
        # 3) 新进程出现（launcher 拉起子进程）
        if _win_enum_procs() - before_procs:
            got_new_proc = True

    if err_hint:
        return False, err_hint
    if got_new_proc:
        return True, "进程已启动（主窗口暂未检测到）"
    return False, "启动后等待期内未检测到新进程或窗口，应用可能未正常启动"


def _app_launch_hint(exe_name: str) -> str:
    """白名单拒绝时，若疑似本地应用则给出"正确启动姿势"提示（Windows）."""
    if not IS_WINDOWS or not exe_name.lower().endswith(".exe"):
        return ""
    resolved = _resolve_known_app(exe_name)
    if resolved:
        return (
            f"安全拦截: 命令 '{exe_name}' 不在白名单中。\n"
            f"💡 检测到已知应用。正确启动方式（Windows）:\n"
            f"  shell(command=\"start\", args=[\"\", \"{resolved}\"])   # 分离启动，推荐\n"
            f"或  shell(command=\"powershell\", args=[\"-NoProfile\", \"-Command\", \"Start-Process '{resolved}'\"])"
        )
    return (
        f"安全拦截: 命令 '{exe_name}' 不在白名单中。\n"
        f"💡 启动本地应用的推荐方式（Windows）:\n"
        f"  shell(command=\"powershell\", args=[\"-NoProfile\", \"-Command\", \"Start-Process 'C:\\\\完整\\\\路径\\\\app.exe'\"])"
        f"\n裸名可能命中 Microsoft Store 占位程序而静默退出，请使用绝对路径。"
    )


def _validate_command(command: str, args: list[str] | None = None, allow_app_launch: bool = False) -> tuple[bool, str]:
    """三重安全校验：白名单 + 黑名单 + 注入检测.

    allow_app_launch=True（Windows 个人版，配置文件开关）：放行"启动本地应用"载荷
    （start xxx.exe / Start-Process / 引号内 .exe 等），仍保留 -EncodedCommand 编码命令拦截。

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
        # ★ 2026-09-01：已知 Windows 应用裸名(.exe) — allow_app_launch=True 时放行。
        # execute 层会把裸名重写为 `start "" "绝对路径"` 分离启动，LLM 无需猜姿势。
        if IS_WINDOWS and allow_app_launch and base_cmd.lower() in KNOWN_APP_PATHS:
            if _resolve_known_app(base_cmd):
                return True, ""
        hint = _app_launch_hint(base_cmd)
        if hint:
            return False, hint
        # ★ 2026-09-01：跨平台命令平台化提示（另一平台命令 → 本平台等价命令 + 用法）
        phint = _platform_hint(base_cmd)
        if phint:
            return False, phint
        return False, f"安全拦截: 命令 '{base_cmd}' 不在白名单中。允许: {', '.join(sorted(SAFE_COMMANDS)[:20])}..."

    # 2.5 解释器载荷深度检查（2026-08-31）：powershell/python/cmd 的 -Command/-c
    # 参数是任意代码执行面，白名单允许解释器本身，但参数中"启动外部程序"的载荷
    # 必须拦截——否则 agent 可用 powershell -Command "Start-Process 'x.exe'" 绕过白名单。
    if base_cmd in EXEC_LAUNCH_BYPASS:
        # 去掉命令名本身（兼容 command 字段直接带参的情况，如 "powershell -Command ..."）
        _payload = full_cmd.replace(base_cmd, "", 1).lstrip()
        for _pat in EXEC_LAUNCH_BYPASS[base_cmd]:
            # allow_app_launch=True：放行"启动本地应用"载荷（Start-Process / .exe 路径等），
            # 仅保留 -EncodedCommand 拦截——base64 编码命令完全不可审计，属另一类风险，无论开关都拦
            if allow_app_launch and "-EncodedCommand" not in _pat:
                continue
            if re.search(_pat, _payload, re.IGNORECASE):
                return False, (
                    "安全拦截: 检测到通过解释器启动外部程序的绕过载荷"
                    f"（{base_cmd} 参数含 Start-Process / subprocess / .exe 路径等，"
                    "这类操作应在你自己的终端里手动执行）。"
                )

    # 3. 危险参数模式检测
    for pattern in DANGEROUS_ARGS:
        # allow_app_launch=True：放行 "start xxx.exe"（Windows 启动应用），
        # 其余危险参数（rm -rf、dd、curl|sh 等）仍拦截
        if allow_app_launch and pattern == r"start\s+.*\.exe":
            continue
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
        "Prefer args parameter for arguments. System directories (/etc, /usr, /bin) are blocked.\n"
        "Windows guidance: "
        "(1) Launch apps via PowerShell with full path: powershell -Command \"Start-Process 'C:\\path\\app.exe'\" "
        "(known apps like Tencent Meeting/WeChat/DingTalk/Feishu/QQ/Chrome are auto-resolved: "
        "just pass the bare exe name e.g. command='wemeetapp.exe'). "
        "(bare names like notepad/calc may hit the Microsoft Store stub and silently exit). "
        "(2) Open folders with: explorer 'D:\\path'. "
        "(3) Chinese output is auto-decoded (GBK/UTF-8). "
        "(4) explorer/start/.msc exit code 1 still means success. "
        "(5) Cross-platform commands auto-translate to the current OS (ls→dir, cat→type, "
        "grep→findstr, which→where, pwd→cd on Windows; dir→ls, findstr→grep, tasklist→ps "
        "aux on Linux/macOS), so common POSIX commands work on Windows without errors."
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

    def adapt_schema(self, schema: dict) -> dict:
        """平台自适应（2026-08-30）：按运行系统调整给 LLM 的命令示例与参数说明.

        Windows → cmd.exe 示例（dir/type/findstr），标注 PTY 仅 Unix 可用；
        Linux/macOS → 保持 bash 示例不变。避免 LLM 在 Windows 上尝试
        ls/cat/grep 等不存在于 cmd 的命令。
        """
        fn = schema.get("function") or {}
        props = ((fn.get("parameters") or {}).get("properties")) or {}
        if IS_WINDOWS:
            fn["description"] = (
                "Execute a shell command in a restricted environment. "
                "Commands run via cmd.exe on Windows; only whitelisted utilities are allowed. "
                "Dangerous operations (recursive delete, disk format, piping to shell) are blocked. "
                "System dirs (C:\\Windows, C:\\Program Files) are blocked. "
                "Prefer args parameter for arguments. "
                "Launching apps: known apps (wemeetapp.exe/wechat.exe/wxwork.exe/dingtalk.exe/"
                "feishu.exe/qq.exe/chrome.exe/msedge.exe etc.) are auto-resolved — just pass the "
                "bare exe name as command (e.g. command='wemeetapp.exe'); for others use "
                "command='start' with args=['', 'C:\\path\\app.exe'] or powershell "
                "-Command \"Start-Process 'C:\\path\\app.exe'\". "
                "After launching a known app, the tool verifies the app really started "
                "(main window detected) and reports any error dialog text (e.g. 'network path "
                "not found') back to you instead of falsely claiming success. "
                "If a launch fails its health check (e.g. a broken install pops an error dialog), "
                "the tool automatically closes the dialog and falls back to the next candidate "
                "install path (e.g. another working install of the same app); only when all "
                "candidates fail does it return failure. "
                "Cross-platform commands are auto-translated to the current OS equivalents: "
                "ls→dir, cat→type, grep→findstr, which→where, pwd→cd, cp→copy, mv→move, "
                "rm→del, clear→cls, uname→ver (e.g. command='ls' works and runs 'dir')."
            )
            cmd = props.get("command")
            if cmd:
                cmd["description"] = (
                    "The base command to execute (e.g. 'dir', 'type', 'findstr', 'where', 'python', "
                    "or a known app exe name like 'wemeetapp.exe'/'wechat.exe' to launch it)."
                )
            for key in ("interactive", "session_keys"):
                p = props.get(key)
                if p:
                    p["description"] = (
                        p.get("description", "")
                        + "（仅 Linux/macOS 支持；Windows 下 PTY 不可用，此参数无效）"
                    )
            persistent = props.get("persistent")
            if persistent:
                persistent["description"] = (
                    "持久会话（2026-08-27）：Windows 下复用长驻 cmd.exe 进程，跨调用保留 cd/环境变量。"
                )
            session_key = props.get("session_key")
            if session_key:
                session_key["description"] = (
                    "持久会话标识（仅 persistent=True 时使用），同一 key 共享同一 cmd.exe 进程。"
                )
        return schema

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
        # allow_app_launch：Windows 个人版默认开启（配置文件可关），放行 start/Start-Process 启动本地应用
        from scout.config.manager import ConfigManager

        _allow_launch = bool(getattr(ConfigManager().load(), "allow_app_launch", False))
        # ★ 2026-09-01：已知 Windows 应用裸名 → ShellExecuteW 直接启动（fire-and-forget）。
        # 解决"打开腾讯会议/微信报错"：LLM 提交裸名被白名单拦，个人版（allow_app_launch=True）
        # 直接帮你启动成功，无需 LLM 猜 powershell Start-Process / cmd start 姿势。
        # 实测 cmd start 在无控制台/打包 exe 下启动 GUI 不可靠，ShellExecuteW 稳定。
        if (
            IS_WINDOWS
            and _allow_launch
            and not args
            and command.strip()
            and not command.startswith("__")
            and command.strip().lower() in KNOWN_APP_PATHS
        ):
            _display = KNOWN_APP_PATHS[command.strip().lower()][0]
            _cands = _resolve_known_app_candidates(command.strip())
            if _cands:
                # ★ 2026-09-02：启动后健康检查 + 候选自动回退——逐候选 ShellExecuteW
                # 启动并验证真实状态（主窗口出现/错误对话框/新进程）；某份安装损坏
                # （如 D:\tencent_meeting\WeMeet 弹"找不到网络路径"）失败时自动关闭
                # 错误框、尝试下一个候选（如 D:\tengxunhuiyi\WeMeet），全部失败才返回。
                _errs: list[str] = []
                for _cand in _cands:
                    _ok, _detail = _launch_app_windows(_cand, app_display=_display)
                    if _ok:
                        return Observation(
                            tool_name=self.name,
                            success=True,
                            output=f"已启动 {_display}: {_cand}\n{_detail}",
                        )
                    _errs.append(f"✗ {_cand}: {_detail}")
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output=(
                        f"启动 {_display} 失败：已尝试 {len(_cands)} 个候选路径，"
                        f"可能均为损坏/不完整安装:\n" + "\n".join(_errs)
                    ),
                )
        # ★ 2026-09-01：跨平台命令透明翻译（不同系统用该系统命令工具）。
        # 混合白名单时代 ls/cat/grep 在 Windows cmd 下"过白名单但命令未找到"，
        # 现在参数兼容时自动翻译（ls→dir、cat→type、grep→findstr…），一次成功；
        # 参数带开关（ls -la / findstr /i）时给平台化提示，不执行报错。
        _mapped = _map_platform_command(command, args)
        if _mapped is None:
            return Observation(
                tool_name=self.name,
                success=False,
                output=_platform_hint(os.path.basename(command.strip().split(None, 1)[0])),
            )
        command, args = _mapped
        is_safe, error = _validate_command(command, args, allow_app_launch=_allow_launch)
        if not is_safe:
            if interactive and not command.strip() and session_keys:
                pass  # 走 interactive 分支处理按键注入
            else:
                # 复合命令自动拆分（2026-08-29）：命令含 && / ; 且被安全校验拦截时，
                # 尝试拆成单条序列逐条执行（每条仍走完整安全校验，不拆管道/重定向）。
                # 避免"整条命令被拦 → 反思"的无效循环。
                split_obs = await self._try_split_execute(command, args, timeout, cwd, allow_app_launch=_allow_launch)
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
            # ★ 2026-09-01：start 命令（启动本地程序）专用通道 ——
            # ① 需 DETACHED_PROCESS 分离（无控制台父进程下绑定隐藏控制台会立即退出）；
            # ② stdout 必须 DEVNULL：管道写端被启动的程序继承时,该程序会启动失败
            #    （实测矩阵：DETACHED+PIPE → 程序死；DETACHED+DEVNULL → 程序活）。
            #    start 是 fire-and-forget 语义,输出本就无意义。
            if IS_WINDOWS and _needs_detached(_proc_cmd):
                _p = await asyncio.create_subprocess_exec(
                    *_proc_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=work_dir,
                    creationflags=subprocess.DETACHED_PROCESS,
                )
                try:
                    await asyncio.wait_for(_p.wait(), timeout=timeout)
                except TimeoutError:
                    pass  # start 不应阻塞；超时也不杀（分离进程与 cmd 无关联）
                return Observation(
                    tool_name=self.name,
                    success=True,  # start 语义即"已发起启动"
                    output="",
                )
            _spawn_kwargs = no_window_kwargs()
            process = await asyncio.create_subprocess_exec(
                *_proc_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
                **_spawn_kwargs,
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

            # ★ 2026-09-01：Windows GUI 启动器的非零退出码不代表失败 ——
            #   explorer.exe 打开文件夹成功时固定返回 1（历史遗留行为），
            #   start 命令、.msc 管理单元等也常返回非零。此前被误判为
            #   失败，导致 agent 重复执行或向用户误报"打开失败"。
            _rc = process.returncode
            _ok = _rc == 0
            if IS_WINDOWS and _rc == 1:
                _base = os.path.basename(cmd_list[0].strip().lower()).strip('"')
                if _base in ("explorer", "explorer.exe", "start") or _base.endswith(".msc"):
                    _ok = True

            return Observation(
                tool_name=self.name,
                success=_ok,
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
    async def _try_split_execute(self, command: str, args: list[str] | None, timeout: int, cwd: str, allow_app_launch: bool = False) -> Observation | None:
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
            # ★ 2026-09-01：Windows 下 shlex.split 吃路径反斜杠，改用引号感知拆分
            tokens = _win_split_args(p) if IS_WINDOWS else shlex.split(p)
            if not tokens:
                return None
            ok, _ = _validate_command(tokens[0], tokens[1:] if len(tokens) > 1 else None, allow_app_launch=allow_app_launch)
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
            # ★ 2026-09-01：Windows 下 shlex.split 会吃掉路径反斜杠
            # （D:\Weixin\Weixin.exe → D:WeixinWeixin.exe），改用引号感知拆分
            if IS_WINDOWS:
                cmd_list = _win_split_args(command) if command else []
            else:
                cmd_list = shlex.split(command) if command else []
            if not cmd_list:
                return Observation(tool_name=self.name, success=False, output="命令为空")
            _proc_cmd = _build_proc_cmd(cmd_list)
            if IS_WINDOWS and _needs_detached(_proc_cmd):
                _p = await asyncio.create_subprocess_exec(
                    *_proc_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=work_dir,
                    creationflags=subprocess.DETACHED_PROCESS,
                )
                try:
                    await asyncio.wait_for(_p.wait(), timeout=timeout)
                except TimeoutError:
                    pass
                return Observation(tool_name=self.name, success=True, output="")
            _spawn_kwargs = no_window_kwargs()
            process = await asyncio.create_subprocess_exec(
                *_proc_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=work_dir,
                **_spawn_kwargs,
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
