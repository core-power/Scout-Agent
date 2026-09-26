r"""Shell 工具 — 安全增强的 Shell 命令执行.

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
- 路径遍历防护（相对 `..` 按风险分级放行，见 _check_path_traversal）+ 系统目录访问拦截（SYSTEM_DIRS / ALLOWED_PATH_PREFIXES）
- 参数注入检测（INJECTION_PATTERNS，同时覆盖 command 与 args）
- 解释器载荷深度检查（2026-08-31）：powershell/python/cmd 的 -Command/-c 参数是任意
  代码执行面，参数中出现"启动外部程序"载荷（Start-Process / subprocess / .exe 路径等）
  一律拦截，防止 agent 用解释器绕过白名单启动任意 exe。
- 跨平台命令平台化（2026-09-01）：白名单按系统过滤（Windows 剔除 POSIX 专用命令、
  Linux/macOS 剔除 Windows 专用命令），常用跨平台命令透明翻译（ls→dir、cat→type、
  grep→findstr、dir→ls、findstr→grep 等），参数不兼容时给出平台化提示——
  避免"过白名单但目标 shell 里命令未找到"的频繁操作报错。- 应用启动后健康检查（2026-09-02）：已知应用（wemeetapp/wechat/dingtalk 等）经
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
import sys
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


def _ps_q(s: str) -> str:
    """PowerShell 单引号字符串转义：内部单引号写成两个（避免路径带引号时被截断）。"""
    return (s or "").replace("'", "''")


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


# ── 系统 python 可用性探测（2026-09-08）──
# 背景：普通用户 Windows 机器大多没装 Python，或 `python` 指向 Microsoft Store
# 占位程序（静默无输出 exit 0）。python 族命令探测失败时，shell 自动改用本应用
# 自带解释器在进程内执行脚本（见 ShellTool._run_python_inprocess）。
_PY_FAMILY = {"python", "python3", "py", "python.exe", "python3.exe", "py.exe"}

# ★ 2026-09-14 路径遍历判定（段语义）：
#   匹配 `..` 作为独立路径段 —— 前后为路径分隔符或 token 边界。
#   "..."（省略号）、"a.../b"、"arr[1:3]" 等不含独立 .. 段 → 放行；
#   "../etc"、"a/../b"、"path/.." → 拦截。
_PATH_TRAVERSAL_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_py_ok_cache: bool | None = None

# ── 相对上级路径（..）的风险分级（2026-09-21 放宽）──────────────────────
# 旧规则「只要出现 .. 段就拒绝」误杀了大量完全正常的本地操作：
#   cd ..\上层目录   dir ..\兄弟目录   type ..\配置.ini   cat ../README.md
# 相对上级路径本身不构成越权（cwd 白名单与系统目录黑名单仍在），因此改为
# 只对「明显危险」的用法硬拦截，共四类：
#   A. 落点命中系统敏感目录（Windows 关键目录，任意深度）
#   B. 上跳 ≥2 级后首个落点是 Unix 系统目录（../../etc/passwd 这类越界读取）
#   C. 病态深逃逸（连续上跳 ≥ _MAX_TRAVERSAL_DEPTH 级，正常操作不会这么写）
#   D. 破坏性命令（del/rm/rmdir/move…）+ ..：最多上跳 1 级、禁通配符、禁裸 `..`
_TRAVERSAL_WIN_SENSITIVE = frozenset({
    "windows", "winnt", "system32", "syswow64", "systemroot",
    "programdata", "perflogs", "recovery", "$recycle.bin",
})
# 含空格的目录名无法靠"段相等"命中（引号+空格会被切成多个词），整串比对
_TRAVERSAL_WIN_SENSITIVE_PHRASES = (
    "program files", "program files (x86)",
    "system volume information", "documents and settings",
)
_TRAVERSAL_UNIX_SENSITIVE = frozenset({
    "etc", "usr", "bin", "sbin", "lib", "lib64",
    "boot", "proc", "sys", "dev", "root", "var",
})
# 破坏性命令取"翻译后"的基名（Windows 下 rm→del/rmdir、mv→move 已在此列）
_TRAVERSAL_DESTRUCTIVE_CMDS = frozenset({
    "rm", "rmdir", "rd", "del", "erase", "mv", "move",
    "dd", "shred", "format", "mkfs", "diskpart", "robocopy",
})
_MAX_TRAVERSAL_DEPTH = 4

# 遍历风险中"用户批准后可执行"的那一类：删除/移动上级内容（D 类，见 _check_path_traversal）。
# 其余（落点系统目录、病态深逃逸）属不可逆/不可审计，不进审批通道。
_APPROVABLE_TRAVERSAL_MARK = "删除/移动类命令"

# 校验失败信息前缀：带此前缀表示"高危但可审批"，执行器据此改为弹窗询问而非直接拒绝
_NEED_APPROVAL_PREFIX = "需要审批: "


def _traversal_path_words(token: str) -> list[list[str]]:
    """取出 token 中所有含 `..` 独立段的路径词，按分隔符切成段（已剥离引号）。

    只切空白，不切引号内的空格 —— 因此 `"..\\Program Files\\x"` 会被切成两个词，
    含空格的敏感目录名改由 _TRAVERSAL_WIN_SENSITIVE_PHRASES 整串兜底比对。
    """
    if not token or not _PATH_TRAVERSAL_RE.search(token):
        return []
    result: list[list[str]] = []
    for word in token.split():
        segs = [s.strip("\"'") for s in re.split(r"[\\/]+", word) if s.strip("\"'")]
        if ".." in segs:
            result.append(segs)
    return result


def _traversal_escape_depth(segs: list[str]) -> int:
    """连续上跳层级：`../../a` → 2；`a/../../b` → 0（先进入子目录再回来）。"""
    depth = 0
    for s in segs:
        if s == "..":
            depth += 1
        else:
            break
    return depth


def _check_path_traversal(token: str, base_cmd: str = "") -> str:
    """相对上级路径（..）的风险判定 —— 返回拦截原因，空串表示放行。"""
    paths = _traversal_path_words(token)
    if not paths:
        return ""
    low = token.lower()
    base = os.path.basename((base_cmd or "").strip().strip('"').lower())
    destructive = base in _TRAVERSAL_DESTRUCTIVE_CMDS
    for segs in paths:
        depth = _traversal_escape_depth(segs)
        tail = [s for s in segs if s != ".."]
        # A. 落点命中 Windows 系统关键目录（任意深度都拦）
        if any(s.lower() in _TRAVERSAL_WIN_SENSITIVE for s in tail):
            return "落点指向 Windows 系统目录"
        if any(p in low for p in _TRAVERSAL_WIN_SENSITIVE_PHRASES):
            return "落点指向 Windows 系统目录"
        # B. 上跳 ≥2 级后直接落到 Unix 系统目录（../../etc/passwd 形态）
        if depth >= 2 and tail and tail[0].lower() in _TRAVERSAL_UNIX_SENSITIVE:
            return "上跳多级后落点指向系统目录"
        # C. 病态深逃逸
        if depth >= _MAX_TRAVERSAL_DEPTH:
            return f"连续上跳 {depth} 级（上限 {_MAX_TRAVERSAL_DEPTH - 1} 级）"
        # D. 破坏性命令 + ..
        if destructive:
            if depth >= 2:
                return "删除/移动类命令配合 .. 最多只允许上跳 1 级"
            if not tail:
                return "删除/移动类命令的目标不能是上级目录本身（..）"
            if any(ch in t for t in tail for ch in "*?"):
                return "删除/移动类命令配合 .. 不允许使用通配符"
    return ""


def _is_python_cmd(cmd0: str) -> bool:
    return os.path.basename((cmd0 or "").strip().strip('"').lower()) in _PY_FAMILY


def _probe_system_python() -> bool:
    """探测系统是否有可用的真 python（结果进程内缓存）.

    探测标准：`python -c "print(1)"` / `py -c "print(1)"` 能在 20s 内
    返回 exit 0 且 stdout 含输出 —— 商店占位程序会静默返回空，未安装则抛异常。
    """
    global _py_ok_cache
    if _py_ok_cache is not None:
        return _py_ok_cache
    for cand in ("python", "py"):
        try:
            r = subprocess.run(
                [cand, "-c", "print(1)"],
                capture_output=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode == 0 and r.stdout.strip():
                _py_ok_cache = True
                return True
        except Exception:  # noqa: BLE001 — 未安装/超时/权限 → 试下一个
            continue
    _py_ok_cache = False
    return False


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
        if _meta.search(single):
            # 含元字符（| && > <）→ 必须交给 shell 解析以保留语义
            if IS_WINDOWS:
                return ["cmd.exe", "/d", "/s", "/c", single]
            return ["bash", "-c", single]
        if re.search(r'\s', single):
            # ★ 2026-09-15 修复「带引号的绝对路径报非法字符」：
            # 无元字符但**含引号**的整串命令绝不能走 cmd /d /s /c —— Python
            # subprocess 会用 list2cmdline 对该参数二次转义，内部引号变成 \"，
            # 而 cmd 不把 \" 当转义，于是 PowerShell 收到带反斜杠的路径，报
            # 「路径中具有非法字符」（实测 powershell -File "D:\.scout\outputs\x.ps1"
            # 必须去掉引号才能跑通）。改为引号感知拆分后直接 exec，绕开 cmd 解析。
            if '"' in single or "'" in single:
                _parts = _win_split_args(single) if IS_WINDOWS else shlex.split(single)
                if len(_parts) > 1 and not (
                    IS_WINDOWS and os.path.basename(_parts[0]).lower() in WIN_BUILTIN_CMDS
                ):
                    return _parts
            # 其余含空格整串（如 "where python"，或上一步拆不动的）保持原行为
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


def _spawn_spec(proc_cmd: list[str]) -> tuple[bool, object]:
    """把 _build_proc_cmd 的结果转成 spawn 规格：``(是否 shell 模式, 目标)``.

    ★ 2026-09-15 修复「含空格的引号路径报错」：
    _build_proc_cmd 在 cmd/bash 包装场景返回
    ``["cmd.exe", "/d", "/s", "/c", <命令串>]``（命令串常含引号，例如
    一条 ``dir "<含空格的目录>"`` 或 ``powershell -File "<带引号的脚本路径>"``）。
    若用 ``create_subprocess_exec`` 启动，Python 会按 list2cmdline 规则对每个
    参数**二次加引号并转义内部引号**（``"x"`` → ``\"x\"``）；而 cmd/bash 在
    ``/c`` 语义下并不把 ``\"`` 当转义，路径因此被破坏，表现为
    「路径中具有非法字符」或「指定的路径无效」（实测去掉引号才跑通）。

    这些参数本质上是**一条完整命令行**，必须用 shell 模式原样交给系统 shell：
    Windows → ``cmd.exe /c <串>``，POSIX → ``/bin/sh -c <串>``。
    """
    # 仅 Windows 的 cmd.exe 包装需要 shell 模式：POSIX 下 create_subprocess_exec
    # 直接 execve，参数原样传递，不存在二次转义问题（改走 /bin/sh 反而会丢失
    # bash 语义，如 pipefail、进程替换）。
    if (
        IS_WINDOWS
        and len(proc_cmd) >= 5
        and os.path.basename(proc_cmd[0]).lower() in ("cmd.exe", "cmd")
    ):
        return True, proc_cmd[-1]
    return False, proc_cmd


async def _spawn(proc_cmd: list[str], **kwargs: object) -> object:
    """按 _build_proc_cmd 的形态选择 exec / shell 模式启动子进程（见 _spawn_spec）."""
    use_shell, target = _spawn_spec(proc_cmd)
    if use_shell:
        return await asyncio.create_subprocess_shell(target, **kwargs)  # type: ignore[arg-type]
    return await asyncio.create_subprocess_exec(*target, **kwargs)  # type: ignore[arg-type]


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
    "tar",
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
    "tracert", "pathping", "getmac", "wmplayer", "mplayer2",
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
    # ── 2026-09-20 补充：无开关形态下的一一对应（此前这些命令在 Windows 直接被拦）──
    "ps": ("tasklist", ()),
    "env": ("set", ()),                # env → set（列出环境变量）
    "printenv": ("set", ()),
    "export": ("set", ()),             # export A=1 → set A=1
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


# ── 2026-09-19：带开关命令的等价改写 ──────────────────────────────────
# 背景：旧逻辑只要参数里出现 -/ 开头就返回 None → shell 工具直接失败，
# 模型必须**再发一整轮**（带着已经涨到几千甚至上万 token 的上下文）才能改对。
# `ls -la` / `grep -rn "x" .` / `rm -rf dir` 是模型最强的 Linux 肌肉记忆，
# 在 Windows 上每次都会先栽一次 —— 历史会话里 36 次「安全拦截」大多来自这里，
# 每一次都是一轮完整的 prompt 重发。
# 现在对形态确定的命令做等价改写并直接执行；认不出的形态仍返回 None 走提示。
_WIN_SWITCH_CMDS = {
    "ls", "grep", "rm", "cp", "mv", "mkdir", "head", "tail", "wc", "find",
    # ── 2026-09-20 新增：此前 Windows 下这些命令一律被白名单拦下，
    #    只能回提示让 LLM 重试一轮（每次都烧一轮 token + 步数）。
    #    形态确定时直接改写成 cmd / PowerShell 等价命令。
    "ps", "kill", "du", "df", "sort", "tee",
}

# ★ 2026-09-20：这批命令即使【不带开关】也要改写（不能走 alias 表）——
#   例如 `kill 1234` 若走 alias 会得到 `taskkill 1234`（缺 /PID 语法必然失败）。
_WIN_REWRITE_ANY = {"ps", "kill", "du", "df", "tee"}


def _split_flags(args: list[str]) -> tuple[set[str], list[str]]:
    """拆分短开关与操作数：['-la', 'x'] → ({'l','a'}, ['x'])。长开关(--x)归操作数。"""
    flagset: set[str] = set()
    rest: list[str] = []
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            flagset.update(a[1:])
        else:
            rest.append(a)
    return flagset, rest


def _win_rewrite_switches(base: str, args: list[str], stdin: bool = False) -> tuple[str, list[str]] | None:
    """Windows 侧：把带开关的 POSIX 命令改写为 cmd / PowerShell 等价命令。

    只在形态确定时改写，任何不确定都返回 None（回退到平台化提示，行为不变）。

    stdin=True 表示该命令位于管道右侧（数据来自上游而非文件）——仅此时允许
    生成 `$input | ...` 形态；独立命令缺文件名时仍返回 None（原行为不变）。
    """
    # 长选项（--color / --exclude ...）语义各异，一律不猜 —— 回退到平台化提示
    if any(a.startswith("--") for a in args):
        return None

    if base == "find":
        # 只认 find <dir> -name "<glob>"（-iname 同义）
        name = None
        for i, a in enumerate(args):
            if a in ("-name", "-iname") and i + 1 < len(args):
                name = args[i + 1]
        if name is None:
            return None
        root = args[0] if args and not args[0].startswith("-") else "."
        return "dir", ["/s", "/b", os.path.join(root, name)]

    fs, rest = _split_flags(args)

    if base == "ls":
        out: list[str] = []
        if "a" in fs or "A" in fs:
            out.append("/a")
        if "R" in fs:
            out.append("/s")
        out.extend(rest)
        return "dir", out

    if base == "grep":
        if not rest:
            return None
        pat, paths = rest[0], rest[1:]
        out = []
        if "r" in fs or "R" in fs:
            out.append("/s")
        if "i" in fs:
            out.append("/i")
        if "n" in fs:
            out.append("/n")
        if "v" in fs:
            out.append("/v")
        # /c: 强制按字面量匹配 —— findstr 默认会把空格/点号当正则，容易匹配错。
        # 注意不要自己塞引号：_win_quote 会把含引号的参数转义成 "" 导致 cmd 解析错乱，
        # 交给它在整参数层面按需加引号即可（/c:hello world → "/c:hello world"）。
        out.append("/c:" + pat)
        # 目录参数要补通配符，否则 findstr 把 "." 当文件规格、什么都搜不到。
        # ★ 管道右侧（数据来自上游）绝对不能补 `*` —— 否则 findstr 会丢掉 stdin
        #   转去搜当前目录，ls|grep x 这类写法直接失效。
        if not paths and stdin:
            return "findstr", out
        _targets = []
        for p in (paths or ["*"]):
            if not any(ch in p for ch in "*?"):
                try:
                    if os.path.isdir(p):
                        p = os.path.join(p, "*")
                except Exception:  # noqa: BLE001
                    pass
            _targets.append(p)
        out.extend(_targets)
        return "findstr", out

    if base == "rm":
        if not rest:
            return None
        if "r" in fs or "R" in fs:
            # rmdir 只能删目录、del 只能删文件 —— 目标类型不一致就别硬凑
            _is_dir = [os.path.isdir(p) for p in rest]
            if all(_is_dir):
                return "rmdir", ["/s", "/q"] + rest
            if not any(_is_dir):
                return "del", ["/f", "/q"] + rest
            return None
        return "del", (["/f"] if "f" in fs else []) + rest

    if base == "cp":
        if "r" in fs or "R" in fs:
            if len(rest) < 2:
                return None
            return "xcopy", ["/e", "/i", "/y", rest[0], rest[1]]
        return "copy", rest

    if base == "mv":
        return "move", rest

    if base == "mkdir":
        # -p 无意义：Windows 的 mkdir 本身就递归创建中间目录。
        # 但 cmd 的 mkdir 不接受正斜杠（报「命令语法不正确」），统一换成反斜杠。
        return "mkdir", [p.replace("/", "\\") for p in rest]

    if base in ("head", "tail"):
        # 用原始 args 扫描：-n 20 / -20 / -5 三种形态都要认，且不能把 20 当成文件名
        n = None
        target = None
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-n", "-c") and i + 1 < len(args):
                n = args[i + 1]
                i += 2
                continue
            if a.startswith("-") and len(a) > 1 and a[1:].isdigit():
                n = a[1:]
                i += 1
                continue
            if a.startswith("-") and len(a) > 1:
                i += 1          # 其余开关忽略
                continue
            if target is None:
                target = a
            i += 1
        if target is None:
            if not stdin:
                return None
            # 管道：cat a.txt | head -20 / tail -5 —— 数据来自上游
            if base == "head":
                ps = f"$input | Select-Object -First {n or '10'}"
            else:
                ps = f"$input | Select-Object -Last {n or '10'}"
            return "powershell", ["-NoProfile", "-Command", ps]
        flag = "-TotalCount" if base == "head" else "-Tail"
        ps = f"Get-Content -LiteralPath '{_ps_q(target)}' {flag} {n or '10'}"
        return "powershell", ["-NoProfile", "-Command", ps]

    if base == "wc":
        if "l" not in fs:
            return None
        if not rest:
            if not stdin:
                return None
            return "powershell", ["-NoProfile", "-Command", "$input | Measure-Object -Line"]
        ps = f"(Get-Content -LiteralPath '{_ps_q(rest[0])}').Count"
        return "powershell", ["-NoProfile", "-Command", ps]

    # ── 2026-09-20：进程/磁盘/文本类高频命令（此前 Windows 下全靠提示重试）──
    if base == "ps":
        # ps aux / ps -ef / ps auxww：形态虽多，Windows 侧一律用 tasklist
        # 就列结果而言等价；只要没冒出不认识的长选项（已在函数开头拦截）即可。
        return "tasklist", []

    if base == "kill":
        # 只认 PID 形态：kill [-9] <pid>... ；进程名/-s 信号等不猜，回退提示。
        pids = [a for a in rest if a.isdigit()]
        if not rest or len(pids) != len(rest):
            return None
        out: list[str] = []
        if "9" in fs or any(a.upper() == "-KILL" for a in args):
            out.append("/F")
        for p in pids:
            out += ["/PID", p]
        return "taskkill", out

    if base == "du":
        # du [-s] [-h] <dir> —— 汇总该目录占用（-h 只是显示单位，忽略即可）
        target = rest[-1] if rest else "."
        ps = (
            "$s=(Get-ChildItem -LiteralPath '%s' -Recurse -File -ErrorAction SilentlyContinue "
            "| Measure-Object -Property Length -Sum).Sum; if($null -eq $s){$s=0}; "
            "'{0:N1}M' -f ($s/1MB)" % _ps_q(target)
        )
        return "powershell", ["-NoProfile", "-Command", ps]

    if base == "df":
        ps = (
            "Get-PSDrive -PSProvider FileSystem | "
            "Select-Object Name,@{n='Used_GB';e={[math]::Round($_.Used/1GB,1)}},"
            "@{n='Free_GB';e={[math]::Round($_.Free/1GB,1)}}"
        )
        return "powershell", ["-NoProfile", "-Command", ps]

    if base == "tee":
        # 独立使用（无上游管道）的 tee 没有输入源，翻译了也是空操作 → 不猜
        if not rest or not stdin:
            return None
        return "powershell", ["-NoProfile", "-Command",
                              f"$input | Tee-Object -FilePath '{_ps_q(rest[0])}'"]

    if base == "sort":
        # 只认 sort [-u] [-r] [file]：cmd 的 sort.exe 没有去重能力，转 PowerShell
        if fs - {"u", "r"}:
            return None
        src = f"Get-Content -LiteralPath '{_ps_q(rest[0])}'" if rest else "$input"
        if not rest and not stdin:
            return None
        steps = "Sort-Object"
        if "r" in fs:
            steps += " -Descending"
        if "u" in fs:
            steps += " -Unique"
        return "powershell", ["-NoProfile", "-Command", f"{src} | {steps}"]

    return None


# ── 2026-09-20：Windows 复合命令（管道 / 重定向 / && / ;）分段翻译 ──────────
# 此前只要命令串含空格就原样透传 → cmd 里没有 ls/cat/grep，`ls | grep x` 必失败，
# LLM 每次都要重试一轮改写成 PowerShell（烧一轮 token + 步数，还常改错）。
# 现在按分隔符切段，逐段翻译后用 cmd 语法拼回；任一段认不出就整串放弃（保守）。
_COMPOUND_SEPS = {"||", "&&", ">>", "|", ">", "<", "&", ";"}


def _split_compound(cmd: str) -> list[str]:
    """按 shell 分隔符切分，分隔符本身作为独立片段保留；引号内的分隔符不切。"""
    segs: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        pair = cmd[i:i + 2]
        if pair in ("||", "&&", ">>", "2>", "1>"):
            if "".join(buf).strip():
                segs.append("".join(buf).strip())
            segs.append(pair)
            buf = []
            i += 2
            continue
        if ch in "|><&;":
            if "".join(buf).strip():
                segs.append("".join(buf).strip())
            segs.append(ch)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segs.append(tail)
    return segs


def _has_win_switch(args: list[str]) -> bool:
    return any(a.startswith("-") or a.startswith("/") for a in args)


def _join_win_cmd(base: str, args: list[str]) -> str:
    """拼回 cmd 命令串；PowerShell 的 -Command 必须整段引起来，不能被拆开。"""
    if base.lower() in _WIN_PS_EXES:
        return " ".join([base] + [_win_quote(a) for a in args])
    return " ".join([base] + [(_win_quote(a) if _needs_win_quote(a, args) else a) for a in args])


def _needs_win_quote(arg: str, args: list[str]) -> bool:
    """cmd 重写后：开关原样输出，仅对含空白/元字符的操作数加引号。"""
    if arg.startswith("/") or arg.startswith("-"):
        return False
    return bool(re.search(r'[\s"&|<>]', arg))


# ── 2026-09-20：PowerShell 表达式模式 ────────────────────────────────────────
# 复合命令里一旦出现 head/tail/wc/sort 这类需要 PowerShell 的段，整条再交给
# cmd.exe 拼接就会踩两层引号解析（cmd /d /s /c 会剥掉 PowerShell -Command 的双
# 引号，表现为命令被 PowerShell 当字符串原样回显 —— e2e 实测确认）。
# 这类命令改成整体走 `powershell -NoProfile -Command "<表达式>"`（不经 cmd），
# 既保住管道语义，又避开引号嵌套。
# 返回 (需要上游输入, 表达式)；不支持返回 None。
def _win_ps_expr(base: str, args: list[str], stdin: bool = False) -> tuple[bool, str] | None:
    def path_of(p: str) -> str:
        return f"Get-Content -LiteralPath '{_ps_q(p)}'"

    if base == "cat":
        if not args or stdin:
            return (True, "$_") if stdin else None
        return (False, path_of(args[0]))

    if base in ("head", "tail", "sort", "wc", "tee"):
        if base == "head":
            n = _take_count_arg(args) or "10"
        elif base == "tail":
            n = _take_count_arg(args) or "10"
        files = [a for a in args if not a.startswith("-") and not a.isdigit()]

        if base == "head":
            core = None if not files else f"{path_of(files[0])} -TotalCount {n}"
            return (False, core) if core else ((True, f"Select-Object -First {n}") if stdin else None)
        if base == "tail":
            core = None if not files else f"{path_of(files[0])} -Tail {n}"
            return (False, core) if core else ((True, f"Select-Object -Last {n}") if stdin else None)
        if base == "wc":
            if "l" not in _fs_of(args):
                return None
            if files:
                return (False, f"({path_of(files[0])}).Count")
            return (True, "Measure-Object -Line | ForEach-Object { $_.Lines }") if stdin else None
        if base == "sort":
            fs = _fs_of(args)
            if fs - {"u", "r"}:
                return None
            tail = "Sort-Object" + (" -Descending" if "r" in fs else "") + (" -Unique" if "u" in fs else "")
            if files:
                return (False, f"{path_of(files[0])} | {tail}")
            return (True, tail) if stdin else None
        if base == "tee":
            return (True, f"Tee-Object -FilePath '{_ps_q(args[0])}'") if args and stdin else None

    if base == "ls":
        fs = _fs_of(args)
        files = [a for a in args if not a.startswith("-")]
        opts = ""
        if "a" in fs or "A" in fs:
            opts += " -Force"
        if "R" in fs:
            opts += " -Recurse"
        # -l 的长格式 PS 没有等价物，忽略（信息不丢，只是列格式不同）
        tgt = (" -LiteralPath '%s'" % _ps_q(files[0])) if files else ""
        return (False, f"Get-ChildItem{tgt}{opts}")

    if base == "grep":
        rest = [a for a in args if not a.startswith("-")]
        fs = _fs_of(args)
        if not rest or "v" in fs or "r" in fs or "R" in fs:
            return None  # -v / -r 语义差异大，不猜
        pat, files = rest[0], rest[1:]
        core = "Select-String -SimpleMatch -Pattern '%s'" % _ps_q(pat)
        if files:
            return (False, f"{path_of(files[0])} | {core}")
        return (True, core) if stdin else None

    if base == "ps":
        return (False, "Get-Process")

    if base == "du":
        target = args[-1] if args else "."
        return (False, "('{0:N1}M' -f ((Get-ChildItem -LiteralPath '%s' -Recurse -File "
                       "-ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum/1MB))"
                       % _ps_q(target))

    if base == "df":
        return (False, "Get-PSDrive -PSProvider FileSystem | "
                       "Select-Object Name,@{n='Used_GB';e={[math]::Round($_.Used/1GB,1)}},"
                       "@{n='Free_GB';e={[math]::Round($_.Free/1GB,1)}}")

    if base == "kill":
        pids = [a for a in args if a.isdigit()]
        if not args or len(pids) != len(args):
            return None
        force = " -Force" if "9" in _fs_of(args) or "-KILL" in [a.upper() for a in args] else ""
        return (False, "Stop-Process -Id %s%s" % (",".join(pids), force))

    if base == "env":
        return (False, "Get-ChildItem Env:")

    # 其余命令（cmd 内建/exe）在 PowerShell 里通常也能直呼其名（type/dir 为别名，
    # findstr/tasklist 为外部程序）；保守起见只对白名单内的放行
    if base in ("type", "dir", "findstr", "tasklist", "where", "hostname", "find"):
        return (False, " ".join([base] + args))

    return None


def _take_count_arg(args: list[str]) -> str | None:
    """取 head/tail 的 -n 20 / -20 / -5 形态。"""
    for i, a in enumerate(args):
        if a in ("-n", "-c") and i + 1 < len(args) and args[i + 1].isdigit():
            return args[i + 1]
        if a.startswith("-") and len(a) > 1 and a[1:].isdigit():
            return a[1:]
    return None


def _fs_of(args: list[str]) -> set[str]:
    fs, _ = _split_flags(args)
    return fs


def _win_compound_ps(segs: list[str]) -> tuple[str, list[str]] | None:
    """把复合命令整体翻译为单条 PowerShell -Command 表达式。

    返回 ("powershell", ["-NoProfile", "-Command", expr]) 或 None（认不出）。
    """
    pieces: list[str] = []
    expect: str | None = None       # "pipe" / "out" / "append"
    redir: tuple[str, str] | None = None
    for seg in segs:
        if seg in ("||", "&&", "&", ";"):
            return None             # 控制流语义不同，不猜
        if seg == "|":
            expect = "pipe"
            continue
        if seg in (">", ">>"):
            expect = "out" if seg == ">" else "append"
            continue
        if expect in ("out", "append"):
            tgt = _win_split_args(seg)
            if not tgt:
                return None
            redir = (tgt[0], "append" if expect == "append" else "out")
            expect = None
            continue
        parts = _win_split_args(seg)
        if not parts:
            continue
        if pieces and expect is None:
            return None             # 两个命令段相邻却无分隔符：不猜
        base = os.path.basename(parts[0]).lower()
        got = _win_ps_expr(base, parts[1:], stdin=bool(pieces))
        if got is None:
            return None
        _needs_input, piece = got
        pieces.append(piece)
        expect = None

    if not pieces:
        return None
    expr = " | ".join(pieces)
    if redir:
        target, mode = redir
        expr += " | Out-File -Encoding utf8 '%s'%s" % (_ps_q(target), " -Append" if mode == "append" else "")
    return "powershell", ["-NoProfile", "-Command", expr]


def _win_translate_compound(full: str) -> str | None:
    """把整条 Windows 复合命令翻译成可执行写法。

    - 全部是 cmd 系命令 → 拼成 `dir /a | findstr /c:x`（仍走 cmd /c）；
    - 任一段需要 PowerShell（head/tail/wc/sort/tee…）→ 整体走 powershell -Command，
      避免 cmd /d /s /c 剥掉引号导致命令被当字符串回显。
    认不出 / 无需翻译 → None。
    """
    segs = _split_compound(full)
    if not segs:
        return None

    # 先判断是否至少需要翻译（第一段本身就是 Unix 命令？后续段呢？）
    out: list[str] = []
    changed = False
    ps_used = False
    for idx, seg in enumerate(segs):
        if seg in _COMPOUND_SEPS:
            out.append(seg)
            continue
        parts = _win_split_args(seg)
        if not parts:
            continue
        base = os.path.basename(parts[0]).lower()
        arg_list = parts[1:]
        # 该段是否位于管道某段的右侧（数据来自上游而非文件）
        has_upstream = any(s in ("|", "||") for s in segs[:idx])

        rw = None
        if base in _WIN_SWITCH_CMDS or base in _WIN_REWRITE_ANY:
            try:
                rw = _win_rewrite_switches(base, arg_list, stdin=has_upstream)
            except Exception:  # noqa: BLE001 — 改写失败就当不认识
                rw = None
        if rw:
            new_base, new_args = rw
        elif base in _WIN_ALIASES and not _has_win_switch(arg_list):
            nb, extra = _WIN_ALIASES[base]
            new_base, new_args = nb, list(extra) + arg_list
        else:
            # Windows 原生命令 / 未知命令：原样保留（后者交给白名单提示）
            new_base, new_args = None, None
        if new_base is None:
            out.append(seg)
            continue
        if new_base != base or new_args != arg_list:
            changed = True
        if new_base.lower() in _WIN_PS_EXES:
            ps_used = True
        out.append(_join_win_cmd(new_base, new_args))

    result = " ".join(out)
    # 分隔符与相邻片段之间不留空格（dir | findstr x 而非 dir  |  findstr）
    result = re.sub(r"\s*(\|\||&&|>>|[|><&;])\s*", r" \1 ", result)
    result = re.sub(r"\s{2,}", " ", result).strip()
    if not changed or not result:
        return None

    # 若某段翻译出了 PowerShell（-Command 里的引号会被 cmd /d /s /c 剥掉，
    # 表现为命令被原样回显），整条升级为单个 PowerShell 调用。
    if ps_used:
        ps = _win_compound_ps(segs)
        if ps:
            base_ps, args_ps = ps
            return _join_win_cmd(base_ps, args_ps)
        return None
    return result


def _map_platform_command(command: str, args: list[str] | None) -> tuple[str, list[str] | None] | None:
    """把另一平台风格的命令翻译为当前平台等价命令（透明，LLM 无感知）.

    返回:
      - (new_command, new_args): 翻译成功或无需翻译，直接使用；
      - None: 命中跨平台命令但参数不兼容（带 - / / 开关且无法改写），
        调用方应给平台化提示。
    """
    if not command or not command.strip():
        return command, args
    full = " ".join([command] + list(args or [])).strip()
    parts = command.strip().split(None, 1)
    if len(parts) > 1:
        # ★ 2026-09-20：整串命令（含管道/重定向）在 Windows 下先做分段翻译，
        #   翻译成功就整串返回（args=None，由 _build_proc_cmd 走 cmd /c 或 persistent 会话）。
        if IS_WINDOWS and any(t in _COMPOUND_SEPS for t in _split_compound(full)):
            translated = _win_translate_compound(full)
            if translated:
                return translated, None
        # 非 Windows / 未命中：保留 shell 语义，不翻译
        return command, args
    base = os.path.basename(parts[0]).lower()
    arg_list = list(args or [])
    has_switch = any(a.startswith("-") or a.startswith("/") for a in arg_list)

    # args 里夹着分隔符的形态（command="ls", args=["|","grep","txt"]）同样走复合翻译
    if IS_WINDOWS and any(a in _COMPOUND_SEPS for a in arg_list):
        translated = _win_translate_compound(full)
        if translated:
            return translated, None

    # ★ 2026-09-19：带开关时先尝试等价改写，命中就直接执行（省掉一轮重试）
    # ★ 2026-09-20：ps/kill/du/df/tee 即使不带开关也要改写（走 alias 会生成错误语法）
    if IS_WINDOWS and (has_switch or base in _WIN_REWRITE_ANY) and base in _WIN_SWITCH_CMDS:
        rw = _win_rewrite_switches(base, arg_list)
        if rw:
            return rw

    table = _WIN_ALIASES if IS_WINDOWS else _UNIX_ALIASES
    entry = table.get(base)
    if entry is None:
        return command, args
    new_base, extra = entry
    if has_switch:
        return None
    return new_base, list(extra) + arg_list


# ── 2026-09-20：Windows 侧「逐命令精确配方」──────────────────────────────
# 这些命令语义与 Windows 差异大（或有权限/副作用），不做自动改写，
# 但给出可直接照抄的 PowerShell 写法，避免 LLM 自己猜一轮再失败一轮。
_WIN_PS_RECIPES: dict[str, str] = {
    "kill": "结束进程用 taskkill /PID 进程号 /F（强制）；按进程名用 taskkill /IM notepad.exe /F",
    "sed": "就地替换请用 PowerShell："
           "(Get-Content 文件 -Raw) -replace '旧','新' | Set-Content 文件 -NoNewline；"
           "仅打印替换结果用 (Get-Content 文件) -replace '旧','新'",
    "awk": "取列请用 PowerShell：Get-Content 文件 | ForEach-Object { ($_ -split '\\s+')[0] }"
           "（[0] 换成所需列序号）",
    "chmod": "Windows 无 chmod；设置权限用 icacls 文件 /grant 用户名:F（撤销用 /remove）",
    "chown": "Windows 无 chown；修改所有者用 icacls 文件 /setowner 用户名",
    "ln": "创建链接用 mklink 链接名 目标（文件符号链接）、mklink /D 链接名 目标（目录符号链接，"
          "通常需管理员权限）；无需管理员时用 mklink /J 联接名 目标（目录联接）",
    "xargs": "遍历输入请用 PowerShell：Get-Content 列表文件 | ForEach-Object { 命令 $_ }",
    "uniq": "去重请用 PowerShell：Get-Content 文件 | Select-Object -Unique"
            "（等价于 sort -u；只去相邻重复请改用 Sort-Object -Unique 后再处理）",
    "nohup": "后台运行请用 Start-Process -FilePath 程序 -ArgumentList '参数' -WindowStyle Hidden",
    "systemctl": "Windows 服务用 sc query 服务名 查询、net start/stop 服务名 启停",
    "tail": "tail -f 请用 Get-Content 文件 -Wait -Tail 20",
    "watch": "周期性执行请用 PowerShell：while ($true) { Clear-Host; 命令; Start-Sleep -Seconds 2 }",
    "grep": "递归搜索请用 findstr /s /n /c:\"关键词\" 目录\\* 或 "
            "PowerShell：Get-ChildItem -Recurse | Select-String \"关键词\"",
    "jq": "处理 JSON 请用 PowerShell：Get-Content 文件 | ConvertFrom-Json",
    "curl": "Windows 自带 curl.exe 可直接用；复杂请求推荐 PowerShell："
            "Invoke-WebRequest -Uri URL -Method POST -Body $body",
}


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
        # ★ 2026-09-20：命令级精确配方（此前一律回「请用 PowerShell 对应命令」，
        #   LLM 还得自己想一遍怎么写，常常写错 → 再失败一轮）
        recipe = _WIN_PS_RECIPES.get(base)
        if recipe:
            return (
                f"安全拦截: '{base_cmd}' 是 Linux/macOS 命令，当前 Windows 环境没有该命令。\n"
                f"💡 {recipe}"
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

# ── Windows 路径 vs 转义序列的甄别（2026-09-21）────────────────────────
# \xNN / \NNN 是 bash/printf 的转义语义，用来拦 POSIX shell 的编码绕过载荷；
# 但 Windows 路径分隔符同样是反斜杠，于是这些完全正常的本地操作被判为"注入攻击"：
#   C:\Windows\Temp\1.tmp        \1   → 命中 \NNN
#   D:\归档\2024\报告.txt          \202 → 命中 \NNN
#   C:\Program Files\7-Zip       \7   → 命中 \NNN
#   D:\test\x64\config.ini       \x64 → 命中 \xNN
# cmd.exe 与 PowerShell 本就不解析 \xNN / \NNN（那是 POSIX 的语法），因此路径形态
# 的 token 在扫描前先把分隔符归一化为 '/'，纯转义串（无任何真实路径文字）不豁免。
_WIN_PATH_LIKE = re.compile(
    r"^(?:[A-Za-z]:[\\/]"          # C:\ 或 C:/
    r"|\\\\[^\\]+\\[^\\]+"         # \\server\share（UNC）
    r"|[\\/][^\\/]+[\\/])"         # \dir\ 或 /dir/
)
_WIN_ESCAPE_SEQ_RE = re.compile(r"\\x[0-9a-fA-F]{2}|\\[0-7]{1,3}")


def _scan_injection_token(token: str) -> str:
    """返回用于注入扫描的 token 副本；Windows 路径语义下反斜杠归一化为 '/'。"""
    if not IS_WINDOWS or "\\" not in token:
        return token
    if not _WIN_PATH_LIKE.search(token):
        return token
    # 纯转义串（如 \x6b\x69\x6c\x6c、\151\144）去掉转义后不剩真实路径文字 → 不豁免
    residue = _WIN_ESCAPE_SEQ_RE.sub("", token).replace("\\", "/").strip(" /")
    if len(residue) < 2:
        return token
    return token.replace("\\", "/")


# ── Windows cmd 内置命令的"隐形失败"（2026-09-21）──────────────────────
# cmd.exe 的部分内置命令失败时【不设置 ERRORLEVEL】，实测：
#   del 目标不存在         → 输出「找不到 <路径>」，退出码 0
#   del / type 目标被占用  → 输出「另一个程序正在使用此文件」，退出码 0
#   rmdir 不存在           → 输出「系统找不到指定的文件」，退出码 0
# 于是工具回报 success=True，LLM 以为操作已完成并据此推进后续步骤 ——
# 这比"报错"危险得多：错误前提会一路传播且难以回溯（例如以为旧文件已删，
# 后面却一直读到它）。与 2026-09-01 对 explorer 退出码=1 的补偿同源。
_WIN_NO_ERRORLEVEL_CMDS = frozenset({
    "del", "erase", "type", "copy", "xcopy", "move", "ren", "rename",
    "rmdir", "rd", "mkdir", "md",
})
_WIN_CMD_ERROR_TEXT = (
    "另一个程序正在使用此文件",
    "另一个程序已锁定文件的一部分",
    "系统找不到指定的文件",
    "系统找不到指定的路径",
    "拒绝访问",
    "找不到网络路径",
    "命令语法不正确",
    "文件名、目录名或卷标语法不正确",
    "不是内部或外部命令",
    "无效驱动器规格",
    "Access is denied",
    "The system cannot find",
    "The process cannot access",
    "being used by another process",
)
# del/rmdir 删除不存在目标时的专属文案，整行以它开头
_WIN_CMD_ERROR_PREFIX = ("找不到 ", "无法找到 ")


def _win_cmd_real_status(cmd_list: list[str], output: str, code: int) -> bool | None:
    """cmd 内置命令隐形失败检测：退出码 0 但输出通篇是错误文案 → 判失败。

    Returns:
        False —— 已判定为失败；None —— 无法判定，交给退出码决定。
    """
    if not IS_WINDOWS or code != 0 or not cmd_list:
        return None
    base = os.path.basename(cmd_list[0].strip().strip('"').lower())
    if base not in _WIN_NO_ERRORLEVEL_CMDS:
        return None
    lines: list[str] = []
    for raw in (output or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        # cmd 的 del/rmdir 报错时会先回显一行目标路径（如 C:\a\b.txt），
        # 它不是错误文本，需剔除后再判断，否则两行结构会被误认为真实输出。
        if re.fullmatch(r"[A-Za-z]:[\\/].*", ln) or re.fullmatch(r"\\\\[^\s]+", ln):
            continue
        lines.append(ln)
    if not lines or len(lines) > 3:
        return None  # 多行输出通常是真实数据，不做猜测
    for ln in lines:
        hit = any(e in ln for e in _WIN_CMD_ERROR_TEXT) or ln.startswith(_WIN_CMD_ERROR_PREFIX)
        if not hit:
            return None  # 存在一行不像错误信息 → 认定为真实输出
    return False

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

_KNOWN_APP_CANDIDATE_CACHE: dict[str, tuple[float, list[str]]] = {}
_CANDIDATE_CACHE_TTL = 600.0  # 2026-09-09：缓存 10min 过期——用户修复/重装应用后
#                              mtime 变化能被重新感知（此前进程内永不过期）

# 通用文件名（无品牌辨识度）：全盘兜底极易误命中任意目录里的同名 exe
# （et.exe/wpp.exe 是 WPS 组件但名字无辨识度，notepad/calc 系统自带不需要兜底）
_GENERIC_EXE_NAMES = frozenset({
    "et.exe", "wpp.exe", "wps.exe", "notepad.exe", "calc.exe",
    "write.exe", "cmd.exe", "powershell.exe",
})


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
        _ts, _paths = _KNOWN_APP_CANDIDATE_CACHE[key]
        if time.monotonic() - _ts < _CANDIDATE_CACHE_TTL:
            return list(_paths)
        _KNOWN_APP_CANDIDATE_CACHE.pop(key, None)  # 过期 → 重新解析

    result: list[str] = []
    entry = KNOWN_APP_PATHS.get(key)
    if entry:
        _display, templates = entry
        for d in _expand_app_dirs(templates):
            cand = os.path.join(d, exe_name)
            if os.path.isfile(cand):
                result.append(cand)
        # ★ 2026-09-08：候选按 exe 修改时间【新→旧】排序 ——
        # 损坏/陈旧安装（真实案例：D:\tencent_meeting\WeMeet）文件 mtime 旧，
        # 新安装（修复重装后）排前，减少"先打坏安装再回退"的等待与弹窗。
        try:
            result.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except OSError:
            pass  # 个别路径 stat 失败 → 保持模板序
    if not result:
        # 2026-09-09：通用名跳过全盘兜底（et.exe 等会误命中任意目录的同名 exe）
        if key not in _GENERIC_EXE_NAMES:
            found = _search_common_roots(exe_name)
            if found:
                result.append(found)
    _KNOWN_APP_CANDIDATE_CACHE[key] = (time.monotonic(), list(result))
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
    ★ 2026-09-08：Post 后验证关闭（自绘错误框可能不响应一次 WM_CLOSE），
    未关掉补发一次；仍不关也不再阻塞（残留交给下次快照的句柄差集兜住）。
    """
    if not hwnd:
        return
    import ctypes
    u32 = ctypes.windll.user32
    for _ in range(2):
        try:
            u32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.3)
        try:
            if not u32.IsWindow(ctypes.c_void_p(hwnd)):
                return
        except Exception:  # noqa: BLE001
            return


