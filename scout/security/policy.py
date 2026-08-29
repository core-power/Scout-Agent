"""安全层 — 工具审批 + 危险命令检测 + 沙箱策略.

借鉴 OpenClaw 的安全模型：
1. 工具权限（allow/deny）
2. 危险命令检测（需要用户确认）
3. 沙箱策略（off / non-main / all）
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.types import ToolCall


class SandboxMode(str, Enum):
    """沙箱模式."""
    OFF = "off"          # 不沙箱
    NON_MAIN = "non-main"  # 非主会话沙箱
    ALL = "all"          # 全部沙箱


# ── 路径安全常量（统一来源，供 shell / web 等工具共享引用）──
# 系统敏感目录黑名单：严格禁止读写/执行的系统关键目录
SYSTEM_DIRS = [
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64",
    "/boot", "/sys", "/proc", "/dev", "/var", "/root",
]

# 允许访问的路径前缀白名单（个人版放宽，与 shell cwd 白名单保持一致）
ALLOWED_PATH_PREFIXES = [
    "/tmp", "/home", "/data", "/opt", "/srv", "/mnt",
    "/media", "/workspace",
]


# 危险命令模式
DANGEROUS_PATTERNS = [
    (r"\brm\s+-rf?\s+/", "递归删除根目录"),
    (r"\brm\s+-rf?\s+~", "递归删除用户目录"),
    (r"\bdd\s+if=", "dd 磁盘操作"),
    (r"\bmkfs\b", "格式化磁盘"),
    (r">\s*/dev/sd", "写入磁盘设备"),
    (r"\bshutdown\b", "关机命令"),
    (r"\breboot\b", "重启命令"),
    (r"\bkill\s+-9\s+1\b", "杀死 init 进程"),
    (r"\biptables\s+-F\b", "清空防火墙规则"),
    (r"\bchmod\s+-R\s+777\s+/", "递归 777 根目录"),
    (r"\bcurl\s+.*\|\s*sh", "管道执行远程脚本"),
    (r"\bwget\s+.*\|\s*sh", "管道执行远程脚本"),
    (r"\s*\(\s*\)\s*\{.*\};", "fork 炸弹"),
    (r"\s*\(\s*\)\s*\{", "fork 炸弹"),
    (r"\{[^}]*\}\s*&\s*\{[^}]*\}\s*&", "fork 炸弹"),
    # 服务重启/停止 — 引导用户手动执行（2026-08-12）
    # 后台 (scout restart &) 会残留孤儿进程导致服务起不来，需用户手动清理。
    # 只拦截重启/停止类，scout status/logs/start 等安全命令不拦截。
    (r"\bscout\b[^;|\n&]*\b(?:restart|stop)\b", "重启/停止 scout 服务"),
    (r"\bpkill[^;|\n&]*\bscout\b", "pkill scout 进程"),
    (r"\bkill\b[^;|\n&]*\bscout\b", "kill scout 进程"),
    # 读取敏感系统文件 / 隐私数据
    (r"\b(cat|less|more|head|tail|awk|grep|sed)\s+[^;|\n&]*(?:/etc/passwd|/etc/shadow|/etc/gshadow|/etc/hosts|/etc/hostname|/etc/resolv\.conf|/etc/ssh/sshd_config|/etc/ssh/ssh_config)\b", "读取敏感系统文件"),
    (r"\b(cat|less|more|head|tail|awk|grep|sed)\s+[^;|\n&]*(?:~|/home/[^/]+)/\.ssh/(?:id_rsa|id_ed25519|id_ecdsa|authorized_keys|known_hosts)\b", "读取 SSH 密钥或授权信息"),
    (r"\b(cat|less|more|head|tail|awk|grep|sed)\s+[^;|\n&]*(?:~|/home/[^/]+)/\.(?:bash_history|zsh_history|sh_history|mysql_history|python_history)\b", "读取用户历史命令"),
]


class SecurityManager:
    """安全管理器 — 工具权限 + 危险检测 + 审批."""

    def __init__(
        self,
        allow_tools: set[str] | None = None,
        deny_tools: set[str] | None = None,
        auto_approve: bool = False,
    ):
        self.allow_tools = allow_tools or set()
        self.deny_tools = deny_tools or set()
        self.auto_approve = auto_approve
        self._approval_callback: Any = None

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
