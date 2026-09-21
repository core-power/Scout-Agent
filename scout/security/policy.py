"""安全层 — 工具审批 + 危险命令检测 + 沙箱策略.

借鉴 OpenClaw 的安全模型：
1. 工具权限（allow/deny）
2. 危险命令检测（需要用户确认）
3. 沙箱策略（off / non-main / all）
"""

from __future__ import annotations

import os
import re
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.types import ToolCall

# SandboxMode 唯一来源在 scout.security.sandbox，避免双份定义漂移
from scout.security.sandbox import SandboxMode  # noqa: E402


# ── 路径安全常量（统一来源，供 shell / web 等工具共享引用）──
# 系统敏感目录黑名单：严格禁止读写/执行的系统关键目录
SYSTEM_DIRS = [
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/sys", "/proc", "/dev", "/var", "/root",
]

# Windows 系统敏感目录（仅 Windows 生效，2026-08-30 新增：
# 原先只有 Unix 路径，Windows 下 C:\Windows 等完全无保护）
if os.name == "nt":
    SYSTEM_DIRS += [
        r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)",
        r"C:\ProgramData", r"C:\Recovery", r"C:\System Volume Information",
    ]

# 允许访问的路径前缀白名单（个人版放宽，与 shell cwd 白名单保持一致）
ALLOWED_PATH_PREFIXES = [
    "/tmp", "/home", "/data", "/opt", "/srv", "/mnt",
    "/media", "/workspace",
]


def path_allowed(abs_path: str) -> bool:
    """跨平台路径白名单判定（供 file / shell / web 工具共享，2026-08-30）.

    Windows: 放行任意盘符根（如 D:\\）及其下路径——系统目录由 SYSTEM_DIRS 另行硬拦截；
    Unix: 放行主目录 + ALLOWED_PATH_PREFIXES 白名单前缀。
    """
    abs_path = os.path.abspath(abs_path)
    if os.name == "nt":
        drive, _ = os.path.splitdrive(abs_path)
        return bool(drive)
    for prefix in ALLOWED_PATH_PREFIXES:
        if abs_path == prefix or abs_path.startswith(prefix + os.sep):
            return True
    return False


# ── 危险命令：两级风险（2026-09-21）────────────────────────────────
# 维度一（风险分级）：同样"危险"的操作，后果并不同量级 ——
#   NEVER：不可逆 / 不可审计，任何权限模式下都硬拦截，不给"审批放行"入口；
#   RISKY：高危但用户有权决定（删自己的目录、重启、强推 git…），默认弹窗审批，
#          用户在输入框把权限切到"全部放行"后直接执行。
# DANGEROUS_PATTERNS 保留为两者合集，供既有调用方（verifier / automation 等）沿用。
NEVER_PATTERNS = [
    (r"\brm\s+-rf?\s+/", "递归删除根目录"),
    (r"\brm\s+-rf?\s+~", "递归删除用户目录"),
    (r"\bdd\s+if=", "dd 磁盘操作"),
    (r"\bmkfs\b", "格式化磁盘"),
    (r">\s*/dev/sd", "写入磁盘设备"),
    (r"\bkill\s+-9\s+1\b", "杀死 init 进程"),
    (r"\bchmod\s+-R\s+777\s+/", "递归 777 根目录"),
    (r"\bcurl\s+.*\|\s*sh", "管道执行远程脚本"),
    (r"\bwget\s+.*\|\s*sh", "管道执行远程脚本"),
    (r"\s*\(\s*\)\s*\{.*\};", "fork 炸弹"),
    (r"\s*\(\s*\)\s*\{", "fork 炸弹"),
    (r"\{[^}]*\}\s*&\s*\{[^}]*\}\s*&", "fork 炸弹"),
]

