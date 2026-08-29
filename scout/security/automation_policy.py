"""无人值守权限模型 — 对标 Codex Automations 的权限体系.

自动化任务（cron/webhook/事件/级联触发）在无人盯着的情况下运行，
需要预设的权限策略而不是交互式审批：

两级策略（组织级覆盖用户级，对标 Codex requirements.toml 强制约束）：
1. **组织级** /etc/scout/requirements.toml — 管理员强制，用户配置无法突破
2. **用户级** ~/.scout/automation_policy.json — 用户自定义

approval_policy 四档（对标 Codex default_tools_approval_mode）：
- "auto": 只读工具直接执行，写操作按 allowlist 判断
- "writes": 文件写操作允许，但 shell/网络类高危操作仍需白名单
- "prompt": 任何不在白名单的操作 → 挂起并发通知等人工确认（有人在线时）
- "never": 全部放行（危险命令硬拦截仍然生效，见 policy.py check_command_block）

此外支持：
- allowed_tools / denied_tools: 工具级白名单/黑名单
- allowed_shell_patterns: shell 命令正则白名单（如只允许 "git log .*"）
- notify_on_danger: 命中危险操作时推送通知事件（替代卡死等待）
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_USER_POLICY_PATH = Path.home() / ".scout" / "automation_policy.json"
_ORG_POLICY_PATH = Path("/etc/scout/requirements.json")  # JSON 兼容版（无需 toml 依赖）

# 只读工具（auto 模式直接放行）
READONLY_TOOLS = {
    "read_file", "list_dir", "web_search", "web_fetch", "memory_search",
    "memory_list", "vision", "knowledge_search", "get_weather", "scheduler_list",
}

# 写操作工具（writes 模式放行）
WRITE_TOOLS = {
    "write_file", "edit_file", "read_file", "list_dir", "web_search", "web_fetch",
    "memory_save", "memory_search", "memory_list", "vision", "image_generation",
    "knowledge_write", "send_file",
}

_VALID_POLICIES = ("auto", "writes", "prompt", "never")


@dataclass
class AutomationPolicy:
    approval_policy: str = "auto"
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    allowed_shell_patterns: list[str] = field(default_factory=list)
    notify_on_danger: bool = True
    max_steps: int = 30
    source: str = "default"  # default | user | org

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_json(path: Path) -> dict | None:
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning(f"策略文件读取失败 {path}: {e}")
    return None


class AutomationPolicyManager:
    """无人值守策略管理器 — 组织级 > 用户级 > 默认."""

    def __init__(self):
        self._org = _load_json(_ORG_POLICY_PATH)
        self._user = _load_json(_USER_POLICY_PATH)

    def reload(self) -> None:
        self._org = _load_json(_ORG_POLICY_PATH)
        self._user = _load_json(_USER_POLICY_PATH)

    def get_policy(self) -> AutomationPolicy:
        """合并得到生效策略 — 组织级字段强制覆盖用户级."""
        policy = AutomationPolicy()

        # 用户级
        if self._user:
            self._apply(policy, self._user, "user")

        # 组织级强制覆盖
        if self._org:
            self._apply(policy, self._org, "org", force=True)

        return policy

    def _apply(self, policy: AutomationPolicy, data: dict, source: str, force: bool = False) -> None:
        ap = data.get("approval_policy")
        if ap in _VALID_POLICIES:
            policy.approval_policy = ap
            policy.source = source
        elif ap is not None:
            logger.warning(f"非法 approval_policy: {ap}（来源 {source}），忽略")

        for key in ("allowed_tools", "denied_tools", "allowed_shell_patterns"):
            val = data.get(key)
            if isinstance(val, list):
                if force:
                    setattr(policy, key, [str(v) for v in val])
                else:
                    getattr(policy, key).extend(str(v) for v in val)

        if "notify_on_danger" in data:
            policy.notify_on_danger = bool(data["notify_on_danger"])
        if "max_steps" in data:
            try:
                policy.max_steps = max(1, int(data["max_steps"]))
            except (ValueError, TypeError):
                pass

    def save_user_policy(self, policy: AutomationPolicy) -> None:
        """保存用户级策略."""
        data = policy.to_dict()
        data.pop("source", None)
        try:
            _USER_POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_USER_POLICY_PATH, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._user = data
        except Exception as e:
            logger.warning(f"用户策略保存失败: {e}")

    # ── 运行时判定 ──

    def check_tool(
        self,
        tool_name: str,
        args: dict,
        policy: AutomationPolicy | None = None,
        security_manager: Any = None,
    ) -> tuple[bool, str]:
        """判定自动化任务中的工具调用是否放行.

        Returns:
            (allowed, reason) — reason 在拒绝时说明原因
        """
        p = policy or self.get_policy()

        # 黑名单优先
        if tool_name in p.denied_tools:
            return False, f"工具 {tool_name} 在自动化黑名单中"

        # 白名单存在时只放行白名单
        if p.allowed_tools:
            if tool_name in p.allowed_tools:
                return True, "白名单放行"
            # shell 命令可按正则白名单放行
            if tool_name == "shell" and self._shell_allowed(args.get("command", ""), p):
                return True, "shell 模式白名单放行"
            return False, f"工具 {tool_name} 不在自动化白名单中"

        mode = p.approval_policy

        # never: 全放行（危险命令硬拦截由 SecurityManager.check_command_block 兜底）
        if mode == "never":
            if tool_name == "shell" and security_manager:
                command = args.get("command", "")
                safe, msg = security_manager.check_command_block(command)
                if not safe:
                    return False, msg or "危险命令被硬拦截"
            return True, "never 模式放行"

        if tool_name in READONLY_TOOLS:
            return True, "只读工具放行"

        if mode == "auto":
            return False, f"auto 模式：写操作 {tool_name} 需加入白名单"

        if mode == "writes":
            if tool_name in WRITE_TOOLS:
                return True, "writes 模式放行"
            if tool_name == "shell" and self._shell_allowed(args.get("command", ""), p):
                return True, "shell 模式白名单放行"
            if tool_name == "shell" and security_manager:
                safe, msg = security_manager.check_command_block(args.get("command", ""))
                if not safe:
                    return False, msg or "危险命令被硬拦截"
            return False, f"writes 模式：{tool_name} 需加入白名单"

        # prompt 模式：无人值守场景退化为拒绝 + 通知（由调用方发通知）
        return False, f"prompt 模式：{tool_name} 需人工确认（自动化场景已拒绝）"

    @staticmethod
    def _shell_allowed(command: str, policy: AutomationPolicy) -> bool:
        for pat in policy.allowed_shell_patterns:
            try:
                if re.fullmatch(pat, command.strip()):
                    return True
            except re.error:
                continue
        return False
