"""分层 Prompt 构建器 — 前缀静态化契约版.

核心契约（与 engine/context_inject.py 的 v3-Final P0 方案对齐）：

- ``build()`` 只返回 **100% 静态** 的 system prompt（stable 层）——
  跨轮逐字节一致，保证前缀缓存可命中；
- 动态内容（技能匹配 / 记忆召回 / 时间戳 / 预算警告）一律经
  ``build_runtime_context()`` 生成，由调用方追加到**当前 user 消息尾部**，
  绝不进入 system prompt。

历史教训：旧版把 context/volatile 层拼进 system prompt，时间戳精确到秒、
记忆每轮检索结果不同 → 前缀每轮变化，隐式缓存全部击穿（成本 +110%，
见 docs/ 智能路由缓存优化 Brief）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class PromptBuilder:
    """分层 Prompt 构建器（system prompt 静态 + 动态尾块分离）."""

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

    def build(self, session: Any = None, **_kwargs: Any) -> str:
        """构建 **静态** system prompt — 仅 stable 层.

        旧签名中的 ``user_input``/``current_step``/``max_steps`` 参数已不再
        影响 system prompt（它们属于动态层，走 :meth:`build_runtime_context`），
        保留 ``**_kwargs`` 以兼容既有调用方，不做破坏性签名变更。

        ⚠️ 本方法返回值在 Agent 生命周期内必须逐字节稳定 —— 任何新增注入
        都必须满足"构造后不再变化"，否则前缀缓存全部失效。
        """
        stable = self._build_stable()
        return stable if stable else self.base_prompt

    def build_runtime_context(
        self,
        user_input: str = "",
        current_step: int = 0,
        max_steps: int = 30,
    ) -> str:
        """构建动态上下文块 — 追加到**当前 user 消息尾部**，勿放入 system prompt.

        内容：技能匹配 + 记忆召回（context 层）→ 时间戳 + 预算警告（volatile 层）。
        每轮内容不同是预期行为：它位于消息列表末尾，不影响前缀缓存。
        """
        parts = []

        # ── Context 层（按需注入：技能 + 记忆）──
        context = self._build_context(user_input)
        if context:
            parts.append(context)

        # ── Volatile 层（时间戳 + 预算警告）──
        volatile = self._build_volatile(current_step, max_steps)
        if volatile:
            parts.append(volatile)

        return "\n\n---\n\n".join(parts)

    def _build_stable(self) -> str:
        """Stable 层 — 身份 + 工作空间 + 文件处理指导."""
        parts = [self.base_prompt]

        if self.workspace:
            ws_prompt = self.workspace.get_system_prompt()
            if ws_prompt:
                parts.append(ws_prompt)

        # 文件处理指导 — 告诉 Agent 如何正确使用文件工具
        # 示例路径用跨平台临时目录（Windows→%TEMP%\scout，Unix→/tmp/scout），
        # 避免硬编码 /tmp 与 platform.py 注入的「Windows 别用 Unix 路径」指令打架。
        try:
            from scout.core.platform import get_temp_dir

            _tmp_example = str(get_temp_dir() / "xxx.docx")
        except Exception:
            _tmp_example = "/tmp/scout/xxx.docx"
        file_guidance = f"""## 文件处理规范

**默认以文本回复，除非用户明确要求文件。**

1. **何时使用 send_file**：仅当用户明确要求发送/导出/下载文件时，才使用 send_file 工具。
   - 触发词例："发给我"、"给我文件"、"导出成文件"、"下载"、"生成一个xxx文件"等明确诉求。
   - 其余情况（回答、总结、整理、写代码、解释等）一律直接用文本回复，不要生成文件。

2. **如需发文件**：先用 write_file 或 execute_code 生成文件到磁盘（如临时目录 `{_tmp_example}`），
   然后调用 send_file(path="{_tmp_example}")。前端会自动显示下载按钮。

3. **不要直接输出文件内容**：尤其二进制文件（docx、xlsx、pdf 等）或大文件，
   不要把文件内容转成 base64 或 markdown 输出到聊天中。

示例：
- 用户："帮我生成一个周报文档并导出给我" → 明确要文件，生成后 send_file
- 用户："帮我总结一下这个项目的架构" → 只要内容，直接文本回复，不要发文件"""

        parts.append(file_guidance)

        return "\n\n".join(p for p in parts if p.strip())

    def _build_context(self, user_input: str) -> str:
        """Context 层 — 技能匹配 + 记忆召回（动态，进 user 消息尾部）."""
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
        """Volatile 层 — 时间戳 + 预算警告（动态，进 user 消息尾部）."""
        parts = [f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"]

        # 预算警告
        remaining = max_steps - current_step
        if remaining <= self.budget_warning_threshold:
            parts.append(f"⚠️ 剩余迭代次数: {remaining}，请尽快收尾")

        return "\n".join(parts)