def _win_is_small_window(hwnd: int) -> bool:
    """对话框尺寸判定（<600x400）：限定子文本补读范围，避免大窗口全树扫描."""
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    try:
        if ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return (rect.right - rect.left) < 600 and (rect.bottom - rect.top) < 400
    except Exception:  # noqa: BLE001
        pass
    return False


# 错误对话框关键词（标题或静态文本命中即视为启动异常）
_ERR_DIALOG_KEYWORDS = (
    "找不到", "网络路径", "错误", "失败", "无法", "不能", "拒绝", "未响应",
    "已停止", "异常", "error", "failed", "not found", "cannot", "unavailable",
    "拒绝访问",
)


def _launch_app_windows(path: str, app_display: str = "", wait: float = 12.0) -> tuple[bool, str]:
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

    ★ 2026-09-08 漏检修复（用户实测"找不到网络路径"弹窗仍残留）：
      - 新窗口按【句柄】差集判定（原标题差集会把"上一候选未关掉的同标题错误框"
        误判为旧窗口而漏检），且不再过滤空标题窗口（空标题 #32770 也是错误框）；
      - 非 #32770 的自绘错误框（Chromium/Qt 应用）——对话框尺寸的小窗口补读
        子 Static 文本，防"网络路径"弹窗因类名不是 #32770 而漏检；
      - 轮询 8s→12s（不可达 UNC 路径的解析重试常超过 8s 才弹窗）；
      - "已在运行"判定由标题子串收紧为标题前缀（防"微信群运营方案.docx - Word"
        误判 wechat 已在运行 → 假成功且无任何动作）；
      - 新进程全部退出的场景由"降级成功"改判失败（launcher 短暂存活即弹错
        退出是损坏安装的典型形态，此前被 got_new_proc 掩盖）。

    Returns:
        (ok, detail) — ok=True 应用已运行；detail 为人类可读诊断/错误文本。
    """
    if not IS_WINDOWS:
        return True, ""
    import ctypes

    before_wins = _win_enum_windows()
    before_hwnds = {h for h, _, _ in before_wins}
    before_procs = _win_enum_procs()
    # 应用已在运行（主窗口已存在）：ShellExecuteW 只会前置激活，直接判健康
    if app_display and any(t.startswith(app_display) for _, t, _ in before_wins):
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
    new_procs_cum: set[str] = set()
    while time.monotonic() < deadline:
        time.sleep(0.5)
        wins = _win_enum_windows()
        new_wins = [(h, t, c) for h, t, c in wins if h not in before_hwnds]
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
            else:
                # 自绘错误框（Chromium/Qt）：小窗口补读子文本再判关键词
                text = t
                if _win_is_small_window(h):
                    body = _win_dialog_text(h)
                    if body:
                        text = (t + " " + body).strip()
                if any(k.lower() in text.lower() for k in _ERR_DIALOG_KEYWORDS):
                    _win_close_window(h)
                    err_hint = err_hint or f"检测到错误窗口: {text[:200]!r}"
        # 3) 新进程出现（launcher 拉起子进程；累计集合供结束时存活判定）
        new_procs_cum |= _win_enum_procs() - before_procs

    if err_hint:
        return False, err_hint
    if new_procs_cum:
        # launcher 曾启动但已全部退出（弹错自退的典型形态）→ 失败而非降级成功
        if not (new_procs_cum & _win_enum_procs()):
            return False, "进程曾启动但已全部退出（疑似损坏安装或路径指向无效目标）"
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


def _validate_command(
    command: str,
    args: list[str] | None = None,
    allow_app_launch: bool = False,
    approved: bool = False,
) -> tuple[bool, str]:
    """三重安全校验：白名单 + 黑名单 + 注入检测.

    allow_app_launch=True（Windows 个人版，配置文件开关）：放行"启动本地应用"载荷
    （start xxx.exe / Start-Process / 引号内 .exe 等），仍保留 -EncodedCommand 编码命令拦截。

    approved=True（2026-09-21 权限开关）：用户已在弹窗中批准，或在输入框把权限切到
    "全部放行"。此时**高危但可撤销/用户可判断后果**的操作（`rm -rf build`、
    `del ..\旧目录`…）予以放行；不可逆与结构性拦截（rm -rf /、mkfs、注入载荷、
    系统目录落点、非白名单命令）**不受 approved 影响**，仍然拦截。

    Returns:
        (is_safe, error_message)
    """
    if not command or not command.strip():
        return False, "命令为空"

    # 0. 危险命令分级拦截（2026-09-21 由"一律硬拦"改为两级）
    #   NEVER：不可逆/不可审计 → 永远拦（连"全部放行"也不放行）
    #   RISKY：高危但用户可判断 → 未批准时拦（引导审批），已批准时放行
    from scout.security.policy import RISK_NEVER, classify_command_risk
    full_cmd0 = command + " " + " ".join(args or [])
    _level, _desc = classify_command_risk(full_cmd0)
    if _level == RISK_NEVER:
        return False, (
            f"\u26d4 危险操作（{_desc}）已被安全保护拦截，我不会替你在后台执行。\n"
            f"这类操作后果不可逆，即使把权限切到「全部放行」也不会执行。\n"
            f"如果你确实需要执行，请自己在终端中手动运行：\n"
            f"    {full_cmd0}"
        )
    if _level != "normal" and not approved:
        return False, (
            f"{_NEED_APPROVAL_PREFIX}高危操作（{_desc}）需要你确认后才会执行。\n"
            f"    {full_cmd0}\n"
            f"（也可在输入框把权限切到「全部放行」，之后同类操作不再询问）"
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
        # ★ 2026-09-21：Windows 路径先做反斜杠归一化，避免 C:\tmp\1.tmp 被当成转义攻击
        _scan = _scan_injection_token(token)
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, _scan):
                return False, "安全拦截: 参数包含可疑的注入模式"

        # 检查路径遍历（★ 2026-09-14 段语义修复误杀 → ★ 2026-09-21 放宽）
        # 段语义：`..` 仅作为独立路径段才算遍历（".../next"、说明文字不误伤）。
        # 放宽：普通 .. 一律放行（cd ..\上层 / dir ..\兄弟 / cat ../README.md 等日常操作），
        # 只拦落点命中系统目录、病态深逃逸、破坏性命令越级删除这三类明显危险用法。
        _trav_risk = _check_path_traversal(token, base_cmd)
        if _trav_risk:
            if approved and _trav_risk.startswith(_APPROVABLE_TRAVERSAL_MARK):
                # 已批准：放行"删除/移动上级内容"这类用户可判断后果的操作
                continue
            return False, (
                f"{_NEED_APPROVAL_PREFIX if _trav_risk.startswith(_APPROVABLE_TRAVERSAL_MARK) else '安全拦截: '}"
                f"参数包含路径遍历 (..) —— {_trav_risk}。\n"
                "需要访问上级目录时请用绝对路径（如 D:\\project\\x 或 /home/u/x）；"
                "确需删除/移动上级目录内容的可在弹窗中批准，或把权限切到「全部放行」。"
            )

        # 检查绝对路径中的敏感目录
        if token.startswith("/"):
            for sensitive in SYSTEM_DIRS:
                if token == sensitive or token.startswith(sensitive + "/"):
                    return False, f"安全拦截: 不允许访问系统目录 {sensitive}"

    return True, ""


def classify_shell_risk(
    command: str, args: list[str] | None = None, allow_app_launch: bool = False
) -> tuple[str, str]:
    """shell 命令的风险分级（供执行器决定"直接跑 / 弹窗问 / 硬拦截"）.

    Returns:
        ("never", 原因)：不可逆或结构性违规 —— 任何权限模式都不执行；
        ("risky", 原因)：高危但用户可判断 —— 默认弹窗，权限全开时直接执行；
        ("normal", "")：常规操作。
    """
    from scout.security.policy import RISK_NEVER, RISK_NORMAL, RISK_RISKY, classify_command_risk

    # 与执行时一致：先做跨平台命令翻译（ls→dir、rm→del…），否则未翻译的形态
    # 会因"不在白名单"被误判为 never。
    _raw = command + " " + " ".join(args or [])
    # 先按**原始写法**判不可逆：翻译会改写命令形态（Windows 下 `rm -rf /`
    # → `rmdir /s /q /`），只看译文会漏掉 rm -rf / 这类根目录删除。
    _lvl, _desc = classify_command_risk(_raw)
    if _lvl == RISK_NEVER:
        return RISK_NEVER, _desc

    _mapped = _map_platform_command(command, args)
    if _mapped is not None:
        command, args = _mapped
    else:
        # 无法翻译（不支持的开关等）：执行层会给出平台化提示，此处只保留
        # 不可逆命令的硬拦截，避免"未翻译 → 不在白名单"被当成结构性违规。
        return RISK_NORMAL, ""

    ok, msg = _validate_command(command, args, allow_app_launch=allow_app_launch, approved=False)
    if ok:
        return RISK_NORMAL, ""
    if msg.startswith(_NEED_APPROVAL_PREFIX):
        return RISK_RISKY, msg[len(_NEED_APPROVAL_PREFIX):].strip()
    return RISK_NEVER, msg


class ShellTool(ToolDefinition):
    """安全 Shell 命令执行."""

    name = "shell"
    description = (
        "Execute a shell command in a restricted environment. "
        "Only whitelisted system utilities are allowed. "
        "Dangerous operations (recursive delete, disk format, piping to shell) are blocked. "
        "Prefer args parameter for arguments. System directories (/etc, /usr, /bin) are blocked.\n"
        "IMPORTANT: this tool is for COMMAND-LINE tasks only (files, processes, git, pip...). "
        "For GUI apps (WeChat/Weixin, QQ, Feishu or any window: launching, clicking, typing, "
        "reading screens) ALWAYS use the `desktop` tool instead — do NOT drive GUIs via "
        "PowerShell/Add-Type/SendKeys here.\n"
        "Windows guidance: "
        "(1) Launch GUI apps with the desktop tool's launch action (target can be a bare name "
        "like 'Weixin'/'notepad'); use shell only to launch CLI tools. "
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
                "description": "PTY 交互式终端：以伪终端运行命令，支持 vim/top/less 等交互式程序。"
                "会话保留，可继续用 session_keys 注入按键。"
                + (
                    "注意：Windows 走 ConPTY，中断会重启会话（丢失 cwd/环境变量）。"
                    if IS_WINDOWS
                    else "超时后发送 Ctrl-C 而非杀进程，会话保留。"
                ),
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

    async def _run_python_inprocess(
        self, cmd_list: list[str], timeout: int, work_dir: str
    ) -> Observation | None:
        """用本应用自带解释器在进程内执行 python 命令（系统无 python 时的兜底）.

        支持 `python script.py [args...]` 与 `python -c "code"`；
        其余形式（-m pip 等）返回 None 落回原进程路径拿真实报错。
        安全级别与 shell 工具等价（shell 本就可执行任意命令）；
        线程内执行 + SystemExit 捕获 + stdout/stderr 重定向 + cwd/argv 还原。
        """
        import contextlib
        import io as _io
        try:
            import runpy
        except ImportError:  # PyInstaller 静态分析可能漏收函数内 import → 降级直接 exec 源码
            runpy = None

        script: str | None = None
        script_idx = -1
        code: str | None = None
        for i, a in enumerate(cmd_list):
            if i == 0:
                continue
            if a in ("-c", "/c") and i + 1 < len(cmd_list):
                code = cmd_list[i + 1]
                break
            if a.startswith("-"):
                continue
            if a.lower().endswith(".py"):
                script = a
                script_idx = i
                break
            break  # 第一个非 flag 参数不是 .py（如 -m 的模块名）→ 不接

        if code is None and script is None:
            return None

        prefix = (
            "[内置解释器] 系统 python 不可用（未安装或为商店占位程序），"
            f"已改用本应用自带解释器执行（Python {sys.version.split()[0]}，含打包依赖）。\n"
        )

        if code is None:
            script_path = script if os.path.isabs(script) else os.path.abspath(
                os.path.join(work_dir if os.path.isdir(work_dir) else ".", script)
            )
            if not os.path.isfile(script_path):
                return Observation(
                    tool_name=self.name, success=False,
                    output=prefix + f"脚本不存在: {script_path}",
                )
            argv_tail = cmd_list[script_idx + 1:]
        else:
            script_path = "<python -c>"
            argv_tail = []

        def _run() -> tuple[str, int]:
            out, err = _io.StringIO(), _io.StringIO()
            old_argv, old_cwd = sys.argv, os.getcwd()
            rc = 0
            try:
                if os.path.isdir(work_dir):
                    os.chdir(work_dir)
                sys.argv = ([script_path] if script else ["-c"]) + argv_tail
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    if code is not None:
                        exec(compile(code, "<python -c>", "exec"), {"__name__": "__main__"})
                    elif runpy is not None:
                        runpy.run_path(script_path, run_name="__main__")
                    else:
                        with open(script_path, encoding="utf-8") as _f:
                            _src = _f.read()
                        exec(
                            compile(_src, script_path, "exec"),
                            {"__name__": "__main__", "__file__": script_path},
                        )
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            except BaseException:  # noqa: BLE001 — 脚本任意异常都不能带崩宿主
                rc = 1
                import traceback as _tb

                _tb.print_exc(file=err)
            finally:
                sys.argv = old_argv
                os.chdir(old_cwd)
            text = out.getvalue()
            if err.getvalue():
                text += ("\n" if text else "") + err.getvalue()
            return text, rc

        try:
            text, rc = await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
        except TimeoutError:
            return Observation(
                tool_name=self.name, success=False,
                output=prefix + f"脚本超过 {timeout}s 未结束（进程内线程无法强杀，可能仍在后台运行）。"
                "长时间任务建议拆分或改写为分步执行。",
            )
        body = text.strip() or f"(无输出, exit={rc})"
        return Observation(
            tool_name=self.name, success=rc == 0,
            output=prefix + body,
            metadata={"inprocess_python": True, "exit_code": rc},
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
                "rm→del, clear→cls, uname→ver (e.g. command='ls' works and runs 'dir').\n"
                "SCHEDULING (schtasks): for tasks that must run while the user is absent "
                "(boot/logon/daily). Pitfalls: task default cwd=C:\\Windows\\System32 — use "
                "ABSOLUTE paths in /TR; NEVER run GUI automation under /RU SYSTEM (Session 0 "
                "has no desktop — task runs but all window ops silently fail); creating under "
                "the CURRENT user needs NO admin: schtasks /Create /TN name /TR "
                "\"C:\\abs\\path.exe\" /SC DAILY /ST 09:00 /F; run-now=/Run, query=/Query /V, "
                "delete=/Delete /F. In-session reminders/AI tasks → scheduler tool instead.\n"
                "BACKGROUND JOBS: Start-Process -WindowStyle Hidden for long CLI; verify via "
                "tasklist. GUI automation needs an interactive desktop — hidden windows cannot "
                "be driven by the desktop tool.\n"
                "AVOID SECURITY BLOCKS (EDR/DLP, legitimate ops): PowerShell blocked by "
                "execution policy → add -ExecutionPolicy Bypass to that invocation only (never "
                "change global policy); binary download/decode via PowerShell often trips EDR → "
                "prefer the desktop tool or ask the user; screenshots black/denied on corporate "
                "machines → desktop tool's screenshot already falls back to PrintWindow (works "
                "where screen DC is blocked); UAC prompts can NOT be automated — ask the user "
                "to approve.\n"
                "PYTHON STUB TRAP (Windows): the bare `python` command may be the Microsoft "
                "Store placeholder — it runs SILENTLY with NO output and exit 0. This tool "
                "AUTO-DETECTS unusable system python (missing or stub) and re-runs python-family "
                "commands with this app's BUILT-IN interpreter (in-process, with PIL etc. "
                "bundled) — no user Python installation required; you'll see an [内置解释器] "
                "prefix in the output when that happens. "
                "For Python tasks you can also call execute_code directly. "
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
                    # 2026-09-08：线程化 —— 健康检查含多轮 0.5s sleep（每候选最长
                    # 12s），同步调用会阻塞整个事件循环（WebSocket 心跳/流式输出全卡）
                    _ok, _detail = await asyncio.to_thread(
                        _launch_app_windows, _cand, _display
                    )
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
        # _approved：执行器在用户弹窗批准后（或权限=全部放行时）注入，
        # 使"高危但可判断后果"的命令真正跑起来（不可逆类仍被拦截，见 _validate_command）。
        _approved = bool(kwargs.pop("_approved", False))
        is_safe, error = _validate_command(command, args, allow_app_launch=_allow_launch, approved=_approved)
        if not is_safe:
            if interactive and not command.strip() and session_keys:
                pass  # 走 interactive 分支处理按键注入
            else:
                # 复合命令自动拆分（2026-08-29）：命令含 && / ; 且被安全校验拦截时，
                # 尝试拆成单条序列逐条执行（每条仍走完整安全校验，不拆管道/重定向）。
                # 避免"整条命令被拦 → 反思"的无效循环。
                split_obs = await self._try_split_execute(command, args, timeout, cwd, allow_app_launch=_allow_launch, approved=_approved)
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
            _p_ok = code == 0
            # ★ 2026-09-21：持久会话同样纠偏 cmd 内置命令的隐形失败
            if IS_WINDOWS and _p_ok and _win_cmd_real_status([command] + (args or []), output, code) is False:
                _p_ok = False
            return Observation(
                tool_name=self.name,
                success=_p_ok,
                output=output or f"(无输出, exit={code})",
                metadata={"persistent": True, "session_key": session_key or "default", "exit_code": code},
            )

        # 2.2 PTY 交互式终端（2026-08-27）：伪终端会话，支持 vim/top 等交互程序
        if interactive:
            from scout.tools.builtin.shell.pty_session import PTY_SUPPORTED

            # ★ 2026-09-25 接通 Windows：此前这里按 `IS_WINDOWS` 直接拒绝，理由是
            # "PTY 依赖 fcntl/termios/pty" —— 那是 Unix 实现的事实，Windows 侧早已
            # 用 ConPTY(pywinpty) 补齐同接口（WindowsPtySession + create_pty_session
            # 工厂），只是没人走到。守卫改成按**能力**判断：只有真缺依赖时才拒绝，
            # 并给出可执行的安装指引，而不是让 Windows 用户误以为功能不存在。
            if not PTY_SUPPORTED:
                if IS_WINDOWS:
                    return Observation(
                        tool_name=self.name,
                        success=False,
                        output="Windows 下的 PTY 交互式终端依赖 pywinpty（ConPTY），当前未安装。"
                               "请执行: pip install pywinpty —— 或改用普通 shell / persistent 持久会话（cmd.exe）。",
                    )
                return Observation(
                    tool_name=self.name,
                    success=False,
                    output="PTY 交互式终端不可用（缺少 pty/termios 支持）。"
                           "请改用普通 shell 或 persistent 持久会话。",
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
            # ★ 2026-09-25 分平台提示：ConPTY 下 \x03 送不到前台子进程（CTRL_C_EVENT
            # 需由挂在同一控制台的进程发），所以 Windows 上"中断"＝重启会话（cwd/env
            # 会丢）。给错提示会让模型白折腾一次按键注入。
            if hung:
                if IS_WINDOWS:
                    _break_hint = (
                        "要中断请用 shell 工具的 kill/新会话（Windows ConPTY 下 Ctrl-C 无法送达"
                        "前台子进程，中断会重启会话并丢失 cwd/环境变量）。"
                    )
                else:
                    _break_hint = "或用 session_keys='\\x03' 发送 Ctrl-C 中断。"
                hint = (
                    "\n\n[PTY] 命令挂起，交互程序仍在前台（会话保留，未中断）。"
                    "可继续调用本工具（interactive=true, command='', session_keys='...'）注入按键，"
                    + _break_hint
                )
            else:
                hint = ""
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
            # ★ 2026-09-08：系统无可用 python（未安装/商店占位程序）→ 自动重定向 ──
            # 普通用户 Windows 机器大多没有 Python 环境；python 族命令探测失败时，
            # 改用本应用自带解释器在进程内执行（安全级别与 shell 等价——shell 本就
            # 可执行任意命令；带 SystemExit 捕获 / 输出重定向 / 超时 / cwd 还原）。
            if IS_WINDOWS and cmd_list and _is_python_cmd(cmd_list[0]):
                if not await asyncio.to_thread(_probe_system_python):
                    obs = await self._run_python_inprocess(cmd_list, timeout, work_dir)
                    if obs is not None:
                        return obs
                    # 解析不出脚本/-c（如 -m pip）→ 落回原进程路径拿到真实报错
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
                # ★ 2026-09-08：start 启动【已知应用】时改走健康检查通道 ——
                # 此前 fire-and-forget 无条件 success=True，损坏安装弹的"找不到
                # 网络路径"错误框既不检测也不回退（假成功 + 弹窗残留）。
                _start_target = ""
                for _a in cmd_list[1:]:
                    if not _a or _a.startswith("-"):
                        continue
                    _base = os.path.basename(_a.strip('"')).lower()
                    if _base in KNOWN_APP_PATHS:
                        _start_target = _base
                        break
                    if _base.endswith(".exe"):
                        _start_target = _a.strip('"')
                        break
                if _start_target:
                    _sname = os.path.basename(_start_target)
                    _sentry = KNOWN_APP_PATHS.get(_sname.lower())
                    _sdisplay = _sentry[0] if _sentry else os.path.splitext(_sname)[0]
                    _scands = (
                        _resolve_known_app_candidates(_sname)
                        if _sentry
                        else ([_start_target] if os.path.isfile(_start_target) else [])
                    )
                    if _scands:
                        _serrs: list[str] = []
                        for _scand in _scands:
                            _sok, _sdetail = await asyncio.to_thread(
                                _launch_app_windows, _scand, _sdisplay
                            )
                            if _sok:
                                return Observation(
                                    tool_name=self.name, success=True,
                                    output=f"已启动 {_sdisplay}: {_scand}\n{_sdetail}",
                                )
                            _serrs.append(f"✗ {_scand}: {_sdetail}")
                        return Observation(
                            tool_name=self.name, success=False,
                            output=(
                                f"启动 {_sdisplay} 失败：已尝试 {len(_scands)} 个候选路径，"
                                f"可能均为损坏/不完整安装:\n" + "\n".join(_serrs)
                            ),
                        )
                _p = await _spawn(
                    _proc_cmd,
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
            process = await _spawn(
                _proc_cmd,
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
            # ★ 2026-09-21：cmd 内置命令隐形失败纠偏（del/type 失败仍返回 0，
            #   详见 _win_cmd_real_status 注释），避免向 LLM 谎报成功。
            if IS_WINDOWS and _ok and _win_cmd_real_status(cmd_list, full_output, _rc) is False:
                _ok = False

            # ★ 2026-09-08：空输出标注 —— 空输出≠失败（脚本可能只是没打印），
            #   但直接返回空串时，反思层只能瞎猜"运行环境不可用"。
            #   补一行退出码事实；Windows 下 bare python 常命中商店占位程序
            #   （静默无输出 exit 0），给出明确换路指引。
            if not full_output.strip():
                full_output = f"(命令已执行，无输出，exit={_rc})"
                if IS_WINDOWS and _ok:
                    _exe = os.path.basename(cmd_list[0].strip().strip('"').lower())
                    if _exe.split(".")[0] in ("python", "python3"):
                        full_output += (
                            "\n[提示] 该 python 可能是 Microsoft Store 占位程序（静默无输出）。"
                            "改用 py 或解释器完整路径重试；可先 python -c \"print('ok')\" 验证解释器可用。"
                        )

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
    async def _try_split_execute(self, command: str, args: list[str] | None, timeout: int, cwd: str, allow_app_launch: bool = False, approved: bool = False) -> Observation | None:
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
                    parts.append("".join(buf))
                    buf = []
                    i += 2
                    continue
                if ch == ";":
                    parts.append("".join(buf))
                    buf = []
                    i += 1
                    continue
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
            ok, _ = _validate_command(tokens[0], tokens[1:] if len(tokens) > 1 else None, allow_app_launch=allow_app_launch, approved=approved)
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
                _p = await _spawn(
                    _proc_cmd,
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
            process = await _spawn(
                _proc_cmd,
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