RISKY_PATTERNS = [
    # 递归 / 强制删除（针对具体目录，用户可判断后果）
    (r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\b", "递归或强制删除"),
    (r"\brmdir\s+/s\b", "递归删除目录（rmdir /s）"),
    (r"\bdel\s+/[a-zA-Z]*[fs][a-zA-Z]*\b", "强制/递归删除文件（del /f /s）"),
    (r"\b(rd|rmdir)\s+/s\s+/q\b", "静默递归删除目录"),
    (r"\bformat\s+[a-zA-Z]:", "格式化盘符"),
    (r"\bdiskpart\b", "磁盘分区工具"),
    (r"\b(shred|wipe|sdelete)\b", "安全擦除文件"),
    # 系统与进程
    (r"\bshutdown\b", "关机命令"),
    (r"\breboot\b", "重启命令"),
    (r"\biptables\s+-F\b", "清空防火墙规则"),
    (r"\bkill\s+-9\b", "强制杀进程"),
    (r"\btaskkill\s+/f\b", "强制结束进程"),
    # 服务重启/停止 — 引导用户确认（2026-08-12）
    # 后台 (scout restart &) 会残留孤儿进程导致服务起不来。
    # 只拦截重启/停止类，scout status/logs/start 等安全命令不拦截。
    (r"\bscout\b[^;|\n&]*\b(?:restart|stop)\b", "重启/停止 scout 服务"),
    (r"\bpkill[^;|\n&]*\bscout\b", "pkill scout 进程"),
    (r"\bkill\b[^;|\n&]*\bscout\b", "kill scout 进程"),
    # Git 破坏性操作
    (r"\bgit\s+push\s+.*--force\b", "强制推送（覆盖远端历史）"),
    (r"\bgit\s+reset\s+--hard\b", "硬重置（丢弃本地改动）"),
    (r"\bgit\s+clean\s+-[a-zA-Z]*f", "强制清理未跟踪文件"),
    # 权限 / 归属变更
    (r"\bchmod\s+-R\b", "递归修改权限"),
    (r"\bchown\s+-R\b", "递归修改属主"),
    (r"\bicacls\b", "修改文件 ACL"),
    (r"\btakeown\b", "夺取文件所有权"),
    # 读取敏感系统文件 / 隐私数据（用户可授权，默认提示）
    (r"\b(cat|less|more|head|tail|awk|grep|sed)\s+[^;|\n&]*(?:/etc/passwd|/etc/shadow|/etc/gshadow|/etc/hosts|/etc/hostname|/etc/resolv\.conf|/etc/ssh/sshd_config|/etc/ssh/ssh_config)\b", "读取敏感系统文件"),
    (r"\b(cat|less|more|head|tail|awk|grep|sed)\s+[^;|\n&]*(?:~|/home/[^/]+)/\.ssh/(?:id_rsa|id_ed25519|id_ecdsa|authorized_keys|known_hosts)\b", "读取 SSH 密钥或授权信息"),
    (r"\b(cat|less|more|head|tail|awk|grep|sed)\s+[^;|\n&]*(?:~|/home/[^/]+)/\.(?:bash_history|zsh_history|sh_history|mysql_history|python_history)\b", "读取用户历史命令"),
]

DANGEROUS_PATTERNS = NEVER_PATTERNS + RISKY_PATTERNS

# 命令风险分级结果
RISK_NEVER = "never"    # 任何权限模式都拦截
RISK_RISKY = "risky"    # 高危：默认询问，权限全开时直接执行
RISK_NORMAL = "normal"  # 常规操作

# 权限模式（维度二：用户授权范围）——由输入框开关控制，持久化到 config.json
PERMISSION_ASK = "ask"      # 标准：仅高危操作询问（默认）
PERMISSION_AUTO = "auto"    # 全部放行：高危也不再询问，直接执行
PERMISSION_STRICT = "strict"  # 谨慎：所有 shell 命令与写操作都先询问
PERMISSION_MODES = (PERMISSION_ASK, PERMISSION_AUTO, PERMISSION_STRICT)


def classify_command_risk(command: str) -> tuple[str, str]:
    """命令风险分级：返回 (RISK_NEVER / RISK_RISKY / RISK_NORMAL, 原因).

    NEVER 优先于 RISKY —— 一条命令同时命中两类时按不可逆处理。
    """
    text = command or ""
    for pat, desc in NEVER_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return RISK_NEVER, desc
    for pat, desc in RISKY_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return RISK_RISKY, desc
    return RISK_NORMAL, ""


class SecurityManager:
    """安全管理器 — 工具权限 + 危险检测 + 审批."""

    def __init__(
        self,
        allow_tools: set[str] | None = None,
        deny_tools: set[str] | None = None,
        auto_approve: bool = False,
        permission_mode: str = PERMISSION_ASK,
    ):
        self.allow_tools = allow_tools or set()
        self.deny_tools = deny_tools or set()
        self.auto_approve = auto_approve
        # 权限模式（输入框开关）：ask=高危询问 / auto=全部放行 / strict=逐条询问
        self.permission_mode = permission_mode if permission_mode in PERMISSION_MODES else PERMISSION_ASK
        self._approval_callback: Any = None

    def set_permission_mode(self, mode: str) -> str:
        """切换权限模式（非法值回落 ask），返回生效值."""
        self.permission_mode = mode if mode in PERMISSION_MODES else PERMISSION_ASK
        return self.permission_mode

    def set_approval_callback(self, callback):
        """设置审批回调函数 — 当工具需要审批时调用."""
        self._approval_callback = callback

    def check_tool(self, tool_name: str, annotations: ToolAnnotations) -> tuple[bool, str]:
        """检查工具是否被允许."""
        if tool_name in self.deny_tools:
            return False, f"工具 {tool_name} 被禁止使用"
        if self.allow_tools and tool_name not in self.allow_tools:
            return False, f"工具 {tool_name} 不在允许列表中"
        return True, ""

    def check_command(self, command: str) -> tuple[bool, str | None]:
        """检查 shell 命令是否危险.

        Returns:
            (is_safe, warning_message)
        """
        for pattern, desc in DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"⚠️ 危险操作: {desc}"
        return True, None

    def needs_approval(self, tool_name: str, args: dict, annotations: ToolAnnotations) -> bool:
        """判断工具调用是否需要用户审批.

        注意: auto_approve 只跳过"需要审批"的事，但**危险命令检测是硬拦截**，
        由 check_command_block 在工具执行前强制拦截，不因 auto_approve 而放行。
        """
        # 危险命令检测优先级最高（即使 auto_approve 也触发审批）
        if tool_name == "shell":
            command = args.get("command", "")
            is_safe, _ = self.check_command(command)
            if not is_safe:
                return True  # 危险命令必须审批，auto_approve 不跳过
        if self.auto_approve:
            return False
        # 需要审批的情况
        if annotations.requires_approval:
            return True
        if annotations.destructive:
            return True
        return False

    def check_command_block(self, command: str) -> tuple[bool, str | None]:
        """硬拦截危险命令 — 不受 auto_approve 影响，永远生效.

        与 check_command 的区别：这是执行前的强制拦截，
        即使 auto_approve=True 也会拦截恶意命令。
        """
        for pattern, desc in DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"⛔ 危险命令已拦截: {desc}"
        return True, None

    async def request_approval(self, tool_name: str, args: dict, reason: str) -> bool:
        """请求用户审批."""
        if self.auto_approve:
            return True
        if self._approval_callback:
            return await self._approval_callback(tool_name, args, reason)
        return True
