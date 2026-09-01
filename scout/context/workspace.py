"""工作空间上下文 — 借鉴 CowAgent 的 AGENT.md / USER.md / RULE.md 设计.

Agent 读取工作空间文件来理解自己的身份、用户偏好和工作规则。
"""

from __future__ import annotations

from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR


class Workspace:
    """工作空间 — Agent 的身份和上下文."""

    def __init__(self, workspace_dir: str | Path | None = None):
        self.dir = Path(
            workspace_dir
            if workspace_dir is not None
            else str(_SCOUT_DATA_DIR / "workspace")
        ).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)

    def get_agent_identity(self) -> str:
        """读取 AGENT.md — Agent 的身份设定."""
        return self._read_file("AGENT.md")

    def get_user_info(self) -> str:
        """读取 USER.md — 用户的基本信息."""
        return self._read_file("USER.md")

    def get_rules(self) -> str:
        """读取 RULE.md — 工作规则."""
        return self._read_file("RULE.md")

    def get_memory(self) -> str:
        """读取 MEMORY.md — 长期记忆索引."""
        return self._read_file("MEMORY.md")

    def get_system_prompt(self) -> str:
        """构建完整的系统 prompt — 合并身份 + 用户 + 规则."""
        parts = []

        identity = self.get_agent_identity()
        if identity:
            parts.append(f"# Agent 身份\n{identity}")

        user = self.get_user_info()
        if user:
            parts.append(f"# 用户信息\n{user}")

        rules = self.get_rules()
        if rules:
            parts.append(f"# 工作规则\n{rules}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def save_memory(self, content: str) -> None:
        """保存长期记忆."""
        self._write_file("MEMORY.md", content)

    def append_memory(self, line: str) -> None:
        """追加记忆."""
        path = self.dir / "MEMORY.md"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _read_file(self, name: str) -> str:
        path = self.dir / name
        if path.exists():
            return path.read_text()
        return ""

    def _write_file(self, name: str, content: str) -> None:
        path = self.dir / name
        path.write_text(content)
