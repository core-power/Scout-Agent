"""Agent 核心循环 — 平台无关的 ReAct 引擎.



融合 Hermes + OpenClaw + CowAgent 三者优势：

- Hermes: ReAct 循环 + 可中断执行 + 回调面 + Provider fallback + 子代理委派

- OpenClaw: 上下文治理 + 安全审批 + 事件总线 + Cron

- CowAgent: 技能匹配 + 工作空间上下文 + 记忆系统

"""

from __future__ import annotations
import asyncio
import logging
import time
import ast
import json
import re
import uuid
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from scout.core.callbacks import Callbacks, NullCallbacks
from scout.core.types import (
    Action,
    LLMResponse,
    Message,
    Observation,
    Role,
    Session,
    ToolCall,
)

from scout.engine.budget import IterationBudget

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

from scout.engine.interrupt import InterruptibleExecutor

from scout.llm.base import LLMClient

from scout.tools.registry import ToolRegistry


# ── v3-Final 优化模块 ──

from scout.engine.cache_monitor import get_cache_monitor

from scout.engine.failover import (
    get_failover_manager,
    should_failover,
)

from scout.engine.sanitize import sanitize_assistant_output, extract_thinking


class Agent:
    """Scout Agent 核心引擎.



    无状态步进：每次 run_conversation() 是一个完整的对话轮次。

    内部 ReAct 循环：think → act → observe → think...

    """

    def __init__(
        self,
        llm: LLMClient,
        system_prompt: str = "",
        tools: ToolRegistry | None = None,
        callbacks: Callbacks | None = None,
        max_turns: int = 60,
        max_loop_seconds: int = 600,  # 2026-08-28：回合总时长看门狗（防无限执行卡死）
        temperature: float = 0.7,
        deep_thinking: bool = True,
        agent_mode: str = "react",  # "react" 或 "multi_agent"
        # 上下文治理
        enable_context: bool = True,
        # 会话持久化
        enable_persistence: bool = True,
        # 记忆系统
        enable_memory: bool = True,
        embedding_provider: Any = None,  # 外部注入的嵌入提供者（None=默认本地 ONNX）
        # 安全层
        enable_security: bool = True,
        auto_approve: bool = False,
        # 自修复
        enable_self_heal: bool = True,
        max_heal_retries: int = 2,
        # 技能系统
        enable_skills: bool = True,
        # 工作空间
        enable_workspace: bool = False,
        workspace_dir: str | Path | None = None,
        # 事件总线
        enable_bus: bool = True,
        # ── 子代理委派控制 ──
        delegate_depth: int = 0,  # 当前委派深度（主 Agent=0，子代理=1，孙代理=2…）
        max_delegate_depth: int = 2,  # 最大允许委派深度
        exclude_tools: set[str] | None = None,  # 要排除的工具（子代理不应拥有委派类工具）
        allow_tools: set[str] | None = None,  # 白名单：仅暴露这些工具（None=全部）— CLI 精简模式用
        register_as_main: bool = True,  # 是否注册为 ToolRegistry._main_agent
        # ── 第一梯队能力 ──
        enable_reflexion: bool = True,  # 反思循环：工具执行后评估方向
        enable_goal_manager: bool = True,  # 目标管理：跨会话任务追踪
        enable_observability: bool = True,  # 可观测性：全链路追踪
        # ── 第二梯队能力 ──
        enable_hitl: bool = True,  # Human-in-the-Loop：危险操作前请求用户确认
        hitl_tools: list[str] | None = None,  # 需要确认的工具列表（默认：shell, execute_code）
        # ── 回复语言 ──
        language: str = "auto",  # auto=跟随用户 / zh=中文 / en=英文
        # ── 双模型（已移除 2026-08-14，参数保留仅为兼容）──
        thinker_llm: Any = None,  # 已废弃：保留兼容
        executor_llm: Any = None,  # 已废弃：保留兼容
        # ── 上下文/记忆工程化（E4，2026-08-27）：跨会话记忆抽取与上下文组装 ──
        memory_extractor: Any = None,  # SessionMemoryExtractor 实例（会话结束时抽取关键记忆）
        context_assembler: Any = None,  # ContextAssembler 实例（跨会话记忆/摘要组装）
        memory_flush: Any = None,  # MemoryFlush 实例（压缩前抽取关键记忆）
    ):

        self.llm = llm

        self.deep_thinking = deep_thinking

        self.agent_mode = agent_mode

        # ── 循环策略选择（2026-08-27，对标 DSH 可插拔 Agent Loop）──
        # agent_mode="dag" 或环境变量 SCOUT_LOOP_MODE=dag 启用 DAG 计划-执行循环；
        # 其余情况（react / multi_agent）均使用默认 ReAct 循环。
        loop_mode = os.environ.get("SCOUT_LOOP_MODE", agent_mode)
        if loop_mode == "dag":
            from scout.engine.loops import DAGLoop

            self.loop = DAGLoop(self)
        else:
            from scout.engine.loops import ReActLoop

            self.loop = ReActLoop(self)

        if agent_mode == "multi_agent":
            self.system_prompt = (
                "You are Scout, an orchestrator agent with persistent memory. You coordinate sub-agents to solve complex tasks.\n\n"
                "Current date: see <runtime_context> in the latest user message.\n\n"
                "## Available Tools\n"
                "- delegate_task: 将子任务委派给隔离子代理执行（串行）\n"
                "- parallel_delegate: 并行委派多个子任务（适合独立子任务）\n"
                "- collaborate_task: 自动分解任务并交给多个子代理协作执行、自动聚合（适合大型复杂任务）\n"
                "- shell: 执行 shell 命令\n"
                "- read_file / write_file / list_dir: 文件操作\n"
                "- web_search / web_fetch: 搜索互联网、获取网页内容\n"
                "- memory_save / memory_search: 长期记忆\n\n"
                "## Multi-Agent Strategy\n"
                "You are an ORCHESTRATOR. Your job is to DECOMPOSE complex tasks and DELEGATE to sub-agents.\n"
                "1. Analyze the user's request and break it into sub-tasks\n"
                "2. ALWAYS decompose: any task with 2+ independent sub-goals MUST use parallel_delegate to run them concurrently — do not do them yourself sequentially.\n"
                "3. Use delegate_task only for sequential dependent sub-tasks\n"
                "4. Only act directly for trivially simple tasks (single quick step, basic Q&A)\n"
                "5. Synthesize sub-agents' results into a coherent final answer — once all sub-agents finish, IMMEDIATELY produce the final answer. Do NOT re-search, do NOT re-delegate, do NOT add extra tool calls. Just synthesize and answer.\n\n"
                "## When to Delegate vs Act Directly\n"
                "- Delegate (DEFAULT for any multi-part task): research, multi-step analysis, complex code tasks, tasks with clear sub-goals — ALWAYS prefer delegation when the task can be split into 2+ independent parts.\n"
                "- Direct (ONLY for truly simple): quick file reads, simple shell commands, memory operations, answering from knowledge in 1 step\n\n"
                "## Thinking Mode\n"
                "Before each action, briefly explain your reasoning (1-3 sentences):\n"
                "- What you plan to do and why\n"
                "- Why you chose to delegate vs act directly\n"
                "Then make the tool call. After receiving results, continue until you can give a final answer.\n\n"
                "## Guidelines\n"
                "- Think before every action — explain WHY\n"
                "- Be concise: 1-3 sentences of reasoning, then act\n"
                "- After sub-agent results, synthesize what you learned\n"
                "- Give final answer in natural language when no more tools needed\n"
                "- Always respond in the same language as the user's input\n\n"
                "## Role Boundary (角色边界)\n"
                "区分「项目内问题」和「通用技术问题」：\n"
                "- 项目内：用户明确提到 Scout、本项目的路由/记忆/工具等 → 可结合项目上下文回答\n"
                "- 通用：用户问技术概念、架构设计、行业方案、编程问题等 → 以通用技术专家身份回答，**不要提及 Scout Agent、不要关联本项目的实现细节**\n"
                "- 回答通用问题时，禁止使用「在 Scout Agent 中」「根据我们的架构」「我们的路由系统」等表述\n"
            )
            if system_prompt:
                self.system_prompt += "\n\n## Additional User Instructions\n" + system_prompt

        else:
            # 基础模板：deep_thinking 优先，否则默认模板（身份/工具/规则 = 稳定前缀）
            if deep_thinking:
                self.system_prompt = (
                "You are Scout, a capable AI assistant with persistent memory and tools.\n\n"
                "Current date: see <runtime_context> in the latest user message. Your training data may be outdated — always trust the current date and search results over your internal knowledge.\n\n"
                "## Tools\n"
                "- web_search: 搜索互联网（自动多路并发+查询改写+翻页去重+相关性排序）\n"
                "- web_fetch: 获取指定 URL 的网页内容\n"
                "- browser: 控制浏览器（导航、点击、填写、截图、提取文本）\n"
                "- shell: 执行 shell 命令\n"
                "- read_file / write_file / list_dir: 文件操作\n"
                "- execute_code: 执行 Python 代码\n"
                "- image_generation: 生成图片\n"
                "- vision: 分析图片内容\n"
                "- memory_save / memory_search / memory_list: 长期记忆\n"
                "- knowledge: 管理知识库\n"
                "- scheduler: 定时任务和提醒\n\n"
                "## Tool Call Efficiency (工具调用效率)\n"
                "为减少决策轮数、更快完成任务：\n"
                "- **一次决策可返回多个独立工具调用**：当多个操作互不依赖、可同时推进时（如搜索多个不同主题、读取多个文件、并行查询多个来源），在同一次回复里一次性返回多个 tool_call，不要逐个串行。\n"
                "- 保持连续的工具调用链条：前一个工具的结果刚产生、下一步动作明确时，直接继续调用下一个工具，不要中途停顿或重复陈述。\n"
                "- 避免不必要的中间回复：除非需要用户澄清，否则持续调用工具直到任务完成，再输出最终结果。\n"
                "- 只调用完成任务真正需要的工具，不做多余的探索。\n\n"
                "## Role Boundary (角色边界)\n"
                "区分「项目内问题」和「通用技术问题」：\n"
                "- 项目内：用户明确提到 Scout、本项目的路由/记忆/工具等 → 可结合项目上下文回答\n"
                "- 通用：用户问技术概念、架构设计、行业方案、编程问题等 → 以通用技术专家身份回答，**不要提及 Scout Agent、不要关联本项目的实现细节**\n"
                "- 回答通用问题时，禁止使用「在 Scout Agent 中」「根据我们的架构」「我们的路由系统」等表述\n\n"
                "## Core Principles\n"
                "1. **事实优先**: 当用户询问事实（如某产品是否发布、某功能是否存在），不要依赖内部知识下结论——先搜索。搜索结果中的官方文档、权威网站优先采信。\n"
                "2. **搜索结果解读**: 搜索返回的每条结果包含标题、URL、日期(📆)、摘要。重点看：\n"
                "   - URL 域名（help.aliyun.com、docs.python.org 等官方文档 > CSDN/知乎等博客）\n"
                "   - 📆 日期（判断信息时效性）\n"
                "   - 摘要中是否直接回答了用户问题\n"
                "   - 如果搜索结果已包含答案，直接引用，不要再搜索\n"
                "3. **多步推理**: 复杂问题拆成步骤，每步搜索→分析→决策下一步。不要期望一次搜索就得到所有答案。\n"
                "4. **信息综合**: 多条搜索结果交叉验证，给出有依据的结论，标注来源。\n\n"
                "## Thinking Mode\n"
                "每次工具调用前，用 1-3 句话解释你的推理：打算做什么、为什么。这段文本会作为思考过程展示给用户。\n"
                "收到工具结果后，先总结你学到了什么，再决定下一步。\n\n"
                "## Memory Rules\n"
                '- 用户说"记住"、"以后"、"总是"时 → memory_save\n'
                "- 不确定时先 memory_search 查找\n"
                "- 主动保存重要的用户偏好、决策和结论\n\n"
                "## Response Guidelines\n"
                "- 用与用户输入相同的语言回复\n"
                "- 回答结构清晰，善用加粗、列表、分段\n"
                "- 引用搜索结果时标注来源链接\n"
                '- 不确定时说"不确定"，不要编造\n'
                "- **除非用户明确要求发送文件，否则不要主动生成或发送文件**。直接以文本形式回复内容即可。\n"
            )

            else:
                self.system_prompt = (
                "You are Scout, a capable AI assistant with persistent memory and tools.\n\n"
                "Current date: see <runtime_context> in the latest user message. Your training data may be outdated — always trust the current date and search results over your internal knowledge.\n\n"
                "## Tools\n"
                "- web_search: 搜索互联网（自动多路并发+查询改写+翻页去重+相关性排序）\n"
                "- web_fetch: 获取指定 URL 的网页内容\n"
                "- shell: 执行 shell 命令\n"
                "- read_file / write_file / list_dir: 文件操作\n"
                "- execute_code: 执行 Python 代码\n"
                "- image_generation: 生成图片\n"
                "- vision: 分析图片内容\n"
                "- memory_save / memory_search: 长期记忆\n"
                "- scheduler: 定时任务和提醒\n\n"
                "## Tool Call Efficiency (工具调用效率)\n"
                "为减少决策轮数、更快完成任务：\n"
                "- **一次决策可返回多个独立工具调用**：当多个操作互不依赖、可同时推进时（如搜索多个不同主题、读取多个文件、并行查询多个来源），在同一次回复里一次性返回多个 tool_call，不要逐个串行。\n"
                "- 保持连续的工具调用链条：前一个工具的结果刚产生、下一步动作明确时，直接继续调用下一个工具，不要中途停顿或重复陈述。\n"
                "- 避免不必要的中间回复：除非需要用户澄清，否则持续调用工具直到任务完成，再输出最终结果。\n"
                "- 只调用完成任务真正需要的工具，不做多余的探索。\n\n"
                "## Role Boundary (角色边界)\n"
                "区分「项目内问题」和「通用技术问题」：\n"
                "- 项目内：用户明确提到 Scout、本项目的路由/记忆/工具等 → 可结合项目上下文回答\n"
                "- 通用：用户问技术概念、架构设计、行业方案、编程问题等 → 以通用技术专家身份回答，**不要提及 Scout Agent、不要关联本项目的实现细节**\n"
                "- 回答通用问题时，禁止使用「在 Scout Agent 中」「根据我们的架构」「我们的路由系统」等表述\n\n"
                "## Core Principles\n"
                "1. **事实优先**: 当用户询问事实，先搜索再回答。搜索结果中的官方文档优先采信。\n"
                "2. **搜索结果解读**: 重点看 URL 域名、📆 日期、摘要。官方文档 > 博客。如果搜索结果已包含答案，直接引用。\n"
                "3. **信息综合**: 多条结果交叉验证，标注来源。\n\n"
                "## Search Strategy (搜索策略)\n"
                "避免无效重复搜索：\n"
                "- 同一目标不要反复用相似关键词重试。若一次搜索未获有用结果，**改变策略**而非换词重试：\n"
                "  1) 直接 web_fetch 访问相关官方域名（官网、文档站、arxiv）的已知或推测 URL；\n"
                "  2) 用 site: 限定域名，或改用英文关键词；\n"
                "  3) 换一个真正不同的搜索角度（作者、平台、时间、具体术语），而不是同义改写。\n"
                "- 搜索失败通常意味着内容可能无公开来源：此时如实告知用户「该内容未找到可靠的公开资料」，并基于已有信息继续，不要无限重试。\n"
                "- 如果 web_search 返回「搜索重试已达上限」提示，立即停止搜索并改用上述策略。\n\n"
                "## Memory Rules\n"
                '- 用户说"记住"、"以后"、"总是"时 → memory_save\n'
                "- 不确定时先 memory_search\n\n"
                '用与用户输入相同的语言回复。回答结构清晰。不确定时说"不确定"，不要编造。\n'
                "除非用户明确要求发送文件，否则不要主动生成或发送文件，直接以文本回复。\n"
                )

            # ── 外部传入的自定义 system_prompt（配置/调用方传入，内容可能变化）──
            # 统一追加到模板末尾，保持前缀稳定：不再整体替换，
            # 避免配置内容每次变化都导致整段前缀缓存失效。
            if system_prompt:
                self.system_prompt += "\n\n## Additional User Instructions\n" + system_prompt

        # ── 平台感知：注入操作系统信息，让 Agent 用正确的 shell 语法 ──

        # 从源头避免 Windows 下反复生成 Linux 命令（ls/cat/python3）导致连续失败

        from scout.core.platform import get_platform_prompt

        self.system_prompt = get_platform_prompt() + self.system_prompt

        # ── 回复语言控制（zh/en/auto）──

        # 覆盖默认的"跟随用户"规则，实现强制中英文切换

        self.language = language

        lang_rule = {
            "zh": (
                "## Response Language (强制)\n"
                "无论用户输入什么语言，你必须始终用简体中文回复。"
                "代码、专有名词、API 名称可保留英文。\n"
            ),
            "en": (
                "## Response Language (FORCED)\n"
                "Always respond in English, regardless of the user's input language. "
                "Code, technical terms, and API names may remain as-is.\n"
            ),
        }.get(language, "")  # auto：不注入，保持默认"跟随用户输入语言"

        if lang_rule:
            self.system_prompt = self.system_prompt + "\n" + lang_rule

        self.callbacks = callbacks or NullCallbacks()

        self.max_turns = max_turns

        self.max_loop_seconds = max(1, int(max_loop_seconds or 600))

        self.temperature = temperature

        # ── 双模型已移除（2026-08-14），恒为 None ──

        self.thinker_llm = None

        self.executor_llm = None

        # ── 子代理委派控制 ──

        self.delegate_depth = delegate_depth

        self.max_delegate_depth = max_delegate_depth

        # 排除的工具集合（子代理不应拥有委派类工具，防止无限递归）

        self._exclude_tools: set[str] = exclude_tools or set()

        # 白名单工具集合（CLI 精简模式：仅暴露核心工具）

        self.allow_tools: set[str] | None = allow_tools

        # 自动发现并注册工具

        if tools is None:
            ToolRegistry.discover()

        self._tool_schemas: list[dict] = (
            ToolRegistry.schemas(
                exclude=self._exclude_tools,
                allow=self.allow_tools,
                compact=True,
            )
            if tools is None
            else tools.schemas(exclude=self._exclude_tools)
        )

        # 渐进式工具加载（2026-08-19）：默认全量，_inject_context 时按输入筛选。
        # 兜底：未走 _inject_context 的调用仍用全量工具，避免工具缺失。
        self._active_tool_schemas: list[dict] = list(self._tool_schemas)

        # 注册主 Agent 引用（供 delegate_task 工具使用）— 子代理不覆盖主引用

        if register_as_main:
            ToolRegistry._main_agent = self

        # 上下文治理

        self.enable_context = enable_context

        if enable_context:
            from scout.context.manager import ContextManager

            # token 预算从配置读取（2026-08-30）：SCOUT_CONTEXT_MAX_TOKENS
            # 或 config.context_max_tokens，默认 0=仅按条数治理
            _max_tokens = 0
            try:
                from scout.config.manager import ConfigManager

                _cfg = ConfigManager().load()
                _max_tokens = int(getattr(_cfg, "context_max_tokens", 0) or 0)
            except Exception:
                _max_tokens = 0
            self.context_mgr = ContextManager(max_tokens=_max_tokens)

        else:
            self.context_mgr = None

        # 会话持久化

        self.enable_persistence = enable_persistence

        if enable_persistence:
            from scout.session.store import get_session_store

            # 工厂：SCOUT_SESSION_STORE=spi → 插件提供 session 实现
            self.session_store = get_session_store()

        else:
            self.session_store = None

        # 记忆系统

        self.enable_memory = enable_memory

        if enable_memory:
            from scout.memory.store import get_memory_store

            # 工厂：SCOUT_MEMORY_STORE=spi → 插件提供 memory 实现
            self.memory_store = get_memory_store()

            # 注入嵌入模型：

            # - EMBEDDING_DISABLED 哨兵 → 显式关闭向量检索（纯文本模式）

            # - 显式注入 provider → 按配置使用（local/API）

            # - 未注入（None）→ 退回本地 ONNX（开箱即用，无需 API Key）

            from scout.memory.vector.embeddings import EMBEDDING_DISABLED

            if embedding_provider is EMBEDDING_DISABLED:
                self._embedding_provider = None

            elif embedding_provider is not None:
                self._embedding_provider = embedding_provider

                self.memory_store.set_embedding_provider(embedding_provider)

            else:
                # 未注入 embedding provider → 纯文本检索（本地 ONNX 已移除）
                self._embedding_provider = None

        else:
            self.memory_store = None

            self._embedding_provider = None

        # ── 上下文/记忆工程化（E4，2026-08-27）──
        # memory_extractor：会话结束时把关键信息沉淀为长期记忆；
        # context_assembler：新回合组装跨会话记忆 + 历史会话摘要；
        # memory_flush：压缩前抽取关键记忆（未显式注入时，若已有
        # memory_extractor 则自动包装，实现压缩前 flush 闭环）。
        # 三者均为可选注入；未注入时保持原有单会话治理行为。
        self.memory_extractor = memory_extractor
        self.context_assembler = context_assembler
        if memory_flush is None and memory_extractor is not None:
            from scout.context.memory_flush import MemoryFlush

            memory_flush = MemoryFlush(extractor=memory_extractor)
        self.memory_flush = memory_flush

        # 安全层

        self.enable_security = enable_security

        if enable_security:
            from scout.security.policy import SecurityManager

            self.security = SecurityManager(auto_approve=auto_approve)

        else:
            self.security = None

        # 沙箱管理器（2026-08-27 强化：支持 env 配置模式与强制 Docker 检查）
        # SCOUT_SANDBOX_MODE=off|non-main|all   （默认 off，保持兼容）
        # SCOUT_SANDBOX_REQUIRE_DOCKER=1        沙箱开启但 Docker 不可用 → 硬失败，不静默回退

        from scout.security.sandbox import SandboxManager, SandboxMode

        _sandbox_mode = os.environ.get("SCOUT_SANDBOX_MODE", "off").lower()
        try:
            _sb_mode = SandboxMode(_sandbox_mode)
        except ValueError:
            logger.warning("未知 SCOUT_SANDBOX_MODE=%s，回退 off", _sandbox_mode)
            _sb_mode = SandboxMode.OFF
        self.sandbox_mgr = SandboxManager(mode=_sb_mode)

        # 自修复循环

        self.enable_self_heal = enable_self_heal

        self.max_heal_retries = max_heal_retries

        if enable_self_heal:
            from scout.engine.heal_loop import SelfHealLoop

            self.heal_loop = SelfHealLoop(llm=self.llm, max_retries=max_heal_retries)

        else:
            self.heal_loop = None

        # 技能沉淀系统（向量检索 + 自动合成）

        self.skill_synthesizer = None

        self.skill_retriever = None

        if enable_self_heal:  # 技能沉淀依赖自愈循环
            try:
                from scout.engine.skill_synthesizer import SkillSynthesizer

                from scout.engine.skill_retriever import SkillRetriever

                from scout.engine.skill_store import VectorSkillStore

                # 复用记忆系统的本地 ONNX 嵌入（语义检索才是真检索；

                # 不传的话 VectorSkillStore 会退回 hash 假向量，检索形同随机）

                self._skill_store = VectorSkillStore(
                    embedding_provider=self._embedding_provider,
                )

                self.skill_synthesizer = SkillSynthesizer(
                    skill_store=self._skill_store,
                    llm_client=self.llm,
                )

                self.skill_retriever = SkillRetriever(
                    skill_store=self._skill_store,
                )

            except Exception as _e:
                import logging

                logging.getLogger(__name__).warning(f"Skill synthesis init failed: {_e}")

        # 技能系统

        self.enable_skills = enable_skills

        if enable_skills:
            from scout.context.skills import SkillManager

            self.skill_mgr = SkillManager()

        else:
            self.skill_mgr = None

        # 工作空间

        self.enable_workspace = enable_workspace

        if enable_workspace:
            from scout.context.workspace import Workspace

            self.workspace = Workspace(
                workspace_dir if workspace_dir is not None else str(_SCOUT_DATA_DIR / "workspace")
            )

            # 用工作空间内容增强 system prompt
            # ── 前缀稳定：工作空间内容（AGENT.md/USER.md/RULE.md）属于外部变动内容，
            #    统一追加到 system prompt 末尾，避免前置导致整段前缀缓存失效 ──
            ws_prompt = self.workspace.get_system_prompt()

            if ws_prompt:
                self.system_prompt = (
                    self.system_prompt
                    + "\n\n# 工作空间指令（AGENT.md / USER.md / RULE.md）\n"
                    + ws_prompt
                )

        else:
            self.workspace = None

        # 事件总线

        self.enable_bus = enable_bus

        if enable_bus:
            from scout.bus.hub import bus

            self.bus = bus

        else:
            self.bus = None

        # 搜索重试检测（2026-08-19）：记录每个 session 的 web_search 历史，
        # 检测"同一目标"重复搜索，避免 agent 陷入无效重试循环。
        # key: session_id -> list[规范化 query]
        self._search_history: dict[str, list[str]] = {}
        # 同一目标连续搜索达到该次数后，返回"换策略"提示
        self.search_retry_limit = 3

        # 工具调用累计统计（2026-08-20）：独立于 session.messages 维护，
        # 不受上下文剪枝（prune_tool_outputs）物理删除影响，供预算耗尽总结使用。
        # key: session_id -> {total, ok, fail, tools(成功名次计), fail_tools(失败名次计),
        #                      snippets(最近成功输出的信息片段，<=3 条，各<=300 字符)}
        # 每个 turn 开始时重置（见 run_conversation/stream_conversation 开头）。
        self._tool_stats: dict[str, dict] = {}

        # 流式 usage（供省钱提示）——每轮开始时重置

        self._last_stream_usage = None

        # 可中断执行器

        self.executor = InterruptibleExecutor()

        self._cancelled = False  # 用户取消标志

        # ── 重复动作检测（2026-08-12）──

        # 防止 agent 陷入"反复执行相同工具调用"的无限循环（如反复搜索同一路径）。

        # 记录最近 N 次工具调用的指纹，连续重复达到阈值时注入打断提示。

        self._recent_tool_calls: list[dict] = []  # [{"tool": str, "sig": str}, ...]

        self._loop_break_injected = False  # 本轮是否已注入过循环打断提示（避免重复注入）

        # ── 第一梯队能力初始化 ──

        # 反思循环

        self.enable_reflexion = enable_reflexion

        if enable_reflexion:
            from scout.engine.reflexion import ReflexionLoop, ReflexionState

            self.reflexion_loop = ReflexionLoop(
                llm=self.llm,
                enable_deep_reflect=True,
                failure_threshold=2,
                progress_interval=10,  # 进度检查从每5步放宽到每10步，减少过度反思
            )

        else:
            self.reflexion_loop = None

        self.reflexion_state = None  # 每轮对话创建新状态

        # 目标管理

        self.enable_goal_manager = enable_goal_manager

        if enable_goal_manager:
            from scout.engine.goal_manager import GoalManager

            self.goal_manager = GoalManager(llm=self.llm)

        else:
            self.goal_manager = None

        # 可观测性

        self.enable_observability = enable_observability

        if enable_observability:
            from scout.engine.observability import ObservabilityTracker

            self.observability = ObservabilityTracker()

        else:
            self.observability = None

        # Human-in-the-Loop

        self.enable_hitl = enable_hitl

        if enable_hitl:
            self.hitl_tools = set(hitl_tools or ["shell", "execute_code"])

        else:
            self.hitl_tools = set()

        # ── P0/P1 能力增强（2026-08-13，对标 Harness/Codex/Hermes）──

        # 自动化策略（无人值守运行时由 AutomationRunner 注入；None=交互模式）

        self.automation_policy = None

        self.auto_run_meta: dict = {}

        # 工作流技能蒸馏（Hermes 四触发条件；依赖文件技能系统）

        self.workflow_distiller = None

        if enable_skills and self.skill_mgr:
            try:
                from scout.engine.workflow_distiller import WorkflowDistiller

                self.workflow_distiller = WorkflowDistiller(
                    skill_mgr=self.skill_mgr,
                    llm_client=self.llm,
                )

            except Exception as _e:
                import logging

                logging.getLogger(__name__).warning(f"WorkflowDistiller init failed: {_e}")

        # 周期性自省（技能库治理 + 记忆合并审查）

        try:
            from scout.engine.introspection import IntrospectionLoop

            self.introspection = IntrospectionLoop(
                llm_client=self.llm,
                skill_store=getattr(self, "_skill_store", None),
                skill_mgr=self.skill_mgr,
                memory_store=self.memory_store,
            )

        except Exception as _e:
            import logging

            logging.getLogger(__name__).warning(f"IntrospectionLoop init failed: {_e}")

            self.introspection = None

        # 记忆治理闸门（use_memories 注入开关）

        try:
            from scout.memory.governance import GenerationGate

            self.memory_gate = GenerationGate()

        except Exception:
            self.memory_gate = None

        # 分层指令链（对标 Codex AGENTS.md：全局→项目→目录 override 链）

        try:
            from scout.context.instructions import InstructionLoader

            from pathlib import Path as _Path

            _chain = InstructionLoader().build(working_dir=_Path.cwd())

            if _chain.combined:
                self.system_prompt = (
                    self.system_prompt
                    + "\n\n# 项目指令（Instruction Chain，就近文件可覆盖全局约定）\n"
                    + _chain.combined
                )

                self._instruction_chain = _chain

            else:
                self._instruction_chain = None

        except Exception as _e:
            import logging

            logging.getLogger(__name__).debug(f"Instruction chain skipped: {_e}")

            self._instruction_chain = None

        # Checkpoint 系统

        from scout.engine.checkpoint import CheckpointManager

        self.checkpoint_manager = CheckpointManager()

        # A2A 协议支持

        from scout.a2a.client import A2AManager

        self.a2a_manager = A2AManager()

    def cancel(self):
        """用户取消当前对话 — 设置标志位，循环会在下一步检查后退出."""

        self._cancelled = True

        self.executor.cancel_all()

    def _reset_cancel(self):
        """重置取消标志（新一轮对话前调用）."""

        self._cancelled = False

    def _get_enabled_plugins(self) -> list:
        """获取已启用插件实例（统一走 scout.plugins 正式版管理器，失败静默）."""

        try:
            from scout.plugins.manager import get_plugin_manager

            pm = get_plugin_manager()
            return [
                p
                for n in [p["name"] for p in pm.list_plugins()]
                if (p := pm.get_plugin(n)) is not None and getattr(p, "enabled", True)
            ]
        except Exception:
            import logging

            logging.getLogger(__name__).debug("插件加载失败，跳过运行时钩子", exc_info=True)
            return []

    async def _run_plugin_before_chat(self, message: str, session_id: str) -> str:
        """调用所有已启用插件的 before_chat 钩子（可改写用户消息）."""

        for plugin in self._get_enabled_plugins():
            try:
                new_msg = await plugin.before_chat(message, session_id)
                if new_msg is not None:
                    message = str(new_msg)
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    f"插件 {getattr(plugin, 'name', '?')} before_chat 失败", exc_info=True
                )
        return message

    async def _run_plugin_on_message(self, role: str, content: str, session_id: str) -> None:
        """调用所有已启用插件的 on_message 钩子（消息记录通知）."""

        for plugin in self._get_enabled_plugins():
            try:
                await plugin.on_message(role, content, session_id)
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    f"插件 {getattr(plugin, 'name', '?')} on_message 失败", exc_info=True
                )

    async def _run_plugin_after_chat(self, message: str, response: str, session_id: str) -> str:
        """调用所有已启用插件的 after_chat 钩子（可改写助手回复）."""

        for plugin in self._get_enabled_plugins():
            try:
                new_resp = await plugin.after_chat(message, response, session_id)
                if new_resp is not None:
                    response = str(new_resp)
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    f"插件 {getattr(plugin, 'name', '?')} after_chat 失败", exc_info=True
                )
        return response

    @staticmethod
    def _parse_heal_args(value) -> dict:
        """安全解析 heal 记录中的工具参数（兼容 dict 与 str 两种存储格式）.

        历史数据以 str() 形式存储，此处用 ast.literal_eval 仅解析字面量，
        绝不执行任意代码（修复 2026-08-20: 原 eval() 存在 RCE 风险）。
        解析失败（截断/非法）返回 {}，由调用方兜底跳过技能合成。
        """
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            try:
                parsed = ast.literal_eval(value)
                return parsed if isinstance(parsed, dict) else {}
            except (ValueError, SyntaxError, TypeError, MemoryError):
                return {}
        return {}

    def _prepare_turn_state(self, session: Session) -> IterationBudget:
        """公共前置：本轮 budget 初始化 + 工具统计计数器重置（run/stream 共用）."""

        budget = IterationBudget(max_turns=self.max_turns)
        self._tool_stats[session.id] = {
            "total": 0, "ok": 0, "fail": 0, "tools": {}, "fail_tools": {}, "snippets": [],
        }
        return budget

    async def resume_from_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        """从 checkpoint 恢复执行.



        Args:

            session_id: 会话 ID



        Returns:

            恢复结果或 None（如果没有 checkpoint）

        """

        if not self.checkpoint_manager:
            return None

        checkpoint = self.checkpoint_manager.load_checkpoint(session_id)

        if not checkpoint:
            return None

        # 从 checkpoint 恢复会话状态

        session = Session(id=session_id)

        for msg_dict in checkpoint.messages_snapshot:
            session.messages.append(
                Message(
                    role=Role(msg_dict["role"]),
                    content=msg_dict.get("content", ""),
                    reasoning=msg_dict.get("reasoning", ""),
                    metadata=msg_dict.get("metadata", {}),
                )
            )

        # 继续执行（从 checkpoint 的步数继续）

        remaining_budget = checkpoint.budget_max - checkpoint.budget_used

        if remaining_budget <= 0:
            return {
                "status": "error",
                "message": "预算已耗尽，无法恢复",
                "checkpoint": checkpoint.to_dict(),
            }

        # 通知前端恢复开始

        await self.callbacks.on_status("resuming")

        await self.callbacks.on_tool_progress(
            "checkpoint",
            "resume",
            f"从步骤 {checkpoint.budget_used} 恢复，剩余预算 {remaining_budget}",
        )

        # 继续执行对话（简化版，实际应该调用 stream_conversation）

        # 这里返回 checkpoint 信息，让前端决定是否继续

        return {
            "status": "ready",
            "checkpoint": checkpoint.to_dict(),
            "remaining_budget": remaining_budget,
            "message": f"已加载 checkpoint，从步骤 {checkpoint.budget_used} 恢复",
        }

    async def run_conversation(
        self,
        user_message: str,
        session: Session | None = None,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        """核心对话入口 — 按 self.loop 策略分发（ReAct 默认 / DAG 可插拔）."""
        return await self.loop.run(user_message, session, attachments)

    async def _run_react(
        self,
        user_message: str,
        session: Session | None = None,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        """核心对话循环（ReAct 实现，供 ReActLoop / DAGLoop 调用）.



        Args:

            user_message: 用户输入

            session: 会话状态（为 None 则创建新会话）



        Returns:

            {"response": str, "session": Session, "steps": int}

        """

        # 初始化会话

        if session is None:
            session = Session(id=str(uuid.uuid4()))

        # ── 复位取消状态：防止上一轮取消标记泄漏到本轮（非流式对话） ──
        self._reset_cancel()

        # ── Turn 用量统计：记录开始时间，结束时聚合本次回合的 token/缓存/耗时 ──
        _turn_start_ts = time.time()
        _turn_usage = {
            "tokens": 0,
            "prompt": 0,
            "completion": 0,
            "cached": 0,
            "calls": 0,
            "latency_ms": 0,
        }

        # 事件: 对话开始

        if self.bus:
            await self.bus.emit(
                "conversation.start", {"session_id": session.id, "message": user_message}
            )

        # ── 插件钩子：before_chat（可改写用户消息） → on_message ──
        user_message = await self._run_plugin_before_chat(user_message, session.id)
        await self._run_plugin_on_message("user", user_message, session.id)

        # 注入本轮上下文（记忆召回 → 技能匹配 → 追加用户消息；保留历史记忆以稳定缓存前缀）

        await self._inject_context(session, user_message, attachments)

        # 模型选择由 deep_thinking 开关直接控制（见下方 ReAct 循环）。

        # 上下文压缩（如果需要）

        if self.enable_context and self.context_mgr:
            if self.context_mgr.needs_compression(session):
                await self.callbacks.on_status("compressing")

                compress_info = await self.context_mgr.compress(
                    session, self.llm, memory_flush=self.memory_flush
                )

                if compress_info.get("compressed"):
                    await self.callbacks.on_tool_progress(
                        "context",
                        "done",
                        f"压缩 {compress_info['removed']} 条消息",
                    )

        budget = self._prepare_turn_state(session)

        # ReAct 循环
        # ── 2026-08-28：回合总时长看门狗 ──
        # 防止 LLM/工具单点挂起或轮数爆炸导致整个回合无限执行（曾出现 60+ 分钟
        # 无响应卡死）。超时后走下方预算耗尽/强制总结路径，保证本轮必返回。
        _turn_deadline = time.monotonic() + self.max_loop_seconds

        while not budget.exhausted:
            if self._cancelled:
                break

            if time.monotonic() > _turn_deadline:
                logging.getLogger(__name__).warning(
                    "对话回合超过 %ss 上限（max_turns=%s），强制收尾",
                    self.max_loop_seconds, budget.max_turns,
                )
                break

            budget.tick()

            await self.callbacks.on_step(budget.current, budget.max_turns)

            # 1. Think: 构建 API 消息并调用 LLM

            await self.callbacks.on_thinking(True)

            await self.callbacks.on_status("thinking")

            try:
                api_messages = self._build_api_messages(session)

                # 模型选择：单模型（双模型已移除 2026-08-14）

                _active = self.llm

                # 带工具时关思考：思考模型(qwen3.7-plus 等)在思考模式下会把工具调用写成

                # XML 文本标记塞进 content 而非结构化 tool_calls，框架解析不到→工具不执行+吐原始标记。

                # 非流式路径(非流式 API / Multi-Agent 子代理 / messenger)都走这里，统一加固。

                # 注意：部分模型(如 qwen3.8-max-preview)强制 thinking=True，传 False 会返回 400，

                # 需捕获后退回默认(不带 enable_thinking)重试，避免误伤这类模型。

                _tools = self._active_tool_schemas if self._active_tool_schemas else None

                _ck = dict(
                    messages=api_messages,
                    tools=_tools,
                    temperature=self._compute_temperature(_active),
                    _role="main",
                    _session_id=session.id,
                )

                if _tools:
                    # 按 deep_thinking 控制思维链（快速关闭，思考开启）
                    # Multi-Agent 编排模式化，关闭思考加速（子代理已 deep_thinking=False）

                    _ck["extra_body"] = {
                        "enable_thinking": True
                        if (self.deep_thinking and self.agent_mode != "multi_agent")
                        else False
                    }

                try:
                    response = await _active.complete(**_ck)

                except Exception as _te:
                    if _tools and "enable_thinking" in str(_te):
                        _ck.pop("extra_body", None)  # 该模型不允许关思考，退回默认重试

                        response = await _active.complete(**_ck)

                    else:
                        raise

            except Exception as e:
                await self.callbacks.on_thinking(False)

                # ── 主循环重试机制：LLM 调用失败时自动重试 ──

                if not hasattr(self, "_llm_retry_count"):
                    self._llm_retry_count = 0

                max_llm_retries = 2

                if self._llm_retry_count < max_llm_retries:
                    self._llm_retry_count += 1

                    await self.callbacks.on_status("retrying")

                    await self.callbacks.on_thinking(
                        True,
                        f"LLM 调用失败，第 {self._llm_retry_count}/{max_llm_retries} 次重试...",
                    )

                    # 短暂延迟后重试

                    await asyncio.sleep(1.0)

                    continue  # 回到循环开头重试

                # 重试次数用尽，返回错误

                self._llm_retry_count = 0  # 重置计数器

                await self.callbacks.on_status("error")

                error_msg = f"LLM 调用失败 (已重试 {max_llm_retries} 次): {e}"

                session.messages.append(Message(role=Role.ASSISTANT, content=error_msg))

                session.status = "error"

                if self.enable_persistence and self.session_store:
                    self.session_store.save_session(session)

                if self.bus:
                    await self.bus.emit(
                        "conversation.error",
                        {
                            "error": str(e),
                            "retries": max_llm_retries,
                        },
                    )

                return {"response": error_msg, "session": session, "steps": budget.current}

            finally:
                # 注意：不能在 finally 中重置 _llm_retry_count！
                # Python 的 continue 会先执行 finally，若在此清零，计数器永远到不了上限 → 无限重试。
                await self.callbacks.on_thinking(False)

            # LLM 调用成功返回 → 仅走成功路径重置重试计数器
            if hasattr(self, "_llm_retry_count") and self._llm_retry_count > 0:
                self._llm_retry_count = 0

            # 2. Act: 解析响应

            if response.tool_calls:
                # 先记录 assistant 的工具调用消息

                tool_call_meta = []

                for idx, tc in enumerate(response.tool_calls):
                    call_id = f"call_{budget.current}_{idx}"

                    tool_call_meta.append({**tc.model_dump(), "call_id": call_id})

                session.messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content="",
                        reasoning=response.reasoning or "",
                        metadata={"tool_calls": tool_call_meta},
                    )
                )

                # ── 策略④：并行工具调用（保体验）──

                # 实时任务最怕串行等待。多个独立读工具调用（查天气+查汇率）用

                # asyncio.gather 并发执行，延迟从"串行之和"降到"最慢一个"。

                # 安全约束：仅对"纯读工具"（ToolCache 判据）并行；含副作用/需审批

                # 的写工具保持串行——避免并发写 session 历史导致 tool_call_id 顺序错乱。

                _read_tcs = []

                _write_tcs = []

                # 并行工具执行（2026-08-19 修复）：按 pure_read 标记区分。
                # 纯读工具（web_search/web_fetch/memory_search/vision）无副作用，
                # 用 asyncio.gather 并发执行，多工具轮延迟从"串行之和"降到"最慢一个"；
                # 含副作用的写工具保持串行，避免并发写 session 历史导致顺序错乱。
                from scout.tools.registry import ToolRegistry as _TR

                for _i, _tc in enumerate(response.tool_calls):
                    _tool_cls = _TR.get_tool(_tc.name)
                    if _tool_cls is not None and getattr(_tool_cls, "pure_read", False):
                        _read_tcs.append((_i, _tc))
                    else:
                        _write_tcs.append((_i, _tc))

                if _read_tcs:
                    await asyncio.gather(
                        *[
                            self._execute_single_tool(session, tc, f"call_{budget.current}_{idx}")
                            for idx, tc in _read_tcs
                        ]
                    )

                for idx, tc in sorted(_write_tcs, key=lambda x: x[0]):
                    await self._execute_single_tool(session, tc, f"call_{budget.current}_{idx}")

                # 工具执行后剪枝（被移除的消息归档，保证历史可追溯，2026-08-20）

                if self.enable_context and self.context_mgr:
                    _removed = self.context_mgr.prune_tool_outputs(session)
                    if _removed and self.enable_persistence and self.session_store:
                        try:
                            await self.session_store.async_archive_messages(
                                session.id, _removed, reason="context_prune"
                            )
                        except Exception:
                            import logging
                            logging.getLogger(__name__).debug(
                                "归档被剪枝消息失败", exc_info=True
                            )

                # ── Checkpoint：工具执行后保存状态（每3步，与 stream 路径一致，2026-08-31）──
                if self.checkpoint_manager and budget.current % 3 == 0:
                    try:
                        _ckpt_tools = []
                        for _m in session.messages:
                            if _m.role == Role.TOOL:
                                _ckpt_tools.append(
                                    {
                                        "tool_name": _m.metadata.get("tool_name", ""),
                                        "call_id": _m.metadata.get("call_id", ""),
                                        "success": _m.metadata.get("success", False),
                                    }
                                )
                        self.checkpoint_manager.save_checkpoint(
                            session_id=session.id,
                            step=budget.current,
                            status="acting",
                            messages=[_m.to_api_dict() for _m in session.messages],
                            pending_tools=[],
                            completed_tools=_ckpt_tools,
                            budget_used=budget.current,
                            budget_max=budget.max_turns,
                            context_summary=f"已执行 {len(_ckpt_tools)} 个工具调用",
                        )
                    except Exception as _ckpt_err:
                        logging.getLogger(__name__).warning(
                            f"保存 checkpoint 失败: {_ckpt_err}"
                        )

                continue

            else:
                # 无工具调用，直接回复

                _final_content = response.content or ""

                reply = Message(
                    role=Role.ASSISTANT,
                    content=_final_content,
                    reasoning=response.reasoning,
                )

                session.messages.append(reply)

                session.status = "done"

                await self.callbacks.on_status("done")

                # 2026-08-14: 语义缓存已移除（命中率低+实时性腐蚀），不再写回

                if self.enable_persistence and self.session_store:
                    self.session_store.save_session(session)

                if self.bus:
                    await self.bus.emit(
                        "conversation.complete",
                        {
                            "session_id": session.id,
                            "steps": budget.current,
                            "response": (response.content or "")[:500],
                            "automated": bool(self.auto_run_meta),
                        },
                    )

                # ── P1 工作流蒸馏：任务完成后评估四触发条件（后台执行不阻塞回复）──

                if self.workflow_distiller:
                    _um, _fr = user_message, (response.content or "")

                    async def _distill_task():

                        try:
                            r = await self.workflow_distiller.on_task_complete(_um, _fr)

                            if r and r.get("saved"):
                                if self.bus:
                                    await self.bus.emit(
                                        "notification",
                                        {
                                            "type": "skill_distilled",
                                            "title": f"🧠 已沉淀新技能: {r.get('skill')}",
                                            "message": f"触发原因: {'; '.join(r.get('reasons', []))}",
                                        },
                                    )

                        except Exception:
                            pass

                    # ★ 2026-09-01：create_task 需持引用，否则任务可能被 GC 静默丢弃
                    if not hasattr(self, "_bg_tasks"):
                        self._bg_tasks: set = set()
                    _t = asyncio.create_task(_distill_task())
                    self._bg_tasks.add(_t)
                    _t.add_done_callback(self._bg_tasks.discard)

                # ── P1 周期性自省计数（后台，不阻塞回复）──

                if self.introspection:
                    self.introspection.add_turns(budget.current)

                    async def _maybe_introspect():

                        try:
                            await self.introspection.maybe_run()

                        except Exception:
                            pass

                    # ★ 2026-09-01：create_task 需持引用，否则任务可能被 GC 静默丢弃
                    if not hasattr(self, "_bg_tasks"):
                        self._bg_tasks: set = set()
                    _t = asyncio.create_task(_maybe_introspect())
                    self._bg_tasks.add(_t)
                    _t.add_done_callback(self._bg_tasks.discard)

                # ── 插件钩子：after_chat（可改写助手回复） ──
                _resp_text = await self._run_plugin_after_chat(
                    user_message, response.content, session.id
                )

                # ── E4 跨会话记忆抽取（2026-08-27）：会话结束沉淀关键记忆 ──
                await self._maybe_extract_session_memory(session)

                return {
                    "response": _resp_text,
                    "session": session,
                    "steps": budget.current,
                    "usage": self._collect_turn_usage(session.id, _turn_start_ts, _turn_usage),
                }

        # 预算耗尽

        budget_msg = self._build_budget_exhausted_msg(session, budget.current)

        # ── 2026-08-20：预算耗尽强制总结（与 stream 路径一致）──
        forced = ""
        if (
            not self._cancelled
            and self.llm is not None
            and (self._tool_stats.get(session.id, {}).get("total", 0) or 0) > 0
        ):
            forced = await self._force_final_output(session)

        if forced:
            final_text = (
                forced
                + "\n\n---\n\n⚠️ 本轮已达到步数上限（"
                + str(self.max_turns)
                + " 步），以上成果已基于本轮获取的信息生成。如需继续完善可回复「继续」。"
            )
        else:
            final_text = budget_msg

        session.messages.append(Message(role=Role.ASSISTANT, content=final_text))

        session.status = "done"

        await self.callbacks.on_status("done")

        if self.enable_persistence and self.session_store:
            self.session_store.save_session(session)

        # ── 插件钩子：after_chat（可改写助手回复） ──
        final_text = await self._run_plugin_after_chat(user_message, final_text, session.id)

        # ── E4 跨会话记忆抽取（2026-08-27）：预算耗尽路径同样沉淀 ──
        await self._maybe_extract_session_memory(session)

        return {"response": final_text, "session": session, "steps": budget.current}

    def _collect_turn_usage(self, session_id: str, turn_start_ts: float, _acc: dict) -> dict:
        """聚合本次回合（run_conversation 期间）的 LLM 用量统计.

        通过 usage.db 查询该 session 在 [turn_start, now] 时间窗内的记录
        （session_id 匹配 + 时间窗过滤，近似本次 turn 的调用）。
        """
        try:
            from scout.llm.tracker import token_tracker
            from datetime import datetime

            rows = token_tracker._query(
                """SELECT
                       SUM(prompt_tokens) as prompt, SUM(completion_tokens) as completion,
                       SUM(total_tokens) as total, SUM(cached_tokens) as cached,
                       COUNT(*) as calls, AVG(latency_ms) as avg_latency
                   FROM llm_usage
                   WHERE session_id = ? AND timestamp >= ?
                     AND timestamp <= ?""",
                (
                    session_id,
                    datetime.fromtimestamp(turn_start_ts).isoformat(),
                    datetime.now().isoformat(),
                ),
            )
            r = rows[0] if rows else {}
            total = r.get("total") or 0
            prompt = r.get("prompt") or 0
            cached = r.get("cached") or 0
            rate = round(cached / prompt, 4) if prompt else 0.0
            source = "api"
            # ── 兜底：API 未返回真实 cached 时用本地前缀稳定率推断（2026-08-16）──
            if rate == 0.0:
                try:
                    from scout.llm.prompt_cache import get_prompt_cache_optimizer

                    local_rate = get_prompt_cache_optimizer().get_session_hit_ratio(session_id)
                    if local_rate is not None:
                        rate = local_rate
                        source = "local"
                except Exception:
                    pass
            usage = {
                "tokens": int(total),
                "prompt": int(prompt),
                "completion": int(r.get("completion") or 0),
                "cached": int(cached),
                "cache_hit_rate": rate,
                "cache_source": source,
                "calls": int(r.get("calls") or 0),
                "avg_latency_ms": int(r.get("avg_latency") or 0),
            }
            return usage
        except Exception:
            return {"tokens": 0, "calls": 0, "cache_hit_rate": 0.0, "avg_latency_ms": 0}

    async def stream_conversation(
        self,
        user_message: str,
        session: Session | None = None,
        attachments: list[dict] | None = None,
    ):
        """流式对话循环 — 逐字推送文本 + 工具执行追踪.



        Yields:

            Delta: {"text": str, "tool_calls": list, "done": bool}

        """

        from scout.core.types import Delta

        self._reset_cancel()

        if session is None:
            session = Session(id=str(uuid.uuid4()))

        # ── 模型选择（Zero-Waste Architecture：智能路由 + 语义缓存） ──

        # Multi-Agent 模式：主 agent 固定 thinker（编排决策），子 agent 固定 executor（执行）

        # 智能路由已移除（2026-08-14）：模型选择由 deep_thinking 开关控制

        self._is_executor_direct = True

        # ── 插件钩子：before_chat（可改写用户消息） → on_message ──
        user_message = await self._run_plugin_before_chat(user_message, session.id)
        await self._run_plugin_on_message("user", user_message, session.id)

        # 注入本轮上下文（记忆召回 → 技能匹配 → 追加用户消息；保留历史记忆以稳定缓存前缀）

        await self._inject_context(session, user_message, attachments)

        # ── 目标管理：注入相关目标上下文 ──

        if self.enable_goal_manager and self.goal_manager:
            goal_context = self.goal_manager.get_context_for_conversation(user_message)

            if goal_context:
                session.messages.append(
                    Message(
                        role=Role.SYSTEM,
                        content=goal_context,
                        metadata={"type": "goal_context"},
                    )
                )

        # ── 反思循环：初始化本轮状态 ──

        if self.enable_reflexion and self.reflexion_loop:
            from scout.engine.reflexion import ReflexionState

            self.reflexion_state = ReflexionState()

        # ── 可观测性：追踪整个对话 ──

        observability_trace = None

        if self.enable_observability and self.observability:
            observability_trace = self.observability.start_trace(
                session_id=session.id,
                user_message=user_message,
            )

        if self.enable_context and self.context_mgr:
            if self.context_mgr.needs_compression(session):
                await self.context_mgr.compress(
                    session, self.llm, memory_flush=self.memory_flush
                )

        budget = self._prepare_turn_state(session)

        # ── 2026-08-28：回合总时长看门狗（与 _run_react 一致）──
        _turn_deadline = time.monotonic() + self.max_loop_seconds

        while not budget.exhausted:
            if self._cancelled:
                break

            if time.monotonic() > _turn_deadline:
                logging.getLogger(__name__).warning(
                    "对话回合超过 %ss 上限（max_turns=%s），强制收尾",
                    self.max_loop_seconds, budget.max_turns,
                )
                break

            budget.tick()

            await self.callbacks.on_step(budget.current, budget.max_turns)

            await self.callbacks.on_thinking(True)

            await self.callbacks.on_status("thinking")

            try:
                api_messages = self._build_api_messages(session)

                collected_text = ""

                collected_reasoning = ""

                collected_tool_calls: list[ToolCall] = []

                # 模型选择：单模型（双模型已移除 2026-08-14）

                active_llm = self.llm

                # thinker 思考时不带 tools（qwen3.7-max thinking+tools 不兼容）

                # thinker 只做分析/规划，工具调用交给 executor。

                # ⚠️ 只有两阶段接力真正可用（thinker/executor 都配置了）时才能剥离工具；

                # 否则单个模型既没有结构化 tool-calling 能力、系统提示词又告诉它有工具，

                # 它会把工具调用写成 XML 文本直接吐给用户（工具不执行 + 内容重复显示）。

                # 2026-08-12 改造：thinker(qwen3.7-max) 实测支持结构化 tool_calls（多步带工具正常），

                # 因此不再走"thinker思考→executor执行"两阶段接力（两阶段导致 executor 降级输出 XML）。

                # 改为：thinker 路由 → thinker 单模型直接带工具干活（ReAct）；executor 路由 → executor 单模型。

                _two_stage_available = False  # 禁用两阶段接力（改用单模型带工具）

                # 工具分配：所有路由都带工具（单模型结构化 tool_calls）

                active_tools = self._active_tool_schemas if self._active_tool_schemas else None

                # 2026-08-12：带工具时禁用 enable_thinking——

                # 深度思考 + 工具调用会显著变慢甚至超时（实测 thinker 带工具+思考 90s 超时，

                # 关闭思考后 5-10s 正常）。单模型带工具干活走结构化 tool_calls，快且稳。

                # 2026-08-12 修复：按 deep_thinking 动态设置 enable_thinking，让"思考"模式真正开启思维链

                # （快速模式关闭思维链，思考模式开启思维链）。超时由 stream_timeout(300s) 保护，不会卡死。

                # 按 deep_thinking 开关控制思维链（思考模式开启，快速模式关闭）

                if self.deep_thinking and self.agent_mode != "multi_agent":
                    # Multi-Agent 编排是模式化任务（分解→委派→汇总），深度思考收益低、
                    # 却让每次 LLM 调用多花 5-15s 生成思维链 → 编排阶段关闭，提速
                    # （仅限有子代理承接推理的场景；单 Agent 深度问答仍保留思考）
                    active_extra = {"extra_body": {"enable_thinking": True}}

                else:
                    active_extra = {"extra_body": {"enable_thinking": False}}

                _stream_kwargs = dict(
                    messages=api_messages,
                    tools=active_tools,
                    temperature=self.temperature,
                    _role="main",
                    _session_id=session.id,
                    **active_extra,
                )

                _stream_kwargs = dict(
                    messages=api_messages,
                    tools=active_tools,
                    temperature=self._compute_temperature(active_llm),
                    _role="main",
                    _session_id=session.id,
                    **active_extra,
                )

                # 追踪单次 LLM 调用

                llm_span = None

                if self.enable_observability and self.observability and observability_trace:
                    llm_name = "main"

                    llm_span = self.observability.start_span(
                        trace_id=observability_trace.id,
                        span_type="llm",
                        name=llm_name,
                    )

                    llm_span.input_data = {"model": active_llm.model, "stage": "single_call"}

                async def _stream_with_fallback():

                    # 部分模型（如 qwen3.8-max-preview）强制 thinking=True，

                    # 传 enable_thinking=False 会返回 400 — 与非流式路径一致，

                    # 捕获后退回默认（不带 enable_thinking）重试。此时 thinking+tools

                    # 仍产出结构化 tool_calls（实测可用），不会泄漏 XML 文本。

                    try:
                        async for d in active_llm.stream(**_stream_kwargs):
                            yield d

                    except Exception as _se:
                        if active_extra and "enable_thinking" in str(_se):
                            _retry_kwargs = dict(_stream_kwargs)

                            _retry_kwargs.pop("extra_body", None)

                            async for d in active_llm.stream(**_retry_kwargs):
                                yield d

                        else:
                            raise

                async for delta in _stream_with_fallback():
                    if self._cancelled:
                        break

                        # 推理模型的思考内容 → 推到思考区

                    if delta.reasoning:
                        collected_reasoning += delta.reasoning
                        await self.callbacks.on_reasoning(delta.reasoning)

                    if delta.text:
                        collected_text += delta.text

                        yield Delta(text=delta.text)

                    if delta.done:
                        if delta.tool_calls:
                            collected_tool_calls = delta.tool_calls

                            # 记录 token 使用

                        if llm_span and delta.usage:
                            llm_span.output_data = {
                                "tokens": delta.usage.get("total_tokens", 0),
                                "usage": delta.usage,
                            }

                            # ── v3-Final P0a: 缓存命中率埋点 ──

                        if delta.usage:
                            try:
                                _cm = get_cache_monitor()

                                _cm.record(
                                    session_id=session.id,
                                    prompt_tokens=delta.usage.get("prompt_tokens", 0),
                                    cached_tokens=delta.usage.get("cached_tokens", 0)
                                    or delta.usage.get("prompt_cache_hit_tokens", 0),
                                    completion_tokens=delta.usage.get("completion_tokens", 0),
                                    model=getattr(active_llm, "model", ""),
                                )

                            except Exception:
                                pass  # 埋点失败不影响主流程

                        break

                if llm_span:
                    self.observability.end_span(llm_span)

            except Exception as e:
                await self.callbacks.on_thinking(False)

                # ── v3-Final P0.5: Failover 收紧 — 仅硬异常触发升级 ──

                _fm = get_failover_manager()

                _is_timeout = "timeout" in str(e).lower() or "TimeoutError" in type(e).__name__

                _is_malformed = "malformed" in str(e).lower() or "parse" in str(e).lower()

                _failover_reason = _fm.try_failover(
                    session_id=session.id,
                    answer="",
                    user_msg=user_message,
                    is_timeout=_is_timeout,
                    is_malformed=_is_malformed,
                )

                if _failover_reason:
                    await self.callbacks.on_status("route:escalated")

                    continue  # 用决策者重试本轮

                # 无法升级 → 记录日志并退出

                import logging

                logging.getLogger(__name__).warning(
                    f"[Failover] LLM 调用失败 (reason={_failover_reason}): {e}"
                )

                await self.callbacks.on_status("error")

                error_msg = f"LLM 调用失败: {e}"

                # ── 2026-08-28：异常路径同样关闭 trace（此前漏调 → trace 永久 running）──
                if observability_trace and self.observability:
                    self.observability.end_trace(
                        observability_trace, status="error", error=error_msg
                    )

                session.messages.append(Message(role=Role.ASSISTANT, content=error_msg))

                yield Delta(text=error_msg, done=True)

                return

            finally:
                await self.callbacks.on_thinking(False)

            # 有工具调用

            if collected_tool_calls:
                tool_call_meta = []

                for idx, tc in enumerate(collected_tool_calls):
                    call_id = f"call_{budget.current}_{idx}"

                    tool_call_meta.append({**tc.model_dump(), "call_id": call_id})

                # v3-Final P0: History sanitize — 写入时一次性剥离 thinking，

                # 保证 history 字节稳定，读取时不再二次处理

                sanitized_content = sanitize_assistant_output(collected_text)

                session.messages.append(
                    Message(
                        role=Role.ASSISTANT,
                        content=sanitized_content,
                        reasoning=collected_reasoning,
                        metadata={"tool_calls": tool_call_meta},
                    )
                )

                # 注：流式路径保持串行执行工具——前端需要逐个 on_tool_gen/on_status

                # 事件实时展示进度；并行执行留给 run_conversation（API/后台主路径，

                # 见策略④：read 工具 gather 并发）。

                for idx, tc in enumerate(collected_tool_calls):
                    call_id = f"call_{budget.current}_{idx}"

                    await self.callbacks.on_tool_gen(tc.name, tc.arguments)

                    await self.callbacks.on_status("acting")

                    # 让 WebSocket 排空事件队列

                    yield Delta()

                    # ── 可观测性：追踪工具调用 ──

                    tool_span = None

                    if self.enable_observability and self.observability and observability_trace:
                        tool_span = self.observability.start_span(
                            trace_id=observability_trace.id,
                            span_type="tool",
                            name=tc.name,
                        )

                        tool_span.input_data = {"arguments": tc.arguments}

                    # 公共工具执行逻辑（安全/审批/执行/回调/消息/事件）

                    await self._execute_single_tool(session, tc, call_id)

                    # ── 重复动作检测（2026-08-12）──

                    # 检测 agent 是否反复执行相同工具+参数（无限循环），达到阈值则注入打断提示。

                    try:
                        _sig = (
                            json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False)
                            if tc.arguments
                            else ""
                        )

                        self._recent_tool_calls.append({"tool": tc.name, "sig": _sig})

                        if len(self._recent_tool_calls) > 4:  # 最多保留最近 4 次
                            self._recent_tool_calls.pop(0)

                        # 连续重复检测：最近 3 次工具+参数完全相同

                        if len(self._recent_tool_calls) >= 3 and all(
                            c["tool"] == self._recent_tool_calls[-1]["tool"]
                            and c["sig"] == self._recent_tool_calls[-1]["sig"]
                            for c in self._recent_tool_calls[-3:]
                        ):
                            if not self._loop_break_injected:
                                self._loop_break_injected = True

                                _loop_hint = (
                                    "[⚠️ 循环打断提示] 系统检测到你已连续 3 次执行相同的工具调用"
                                    f"（{tc.name}: {str(tc.arguments)[:80]}），但没有产生进展。"
                                    "请立即停止重复该操作，换一种方式：要么直接基于已有信息给出最终答案，"
                                    "要么用不同的命令/参数，要么明确告知用户当前无法完成。不要重复同一个动作。"
                                )

                                session.messages.append(
                                    Message(
                                        role=Role.SYSTEM,
                                        content=_loop_hint,
                                        metadata={"type": "loop_break"},
                                    )
                                )

                                await self.callbacks.on_status("loop_break")

                    except Exception:
                        pass  # 重复检测失败不影响主流程

                    # ── 可观测性：记录工具结果 ──

                    if tool_span:
                        tool_result_msg = session.messages[-1] if session.messages else None

                        tool_success = (
                            tool_result_msg.metadata.get("success", False)
                            if tool_result_msg
                            else False
                        )

                        tool_output = tool_result_msg.content if tool_result_msg else ""

                        tool_span.output_data = {
                            "success": tool_success,
                            "output_length": len(tool_output),
                        }

                        self.observability.end_span(tool_span)

                    # ── 反思循环：工具执行后评估方向 ──

                    # 编排类工具跳过反思：子代理已完成推理并返回结论，
                    # 主 Agent 再反思一次是纯开销（多一次 10-20s LLM 调用）
                    if (
                        self.enable_reflexion
                        and self.reflexion_loop
                        and tc.name not in ("parallel_delegate", "delegate_task")
                    ):
                        # 获取刚执行的工具结果

                        tool_result_msg = session.messages[-1] if session.messages else None

                        tool_success = (
                            tool_result_msg.metadata.get("success", False)
                            if tool_result_msg
                            else False
                        )

                        tool_output = tool_result_msg.content if tool_result_msg else ""

                        # 执行反思（_should_reflect 内部已做节流，判定不需要时不发 LLM 调用）

                        reflection = await self.reflexion_loop.reflect(
                            state=self.reflexion_state,
                            tool_name=tc.name,
                            tool_args=tc.arguments,
                            tool_success=tool_success,
                            tool_output=tool_output,
                            user_goal=user_message,
                            step=budget.current,
                        )

                        if reflection and reflection.to_context_hint():
                            # 仅在真正产生反思内容时才记录 span，避免每步工具都留一条 reflection 记录
                            reflection_span = None

                            if self.enable_observability and self.observability and observability_trace:
                                reflection_span = self.observability.start_span(
                                    trace_id=observability_trace.id,
                                    span_type="reflection",
                                    name="reflection",
                                )

                                reflection_span.input_data = {"tool": tc.name, "step": budget.current}

                            # 将反思结果注入上下文

                            await self.callbacks.on_reflection(reflection.to_context_hint())

                            session.messages.append(
                                Message(
                                    role=Role.SYSTEM,
                                    content=reflection.to_context_hint(),
                                    metadata={"type": "reflection"},
                                )
                            )

                            if reflection_span:
                                reflection_span.output_data = {"hint": reflection.to_context_hint()}
                                self.observability.end_span(reflection_span)

                    # ── Checkpoint：工具执行后保存状态 ──

                    if self.checkpoint_manager and budget.current % 3 == 0:  # 每3步保存一次
                        try:
                            # 收集已完成的工具调用

                            completed_tools = []

                            for msg in session.messages:
                                if msg.role == Role.TOOL:
                                    completed_tools.append(
                                        {
                                            "tool_name": msg.metadata.get("tool_name", ""),
                                            "call_id": msg.metadata.get("call_id", ""),
                                            "success": msg.metadata.get("success", False),
                                        }
                                    )

                            self.checkpoint_manager.save_checkpoint(
                                session_id=session.id,
                                step=budget.current,
                                status="acting",
                                messages=[m.to_api_dict() for m in session.messages],
                                pending_tools=[],
                                completed_tools=completed_tools,
                                budget_used=budget.current,
                                budget_max=budget.max_turns,
                                context_summary=f"已执行 {len(completed_tools)} 个工具调用",
                            )

                        except Exception as e:
                            import logging

                            logging.getLogger(__name__).warning(f"保存 checkpoint 失败: {e}")

                    # 让 WebSocket 排空 tool_progress(done) 事件

                    yield Delta()

                if self.enable_context and self.context_mgr:
                    _removed = self.context_mgr.prune_tool_outputs(session)
                    if _removed and self.enable_persistence and self.session_store:
                        try:
                            await self.session_store.async_archive_messages(
                                session.id, _removed, reason="context_prune"
                            )
                        except Exception:
                            import logging
                            logging.getLogger(__name__).debug(
                                "归档被剪枝消息失败", exc_info=True
                            )

                # 智能路由 (2026-08-04 修改): 移除步数升级逻辑。

                # ReAct 模式下 thinker 仅在 executor 执行失败(异常)时才介入，

                # 不再因步数超限而升级，避免无谓消耗复杂模型 token。

                continue

            else:
                # 无工具调用 — 文本回复

                final_text = collected_text

                if self.deep_thinking and collected_text and not self._is_executor_direct:
                    yield Delta(text=collected_text)

                # v3-Final P0: History sanitize — 写入时一次性剥离 thinking

                sanitized_final = sanitize_assistant_output(final_text)

                reply = Message(
                    role=Role.ASSISTANT,
                    content=sanitized_final,
                    reasoning=None,
                )

                session.messages.append(reply)

                session.status = "done"

                await self.callbacks.on_status("done")

                # 2026-08-14: 语义缓存已移除（命中率低+实时性腐蚀），不再写回

                if self.enable_persistence and self.session_store:
                    self.session_store.save_session(session)

                if self.bus:
                    await self.bus.emit(
                        "conversation.complete",
                        {
                            "session_id": session.id,
                            "steps": budget.current,
                        },
                    )

                # ── 可观测性：关闭 trace ──

                if observability_trace:
                    self.observability.end_trace(observability_trace)

                # ── 先推送 done（前端立即显示完成），再异步生成追问建议 ──

                yield Delta(text="", done=True)

                # 生成追问建议（best-effort，失败绝不影响主回复）

                if not self._cancelled and final_text and len(final_text.strip()) >= 20:
                    try:
                        suggestions = await self._generate_suggestions(
                            user_message, final_text, session.id
                        )

                        if suggestions:
                            yield Delta(suggestions=suggestions)
                            # 持久化建议到会话，重进后仍可恢复（suggest 默认不落库，
                            # 这里存到 session.extra 并在 done 之后再次保存一次）
                            session.extra["suggestions"] = suggestions
                            if self.enable_persistence and self.session_store:
                                self.session_store.save_session(session)

                    except Exception:
                        pass

                # 自动目标提取（best-effort，失败不影响主流程）

                if self.enable_goal_manager and self.goal_manager and not self._cancelled:
                    try:
                        extracted_goals = await self.goal_manager.extract_goals_from_conversation(
                            user_message, final_text
                        )

                        if extracted_goals:
                            # 通过回调通知前端

                            await self.callbacks.on_goals_extracted(
                                [
                                    {"id": g.id, "title": g.title, "tasks_count": len(g.tasks)}
                                    for g in extracted_goals
                                ]
                            )

                    except Exception as e:
                        import logging

                        logging.getLogger(__name__).debug(f"自动目标提取失败: {e}")

                return

        # 预算耗尽

        budget_msg = "\n\n" + self._build_budget_exhausted_msg(session, budget.current)

        # ── 2026-08-20：预算耗尽强制总结（参考 CowAgent 优点）──
        # 本轮只要通过工具获取过信息，就最后调一次模型（不带工具）
        # 基于已获取信息直接产出最终成果（如把文章写完），避免"步数用尽
        # 却空手而归"——这是 scout 完不成写文章类任务的根因之一。
        forced = ""
        if (
            not self._cancelled
            and self.llm is not None
            and (self._tool_stats.get(session.id, {}).get("total", 0) or 0) > 0
        ):
            forced = await self._force_final_output(session)

        if forced:
            final_text = (
                forced
                + "\n\n---\n\n⚠️ 本轮已达到步数上限（"
                + str(self.max_turns)
                + " 步），以上成果已基于本轮获取的信息生成。如需继续完善可回复「继续」。"
            )
        else:
            final_text = budget_msg

        # ── 2026-08-28：预算耗尽收尾同样关闭 trace（此前漏调 → trace 永久 running）──
        if observability_trace and self.observability:
            self.observability.end_trace(observability_trace)

        session.messages.append(Message(role=Role.ASSISTANT, content=final_text))

        session.status = "done"

        if self.enable_persistence and self.session_store:
            self.session_store.save_session(session)

        # 先推送正文 + done，再附上"继续"引导建议（与正常完成路径一致）

        yield Delta(text=final_text, done=True)

        if not self._cancelled:
            yield Delta(suggestions=["继续完成剩余任务", "总结目前已完成的结果"])

        # ── E4 跨会话记忆抽取（2026-08-27）：流式路径收尾同样沉淀 ──
        await self._maybe_extract_session_memory(session)

    async def _generate_suggestions(
        self,
        user_message: str,
        reply_text: str,
        session_id: str = "",
    ) -> list[str]:
        """生成追问建议 — 用轻量模型基于本轮问答产出 2-4 个简短追问.



        设计：单模型（双模型已移除）；max_tokens 受限；

        任何异常都吞掉返回空列表（建议是锦上添花，绝不影响主回复）。

        """

        llm = self.llm

        if not llm:
            return []

        reply_excerpt = reply_text.strip()

        if len(reply_excerpt) > 1500:
            reply_excerpt = reply_excerpt[:1500] + "…"

        prompt = (
            "根据下面的对话，生成 3 个用户接下来最可能想做的后续操作或追问。\n"
            "要求：\n"
            "- 每个一行，不超过 18 字，不要编号、不要引号、不要任何前缀或解释\n"
            '- 优先给可执行的具体动作（如"重启服务验证""查看修改的文件""继续完成剩余部分"），'
            "其次才是有价值的深入问题\n"
            "- 不要问助手回复中已经回答了的内容\n"
            "- 使用与对话相同的语言\n\n"
            f"用户：{user_message.strip()[:500]}\n"
            f"助手：{reply_excerpt}\n\n"
            "直接输出 3 行："
        )

        resp = await llm.complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=150,
            _role="main",
            _session_id=session_id,
            extra_body={
                "enable_thinking": False
            },  # 关闭思考：建议生成要快(1-2s)，不要深度推理(否则20-30s易超时)
        )

        items: list[str] = []

        for raw in (resp.content or "").splitlines():
            line = raw.strip()

            if not line:
                continue

            # 去掉编号 / 列表符号前缀

            line = re.sub(r"^\s*\d+[.、)）]\s*", "", line)

            line = re.sub(r"^\s*[-*•·]\s*", "", line)

            # 去掉首尾引号（含中英文）和反引号

            for q in ('"', "'", "`", "“", "”", "‘", "’"):
                line = line.strip(q)

            line = line.strip()

            if not line or len(line) > 60:
                continue

            if line not in items:
                items.append(line)

            if len(items) >= 4:
                break

        return items

    def _compute_temperature(self, llm: Any) -> float:
        """Orchestrator-Worker 温度分层.



        决策者（thinker）用低温度（0.2）保证规划严谨、减少幻觉；

        执行者（executor/主模型）用全局温度（0.7）保证生成自然。

        """

        return self.temperature  # 单模型（双模型已移除 2026-08-14）

        return self.temperature  # 执行者/生成：正常温度

    async def _maybe_extract_session_memory(self, session: Session) -> None:
        """会话结束时抽取关键记忆（E4 跨会话记忆工程化，2026-08-27）.

        可选能力：仅在注入 ``memory_extractor`` 时生效；任何失败只记录日志，
        绝不影响主流程返回。
        """
        if not self.memory_extractor or not session or not session.messages:
            return
        import logging

        _log = logging.getLogger(__name__)
        try:
            report = await self.memory_extractor.extract(session)
            if report.added:
                _log.info("会话 %s 记忆抽取 %s", session.id, report.summary())
        except Exception as exc:
            _log.warning("会话 %s 记忆抽取失败: %s", session.id, exc)

    def _build_runtime_context(
        self,
        current_time: str,
        memories: str = "",
        summary: str = "",
    ) -> str:
        """构建 runtime_context XML 块，追加到最后一条 user message 尾部.



        v3-Final P0 设计：动态内容（时间/记忆/技能）全部收口到 user 消息尾部，

        保持 system prompt 100% 静态，最大化前缀缓存命中率。



        Args:

            current_time: 当前时间字符串（%Y-%m-%d %H:%M:%S）

            memories: 相关记忆文本（多行）

            summary: 会话摘要（可选）



        Returns:

            XML 格式的 runtime_context 字符串，末尾包含 </runtime_context>

            （供 _inject_context 用 .replace 插入 <skills> 块）

        """

        parts = [
            "<runtime_context>",
            f"<current_time>{current_time}</current_time>",
        ]

        if summary:
            parts.append(f"<summary>{summary}</summary>")

        if memories:
            parts.append(f"<memories>{memories}</memories>")

        parts.append("</runtime_context>")

        return "\n".join(parts)

    # ── 2026-08-19 渐进式工具加载 ──────────────────────────────────────
    # 核心常用工具始终注入（保持基本能力 + 稳定前缀）；边缘/重工具按关键词
    # 渐进式注入，减少无关工具 schema 的 token 占用（工具总数 21 个、compact
    # schema 约 2233 tokens，简单任务往往只需要其中一小部分）。

    # 始终在场的核心工具（基本能力，不依赖关键词）
    _CORE_TOOLS = {
        "web_search", "web_fetch", "file", "shell", "execute_code",
        "memory_search", "memory_save", "memory_list",
        "env_config_get", "env_config_save", "env_config_list", "env_config_delete",
    }

    # 渐进式工具 → 触发关键词（任一命中即注入该工具 schema）
    # 关键词设计避免宽泛误触发：图片/图 这类词既可能指生成、也可能指识别，
    # 故用"动词+对象"组合（生成/画/做…图  vs  识别/分析/看…图）区分。
    # 渐进式工具 → 触发关键词（任一命中即注入该工具 schema）
    # 关键词同时覆盖中英文，避免英文/中文界面下能力不一致。
    _PROGRESSIVE_TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
        "image_generation": ("生成图片", "生成图像", "画一张", "画一个", "画张", "画只",
                             "画只", "画一", "插画", "海报", "图标", "logo", "设计图",
                             "配图", "头像", "封面", "生成一张", "做一个logo", "做一张",
                             "画个", "画一只", "生成图", "生成一张图",
                             "generate image", "create image", "draw a", "draw an", "generate a logo",
                             "make a poster", "create a icon", "image generation", "generate picture"),
        "vision": ("识别图片", "分析图片", "看图", "ocr", "提取文字", "图片内容",
                   "这张图", "这个图片", "图片里", "图片识别", "识别这张", "看看这张图",
                   "图片是什么", "图里是什么",
                   "recognize image", "analyze image", "read image", "image content", "what is in the image"),
        "knowledge": ("知识库", "保存知识", "知识页面", "知识检索", "记笔记",
                      "knowledge base", "save knowledge", "knowledge page", "take note"),
        "scheduler": ("定时", "提醒我", "定时任务", "定时提醒", "设置提醒", "预约",
                      "每日提醒", "提醒",
                      "schedule", "remind me", "timer", "daily reminder", "set reminder"),
        "delegate_task": ("委派", "子任务", "子代理", "拆分任务", "分解任务", "并行任务",
                          "delegate", "subtask", "sub-agent", "break down task", "decompose task"),
        "parallel_delegate": ("并行", "同时执行", "并行委派", "同时处理", "批量",
                              "parallel", "in parallel", "parallel delegate", "at the same time",
                              "concurrently", "simultaneously", "batch process"),
        "collaborate_task": ("协作", "协作执行", "多代理", "多agent", "团队",
                             "collaborate", "collaboration", "multi-agent", "team"),
        "mcp": ("mcp", "外部服务", "model context protocol", "mcp服务器", "外部工具",
                "external service", "external tool"),
        "mcp_tool": ("mcp", "外部服务", "model context protocol", "external service"),
        "scout_report": ("运行报告", "状况报告", "自检", "scout报告", "系统报告", "健康报告",
                         "scout report", "status report", "self check", "health report"),
        "send_file": ("发文件", "发送文件", "下载文件", "发给我", "附件", "文件给我",
                      "send file", "download file", "attachment"),
    }

    @staticmethod
    def _normalize_search_key(query: str) -> str:
        """把搜索 query 规范化成"目标 key"，用于检测重复搜索.

        核心思路：提取 query 中的"实体标记"——字母数字词（如 glm-5.3、
        arxiv、sao、post-training）和连续字母，去掉常见停用词后排序连接。
        这样『GLM-5.3 technical report arxiv』『GLM-5.3 arxiv 技术报告』
        『帮我搜索GLM-5.3技术报告』都会归一到同一 key（含核心实体词），
        从而被判定为"同一目标"而触发重试上限。
        """
        import re

        if not query:
            return ""
        text = query.lower()
        STOP = {
            "search", "searching", "查询", "搜索", "查", "找", "查找", "关于", "最新",
            "的", "技术", "报告", "technical", "tech", "report", "paper", "论文",
            "博客", "blog", "官方", "official", "文档", "docs", "documentation",
            "今天", "今年", "解读", "分析", "帮我", "请", "一下", "a", "an", "the",
            "and", "or", "of", "for", "to", "in", "on", "is", "are", "be", "是", "有",
            "以及", "与", "和", "怎么", "如何", "what", "which", "where", "give",
        }
        # 提取字母数字 token：覆盖英文单词、带连字符/点号的实体（glm-5.3、post-training）
        tokens = re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", text)
        # 过滤纯数字、停用词、单字母
        core = [
            t for t in tokens
            if len(t) > 1 and not t.isdigit() and t not in STOP
        ]
        if not core:
            # 兜底：没有可辨识实体时，用原文本去掉空格
            return re.sub(r"\s+", "", text)[:40]
        # 排序连接，保证词序变化（中英混排）不影响判定
        return " ".join(sorted(set(core)))[:60]

    def _select_progressive_tools(self, user_input: str) -> list[dict]:
        """按用户输入渐进式筛选本次 turn 的工具子集.

        核心工具始终在场；渐进式工具按关键词匹配，命中才注入。
        结果按名称排序，保证同一输入下工具集稳定（前缀可复用）。
        任何情况下都返回非空列表（至少核心工具）。
        """
        if not self._tool_schemas:
            return self._tool_schemas

        text = (user_input or "").lower()

        selected_names: set[str] = set(self._CORE_TOOLS)
        for tool_name, keywords in self._PROGRESSIVE_TOOL_KEYWORDS.items():
            if any(kw.lower() in text for kw in keywords):
                selected_names.add(tool_name)

        # 从全量 schema 中筛选（保持顺序稳定）
        result = [
            s for s in self._tool_schemas
            if s.get("function", {}).get("name", "") in selected_names
        ]

        # 兜底：若筛选结果为空（理论上不应发生），退回全量，避免工具缺失
        if not result:
            return self._tool_schemas

        return result

    async def _inject_context(
        self,
        session: Session,
        user_message: str,
        attachments: list[dict] | None = None,
    ) -> None:
        """注入本轮上下文 — 记忆召回 → 技能匹配 → 追加用户消息.



        run_conversation 与 stream_conversation 共用，保证两条链路行为一致。



        v3-Final P0 改造:

        - 动态内容（记忆、技能、时间戳）不再插入 system 消息

        - 改为追加到最后一条 user message 的 <runtime_context> 中

        - 保持 system prompt 100% 静态，最大化前缀缓存命中率

        """

        # ── 2026-08-19 渐进式工具加载：按用户输入筛选本次 turn 的工具子集 ──
        # 核心常用工具始终在场（保持基本能力 + 前缀稳定），边缘工具按需注入，
        # 减少无关工具 schema 的 token 占用。整个 turn 内工具集固定，前缀稳定。
        self._active_tool_schemas = self._select_progressive_tools(user_message)

        # 仅清理上一轮的"技能匹配"指令（陈旧技能指令不应累积）。

        # 记忆召回消息刻意保留在历史中：它们位置稳定，使整段对话历史构成稳定的可缓存前缀。

        # prompt cache 按前缀匹配，前缀越稳定、越长，命中越多、越省钱；

        # 历史长度由上下文压缩（compress_threshold）兜底，不会无限膨胀。

        session.messages = [m for m in session.messages if m.metadata.get("type") != "skill_match"]

        # ── v3-Final P0: 收集动态内容，稍后注入 runtime_context ──

        memory_text = ""

        skill_text = ""

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 记忆召回（异步混合检索：向量语义 + FTS5 文本）

        # P1 记忆治理：use_memories 开关 + 注入前安全清洗（防御纵深）

        _mem_enabled = self.enable_memory and self.memory_store

        if _mem_enabled and self.memory_gate and not self.memory_gate.should_inject():
            _mem_enabled = False

        if _mem_enabled:
            if self.context_assembler and self.context_assembler.memory_store:
                # E4 跨会话记忆组装（2026-08-27）：相关性 × 重要性 × 时间衰减排序 + 预算截断
                memory_text = await self.context_assembler.build_memory_context(
                    user_message
                )
            else:
                memories = await self.memory_store.search_async(user_message, limit=3)

                if memories:
                    # 记忆注入长度控制（2026-08-19）：每条最多保留 300 字符。
                    # 记忆全量注入会放大"动态尾部"，拉低前缀缓存命中率；
                    # 3 条 × 300 字符足以提供上下文，超长细节靠语义检索已保证相关性。
                    _MEM_CHARS = 300
                    try:
                        from scout.memory.security_scan import sanitize_for_injection

                        memory_text = "\n".join(
                            f"- {sanitize_for_injection(m.content)[:_MEM_CHARS]}"
                            for m in memories
                        )

                    except Exception:
                        memory_text = "\n".join(
                            f"- {m.content[:_MEM_CHARS]}" for m in memories
                        )

        # E4 跨会话历史摘要（2026-08-27）：最近已完成会话的标题/摘要 → <summary>
        summary_text = ""
        if self.context_assembler and self.context_assembler.session_store:
            try:
                summary_text = await self.context_assembler.build_session_summary(
                    exclude_session_id=session.id
                )
                if summary_text:
                    from scout.memory.security_scan import sanitize_for_injection

                    summary_text = sanitize_for_injection(summary_text)
            except Exception as _sum_err:
                summary_text = ""

        # 技能匹配（静态文件技能）

        if self.enable_skills and self.skill_mgr:
            skill_prompt = self.skill_mgr.to_prompt(user_message)

            if skill_prompt:
                skill_text = skill_prompt

        # 技能匹配（动态沉淀技能 — 向量检索）

        if self.skill_retriever:
            try:
                synthesized_skills = await self.skill_retriever.retrieve_for_task(
                    user_message=user_message,
                )

                if synthesized_skills:
                    hint = self.skill_retriever.format_as_prompt_hint(synthesized_skills)

                    if skill_text:
                        skill_text += "\n" + hint

                    else:
                        skill_text = hint

            except Exception as _e:
                import logging

                logging.getLogger(__name__).debug(f"Skill retrieval failed: {_e}")

        # P1 渐进式披露：未命中技能时注入技能索引（name+description，预算受限）

        if not skill_text and self.enable_skills and self.skill_mgr:
            try:
                # 预算 1500 字符：技能索引只是 name+description 目录，足够定位；

                # 之前 4000 字符在无技能/少技能时造成大量上下文浪费

                _index = self.skill_mgr.build_skills_index(budget_chars=1500)

                if _index:
                    skill_text = _index

            except Exception:
                pass

        # ── 工作流蒸馏追踪：新任务开始，检测用户纠正 ──

        if self.workflow_distiller:
            self.workflow_distiller.reset_task()

            if self._looks_like_correction(user_message):
                self.workflow_distiller.track_user_correction(user_message)

        # ── 构建 runtime_context 并追加到最后一条 user message ──

        # 注意：此时 user_message 还未追加到 session.messages，需要手动追加

        runtime_context = self._build_runtime_context(
            current_time=current_time,
            memories=memory_text,
            summary=summary_text,
        )

        # 如果有技能匹配结果，也放入 runtime_context

        if skill_text:
            runtime_context = runtime_context.replace(
                "</runtime_context>", f"<skills>{skill_text}</skills>\n</runtime_context>"
            )

        # 追加用户消息：入库 content 仅为纯用户输入；runtime_context 只存 metadata，
        # 由 _build_api_messages 在构建 API 消息时注入到当轮 user 消息，
        # 避免动态上下文污染持久化历史、跨轮重复累积。

        session.messages.append(
            Message(
                role=Role.USER,
                content=user_message,
                metadata=(
                    {"attachments": attachments, "runtime_context": runtime_context}
                    if attachments
                    else {"runtime_context": runtime_context}
                ),
                timestamp=datetime.now(),
            )
        )

    def _log_run_event(self, event: dict) -> None:
        """自动化运行时：把执行事件写入 RunStore 事件流（交互模式无操作）."""

        if not self.auto_run_meta:
            return

        run_id = self.auto_run_meta.get("run_id", "")

        if not run_id:
            return

        try:
            from scout.engine.runs import RunStore

            if not hasattr(self, "_run_store"):
                self._run_store = RunStore()

            self._run_store.append_event(run_id, event)

        except Exception:
            pass

    def _looks_like_correction(self, user_message: str) -> bool:
        """启发式检测用户是否在纠正 Agent（工作流蒸馏触发条件3）."""

        text = user_message.strip()

        if len(text) < 2 or len(text) > 300:
            return False

        _correction_markers = (
            "不对",
            "错了",
            "不是这样",
            "应该是",
            "改成",
            "换成",
            "别用",
            "不要用",
            "重新",
            "再试",
            "你搞错",
            "更正",
            "纠正",
            "no, ",
            "wrong",
            "actually",
            "instead",
            "don't use",
            "should be",
        )

        return any(m in text.lower() for m in _correction_markers)

    def _record_tool_result(self, session_id: str, name: str, success: bool, output: str) -> None:
        """累计工具调用统计（2026-08-20）.

        在 _execute_single_tool 的所有 TOOL 消息生成点调用，保证统计不受
        上下文剪枝（物理删除旧消息）影响，预算耗尽总结能反映真实调用数。
        按 session 隔离、每个 turn 开头重置。
        """
        st = self._tool_stats.setdefault(
            session_id,
            {"total": 0, "ok": 0, "fail": 0, "tools": {}, "fail_tools": {}, "snippets": []},
        )
        st["total"] += 1
        if success:
            st["ok"] += 1
            st["tools"][name] = st["tools"].get(name, 0) + 1
        else:
            st["fail"] += 1
            st["fail_tools"][name] = st["fail_tools"].get(name, 0) + 1

        # 收集成功输出中的信息片段（供预算耗尽摘要展示，即使消息已被剪枝）
        if success and output:
            _clean = (output or "").strip()
            if _clean and not _clean.startswith(("🔍", "📊", "ℹ️", "⚠️")):
                frag = " ".join(_clean.split())[:300]
                if frag:
                    snippets = st["snippets"]
                    if frag not in snippets:
                        snippets.append(frag)
                        # 只保留最近 3 条，保持总结简洁
                        del snippets[:-3]

    async def _force_final_output(self, session: Session) -> str:
        """预算耗尽时，最后再调一次主模型（不带工具）基于已获取信息直接产出最终成果.

        2026-08-20 新增（参考 CowAgent 的"达到上限强制总结"优点）：
        此前 scout 在步数耗尽时直接停下、只输出统计信息；写文章/报告类任务常在
        内容抓取阶段就把步数用尽，"空手而归"。此方法让模型基于已有信息把成果
        （如文章）一次性写完，是"一个会话完成"的关键兜底。

        Returns: 模型产出的最终文本；失败或为空时返回 ""（调用方回退到统计消息）。
        """
        try:
            api_messages = self._build_api_messages(session)
            api_messages.append(
                {
                    "role": "user",
                    "content": (
                        "【步数已用尽】现在请不要再调用任何工具，直接基于以上对话中"
                        "已经获取到的全部信息完成最终输出：\n"
                        "- 如果是写文章/报告类任务：请把文章/报告**完整写完**，包含所有"
                        "关键信息、结构清晰、可直接发布，并注明图片/资料来源；\n"
                        "- 如果是查询/解答类任务：请给出完整、准确、可直接使用的最终回答；\n"
                        "- 如果信息仍有缺口，请基于已有信息尽力完成，并简要说明缺少什么。"
                    ),
                }
            )
            active_llm = self.llm
            text = ""

            async def _consume(stream):
                nonlocal text
                async for d in stream:
                    if d.text:
                        text += d.text
                    if d.done:
                        break

            _extra = {"extra_body": {"enable_thinking": False}}
            try:
                await _consume(
                    active_llm.stream(
                        messages=api_messages,
                        tools=None,  # 关键：不带工具，只产出文本
                        temperature=self._compute_temperature(active_llm),
                        _role="main",
                        _session_id=session.id,
                        **_extra,
                    )
                )
            except Exception:
                # 部分模型（如 qwen3.8-max-preview）不允许关思考，退回默认重试
                await _consume(
                    active_llm.stream(
                        messages=api_messages,
                        tools=None,
                        temperature=self._compute_temperature(active_llm),
                        _role="main",
                        _session_id=session.id,
                    )
                )
            return text.strip()
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning("预算耗尽强制总结失败: %s", e)
            return ""

    def _build_budget_exhausted_msg(self, session: Session, llm_steps: int) -> str:
        """构建"达到最大迭代次数"的总结消息（CowAgent 风格，简洁清晰）.

        llm_steps: 本轮大模型（LLM）决策轮数，即"大模型步数"，
        对应步数上限 max_turns 的计数口径（budget.current）。
        工具操作数可能多于决策轮数（一次决策可调多个工具），
        这里主展示决策步数，工具操作数作为补充说明。
        """

        # 定位本轮起点：最后一条用户消息

        turn_start = 0

        for i in range(len(session.messages) - 1, -1, -1):
            if session.messages[i].role == Role.USER:
                turn_start = i + 1

                break

        # ── 工具统计：优先使用会话级计数器（2026-08-20 修复）──
        # 上下文剪枝（prune_tool_outputs）会物理删除最旧的 assistant+tool 消息，
        # 仅扫描剩余消息会导致"50 步决策却只显示 10 次工具"的失真统计。
        # 计数器在每次工具调用时独立累计（_record_tool_result），不受剪枝影响。
        st = self._tool_stats.get(session.id)

        if st and st["total"] > 0:
            total = st["total"]
            ok = st["ok"]
            fail = st["fail"]
            used_tools: set[str] = set(st["tools"].keys())
            snippets: list[str] = list(st["snippets"])
            fail_counter = dict(st["fail_tools"])
            has_stats = True
        else:
            # 兜底：计数器为空（如旧会话/工具未走 _execute_single_tool）时扫描幸存消息
            steps: list[tuple[str, bool, str]] = []
            arg_briefs: dict[str, str] = {}
            for m in session.messages[turn_start:]:
                tool_call_metas = (
                    (m.metadata.get("tool_calls") or []) if m.role == Role.ASSISTANT else []
                )
                for tcm in tool_call_metas:
                    args = tcm.get("arguments") or {}
                    primary = next((v for v in args.values() if v), "")
                    arg_briefs[tcm.get("name", "")] = str(primary)[:80]
                if m.role == Role.TOOL:
                    name = m.metadata.get("tool_name", "unknown")
                    steps.append(
                        (name, bool(m.metadata.get("success", False)), arg_briefs.get(name, ""))
                    )
            total = len(steps)
            ok = sum(1 for _, s, _ in steps if s)
            fail = total - ok
            used_tools = {name for name, success, _ in steps if success}
            from collections import Counter
            fail_counter = Counter(name for name, success, _ in steps if not success)
            snippets = []
            for m in session.messages[turn_start:]:
                if m.role == Role.TOOL and m.metadata.get("success"):
                    _clean = (m.content or "").strip()
                    if _clean and not _clean.startswith(("🔍", "📊", "ℹ️")):
                        frag = " ".join(_clean.split())[:300]
                        if frag and frag not in snippets:
                            snippets.append(frag)
                    if len(snippets) >= 3:
                        break
            has_stats = total > 0

        lines = [f"⚠️ 本轮执行已达到步数上限（{self.max_turns} 步），暂先在这里停下。", ""]

        if has_stats:
            # 主展示大模型决策步数（与 max_turns 同口径），工具操作数作为补充
            lines.append(f"**已完成 {llm_steps} 步大模型决策**（共调用 {total} 次工具，成功 {ok} / 失败 {fail}）。")

            if used_tools:
                lines.append(f"主要已完成：{', '.join(sorted(used_tools))} 等操作。")

        else:
            lines.append("本轮尚未完成任何操作。")

        # ── 2026-08-19 增强：附上本轮已获取的关键信息摘要 ──
        # 用户关心的是"任务到底获取到了什么"，而不只是步数统计。
        # 优先使用计数器保存的片段（剪枝后仍保留），其次扫描幸存消息。
        if snippets:
            lines.append("")
            lines.append("**本轮已获取到的部分信息：**")
            for s in snippets[:3]:
                lines.append(f"- {s}")
            lines.append("")

        # 失败统计（如有）

        if fail_counter:
            fail_brief = "、".join(
                f"{k}×{v}" if v > 1 else k for k, v in fail_counter.items()
            )

            lines.append(f"其中有 {fail} 次操作未成功：{fail_brief}。")

        lines += [
            "",
            "任务可能还没全部完成。你可以：",
            "- 回复「**继续**」，我会基于当前进度接着完成剩余部分；",
            "- 或告诉我需要调整的地方，我重新来过。",
        ]

        return "\n".join(lines)

    async def _execute_single_tool(
        self,
        session: Session,
        tc: ToolCall,
        call_id: str,
    ) -> None:
        """执行单个工具调用 — 安全检查 / 危险命令审批 / 执行 / 回调 / 消息记录 / 事件.



        run_conversation 与 stream_conversation 共用，消除两条链路间的逻辑漂移。

        shell 工具支持流式输出（实时回调 on_tool_progress stream 事件）。

        """

        # ── 搜索重试检测（2026-08-19）：拦截对"同一目标"的重复搜索 ──
        # 若同一 session 内对高度相似的 query 连续搜索达 search_retry_limit 次，
        # 返回明确提示引导 agent 换策略（改直接访问官网、限定 site:、接受无公开版），
        # 避免陷入"搜不到就换关键词重试"的无效循环。
        if tc.name == "web_search":
            _q = (tc.arguments or {}).get("query", "")
            _norm = self._normalize_search_key(_q)
            if _norm:
                _cur_tokens = set(_norm.split())
                _hist = self._search_history.setdefault(session.id, [])
                # 统计最近 search_retry_limit 次里，与当前目标共享核心实体的次数
                _recent = _hist[-self.search_retry_limit:]
                _same_goal = sum(
                    1 for h in _recent
                    if h and (set(h.split()) & _cur_tokens)
                )
                _hist.append(_norm)
                if _same_goal >= self.search_retry_limit - 1:
                    obs = Observation(
                        tool_name="web_search",
                        success=False,
                        output=(
                            f"⚠️ 搜索重试已达上限：已连续 {self.search_retry_limit} 次搜索"
                            f"『{_q}』（或指向同一目标 {sorted(_cur_tokens)[:3]} 的相似查询）仍未获得有用结果。"
                            "请停止重复搜索，改用以下策略之一：\n"
                            "1) 直接 web_fetch 访问相关官方域名（如 z.ai、bigmodel.cn 等）的已知/推测 URL；\n"
                            "2) 用 site: 限定域名搜索；\n"
                            "3) 若确认该内容无公开来源，如实告知用户并基于已有信息继续。"
                        ),
                    )
                    session.observations.append(obs)
                    session.messages.append(
                        Message(
                            role=Role.TOOL,
                            content=obs.output,
                            metadata={"tool_name": obs.tool_name, "success": False, "call_id": call_id},
                        )
                    )
                    self._record_tool_result(session.id, obs.tool_name, False, obs.output)
                    await self.callbacks.on_tool_progress(tc.name, "error", obs.output, metadata={"call_id": call_id})
                    return

        # ── P0 无人值守权限门控：自动化运行受 AutomationPolicy 管控 ──

        if self.automation_policy is not None:
            try:
                from scout.security.automation_policy import AutomationPolicyManager

                allowed, reason = AutomationPolicyManager().check_tool(
                    tc.name,
                    tc.arguments,
                    policy=self.automation_policy,
                    security_manager=self.security,
                )

                if not allowed:
                    obs = Observation(
                        tool_name=tc.name,
                        success=False,
                        output=f"自动化策略拒绝: {reason}",
                    )

                    session.observations.append(obs)

                    session.messages.append(
                        Message(
                            role=Role.TOOL,
                            content=obs.output,
                            metadata={
                                "tool_name": obs.tool_name,
                                "success": False,
                                "call_id": call_id,
                            },
                        )
                    )

                    self._record_tool_result(session.id, obs.tool_name, False, obs.output)

                    self._log_run_event({"type": "tool_denied", "tool": tc.name, "reason": reason})

                    if self.bus:
                        await self.bus.emit(
                            "tool.blocked",
                            {
                                "tool": tc.name,
                                "reason": reason,
                                "automated": True,
                            },
                        )

                    return

            except Exception:
                pass  # 策略模块异常不阻塞执行（危险命令硬拦截仍生效）

        # 安全检查

        if self.enable_security and self.security:
            tool = ToolRegistry.get_tool(tc.name)

            if tool:
                allowed, reason = self.security.check_tool(tc.name, tool.annotations)

                if not allowed:
                    obs = Observation(
                        tool_name=tc.name,
                        success=False,
                        output=f"安全拦截: {reason}",
                    )

                    session.observations.append(obs)

                    session.messages.append(
                        Message(
                            role=Role.TOOL,
                            content=obs.output,
                            metadata={
                                "tool_name": obs.tool_name,
                                "success": False,
                                "call_id": call_id,
                            },
                        )
                    )

                    if self.bus:
                        await self.bus.emit("tool.blocked", {"tool": tc.name, "reason": reason})

                    return

                # 危险命令硬拦截（不受 auto_approve 影响，与 policy.py 注释一致）
                # 同时检查 command 与 args，防止 LLM 把命令拆到 args 里绕过检测。

                if tc.name == "shell":
                    parts = [tc.arguments.get("command", "")]
                    if isinstance(tc.arguments.get("args"), list):
                        parts.extend(str(a) for a in tc.arguments["args"])
                    command = " ".join(str(p).strip() for p in parts if str(p).strip())

                    is_safe, warning = self.security.check_command_block(command)

                    if not is_safe:
                        obs = Observation(
                            tool_name=tc.name,
                            success=False,
                            output=f"⛔ 危险命令已拦截: {warning}",
                        )

                        session.observations.append(obs)

                        session.messages.append(
                            Message(
                                role=Role.TOOL,
                                content=obs.output,
                                metadata={
                                    "tool_name": obs.tool_name,
                                    "success": False,
                                    "call_id": call_id,
                                },
                            )
                        )

                        self._record_tool_result(session.id, obs.tool_name, False, obs.output)

                        if self.bus:
                            await self.bus.emit(
                                "tool.blocked",
                                {"tool": tc.name, "reason": warning, "automated": True},
                            )

                        return

        # Human-in-the-Loop: 危险操作前请求用户确认（auto_approve 开启时跳过；

        # 自动化运行时无人可确认，由 AutomationPolicy 门控替代）

        if (
            self.enable_hitl
            and self.security is not None
            and not self.security.auto_approve
            and self.automation_policy is None
            and tc.name in self.hitl_tools
        ):
            import uuid

            request_id = str(uuid.uuid4())[:8]

            # 构建确认请求的原因说明

            if tc.name == "shell":
                command = tc.arguments.get("command", "")

                reason = f"即将执行命令: {command[:100]}"

            elif tc.name == "execute_code":
                code = tc.arguments.get("code", "")

                reason = f"即将执行代码: {code[:100]}"

            else:
                reason = f"即将执行 {tc.name}"

            # 请求用户确认

            approved = await self.callbacks.on_confirm(
                request_id=request_id, tool_name=tc.name, args=tc.arguments, reason=reason
            )

            if not approved:
                obs = Observation(
                    tool_name=tc.name,
                    success=False,
                    output="用户拒绝执行此操作",
                )

                session.observations.append(obs)

                session.messages.append(
                    Message(
                        role=Role.TOOL,
                        content=obs.output,
                        metadata={"tool_name": obs.tool_name, "success": False, "call_id": call_id},
                    )
                )

                self._record_tool_result(session.id, obs.tool_name, False, obs.output)

                return

        # 事件: 工具执行前

        if self.bus:
            await self.bus.emit("tool.start", {"tool": tc.name, "args": tc.arguments})

        # 沙箱判断：根据委派深度决定是否使用沙箱

        sandbox = None

        if self.sandbox_mgr and self.sandbox_mgr.should_sandbox(self.delegate_depth):
            # 使用 session_id 作为沙箱 key，同一会话共享沙箱

            sandbox_key = f"session-{session.id}"

            sandbox = await self.sandbox_mgr.get_sandbox(sandbox_key)

        # 执行工具 — shell 工具支持流式输出，支持自修复重试

        current_tc = tc

        heal_attempt = 0

        obs = None

        if obs is None:
            while True:
                if current_tc.name == "shell":

                    def on_output(text: str):

                        asyncio.ensure_future(
                            self.callbacks.on_tool_progress(current_tc.name, "stream", text, metadata={"call_id": call_id})
                        )

                    obs = await ToolRegistry.execute(
                        current_tc,
                        on_output=on_output,
                        sandbox=sandbox,
                        session_key=session.id,  # 持久会话按对话隔离（2026-08-27）
                    )

                else:
                    obs = await ToolRegistry.execute(current_tc)

                # ── 自修复循环：失败时尝试自动修复 ──

                # 注意：安全拦截/用户拒绝是确定性结果，自修复不可能改变结局，

                # 跳过以节省 LLM 调用（此前每次拦截会白烧最多 2 次 healer 调用）

                if (
                    not obs.success
                    and self.enable_self_heal
                    and self.heal_loop
                    and heal_attempt < self.max_heal_retries
                    and not obs.output.startswith(("安全拦截", "用户拒绝执行"))
                    and await self.heal_loop.should_heal(obs)
                ):
                    heal_attempt += 1

                    await self.callbacks.on_tool_progress(
                        current_tc.name,
                        "healing",
                        f"自修复第 {heal_attempt}/{self.max_heal_retries} 次尝试...",
                    )

                    # 构建修复上下文

                    context = self._build_api_messages(session)

                    fixed_tc = await self.heal_loop.generate_fix(
                        current_tc,
                        obs,
                        context,
                    )

                    if fixed_tc and fixed_tc.arguments != current_tc.arguments:
                        # 记录修复尝试到 session 元数据（extra 字段，随会话持久化）

                        heal_meta = session.extra.setdefault("heal_attempts", [])

                        heal_meta.append(
                            {
                                "tool": current_tc.name,
                                "attempt": heal_attempt,
                                # 存原始 dict（JSON 可序列化），不再 str() 化；
                                # 历史 str 数据由 _parse_heal_args 兼容（2026-08-20）
                                "original_args": dict(current_tc.arguments),
                                "error": obs.output[:300],
                                "fixed_args": dict(fixed_tc.arguments),
                            }
                        )

                        current_tc = fixed_tc

                        continue  # 用修复后的参数重试

                    elif fixed_tc and fixed_tc.arguments == current_tc.arguments:
                        # LLM 返回了相同的参数，说明无法修复

                        break

                    else:
                        # LLM 无法生成有效修复，放弃重试

                        break

                else:
                    break

        session.observations.append(obs)

        # 工具结果缓存已移除（2026-08-14），不再写回

        # ── 技能沉淀：自愈成功后异步合成新技能 ──

        if obs.success and heal_attempt > 0 and self.skill_synthesizer:
            try:
                heal_records = session.extra.get("heal_attempts", [])

                last_record = heal_records[-1] if heal_records else {}

                await self.skill_synthesizer.on_heal_success(
                    tool_name=last_record.get("tool", tc.name),
                    original_error=last_record.get("error", ""),
                    original_args=self._parse_heal_args(last_record.get("original_args")),
                    fixed_args=self._parse_heal_args(last_record.get("fixed_args")),
                    heal_attempts=heal_attempt,
                )

            except Exception as _e:
                import logging

                logging.getLogger(__name__).debug(f"Skill synthesis failed: {_e}")

        # ── P1 工作流蒸馏追踪：记录每次工具调用（含自修复标记）──

        if self.workflow_distiller:
            try:
                self.workflow_distiller.track_tool_call(
                    tool=obs.tool_name,
                    args=current_tc.arguments,
                    success=obs.success,
                    error="" if obs.success else (obs.output or "")[:200],
                    self_fixed=(heal_attempt > 0 and obs.success),
                )

            except Exception:
                pass

        # ── P0 运行留痕：自动化工具调用写入 run 事件流 ──

        self._log_run_event(
            {
                "type": "tool",
                "tool": obs.tool_name,
                "success": obs.success,
                "ms": obs.duration_ms,
                "healed": heal_attempt > 0,
            }
        )

        # 工具输出截断到 2000 字符

        output_preview = obs.output[:2000] if obs.output else "(无输出)"

        heal_suffix = f" [自修复 {heal_attempt} 次]" if heal_attempt > 0 else ""

        # 合并 call_id 到事件 metadata，前端可据此将输出精确归属到对应的工具卡片（多工具并行时）
        _ev_meta = {"call_id": call_id}
        if obs.metadata:
            _ev_meta.update(obs.metadata)

        await self.callbacks.on_tool_progress(
            current_tc.name,
            "done" if obs.success else "error",
            f"{'完成' if obs.success else '失败'} ({obs.duration_ms}ms){heal_suffix}",
            metadata=_ev_meta,
        )

        # shell 流式输出已实时推送，不再重复推 output

        if current_tc.name != "shell":
            await self.callbacks.on_tool_progress(
                current_tc.name,
                "output",
                output_preview,
                metadata=_ev_meta,
            )

        # 工具结果消息

        tool_metadata = {"tool_name": obs.tool_name, "success": obs.success, "call_id": call_id}

        # 合并工具返回的 metadata（如 downloadable、path 等）

        if obs.metadata:
            tool_metadata.update(obs.metadata)

        # ── 策略③：工具结果"代码层瘦身"（Data Minimization）──

        # 实时任务最烧钱点：工具返回的原始数据（网页全文/搜索原文）不可缓存，

        # 且会撑爆 Input Token。统一瘦身到合理上限再写入历史：

        #   - 默认上限 3000 字符（远小于 web_fetch 全文）

        #   - 保留头部 + 尾部，中间截断（关键信息通常在头尾）

        #   - 仅作用于"写入会话历史"的副本，前端展示用 output_preview 不受影响

        # 注：shell 流式输出已在执行中实时推送，历史里瘦身无感知

        _content = obs.output or ""

        _max_tool_chars = 3000

        if len(_content) > _max_tool_chars:
            head = _content[: _max_tool_chars // 2]

            tail = _content[-_max_tool_chars // 2 :]

            _content = f"{head}\n\n...[中间内容已瘦身，节省了 {len(_content) - _max_tool_chars} 字符]...\n\n{tail}"

        session.messages.append(
            Message(
                role=Role.TOOL,
                content=_content,
                metadata=tool_metadata,
            )
        )

        # 工具统计累计（2026-08-20）：独立计数，避免被剪枝后统计失真
        self._record_tool_result(session.id, obs.tool_name, obs.success, obs.output)

        # 事件: 工具执行后

        if self.bus:
            await self.bus.emit(
                "tool.complete",
                {
                    "tool": current_tc.name,
                    "success": obs.success,
                    "duration_ms": obs.duration_ms,
                    "heal_attempts": heal_attempt,
                },
            )

        # 如果有可下载文件，发送独立的 file 事件

        if obs.metadata and obs.metadata.get("downloadable"):
            file_path = obs.metadata.get("path")

            if file_path:
                import os

                file_name = os.path.basename(file_path)

                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

                # 2026-08-12 修复: 通过 callbacks.on_file 直接推送到前端（WebSocket）

                # 此前只发 bus.emit("file")，但 WebSocket 层未订阅该事件 → 前端收不到文件卡片

                try:
                    await self.callbacks.on_file(
                        file_path=file_path,
                        file_name=file_name,
                        file_size=file_size,
                    )

                except Exception:
                    pass

                # 持久化输出文件记录到会话，重进后仍可下载/查看（此前只实时推送，
                # 不落库 → 重进会话文件卡片丢失）
                try:
                    files = session.extra.get("files", [])
                    files.append({
                        "file_path": file_path,
                        "file_name": file_name,
                        "file_size": file_size,
                        "created_at": __import__("datetime").datetime.now().isoformat(),
                    })
                    session.extra["files"] = files
                    if self.enable_persistence and self.session_store:
                        self.session_store.save_session(session)
                except Exception:
                    pass

                # bus 事件保留（供其他平台/插件监听，如 wecom/weixin 等）

                if self.bus:
                    await self.bus.emit(
                        "file",
                        {
                            "type": "file",
                            "file_path": file_path,
                            "file_name": file_name,
                            "file_size": file_size,
                        },
                    )

    def _build_api_messages(self, session: Session) -> list[dict]:
        """构建发送给 LLM 的消息列表.



        - 使用 self.system_prompt 作为唯一 system 消息

        - 跳过所有 role=SYSTEM 的历史消息（动态内容已移入 runtime_context）

        """

        # ── 使用 Agent 构造时确定的 system prompt ──

        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]

        # 仅对当轮（最后一条 user 消息）注入 runtime_context，历史轮次的注入内容不再重复发送
        _last_user_rt = ""
        for _msg in reversed(session.messages):
            if _msg.role == Role.USER and _msg.metadata.get("runtime_context"):
                _last_user_rt = _msg.metadata["runtime_context"]
                break

        for msg in session.messages:
            # ── SYSTEM 消息：仅保留压缩器生成的 [对话摘要]，其余动态内容已移入 runtime_context ──

            if msg.role == Role.SYSTEM:
                if msg.content and msg.content.startswith("[对话摘要]"):
                    messages.append({"role": "system", "content": msg.content})
                continue

            elif msg.role == Role.USER:
                messages.append({"role": "user", "content": msg.content})

            elif msg.role == Role.ASSISTANT:
                if msg.metadata.get("tool_calls"):
                    messages.append(
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": tc.get("call_id", f"call_{i}"),
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": json.dumps(
                                            tc["arguments"], ensure_ascii=False
                                        ),
                                    },
                                }
                                for i, tc in enumerate(msg.metadata["tool_calls"])
                            ],
                        }
                    )

                else:
                    # ── v3-Final P0: assistant 消息已在写入时 sanitize，此处原样返回 ──

                    messages.append({"role": "assistant", "content": msg.content})

            elif msg.role == Role.TOOL:
                call_id = msg.metadata.get("call_id", "call_0")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": msg.content,
                    }
                )

        # ── 当轮 runtime_context 注入（仅最后一条 user 消息，不落库） ──

        if _last_user_rt:
            for _m in reversed(messages):
                if _m["role"] == "user":
                    _m["content"] = _m["content"] + "\n\n" + _last_user_rt
                    break

        # ── v3-Final P0: 断言仅 1 条非摘要 system 消息（[对话摘要] 不计入，保证长对话压缩后上下文不丢失） ──

        system_count = sum(
            1
            for m in messages
            if m["role"] == "system" and not m["content"].startswith("[对话摘要]")
        )

        assert system_count == 1, f"检测到 {system_count} 条主 system 消息，破坏缓存前缀！"

        return messages

    async def cleanup(self):
        """清理资源（沙箱容器等）."""

        if hasattr(self, "sandbox_mgr") and self.sandbox_mgr:
            try:
                await self.sandbox_mgr.cleanup()

            except Exception as e:
                import logging

                logging.getLogger(__name__).debug(f"Sandbox cleanup failed: {e}")
