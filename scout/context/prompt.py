"""分层 Prompt 构建器 — 借鉴 Hermes 的 prompt_builder.

三层构建: stable(不变) → context(按需) → volatile(每次变)
确保 system prompt 在对话中途不突变。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class PromptBuilder:
    """分层 Prompt 构建器."""

    def __init__(
        self,
        system_prompt: str = "",
        workspace: Any = None,
        skill_mgr: Any = None,
        memory_store: Any = None,
        budget_warning_threshold: int = 25,
    ):
        self.base_prompt = system_prompt
        self.workspace = workspace
        self.skill_mgr = skill_mgr
        self.memory = memory_store
        self.budget_warning_threshold = budget_warning_threshold

    def build(
        self,
        session: Any = None,
        user_input: str = "",
        current_step: int = 0,
        max_steps: int = 30,
    ) -> str:
        """构建完整 system prompt — 三层叠加."""
        layers = []

        # ── 1. Stable 层（不在对话中途变化）──
        stable = self._build_stable()
        if stable:
            layers.append(stable)

        # ── 2. Context 层（按需注入）──
        context = self._build_context(user_input)
        if context:
            layers.append(context)

        # ── 3. Volatile 层（每次变化）──
        volatile = self._build_volatile(current_step, max_steps)
        if volatile:
            layers.append(volatile)

        return "\n\n---\n\n".join(layers) if layers else self.base_prompt

    def _build_stable(self) -> str:
        """Stable 层 — 身份 + 工作空间 + 文件处理指导."""
        parts = [self.base_prompt]

        if self.workspace:
            ws_prompt = self.workspace.get_system_prompt()
            if ws_prompt:
                parts.append(ws_prompt)

        # 文件处理指导 — 告诉 Agent 如何正确使用文件工具
        file_guidance = """## 文件处理规范

**默认以文本回复，除非用户明确要求文件。**

1. **何时使用 send_file**：仅当用户明确要求发送/导出/下载文件时，才使用 send_file 工具。
   - 触发词例："发给我"、"给我文件"、"导出成文件"、"下载"、"生成一个xxx文件"等明确诉求。
   - 其余情况（回答、总结、整理、写代码、解释等）一律直接用文本回复，不要生成文件。

2. **如需发文件**：先用 write_file 或 execute_code 生成文件到磁盘（如 `/tmp/xxx.docx`），
   然后调用 send_file(path="/tmp/xxx.docx")。前端会自动显示下载按钮。

3. **不要直接输出文件内容**：尤其二进制文件（docx、xlsx、pdf 等）或大文件，
   不要把文件内容转成 base64 或 markdown 输出到聊天中。

示例：
- 用户："帮我生成一个周报文档并导出给我" → 明确要文件，生成后 send_file
- 用户："帮我总结一下这个项目的架构" → 只要内容，直接文本回复，不要发文件"""
        
        parts.append(file_guidance)

        return "\n\n".join(p for p in parts if p.strip())

    def _build_context(self, user_input: str) -> str:
        """Context 层 — 技能匹配 + 记忆召回."""
        parts = []

        # 技能匹配
        if self.skill_mgr and user_input:
            skill_prompt = self.skill_mgr.to_prompt(user_input)
            if skill_prompt:
                parts.append(skill_prompt)

        # 记忆召回
        if self.memory and user_input:
            memories = self.memory.search(user_input, limit=3)
            if memories:
                mem_text = "\n".join(f"- {m.content}" for m in memories)
                parts.append(f"[相关记忆]\n{mem_text}")

        return "\n\n".join(p for p in parts if p.strip())

    def _build_volatile(self, current_step: int, max_steps: int) -> str:
        """Volatile 层 — 时间戳 + 预算警告."""
        parts = [f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]

        # 预算警告
        remaining = max_steps - current_step
        if remaining <= self.budget_warning_threshold:
            parts.append(f"⚠️ 剩余迭代次数: {remaining}，请尽快收尾")

        return "\n".join(parts)
