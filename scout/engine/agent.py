"""Agent 核心循环 — 平台无关的 ReAct 引擎.



融合 Hermes + OpenClaw + CowAgent 三者优势：

- Hermes: ReAct 循环 + 可中断执行 + 回调面 + Provider fallback + 子代理委派

- OpenClaw: 上下文治理 + 安全审批 + 事件总线 + Cron

- CowAgent: 技能匹配 + 工作空间上下文 + 记忆系统

"""

from __future__ import annotations
import asyncio
import copy
import logging
import time
import json
import re
import uuid
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from scout.core.callbacks import Callbacks, NullCallbacks
from scout.core.types import (
    Message,
    Role,
    Session,
    ToolCall,
)

from scout.engine.budget import AdaptiveBudget, IterationBudget, make_budget

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

from scout.engine.interrupt import InterruptibleExecutor


# ── 图片直收（2026-09-24；2026-09-26 V2 调整）─────────────────────────────
# 视觉能力开启时，聊天里发的图片会作为 image_url 内容直接进 LLM 消息。
# 这里只限"张数"；单张的边长与字节预算改由 scout/llm/image_prep.py 统一负责
# （降采样 + PNG/JPEG 择优 + 结果缓存）。原来的 `_IMAGE_MAX_BYTES = 5MB` 粗筛
# 已删除 —— 它的实际效果是"大图静默丢弃"，实测一张 14MB 截图降采样后只有
# 1.2MB，本可以正常送达；静默丢弃还会让模型以为自己看见了图。
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMAGE_MAX_COUNT = 4



def _is_image_attachment(att: Any) -> bool:
    """附件是否图片（按 mime 或扩展名判断）."""
    if not isinstance(att, dict):
        return False
    mime = str(att.get("type") or "").lower()
    if mime.startswith("image/"):
        return True
    name = str(att.get("name") or "").lower()
    return any(name.endswith(ext) for ext in _IMAGE_EXTS)

# ★ 2026-09-25 启动减负（Windows 实测）：LLMClient 在本文件里**只用作类型注解**
# （__init__ 的 `llm: LLMClient`），而文件头已有 `from __future__ import annotations`
# ——注解是字符串，运行时根本不需要这个类。此前写成顶层导入，会先初始化包
# `scout.llm`，其 `__init__.py` 再 eager 导入 providers.openai → 拉起整个 openai SDK
# （连带 aiohttp / httpx2），单这一条链在 Windows 上实测 1.13 s（`import
# scout.engine.agent` 1593ms → 桩掉 openai 后 458ms），打包版 PYZ 里 openai 占
# 1524/4669 个模块。放进 TYPE_CHECKING 后 CLI/桌面服务冷启动直接省掉这 1 秒。
if TYPE_CHECKING:
    from scout.llm.base import LLMClient

from scout.tools.registry import ToolRegistry


# ── v3-Final 优化模块 ──

from scout.engine.cache_monitor import get_cache_monitor

from scout.engine.failover import get_failover_manager

from scout.engine.sanitize import sanitize_assistant_output

# A2（2026-09-14）：工具执行域分离 —— 执行编排/自愈/留痕/瘦身/文件推送
from scout.engine.tool_executor import ToolExecutionMixin
# A3（2026-09-14）：上下文注入链分离（注入 → 运行上下文 → 环境上下文）
from scout.engine.context_inject import ContextInjectMixin

# A1（2026-09-14）：回合护栏/收尾公共件 —— stream/_run_react 双轨共用
from scout.engine.loop_common import check_turn_budget, finish_reason


class Agent(ToolExecutionMixin, ContextInjectMixin):
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
        max_loop_seconds: int = 3600,  # 2026-09-08：回合总时长看门狗默认 3600s（桌面 GUI 任务单步 5~60s，长任务易超 1800s）
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
        permission_mode: str = "ask",  # ask / auto / strict（输入框权限开关，2026-09-21）
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
        # ── 模型能力（2026-09-24，用户可在设置里配）──
        reasoning_effort: str = "auto",  # auto / off / low / medium / high
        vision_input: bool | None = None,  # None=按模型能力自动判断；True/False=用户强制
        model_provider: str = "",  # 用于能力解析（思考参数风格/视觉）的厂商标识
    ):

        self.llm = llm

        self.deep_thinking = deep_thinking
        self.reasoning_effort = str(reasoning_effort or "auto").lower()
        self.vision_input = vision_input
        self.model_provider = str(model_provider or "")

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
                "- scheduler: 定时任务和提醒\n"
                "- ask_user: 有疑惑时向用户提问澄清，等回答后再继续（可附 2-4 个选项）\n\n"
                "## Subagent Delegation (子代理委派)\n"
                "你有隔离的子代理可以委派子任务（delegate_task 串行 / parallel_delegate 并行 / collaborate_task 自动分解协作）。\n"
                "主流实践（Claude Code / Codex 同款）：主 agent 保持 ReAct 循环，把**独立且繁重**的子任务派给子代理，"
                "自己只做拆解、整合与决策——子代理的几十步过程不占用你的上下文，你只收到它的最终结论。\n"
                "- **该委派**：任务含 2+ 个互不依赖的子目标（如并行搜索多个主题、分别处理多个文件）→ parallel_delegate；"
                "资料收集/多步分析这类重过程子任务 → delegate_task 串行委派。\n"
                "- **不委派**：单步能完成的（快速查询、单个文件读写、简单问答）——委派反而更慢（子代理冷启动）。\n"
                "- 委派时给出**自包含的任务描述**（目标、验收标准、需要的上下文），子代理看不到你们的对话历史。\n"
                "- 收到子代理结论后直接整合进答案，不要重复验证（除非结论互相矛盾）。子代理有独立上下文，"
                "它们的执行过程对你不可见也不需要可见。\n\n"
                "## Tool Call Efficiency (工具调用效率)\n"
                "为减少决策轮数、更快完成任务：\n"
                "- **一次决策可返回多个独立工具调用**：当多个操作互不依赖、可同时推进时（如搜索多个不同主题、读取多个文件、并行查询多个来源），在同一次回复里一次性返回多个 tool_call，不要逐个串行。\n"
                "- 保持连续的工具调用链条：前一个工具的结果刚产生、下一步动作明确时，直接继续调用下一个工具，不要中途停顿或重复陈述。\n"
                "- 避免不必要的中间回复：需要用户澄清时调用 ask_user 工具（会暂停等你回答），否则持续调用工具直到任务完成，再输出最终结果。\n"
                "- 只调用完成任务真正需要的工具，不做多余的探索。\n\n"
                "## When to Ask the User（用户澄清）\n"
                "有疑惑时交给用户澄清，而不是猜：\n"
                "- **该问**：需求存在歧义（\"处理一下那个文件\"——哪个？）、有多种做法且选择影响结果（覆盖还是新建？）、"
                "缺少关键信息（目标路径/范围/格式/验收标准）、或下一步操作有破坏性风险。\n"
                "- **不问**：能从上下文/记忆/文件中查到的事实、对结果影响很小的实现细节、或连续追问已回答过的问题——"
                "这类自己查证后决定，不要把思考转嫁给用户。\n"
                "- 调用 ask_user 时把问题写具体（一句话点明疑惑），候选项给 2-4 个互斥且描述清楚的做法；"
                "用户回答后立即据此继续，不要重复确认。\n\n"
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
                "## Document Edit Rules（重要）\n"
                '- 用户要求"写入/更新/加到/补充到"某文档（简历、报告、项目经历等）时 → **必须用 file 工具'
                "实际执行编辑**（此前生成过的产物优先在原文件上迭代），完成后**用 send_file 把文件推送给"
                "用户**（前端显示下载卡片）并说明改了什么——**只输出建议文本而不动文件 = 未完成任务**。\n"
                "- 不确定写入哪个文件时：先找此前产物（工作目录 outputs/ 下）或列候选问一次用户，"
                "不要因此转入长篇分析。\n"
                "## Response Guidelines\n"
                "- 用与用户输入相同的语言回复\n"
                "- 回答结构清晰，善用加粗、列表、分段\n"
                "- 引用搜索结果时标注来源链接\n"
                '- 不确定时说"不确定"，不要编造\n'
                "- **不要未经要求主动生成/发送文件**；但用户明确要求写入/更新文档时必须执行文件编辑"
                "（见 Document Edit Rules），此时仅文本回复是错误的。\n"
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
                "- scheduler: 定时任务和提醒\n"
                "- ask_user: 有疑惑时向用户提问澄清，等回答后再继续（可附 2-4 个选项）\n\n"
                "## Subagent Delegation (子代理委派)\n"
                "你有隔离的子代理可以委派子任务（delegate_task 串行 / parallel_delegate 并行 / collaborate_task 自动分解协作）。\n"
                "主流实践（Claude Code / Codex 同款）：主 agent 保持 ReAct 循环，把**独立且繁重**的子任务派给子代理，"
                "自己只做拆解、整合与决策——子代理的几十步过程不占用你的上下文，你只收到它的最终结论。\n"
                "- **该委派**：任务含 2+ 个互不依赖的子目标（如并行搜索多个主题、分别处理多个文件）→ parallel_delegate；"
                "资料收集/多步分析这类重过程子任务 → delegate_task 串行委派。\n"
                "- **不委派**：单步能完成的（快速查询、单个文件读写、简单问答）——委派反而更慢（子代理冷启动）。\n"
                "- 委派时给出**自包含的任务描述**（目标、验收标准、需要的上下文），子代理看不到你们的对话历史。\n"
                "- 收到子代理结论后直接整合进答案，不要重复验证（除非结论互相矛盾）。子代理有独立上下文，"
                "它们的执行过程对你不可见也不需要可见。\n\n"
                "## Tool Call Efficiency (工具调用效率)\n"
                "为减少决策轮数、更快完成任务：\n"
                "- **一次决策可返回多个独立工具调用**：当多个操作互不依赖、可同时推进时（如搜索多个不同主题、读取多个文件、并行查询多个来源），在同一次回复里一次性返回多个 tool_call，不要逐个串行。\n"
                "- 保持连续的工具调用链条：前一个工具的结果刚产生、下一步动作明确时，直接继续调用下一个工具，不要中途停顿或重复陈述。\n"
                "- 避免不必要的中间回复：需要用户澄清时调用 ask_user 工具（会暂停等你回答），否则持续调用工具直到任务完成，再输出最终结果。\n"
                "- 只调用完成任务真正需要的工具，不做多余的探索。\n\n"
                "## When to Ask the User（用户澄清）\n"
                "有疑惑时交给用户澄清，而不是猜：\n"
                "- **该问**：需求存在歧义（\"处理一下那个文件\"——哪个？）、有多种做法且选择影响结果（覆盖还是新建？）、"
                "缺少关键信息（目标路径/范围/格式/验收标准）、或下一步操作有破坏性风险。\n"
                "- **不问**：能从上下文/记忆/文件中查到的事实、对结果影响很小的实现细节、或连续追问已回答过的问题——"
                "这类自己查证后决定，不要把思考转嫁给用户。\n"
                "- 调用 ask_user 时把问题写具体（一句话点明疑惑），候选项给 2-4 个互斥且描述清楚的做法；"
                "用户回答后立即据此继续，不要重复确认。\n\n"
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
                "## Document Edit Rules（重要）\n"
                '- 用户要求"写入/更新/加到/补充到"某文档（简历、报告、项目经历等）时 → **必须用 file 工具'
                "实际执行编辑**（此前产物优先在原文件上迭代），完成后**用 send_file 把文件推送给用户**"
                "（前端显示下载卡片）并说明改动——只输出建议文本而不动文件 = 未完成任务；"
                "不确定写入哪个文件时先找产物或问一次，不要转入长篇分析。\n\n"
                '用与用户输入相同的语言回复。回答结构清晰。不确定时说"不确定"，不要编造。\n'
                "不要未经要求主动生成/发送文件；但用户明确要求写入/更新文档时必须执行文件编辑，"
                "此时仅文本回复是错误的。\n"
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

        # ── 产物目录约定（2026-09-04）：Agent 生成的文件统一收纳 ──
        # 注入在平台提示之后、所有模式共享；路径进程内固定，不破坏前缀缓存。
        from scout.config.paths import OUTPUTS_DIR as _OUTPUTS_DIR

        self.system_prompt += (
            "\n## File Outputs Convention\n"
            f"Default output directory for ALL files you generate (reports, exports, "
            f"converted/processed files, intermediate artifacts): {_OUTPUTS_DIR}\n"
            "Create it if missing (mkdir -p). Write there unless the user explicitly names "
            "another location; NEVER default to Desktop/Downloads/system dirs. When sending "
            "the file to the user afterwards, reference this absolute path.\n"
        )

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

        # ── 2026-09-20 自适应步数预算 ──
        # 旧模型：max_turns 一把梭（用户被逼到 500），简单任务也有 500 步空转空间，
        # 真长任务撞到硬墙又被腰斩。改为：小基准起步 + 有进展续期 + 无进展早停。
        # 关法：SCOUT_ADAPTIVE_BUDGET=0（退回固定 max_turns，行为与旧版一致）。
        self.adaptive_budget = str(
            os.environ.get("SCOUT_ADAPTIVE_BUDGET", "1")
        ).strip().lower() not in ("0", "false", "no", "off")

        self.max_loop_seconds = max(1, int(max_loop_seconds or 3600))

        # ── 2026-09-06 回合输入 token 熔断阈值 ──
        # 单回合(一次 run_conversation/stream_conversation)累计"新增(非缓存)输入"超过该值
        # 即强制收尾(走预算耗尽强制总结路径)。防止"截图→看→没进展→再截图"类空转把
        # 十几万 token 烧完(实测 292.9k token 案例: 35 轮输入 27.9 万, 输出仅 5%)。
        # 2026-09-06 二轮: 100k 对 GUI 任务过紧(实测 step=18 刚打开软件就被掐), 提到 250k。
        # 2026-09-06 三轮: 口径改为"prompt - cached"——缓存命中的前缀重放不计入熔断
        # (API 对缓存只收新计算的零头)，250k 实际可支撑步数提高约 20 倍；真实空转
        # (每次新截图/新输出都是未缓存内容)仍会累计新 token，止损能力不变。
        # 仍可用环境变量 SCOUT_TURN_INPUT_LIMIT 按需调整。
        self._turn_input_limit = int(os.environ.get("SCOUT_TURN_INPUT_LIMIT", "250000"))

        # 长链里程碑压缩频率（2026-09-06）：GUI/工具长链"消息短小、步数多"，
        # max_tokens 预算与 80 条消息双门槛触发太迟；每 N 步强制一次阶段摘要压缩，
        # 让回合中段历史保持低位、单次调用输入不再线性膨胀。
        # 可用环境变量 SCOUT_MILESTONE_EVERY 调整。
        self._milestone_every = int(os.environ.get("SCOUT_MILESTONE_EVERY", "15"))

        # ★ 2026-09-19 压缩冷却：两次压缩之间至少间隔 N 步（可用
        # SCOUT_COMPRESS_COOLDOWN 调整，0 = 不冷却）。
        # 背景：压缩本身是一次**完整 LLM 摘要调用**。实测会话 4126a066（454 步）
        # 触发了 215 次压缩——视图里堆积的孤儿摘要让 needs_compression 恒为真，
        # 于是「每 2 步压一次」，光摘要调用就吃掉一大块预算。任何判据抖动都可能
        # 复现这种高频压缩，冷却是最省事的兜底闸。
        # 例外：真实 token 已超预算时不冷却（该压就得压），见 _context_govern。
        self._compress_cooldown = int(os.environ.get("SCOUT_COMPRESS_COOLDOWN", "3"))
        self._last_compress_step: dict[str, int] = {}

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

        # ── 两阶段按需加载（2026-09-24）──────────────────────────────
        # 开启后：常驻前缀只带「极小核心集完整 schema + 全量工具目录(name+一句话)」，
        # 其余工具由 LLM 调 load_tools 按需展开。相比旧的关键词渐进式加载，
        # 常驻 token 从 compact 全量(~3.7k) 降到核心集+目录(~1.5k)，且不随会话单调膨胀。
        # SCOUT_TOOL_LAZY_LOAD=0 可回退到旧行为（全量 compact 常驻）。
        self._tool_lazy_load = os.getenv("SCOUT_TOOL_LAZY_LOAD", "1") not in ("0", "false", "no")
        # 当前轮次所属会话（供 load_tools 把已加载工具名写入 session.extra 跨轮持久）。
        # 与既有 _active_tool_schemas 同属「单活跃轮次」并发模型，每轮 _inject_context 刷新。
        self._current_session: Session | None = None

        # 注册主 Agent 引用（供 delegate_task 工具使用）— 子代理不覆盖主引用

        if register_as_main:
            ToolRegistry._main_agent = self

        # 上下文治理

        self.enable_context = enable_context

        if enable_context:
            from scout.context.manager import ContextManager

            # token 预算（2026-08-30 + 2026-09-05 默认开启）：
            # 优先级 config.context_max_tokens > env SCOUT_CONTEXT_MAX_TOKENS > 默认 32768。
            # 此前默认 0=仅按条数治理：长工具输出（搜索/抓取全文可达数万字符）会按原始体积
            # 反复全量重发，是多步/GUI 长链任务 token 消耗的主要来源之一。
            # 默认开启后按 token 即时剪枝超大输出 + 提前触发压缩。
            # 仅 32K 以下小窗口模型请用 SCOUT_CONTEXT_MAX_TOKENS 调低（如 16384）。
            _max_tokens = 0
            try:
                from scout.config.manager import ConfigManager

                _cfg = ConfigManager().load()
                _max_tokens = int(getattr(_cfg, "context_max_tokens", 0) or 0)
            except Exception:
                _max_tokens = 0
            if not _max_tokens:
                try:
                    _max_tokens = int(os.getenv("SCOUT_CONTEXT_MAX_TOKENS", "0") or 0)
                except ValueError:
                    _max_tokens = 0
            if not _max_tokens:
                _max_tokens = 32768
            self.context_mgr = ContextManager(max_tokens=_max_tokens)

        else:
            self.context_mgr = None

        # ★ 2026-09-19 省 token 开关（都是「体验功能 / 后台沉淀」，不是主链路）
        #   SCOUT_MEMORY_EXTRACT_EVERY : 记忆抽取每几个回合跑一次（默认 3，1=每回合）
        #   SCOUT_SUGGEST_ENABLED      : 追问建议（每回合 1 次 LLM 调用，0=关闭）
        try:
            self._memory_extract_every = max(
                1, int(os.getenv("SCOUT_MEMORY_EXTRACT_EVERY", "1") or 1)
            )
        except ValueError:
            self._memory_extract_every = 3
        self._suggest_enabled = os.getenv("SCOUT_SUGGEST_ENABLED", "1") not in ("0", "false", "no")

        # 会话持久化

        self.enable_persistence = enable_persistence

        # ★ 2026-09-23 活跃会话注册表（上下文圆环实时性）：
        # /api/context/stats 优先读这里的内存 session——生成期间消息只 append
        # 进内存对象、回合结束才落盘，此前 stats 读磁盘版导致整轮生成中数值不动。
        # 注意：各端点用 copy.copy(agent) 换 callbacks，浅拷贝共享本 dict 的引用
        # （不会各自复制一份），agent_copy 的注册对原始 agent 的 stats 可见。
        # 只保留最近 8 个，回合结束后残留条目与磁盘内容一致（stale 无害），
        # 下次同 sid 生成自然覆盖。
        self._active_sessions: dict[str, Session] = {}

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

            self.security = SecurityManager(auto_approve=auto_approve, permission_mode=permission_mode)

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
            # ★ A2（2026-09-14）顺带修复：原代码用裸 logger（本文件无模块级
            # logger 定义，惯例为内联 logging.getLogger）——此分支一旦触发
            # 即 NameError 掩盖真实错误。
            logging.getLogger(__name__).warning("未知 SCOUT_SANDBOX_MODE=%s，回退 off", _sandbox_mode)
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
                from scout.engine.skills.synthesizer import SkillSynthesizer

                from scout.engine.skills.retriever import SkillRetriever

                from scout.engine.skills.store import VectorSkillStore

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
            from scout.engine.reflexion import ReflexionLoop

            self.reflexion_loop = ReflexionLoop(
                llm=self.llm,
                enable_deep_reflect=True,
                failure_threshold=3,  # 2026-09-09：2→3，试错自纠不应频繁触发反思 LLM 调用
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
                from scout.engine.skills.distiller import WorkflowDistiller

                self.workflow_distiller = WorkflowDistiller(
                    skill_mgr=self.skill_mgr,
                    llm_client=self.llm,
                )

            except Exception as _e:

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

                logging.getLogger(__name__).debug(
                    f"插件 {getattr(plugin, 'name', '?')} after_chat 失败", exc_info=True
                )
        return response

    def _register_active_session(self, session: Session) -> None:
        """注册活跃会话（上下文圆环实时读取用，2026-09-23）.

        stats 接口优先读内存版 session；超限淘汰最旧的。
        只做原地写（不重绑 dict），保证 copy.copy 的 agent 副本共享同一注册表。
        """
        try:
            sid = str(session.id)
            self._active_sessions[sid] = session
            while len(self._active_sessions) > 8:
                oldest = next(iter(self._active_sessions))
                self._active_sessions.pop(oldest, None)
        except Exception:  # noqa: BLE001 — 注册失败不影响主流程
            pass

    def _prepare_turn_state(self, session: Session) -> IterationBudget:
        """公共前置：本轮 budget 初始化 + 工具统计计数器重置（run/stream 共用）."""

        budget = make_budget(self.max_turns, adaptive=self.adaptive_budget)
        self._budget_hint_stage = 0  # 2026-09-20：自适应预算提示阶段（0/1/2）
        self._tool_stats[session.id] = {
            "total": 0, "ok": 0, "fail": 0, "tools": {}, "fail_tools": {}, "snippets": [],
        }
        return budget

    def _budget_step_hint(self, budget: IterationBudget) -> str | None:
        """自适应预算的进度提示（2026-09-20）：把步数决策权交给模型.

        旧模型里模型对步数一无所知——它不知道自己跑了 24 步还是 90 步，
        于是"该收尾了"只能靠外部硬墙来判。这里在关键节点注入一次提示，
        让模型自己判断"任务是否已完成 / 还需几步 / 是否在原地打转"。

        两个时机各注入一次（用 _budget_hint_stage 防重复，避免每步都塞消息）：
        1) 首次续期后：告知已用步数、已续期，请评估是否接近完成；
        2) 逼近 hard_max：只剩最后几步，请基于已有成果收尾。
        """
        if not isinstance(budget, AdaptiveBudget):
            return None
        stage = getattr(self, "_budget_hint_stage", 0)
        remaining = budget.max_turns - budget.current

        if stage < 1 and budget.granted >= 1 and budget.max_turns < budget.hard_max:
            self._budget_hint_stage = 1
            return (
                f"【步数提示】本轮已执行 {budget.current} 步（预算已按需续期至 "
                f"{budget.max_turns} 步，硬上限 {budget.hard_max}）。"
                "请评估当前进度：若目标已达成，直接给出最终成果不要再调用工具；"
                "若仍有明确且必要的剩余步骤，简要说明还差什么再继续；"
                "若发现自己在重复类似操作而没有新信息，立即换路线或如实汇报卡点。"
            )
        if stage < 2 and remaining <= 5:
            self._budget_hint_stage = 2
            return (
                f"【步数提示·最后 {max(1, remaining)} 步】本轮步数即将用尽"
                f"（已执行 {budget.current} / {budget.max_turns}）。"
                "立即停止开启新的探索，直接基于已获取的信息输出最终成果或当前进度；"
                "未完成的剩余部分请明确列出，用户回复「继续」可在新回合接着做。"
            )
        return None

    def _step_progress_calls(
        self, session: Session, mark: int
    ) -> list[tuple[str, bool, str]]:
        """取本步新增的 TOOL 消息作为进展信号（供 AdaptiveBudget.observe）.

        mark: 本步工具执行前的 ``len(session.messages)``。必须在上下文治理
        （_context_govern 会剪枝删旧消息）**之前**取增量，否则索引错位。
        """
        out: list[tuple[str, bool, str]] = []
        try:
            from scout.core.types import Role as _Role

            for m in session.messages[mark:]:
                if getattr(m, "role", None) != _Role.TOOL:
                    continue
                md = getattr(m, "metadata", None) or {}
                out.append(
                    (
                        str(md.get("tool_name", "") or ""),
                        bool(md.get("success", False)),
                        (getattr(m, "content", "") or "")[:300],
                    )
                )
        except Exception:  # noqa: BLE001 - 观测失败不应影响主流程
            return out
        return out

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
        # ★ 2026-09-09：loop.agent 持有的是构造时的原始 agent 实例。
        # 各端点用 copy.copy(agent) 换 callbacks 时，浅拷贝共享的 loop 仍指回
        # 原 agent → 副本上的 callbacks 从未生效（chat SSE 全程零事件；
        # webhook/A2A/voice 设的 NullCallbacks 静音同样无效，事件一直泄漏
        # 给原始 agent 的回调）。执行前把 loop 的 agent 重绑到 self，
        # 一处修复覆盖全部调用点；原始 agent 自调用时为无操作。
        if getattr(self.loop, "agent", None) is not self:
            import copy as _copy
            loop_copy = _copy.copy(self.loop)
            loop_copy.agent = self
            return await loop_copy.run(user_message, session, attachments)
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

        # ★ 注册活跃会话 → /api/context/stats 生成期间可读到实时消息
        self._register_active_session(session)

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

        # ★ 2026-09-14：回合起点强制落盘 —— 用户消息与新会话立即可见/可恢复。
        # 此前首条落盘要到「回合收尾」或「首次工具执行完成」才发生，而工具执行
        # 可能持续数十秒（GUI/长命令），期间强杀/重启会丢掉**整个回合**（实测：
        # 中途 kill 后新会话甚至不出现在会话列表里）。
        await self._persist_progress(session, force=True)

        # ★ 注册活跃会话 → /api/context/stats 生成期间可读到实时消息
        # （工具输出 append 只进内存，落盘要到回合收尾；见 __init__ 注释）
        self._register_active_session(session)

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

        # 防空转看门狗触发计数（同一回合内提示 2 次仍无进展则强制收尾，2026-09-05）
        _wd_trips = 0

        # 空输出保护计数（2026-09-06）：连续空回复(无内容无工具调用)≥2 次则按失败收尾，
        # 禁止"哑火即 done"把没完成的任务谎报完成
        _empty_replies = 0

        # token 熔断标志（2026-09-06）：break 收尾文案据此区分"熔断"与"步数上限"，
        # 避免把 max_turns(可能很大)谎报成实际执行步数
        _fused_by_token = False

        # ★ 2026-09-10 熔断软预警标志（50%/75% 两级）：熔断前给 agent 预算信号
        _budget_warned = [False, False]

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

            await self.callbacks.on_step(budget.current, getattr(
                budget, "display_max", budget.max_turns
            ))

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

                _msg_mark = len(session.messages)  # 2026-09-20：进展观测基线（剪枝前）

                if _read_tcs:
                    await asyncio.gather(
                        *[
                            self._execute_single_tool(session, tc, f"call_{budget.current}_{idx}")
                            for idx, tc in _read_tcs
                        ]
                    )

                for idx, tc in sorted(_write_tcs, key=lambda x: x[0]):
                    await self._execute_single_tool(session, tc, f"call_{budget.current}_{idx}")

                # ── 2026-09-20 自适应预算观测：有进展→按需续期；连续无进展→判死循环 ──
                if isinstance(budget, AdaptiveBudget):
                    budget.observe(self._step_progress_calls(session, _msg_mark))
                    _bh = self._budget_step_hint(budget)
                    if _bh:
                        session.messages.append(
                            Message(
                                role=Role.SYSTEM,
                                content=_bh,
                                metadata={"type": "budget_hint"},
                            )
                        )

                # ── 防空转看门狗（2026-09-05）：同参重试/零进展 → 注入中断提示 ──
                _wd_hint = self._watchdog_hint(session.id)
                if _wd_hint:
                    _wd_trips += 1
                    session.messages.append(
                        Message(role=Role.USER, content=_wd_hint, metadata={"watchdog": True})
                    )
                    # ★ 2026-09-24：首次空转征询用户「继续/停止」（每回合限一次），
                    #   与 stream_conversation 路径一致。停止 → break 走正常收尾。
                    if _wd_trips == 1:
                        _keep = True
                        try:
                            _st = self._tool_stats.get(session.id) or {}
                            _keep = await self.callbacks.on_watchdog(
                                "检测到任务可能在原地打转（重复失败或长时间无进展）。"
                                "要继续尝试，还是停止本次任务？",
                                {
                                    "steps": budget.current,
                                    "tool_calls": _st.get("total", 0),
                                    "ok": _st.get("ok", 0),
                                },
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001
                            _keep = True
                        if not _keep or self._cancelled:
                            logging.getLogger(__name__).info(
                                "看门狗：用户选择停止（session=%s step=%s）", session.id, budget.current
                            )
                            break
                    if _wd_trips >= 2:
                        logging.getLogger(__name__).warning(
                            "看门狗已提示 2 次仍无进展（session=%s step=%s），强制收尾",
                            session.id,
                            budget.current,
                        )
                        break

                # ── 回合预算护栏（软预警+熔断，A1 收敛为双轨共用 loop_common）──
                # 2026-09-10 引入（腾讯会议死磕教训），2026-09-14 抽出双轨共用。
                _, _fused_by_token = await check_turn_budget(
                    self, session, _turn_start_ts, budget.current, _budget_warned,
                )
                if _fused_by_token:
                    break

                # 工具执行后治理（2026-09-06）：剪枝归档 + token 超预算即时压缩 +
                # 长链里程碑摘要压缩（react/stream 共用 _context_govern）
                await self._context_govern(session, budget.current)

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
                            budget_max=getattr(budget, "display_max", budget.max_turns),
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

                # ── 2026-09-06 空输出保护: 模型"哑火"(无内容、无工具调用)不得静默 done ──
                # 背景: GUI 自动化任务曾出现模型某轮吐空 → 这里直接 status="done"，
                # 任务实际没完成却被当成功收尾(且不重试、不汇报)。现改为:
                # 第 1 次空回复 → 注入纠错提示再给一次机会；连续 ≥2 次 → 按失败如实收尾。
                if not _final_content.strip():
                    _empty_replies += 1
                    if _empty_replies < 2:
                        session.messages.append(
                            Message(
                                role=Role.USER,
                                content=(
                                    "【系统提示】你刚才没有产生任何输出内容（空回复），也没有调用工具。"
                                    "请继续完成用户的任务：需要更多信息就先调用对应工具获取，"
                                    "能够作答就直接给出最终回答。不要静默结束。"
                                ),
                                metadata={"watchdog": True},
                            )
                        )
                        continue
                    _final_content = (
                        "⚠️ 任务未能完成：模型连续两次空回复（无内容、无工具调用），"
                        "疑似输出中断或陷入死循环。请检查模型服务状态后重试。"
                    )

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
                # ★ 2026-09-16：改为**后台**执行 —— 抽取是一次完整 LLM 调用
                # （实测 10~25s），此前在 return 前 await，用户每回合白等一次。
                self._spawn_bg(self._maybe_extract_session_memory(session))

                return {
                    "response": _resp_text,
                    "session": session,
                    "steps": budget.current,
                    "usage": self._collect_turn_usage(session.id, _turn_start_ts, _turn_usage),
                }

        # 预算耗尽 / 熔断 / 超时收尾
        # 2026-09-07：推断真实收尾原因，修复"任何原因都谎报为步数上限"的误导文案
        # （54 步/500 上限被时间或看门狗收尾时，旧文案显示"达到步数上限（500 步）"）
        _reason = finish_reason(
            _fused_by_token, _wd_trips,
            time.monotonic() > _turn_deadline, self._cancelled,
            stalled=getattr(budget, "stalled", False),
        )

        budget_msg = self._build_budget_exhausted_msg(session, budget.current, reason=_reason)

        # ── 2026-08-20：预算耗尽强制总结（与 stream 路径一致）──
        forced = ""
        if (
            not self._cancelled
            and self.llm is not None
            and (self._tool_stats.get(session.id, {}).get("total", 0) or 0) > 0
        ):
            forced = await self._force_final_output(session)

        if _fused_by_token:
            # ── 2026-09-06：token 熔断收尾——如实说明原因，不谎报"已达步数上限" ──
            _base = forced or budget_msg
            final_text = _base + (
                "\n\n---\n\n⚠️ 本回合新增输入 token（不含缓存重放）已达熔断阈值（"
                + str(self._turn_input_limit)
                + "），已提前终止以避免继续计费膨胀。任务可能尚未完成，以上为当前进度。"
                "回复「继续」可在新回合中续跑（预算重新计算）。"
                "续跑时请**基于本对话中已有的观察结果与最后截图直接继续执行**，"
                "不要从头重新探索界面。"
            )
        elif forced:
            final_text = (
                forced
                + "\n\n---\n\n⚠️ 本轮已执行 "
                + str(budget.current)
                + " 步后收尾（"
                + ("检测到连续无进展，判定为原地打转" if _reason == "stalled"
                   else "达到步数上限 " + str(getattr(budget, "display_max", self.max_turns)) + " 步")
                + "），以上成果已基于本轮获取的信息生成。如需继续完善可回复「继续」。"
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
        self._spawn_bg(self._maybe_extract_session_memory(session))  # ★ 2026-09-16 后台化

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

        # ★ 2026-09-14：回合起点强制落盘 —— 用户消息与新会话立即可见/可恢复。
        # 此前首条落盘要到「回合收尾」或「首次工具执行完成」才发生，而工具执行
        # 可能持续数十秒（GUI/长命令），期间强杀/重启会丢掉**整个回合**（实测：
        # 中途 kill 后新会话甚至不出现在会话列表里）。
        await self._persist_progress(session, force=True)

        # ★ 注册活跃会话 → /api/context/stats 生成期间可读到实时消息
        # （工具输出 append 只进内存，落盘要到回合收尾；见 __init__ 注释）
        self._register_active_session(session)

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

        # 回合输入 token 熔断起点（2026-09-06，与 _run_react 路径一致）
        _turn_start_ts = time.time()

        # 防空转看门狗触发计数（2026-09-05）：同回合内提示 2 次仍无进展则强制收尾，与 _run_react 一致
        _wd_trips = 0

        # 空输出保护计数（2026-09-06）：连续空回复(无内容无工具调用)≥2 次则按失败收尾，
        # 与 _run_react 路径一致，禁止"哑火即 done"把没完成的任务谎报完成
        _empty_replies = 0

        # token 熔断标志（2026-09-06，与 _run_react 路径一致）
        _fused_by_token = False

        # ★ 2026-09-10 熔断软预警标志（50%/75% 两级），与 _run_react 路径一致
        _budget_warned = [False, False]

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


                # 工具分配：所有路由都带工具（单模型结构化 tool_calls）

                active_tools = self._active_tool_schemas if self._active_tool_schemas else None

                # 2026-08-12：带工具时禁用 enable_thinking——

                # 深度思考 + 工具调用会显著变慢甚至超时（实测 thinker 带工具+思考 90s 超时，

                # 关闭思考后 5-10s 正常）。单模型带工具干活走结构化 tool_calls，快且稳。

                # 2026-08-12 修复：按 deep_thinking 动态设置 enable_thinking，让"思考"模式真正开启思维链

                # （快速模式关闭思维链，思考模式开启思维链）。超时由 stream_timeout(300s) 保护，不会卡死。

                # 按「思考强度档位」+ 模型能力生成思考参数（2026-09-24）
                active_extra = {"extra_body": self._thinking_extra()}

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
                        # 思考参数不被上游接受时（400：enable_thinking /
                        # reasoning_effort / thinking_budget / reasoning …），
                        # 去掉整包 extra_body 重试一次，避免"配了档位就聊不了天"
                        _think_keys = (
                            "enable_thinking", "thinking_budget", "reasoning_effort",
                            "reasoning", "thinking",
                        )
                        if active_extra and any(k in str(_se) for k in _think_keys):
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
                # 2026-09-09：本轮流式成功 → 清零硬异常重试计数
                self._stream_llm_errors = 0

            except Exception as e:
                await self.callbacks.on_thinking(False)

                # ── v3-Final P0.5: Failover 收紧 — 仅硬异常触发升级 ──
                # ★ 2026-09-09：硬异常重试上限 —— 此前恒传 answer="" 使
                # should_failover 的"空回复"条件恒真 → 每次异常都 continue
                # 空转到 max_turns 耗尽（配错 Key 时白烧几十次调用），
                # 真实错误（401/模型名不存在）永不暴露。改为最多重试 2 次
                # （对齐非流式路径），超过即如实报错收尾。
                _stream_err_count = getattr(self, "_stream_llm_errors", 0) + 1
                self._stream_llm_errors = _stream_err_count

                if _stream_err_count <= 2:
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

                # 无法升级 / 达到重试上限 → 记录日志并退出


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

                _msg_mark = len(session.messages)  # 2026-09-20：进展观测基线（剪枝前）

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

                            # ★ 2026-09-09：反思消息【滚动替换】而非累积追加 ——
                            # 反思是针对当下步骤的即时建议，旧内容随任务推进失效；
                            # 此前每次反思都追加一条 SYSTEM 消息并在后续每步重放，
                            # 是多步任务 token 浪费的重要来源。原地替换保持消息
                            # 数量与位置稳定（对前缀缓存也更友好）。
                            _replaced = False
                            for _m in session.messages:
                                if (
                                    _m.role == Role.SYSTEM
                                    and (_m.metadata or {}).get("type") == "reflection"
                                ):
                                    _m.content = reflection.to_context_hint()
                                    _replaced = True
                                    break
                            if not _replaced:
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
                                budget_max=getattr(budget, "display_max", budget.max_turns),
                                context_summary=f"已执行 {len(completed_tools)} 个工具调用",
                            )

                        except Exception as e:

                            logging.getLogger(__name__).warning(f"保存 checkpoint 失败: {e}")

                    # 让 WebSocket 排空 tool_progress(done) 事件

                    yield Delta()

                # ── 2026-09-20 自适应预算观测：有进展→按需续期；连续无进展→判死循环 ──
                if isinstance(budget, AdaptiveBudget):
                    budget.observe(self._step_progress_calls(session, _msg_mark))
                    _bh = self._budget_step_hint(budget)
                    if _bh:
                        session.messages.append(
                            Message(
                                role=Role.SYSTEM,
                                content=_bh,
                                metadata={"type": "budget_hint"},
                            )
                        )

                # ── 防空转看门狗（2026-09-05，与 _run_react 一致）──
                # 回合内工具全失败 ≥8 次，或最近 4 次同工具同失败文本 ≥3 → 注入中断提示；
                # 同一回合提示 2 次仍无进展则强制收尾（复用下方预算耗尽收尾路径）。
                _wd_hint = self._watchdog_hint(session.id)
                if _wd_hint:
                    _wd_trips += 1
                    session.messages.append(
                        Message(
                            role=Role.USER,
                            content=_wd_hint,
                            metadata={"watchdog": True},
                        )
                    )
                    # ★ 2026-09-24：首次空转即把决策权交给用户（每回合限一次，避免刷屏）。
                    #   停止 → break 走正常收尾（前端据 _wdUserStopped 标「已中断」，保留已产出）；
                    #   继续/超时 → 沿用旧逻辑（模型已收到提示，2 次无进展仍强制收尾）。
                    if _wd_trips == 1:
                        _keep = True
                        try:
                            _st = self._tool_stats.get(session.id) or {}
                            _keep = await self.callbacks.on_watchdog(
                                "检测到任务可能在原地打转（重复失败或长时间无进展）。"
                                "要继续尝试，还是停止本次任务？",
                                {
                                    "steps": budget.current,
                                    "tool_calls": _st.get("total", 0),
                                    "ok": _st.get("ok", 0),
                                },
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:  # noqa: BLE001  # 回调异常不得阻断主循环
                            _keep = True
                        if not _keep or self._cancelled:
                            logging.getLogger(__name__).info(
                                "看门狗：用户选择停止（session=%s step=%s）", session.id, budget.current
                            )
                            break
                    if _wd_trips >= 2:
                        logging.getLogger(__name__).warning(
                            "看门狗已提示 2 次仍无进展（session=%s step=%s），强制收尾",
                            session.id,
                            budget.current,
                        )
                        break

                # ── 回合预算护栏（软预警+熔断，A1 收敛为双轨共用 loop_common）──
                _, _fused_by_token = await check_turn_budget(
                    self, session, _turn_start_ts, budget.current, _budget_warned,
                )
                if _fused_by_token:
                    break

                # 工具执行后治理（2026-09-06）：剪枝归档 + 即时压缩 + 长链里程碑摘要
                await self._context_govern(session, budget.current)

                # 智能路由 (2026-08-04 修改): 移除步数升级逻辑。

                # ReAct 模式下 thinker 仅在 executor 执行失败(异常)时才介入，

                # 不再因步数超限而升级，避免无谓消耗复杂模型 token。

                continue

            else:
                # 无工具调用 — 文本回复

                # ── 2026-09-06 空输出保护（与 _run_react 路径一致）──
                # 模型"哑火"(无文本、无工具调用)不得静默 done：
                # 第 1 次空回复注入纠错提示再给一次机会；连续 ≥2 次按失败如实收尾。
                if not collected_text.strip():
                    _empty_replies += 1
                    if _empty_replies < 2:
                        session.messages.append(
                            Message(
                                role=Role.USER,
                                content=(
                                    "【系统提示】你刚才没有产生任何输出内容（空回复），也没有调用工具。"
                                    "请继续完成用户的任务：需要更多信息就先调用对应工具获取，"
                                    "能够作答就直接给出最终回答。不要静默结束。"
                                ),
                                metadata={"watchdog": True},
                            )
                        )
                        continue
                    collected_text = (
                        "⚠️ 任务未能完成：模型连续两次空回复（无内容、无工具调用），"
                        "疑似输出中断或陷入死循环。请检查模型服务状态后重试。"
                    )

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

                if (self._suggest_enabled and not self._cancelled
                        and final_text and len(final_text.strip()) >= 20):
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
                    # ★ 2026-09-16（延迟优化）：改为**后台**执行。
                    # 这又是一次完整 LLM 调用，此前串在"追问建议"之后 await，
                    # 使流式生成器多挂 5~10s 才真正结束（用户虽已收到 done，
                    # 但回合收尾与服务端连接释放被拖后，多轮对话时还会累积）。
                    # 目标提取只依赖文本入参、结果经回调推送前端，适合后台化。
                    self._spawn_bg(self._extract_goals_bg(user_message, final_text))

                return

        # 预算耗尽 / 熔断 / 超时收尾
        # 2026-09-07：推断真实收尾原因（与 _run_react 路径一致），修复误导文案
        _reason = finish_reason(
            _fused_by_token, _wd_trips,
            time.monotonic() > _turn_deadline, self._cancelled,
            stalled=getattr(budget, "stalled", False),
        )

        budget_msg = "\n\n" + self._build_budget_exhausted_msg(session, budget.current, reason=_reason)

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

        if _fused_by_token:
            # ── 2026-09-06：token 熔断收尾——如实说明原因，不谎报"已达步数上限" ──
            _base = forced or budget_msg
            final_text = _base + (
                "\n\n---\n\n⚠️ 本回合新增输入 token（不含缓存重放）已达熔断阈值（"
                + str(self._turn_input_limit)
                + "），已提前终止以避免继续计费膨胀。任务可能尚未完成，以上为当前进度。"
                "回复「继续」可在新回合中续跑（预算重新计算）。"
                "续跑时请**基于本对话中已有的观察结果与最后截图直接继续执行**，"
                "不要从头重新探索界面。"
            )
        elif forced:
            final_text = (
                forced
                + "\n\n---\n\n⚠️ 本轮已执行 "
                + str(budget.current)
                + " 步后收尾（"
                + ("检测到连续无进展，判定为原地打转" if _reason == "stalled"
                   else "达到步数上限 " + str(getattr(budget, "display_max", self.max_turns)) + " 步")
                + "），以上成果已基于本轮获取的信息生成。如需继续完善可回复「继续」。"
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
        self._spawn_bg(self._maybe_extract_session_memory(session))  # ★ 2026-09-16 后台化

    async def _extract_goals_bg(self, user_message: str, final_text: str) -> None:
        """后台执行自动目标提取（结果经回调推送前端）.

        ★ 2026-09-16：从流式收尾路径移出（原因见调用点说明）。本方法内部把异常
        全部吞掉 —— 目标提取是 best-effort，绝不影响主回复。
        """
        try:
            extracted_goals = await self.goal_manager.extract_goals_from_conversation(
                user_message, final_text
            )
            if extracted_goals:
                await self.callbacks.on_goals_extracted(
                    [
                        {"id": g.id, "title": g.title, "tasks_count": len(g.tasks)}
                        for g in extracted_goals
                    ]
                )
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).debug(f"自动目标提取失败: {e}")

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

    def _spawn_bg(self, coro: Any) -> None:
        """把辅助任务丢到后台执行（不阻塞用户响应），并持引用防 GC 丢弃.

        ★ 2026-09-16（延迟优化）：记忆抽取、自省等辅助动作都会各自发起一次
        **完整的 LLM 调用**（实测单次 10~25s）。此前抽取是在 ``return`` 之前
        ``await``，等于每回合都让用户多等一次大模型往返。改为后台任务后，
        用户拿到回答即可返回；辅助调用在后台继续完成。
        （持引用的原因：裸 create_task 可能被 GC 静默回收 —— 同 2026-09-01 教训。）
        """
        try:
            if not hasattr(self, "_bg_tasks"):
                self._bg_tasks: set = set()
            _t = asyncio.create_task(coro)
            self._bg_tasks.add(_t)
            _t.add_done_callback(self._bg_tasks.discard)
        except Exception:  # noqa: BLE001 — 后台化失败不应影响主流程
            pass

    async def _maybe_extract_session_memory(self, session: Session) -> None:
        """会话结束时抽取关键记忆（E4 跨会话记忆工程化，2026-08-27）.

        可选能力：仅在注入 ``memory_extractor`` 时生效；任何失败只记录日志，
        绝不影响主流程返回。

        ★ 2026-09-19 节流（默认关闭）：抽取是一次**完整 LLM 调用**（prompt 数百
        token、completion 常见 500~2000 token），挂在**每个回合**的收尾路径上。
        实测 usage.db 里 987/4917 次调用是这类小 prompt 调用，合计 87 万 token
        （其中记忆抽取典型区间约占 45 万）。
        但它同时承担「会话结束必沉淀用户偏好」的语义（单测
        test_agent_extracts_memory_on_conversation_end 即覆盖），默认降频会丢掉
        单轮会话的记忆 → **默认 1（保持原行为）**，需要省 token 时设
        SCOUT_MEMORY_EXTRACT_EVERY=3 之类（每 3 回合抽一次）。
        """
        if not self.memory_extractor or not session or not session.messages:
            return

        _turn_gap = self._memory_extract_every
        if _turn_gap > 1:
            try:
                extra = getattr(session, "extra", None)
                if not isinstance(extra, dict):
                    extra = {}
                    session.extra = extra
                _seen = int(extra.get("_mem_extract_turns", 0) or 0) + 1
                extra["_mem_extract_turns"] = _seen
                if _seen % _turn_gap != 0:
                    return
            except Exception:  # noqa: BLE001 - 计数失败就按原行为抽取
                pass

        # ★ 2026-09-16：本方法现在运行在后台任务里，可能与本会话的下一回合并发。
        # 先对消息取浅拷贝快照，避免迭代过程中消息列表被并发改写。
        try:
            session = copy.copy(session)
            session.messages = list(session.messages)
        except Exception:  # noqa: BLE001
            pass

        _log = logging.getLogger(__name__)
        try:
            report = await self.memory_extractor.extract(session)
            if report.added:
                _log.info("会话 %s 记忆抽取 %s", session.id, report.summary())
        except Exception as exc:
            _log.warning("会话 %s 记忆抽取失败: %s", session.id, exc)

    # ── 2026-08-19 渐进式工具加载 ──────────────────────────────────────
    # 核心常用工具始终注入（保持基本能力 + 稳定前缀）；边缘/重工具按关键词
    # 渐进式注入，减少无关工具 schema 的 token 占用（工具总数 21 个、compact
    # schema 约 2233 tokens，简单任务往往只需要其中一小部分）。

    # 始终在场的核心工具（基本能力，不依赖关键词）
    _CORE_TOOLS = {
        "web_search", "web_fetch", "file", "shell", "execute_code",
        "memory_search", "memory_save", "memory_list",
        "env_config_get", "env_config_save", "env_config_list", "env_config_delete",
        # ★ 2026-09-09：send_file 提为核心工具 —— 用户措辞千变万化（"发我一下"
        # "导出发过来""传给我""打包一份"），关键词匹配必漏 → 该轮工具集里没有
        # send_file，agent 只能回文本报路径（"叫发文件却不出下载卡片"的根因）。
        # schema 仅 ~250 字符，常驻代价可忽略。
        "send_file",
    }

    # 两阶段懒加载模式下的「极小核心集」（2026-09-24）：这些工具几乎每轮都可能用到，
    # 常驻完整 schema 免去 load_tools 往返；其余工具只进目录，按需 load。
    # 相比 _CORE_TOOLS(13 个) 精简到 9 个 —— env_config_*/memory_save/list 等低频
    # 写操作移入目录（memory_search 保留，召回是高频读）。load_tools/ask_user 必留，
    # 否则模型无法加载更多工具 / 无法澄清。
    _LAZY_MIN_CORE = {
        "file", "shell", "execute_code", "web_search", "web_fetch",
        "memory_search", "send_file", "ask_user", "load_tools",
    }

    # 渐进式工具 → 触发关键词（任一命中即注入该工具 schema）
    # 关键词设计避免宽泛误触发：图片/图 这类词既可能指生成、也可能指识别，
    # 故用"动词+对象"组合（生成/画/做…图  vs  识别/分析/看…图）区分。
    # 渐进式工具 → 触发关键词（任一命中即注入该工具 schema）
    # 关键词同时覆盖中英文，避免英文/中文界面下能力不一致。
    _PROGRESSIVE_TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
        # 桌面 GUI 自动化（2026-09-03 补：此前完全缺失 → desktop 工具永远不注入，
        # 模型只能用 shell 折腾 GUI 导致"操控不到"。微信 4.x 自绘 UI 强依赖
        # desktop+vision+截图 工作流，关键词需覆盖常见应用名与 GUI 动词）
        "desktop": ("桌面", "窗口", "截图", "截屏", "截个图", "截个屏", "点击", "双击", "右键", "鼠标", "键盘",
                    "按键", "操控", "gui", "前台", "微信", "wechat", "weixin", "企业微信",
                    "qq", "飞书", "feishu", "lark", "钉钉", "dingtalk", "tg", "telegram",
                    "桌面应用", "桌面软件", "桌面程序", "打开应用", "启动应用", "切换窗口",
                    "操作电脑", "操控电脑", "控制电脑",
                    "腾讯会议", "wemeet", "tencent meeting", "zoom", "webex", "网易会议",
                    "desktop", "screenshot", "capture screen", "click on", "mouse",
                    "keyboard", "activate window", "gui app", "operate wechat"),
        "browser": ("浏览器自动化", "网页自动化", "浏览器操作", "网页操作", "控制浏览器",
                    "浏览器", "网页", "打开网站", "打开网址", "打开网页", "上网",
                    "chrome", "edge", "firefox", "淘宝", "京东", "天猫", "知乎",
                    "哔哩", "b站", "微博", "百度一下",
                    "browser automation", "playwright", "control browser",
                    "open website", "open url", "open the page"),
        "image_generation": ("生成图片", "生成图像", "画一张", "画一个", "画张", "画只",
                             "画只", "画一", "插画", "海报", "图标", "logo", "设计图",
                             "配图", "头像", "封面", "生成一张", "做一个logo", "做一张",
                             "画个", "画一只", "生成图", "生成一张图",
                             "generate image", "create image", "draw a", "draw an", "generate a logo",
                             "make a poster", "create a icon", "image generation", "generate picture"),
        "vision": ("识别图片", "分析图片", "看图", "ocr", "提取文字", "图片内容",
                   "这张图", "这个图片", "图片里", "图片识别", "识别这张", "看看这张图",
                   "图片是什么", "图里是什么",
                   "读取截图", "看看截图", "截图里", "截图内容", "截图看看", "读取图片",
                   "看看屏幕", "屏幕上", "界面内容", "界面是什么",
                   "recognize image", "analyze image", "read image", "image content", "what is in the image",
                   "vision", "screenshot", "screen content"),
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
        # ★ 2026-09-25 删除漂移项 "mcp_tool"：注册表里的真实工具名是 "mcp"，该键永不
        # 命中（只会在一次性自检里刷告警）。它原有的 4 个关键词全部是上面 "mcp" 的子集，
        # 所以删除是无损的——MCP 一直靠 "mcp" 这个键被关键词预载，此前并无功能缺失。
        "scout_report": ("运行报告", "状况报告", "自检", "scout报告", "系统报告", "健康报告",
                         "scout report", "status report", "self check", "health report"),
        "send_file": ("发文件", "发送文件", "下载文件", "发给我", "附件", "文件给我",
                      "发我", "发过来", "发过去", "传给我", "传我", "传过来",
                      "导出", "打包给", "给我一份", "拷贝给我", "复制给我",
                      "send file", "download file", "attachment", "export",
                      "send me", "email me the file"),
    }

    def _select_progressive_tools(self, user_input: str, session: Session | None = None) -> list[dict]:
        """按用户输入渐进式筛选本次 turn 的工具子集.

        核心工具始终在场；渐进式工具按关键词匹配，命中才注入。
        结果按名称排序，保证同一输入下工具集稳定（前缀可复用）。
        任何情况下都返回非空列表（至少核心工具）。
        """
        if not self._tool_schemas:
            return self._tool_schemas

        text = (user_input or "").lower()

        # 核心集选择：懒加载模式用极小核心集（其余进目录按需 load），
        # 否则沿用旧的完整核心集（全量 compact schema 常驻）。
        _core = self._LAZY_MIN_CORE if self._tool_lazy_load else self._CORE_TOOLS
        selected_names: set[str] = set(_core)
        for tool_name, keywords in self._PROGRESSIVE_TOOL_KEYWORDS.items():
            if any(kw.lower() in text for kw in keywords):
                selected_names.add(tool_name)

        # ★ 2026-09-15（单调累积）：本会话此前激活过的渐进式工具**保持激活**。
        # 工具 schema 位于提示前缀区，若每轮按关键词重选，集合抖动会让前缀缓存
        # 反复失效（GUI 长任务每轮多付 ~2.2k token 全价）。累积后只在"首次激活"
        # 时改变前缀，之后稳定命中。工具上限受注册表约束（最多全部 27 个）。
        #
        # ★ 2026-09-24（懒加载模式）：不再做关键词单调累积 —— 那正是"激活集一路
        # 涨到接近全量"的膨胀根因。改为只并回模型经 load_tools **显式**加载过的
        # 工具（session.extra["lazy_loaded"]，按会话作用域、模型驱动、数量受控）。
        # 关键词匹配仍每轮生效（免往返预载常见工具），但不再跨轮累积。
        if session is not None:
            try:
                _extra = getattr(session, "extra", None) or {}
                if self._tool_lazy_load:
                    _lazy = _extra.get("lazy_loaded") or []
                    selected_names.update(x for x in _lazy if isinstance(x, str))
                else:
                    _acc = _extra.get("active_tools") or []
                    selected_names.update(x for x in _acc if isinstance(x, str))
            except Exception:  # noqa: BLE001
                pass

        # 配置一致性自检（2026-09-14，一次性）：关键词表 key 必须存在于注册表，
        # 否则命中后按名取 schema 会静默跳过（永不生效）且无人察觉——曾出现
        # "mcp_tool" 这类漂移项。此类漂移会持续制造「该露的工具没露」。
        if not getattr(self.__class__, "_kw_drift_checked", False):
            _known = {s.get("function", {}).get("name", "") for s in self._tool_schemas}
            _unknown = sorted(n for n in self._PROGRESSIVE_TOOL_KEYWORDS if n not in _known)
            if _unknown:
                logging.getLogger(__name__).warning(
                    "_PROGRESSIVE_TOOL_KEYWORDS 含当前注册表不存在的工具名（永不生效，"
                    "可能是配置漂移，或平台/依赖不适用）：%s",
                    _unknown,
                )
            self.__class__._kw_drift_checked = True

        # 联动注入：desktop 工作流（截图 → 读图定位 → 坐标点击）强依赖 vision，
        # 用户提到桌面操控时 vision 必须在场，否则 Agent 截图后无法理解界面。
        if "desktop" in selected_names:
            selected_names.add("vision")

        # 从全量 schema 中筛选（保持顺序稳定）
        result = [
            s for s in self._tool_schemas
            if s.get("function", {}).get("name", "") in selected_names
        ]

        # 兜底：若筛选结果为空（理论上不应发生），退回全量，避免工具缺失
        if not result:
            return self._tool_schemas

        # 记录本会话已激活的工具（只存渐进式部分，核心工具无需记）。
        # 懒加载模式下不写 active_tools（不被读取；跨轮持久由 load_tools 写 lazy_loaded）。
        if session is not None and not self._tool_lazy_load:
            try:
                _extra = getattr(session, "extra", None)
                if isinstance(_extra, dict):
                    _extra["active_tools"] = sorted(selected_names - self._CORE_TOOLS)
            except Exception:  # noqa: BLE001
                pass

        return result

    def _watchdog_hint(self, session_id: str) -> str | None:
        """防空转看门狗（2026-09-05）：同参重复失败 / 零进展 / 假进展检测，返回提示文本或 None.

        背景：GUI 自动化曾出现 500 步 / 137k token 空耗——同一工具、同一报错反复
        重试而不换路线。规则：
        1) 已执行 >=8 次且 0 成功 → 能力缺口/环境不允许（硬死路），强制转向说明或如实汇报；
        2) 最近 4 次调用中 >=3 次同一工具、同一失败文本 → 原地打转；
        3)（2026-09-06）最近多轮 ≥4 次引用同一张图片/同一文件路径（截图→OCR→像素解析
           循环但无任何推进）→ "看得见但动不了"假进展，提示转向或如实汇报。
        """
        st = self._tool_stats.get(session_id)
        if not st:
            return None
        ring = st.get("ring") or []
        if not ring:
            return None
        total = st.get("total", 0)
        ok = st.get("ok", 0)
        if total >= 8 and ok == 0:
            return (
                "【系统看门狗】本回合已连续执行 "
                + str(total)
                + " 次工具调用且无一成功——当前路线大概率不可行"
                "（能力缺口/环境不允许/前置条件缺失）。请立即停止空转：\n"
                "1) 若有更可靠的替代路线（换 API/接口、换工具、补前置条件），先说明再执行一次；\n"
                "2) 否则直接向用户如实汇报：任务为何做不了、卡在哪一步、缺什么，不要再消耗步数。"
            )
        same = 0
        last = ring[-1]
        for r in reversed(list(ring)[-4:]):
            if r[0] == last[0] and not r[1] and r[2] == last[2] and r[2]:
                same += 1
            else:
                break
        if same >= 3:
            return (
                "【系统看门狗】你已连续 "
                + str(same)
                + " 次对同一工具（"
                + last[0]
                + "）发起相同调用并得到相同失败结果——这是在原地打转。\n"
                "立即停止重复该调用：要么换一种完全不同的方式，要么向用户如实汇报当前障碍"
                "与所需前置条件。"
            )
        # 规则 3（2026-09-06）："假进展"——最近 8 次 ≥4 次引用同一张图片/同一文件路径。
        # 实测案例：agent 对同一张截图反复 desktop 截图 + vision OCR + execute_code 像素解析
        # 35 轮（工具都"成功"、输出各有不同 → 规则 1/2 都不触发），实际毫无推进、白烧 29 万
        # token。凡输出摘要里反复出现同一图片/文件名的，判为"看得见但动不了"。
        _pic_re = re.compile(r"[\w\-]+\.(?:png|jpe?g|bmp|gif)", re.I)
        _recent = list(ring)[-8:]
        if len(_recent) >= 6:
            _pic_keys = []
            for _r in _recent:
                _m = _pic_re.search(_r[2] or "")
                if _m:
                    _pic_keys.append(_m.group(0).lower())
            if _pic_keys:
                from collections import Counter

                _top_file, _top_n = Counter(_pic_keys).most_common(1)[0]
                if _top_n >= 4:
                    return (
                        "【系统看门狗】你已在最近多轮里反复查看/解析同一文件（"
                        + _top_file
                        + "，累计 "
                        + str(_top_n)
                        + " 次）却没有推进任务——这是典型的\"看得见但动不了\"空转。\n"
                        "立即停止重复截图/读图/像素探测：要么直接执行下一步操作"
                        "（点击/输入/写文件/调用真实 API 等），要么向用户如实汇报："
                        "界面是否阻塞、缺少什么条件、你卡在哪一步。"
                    )
        return None

    # 回合内进度落盘的最小间隔（秒）★ 2026-09-14
    _PERSIST_PROGRESS_INTERVAL = 5.0

    async def _archive_replaced(self, session: Session, info: dict | None) -> None:
        """归档被压缩摘要替换掉的原文（2026-09-14）.

        背景：``compress`` 把旧消息段替换为 600 字摘要后，原文此前**不归档** →
        用户可见历史中段凭空消失且不可恢复。此处把原文写入 messages_archive，
        保证「摘要可读 + 原文可溯」（前端归档视图待后续接入）。

        失败只记 debug：归档是旁路保障，不应影响主流程。
        """
        msgs = (info or {}).get("replaced_messages") or []
        if not msgs or not (self.enable_persistence and self.session_store):
            return
        try:
            await self.session_store.async_archive_messages(
                session.id, msgs, reason="context_compress"
            )
        except Exception:
            logging.getLogger(__name__).debug("归档被压缩消息失败", exc_info=True)

    async def _persist_progress(self, session: Session, force: bool = False) -> None:
        """回合内进度落盘（节流）——防「重启/强杀丢整回合」.

        ★ 2026-09-14：此前仅在回合收尾才 ``save_session``，而工具执行中途只存
        checkpoint、不写 sessions 表；TOOL 消息除「带可下载文件」外也不落库 →
        进程被强杀/重启（无任何退出 flush 钩子）时**整个回合凭空消失**，用户
        表现为「重启后最新对话消息丢失」。

        此处按最小间隔落盘一次，把丢失窗口从「整回合」压缩到「≤5 秒」。实现要点：
        - 用 ``asyncio.to_thread`` 执行（save_session 是同步全量重写，直接调用会
          阻塞事件循环最长 30s，拖慢流式推送）；
        - 失败只告警不阻断——内存态仍是真相，后续落盘会覆盖修正；
        - 不依赖 ``enable_context``（与上下文治理无关，纯持久化）。
        """
        if not (self.enable_persistence and self.session_store):
            return
        # 回合进行中标记（每步续期，不受落盘节流影响）—— 供 REST 删除/编辑端
        # 判断"该会话正在跑"，避免被运行中的回合覆盖回来
        try:
            self.session_store.mark_turn_active(session.id)
        except Exception:
            pass
        now = time.monotonic()
        if not force and now - getattr(self, "_last_progress_persist", 0.0) < self._PERSIST_PROGRESS_INTERVAL:
            return
        self._last_progress_persist = now
        try:
            await asyncio.to_thread(self.session_store.save_session, session)
        except Exception:
            logging.getLogger(__name__).warning(
                "回合内进度落盘失败（不影响本轮执行；内存态仍为真相）", exc_info=True
            )

    async def _context_govern(self, session: Session, step: int) -> None:
        """回合内上下文治理（react/stream 共用，2026-09-06 抽取）.

        每步工具执行后调用一次：
        0. 进度落盘（节流，见 ``_persist_progress``）——与治理开关无关，故置于
           下方 early-return 之前；
        1. 剪枝旧工具输出（被移除消息归档，保证历史可追溯）；
        2. token 超预算时立即压缩（保留最近 N 条 + LLM 摘要 + 记忆 flush）；
        3. 里程碑压缩：长链任务（GUI 自动化 30~60 步）单步消息少、常到不了 80 条
           也不超 token 预算，仅靠 needs_compression 可能永不压缩 → 历史全量重发
           到尾。按步数每 ``_milestone_every`` 步强制做一次阶段摘要（min_total 门槛
           放宽到 keep_recent+6），让回合中段历史保持低位、单次调用输入不再膨胀。
        """
        await self._persist_progress(session)

        if not (self.enable_context and self.context_mgr):
            return
        cm = self.context_mgr

        # 0.5) ★ 2026-09-19：把 API 回传的真实 prompt token 回喂给治理器。
        #    本地 estimate_tokens 对代码/路径/JSON 类内容低估约 2 倍，实测出现
        #    「真实 48k、估算仍判未超 19.6k」→ 整个长回合从不压缩。真实值来自
        #    usage 表最近一次调用，是唯一可信标尺（只在开启 token 预算时查询）。
        if cm.max_tokens > 0:
            try:
                from scout.llm.tracker import token_tracker

                # 取「最近 5 次调用的最大值」而非最近一次：一个回合里混杂着
                # 记忆抽取/标题生成等小 prompt 辅助调用（实测最小 228 token），
                # 若恰好取到它们会把真实上下文规模严重低估、治理再次失效。
                # 主循环调用的 prompt 恒为同回合最大值，取 max 即稳定命中它。
                _rows = token_tracker._query(
                    "SELECT MAX(prompt_tokens) AS p FROM ("
                    "  SELECT prompt_tokens FROM llm_usage WHERE session_id = ? "
                    "  ORDER BY id DESC LIMIT 5)",
                    (session.id,),
                )
                _real = int((_rows[0] or {}).get("p") or 0) if _rows else 0
                if _real > 0:
                    # 记录观测点消息数：圆环统计据此把"实测之后新增的消息"
                    # 估算补进显示值，消除实测值到下次治理 tick 之间的低估窗口
                    cm.observe_real_tokens(session.id, _real, msg_count=len(session.messages))
            except Exception:
                pass

        # 1) 剪枝 —— ★ 2026-09-14（视图分离）：不再物理删除真相消息，改为
        #    通过「视图差异」得出本轮移出视图的工具消息（供归档 + 运行笔记）。
        try:
            _view_calls = {
                (m.metadata or {}).get("call_id")
                for m in cm.build_llm_view(session)
                if m.role == Role.TOOL
            }
            _removed = [
                m for m in session.messages
                if m.role == Role.TOOL
                and (m.metadata or {}).get("call_id") not in _view_calls
            ]
        except Exception:
            _removed = []
        if _removed and self.enable_persistence and self.session_store:
            try:
                await self.session_store.async_archive_messages(
                    session.id, _removed, reason="context_prune"
                )
            except Exception:
                logging.getLogger(__name__).debug("归档被剪枝消息失败", exc_info=True)

        # 1.5) Running Notes：被剪工具输出的要点提炼进末尾笔记（2026-09-07）
        #      物理剪枝会让模型"忘记"早期结论 → 长任务重复搜索/重做。
        #      笔记挂消息列表末尾（纯追加），不破坏前缀缓存。
        try:
            cm.update_running_notes(session, _removed)
        except Exception:
            logging.getLogger(__name__).debug("运行笔记更新失败", exc_info=True)

        # ★ 2026-09-19 压缩冷却判据：
        #   - 实测 prompt 确已超预算（硬信号）→ 不受冷却限制，该压就压；
        #   - 其余（条数触发 / 估算触发）→ 距上次压缩不足 cooldown 步则跳过，
        #     避免判据抖动造成「每步一次摘要调用」（实测 454 步压了 215 次）。
        _real_now = cm.real_prompt_tokens(session.id)
        _hard_over = bool(
            cm.max_tokens > 0
            and _real_now
            and _real_now >= int(cm.max_tokens * cm.compress_ratio)
        )

        def _cooled() -> bool:
            if self._compress_cooldown <= 0 or _hard_over:
                return False
            _last = self._last_compress_step.get(session.id)
            return _last is not None and (step - _last) < self._compress_cooldown

        # 2) token 超预算即时压缩
        if cm.needs_compression(session) and not _cooled():
            try:
                _info = await cm.compress(session, self.llm, memory_flush=self.memory_flush)
                self._last_compress_step[session.id] = step
                await self._archive_replaced(session, _info)
            except Exception:
                logging.getLogger(__name__).debug("turn 内上下文压缩失败", exc_info=True)
            return

        # 3) 里程碑压缩（长链按步数兜底，compress 内部会再校验是否有可压缩区间）
        if step > 0 and self._milestone_every > 0 and step % self._milestone_every == 0:
            if _cooled():
                return
            try:
                _m_info = await cm.compress(
                    session,
                    self.llm,
                    memory_flush=self.memory_flush,
                    min_total=cm.keep_recent + 6,
                )
                self._last_compress_step[session.id] = step
                await self._archive_replaced(session, _m_info)
            except Exception:
                logging.getLogger(__name__).debug("里程碑摘要压缩失败", exc_info=True)

    def _turn_input_used(self, session_id: str, turn_start_ts: float) -> int:
        """回合累计"新增(非缓存)输入"token 数（2026-09-10 从熔断判断抽出）.

        通过 llm_usage 表按 session + 时间窗汇总本回合 prompt_tokens - cached_tokens
        （只计真实新计算量；缓存命中的前缀重放按零头计费，不计入预算）。
        提供方不上报 cached_tokens 时自动退化为全量口径；查询失败返回 0（保守）。
        """
        try:
            from scout.llm.tracker import token_tracker

            rows = token_tracker._query(
                "SELECT COALESCE(SUM(MAX(prompt_tokens - COALESCE(cached_tokens, 0), 0)), 0) AS inp "
                "FROM llm_usage WHERE session_id = ? AND timestamp >= ? AND timestamp <= ?",
                (
                    session_id,
                    datetime.fromtimestamp(turn_start_ts).isoformat(),
                    datetime.now().isoformat(),
                ),
            )
            return int((rows[0] or {}).get("inp") or 0) if rows else 0
        except Exception:
            return 0

    def _turn_input_over_budget(self, session_id: str, turn_start_ts: float) -> bool:
        """回合累计"新增(非缓存)输入"是否超过熔断阈值（2026-09-06）.

        超阈值返回 True（调用方 break 走预算耗尽/强制总结收尾）。
        """
        return self._turn_input_used(session_id, turn_start_ts) >= self._turn_input_limit

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

            logging.getLogger(__name__).warning("预算耗尽强制总结失败: %s", e)
            return ""

    def _build_budget_exhausted_msg(
        self, session: Session, llm_steps: int, reason: str = "steps"
    ) -> str:
        """构建收尾总结消息（CowAgent 风格，简洁清晰）.

        llm_steps: 本轮大模型（LLM）决策轮数，即"大模型步数"，
        对应步数上限 max_turns 的计数口径（budget.current）。
        工具操作数可能多于决策轮数（一次决策可调多个工具），
        这里主展示决策步数，工具操作数作为补充说明。

        reason（2026-09-07）: 真实收尾原因 steps|time|token|watchdog|cancelled。
        旧版把所有收尾一律说成"达到步数上限（500 步）"，在时间/看门狗/熔断
        收尾时严重误导（实际才执行 54 步）。现按原因输出准确文案。
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

        if reason == "time":
            lines = [f"⚠️ 本轮执行时长达到上限（{self.max_loop_seconds}s），已自动收尾。", ""]
        elif reason == "token":
            lines = [f"⚠️ 本回合新增输入 token 超过熔断阈值（{self._turn_input_limit}），已强制收尾以控制消耗。", ""]
        elif reason == "watchdog":
            lines = ["⚠️ 检测到连续无进展（防空转看门狗连续触发），已强制收尾。", ""]
        elif reason == "cancelled":
            lines = ["⏹ 已按取消指令停止本轮执行。", ""]
        elif reason == "stalled":
            lines = [
                "⚠️ 检测到连续多步没有产生新进展（工具反复失败，或反复返回相同结果），"
                "已判定为原地打转并提前停止，避免继续空耗。",
                "",
            ]
        else:
            lines = [f"⚠️ 本轮已执行 {llm_steps} 步并达到步数上限，暂先在这里停下。", ""]

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

    def _llm_view(self, session: Session) -> list:
        """返回「发给 LLM 的视图」（P0 真相/视图分离，2026-09-14）.

        - 启用上下文管理时：``ContextManager.build_llm_view`` —— 在视图上应用
          压缩摘要 + 工具输出裁剪，真相 ``session.messages`` 不被修改；
        - 未启用时降级为真相列表（无治理，行为与旧版一致）。
        """
        cm = getattr(self, "context_mgr", None)
        if self.enable_context and cm is not None:
            try:
                return cm.build_llm_view(session)
            except Exception:
                logging.getLogger(__name__).warning(
                    "构建 LLM 视图失败，降级为完整历史", exc_info=True
                )
        return session.messages

    # ── 模型能力：思考强度 / 视觉（2026-09-24，用户可在设置里配）──────────────
    def _thinking_extra(self) -> dict:
        """把统一档位（auto/off/low/medium/high）翻译成当前模型认识的思考参数.

        各家参数名不同（Qwen 用 thinking_budget、OpenAI o/GPT-5 用 reasoning_effort、
        Claude 用 thinking.budget_tokens、OpenRouter 用 reasoning.effort），发错
        会 400，所以这里按 (provider, model) 判定风格再翻译。
        auto 时保持旧行为：由 deep_thinking 布尔控制（兼容老配置）。
        """
        # Multi-Agent 编排是模式化任务（分解→委派→汇总），深度思考收益低、
        # 却让每次 LLM 调用多花 5-15s → 编排阶段关闭，提速
        if self.agent_mode == "multi_agent":
            return {"enable_thinking": False}
        eff = str(getattr(self, "reasoning_effort", "auto") or "auto").lower()
        if eff == "auto":
            return {"enable_thinking": bool(self.deep_thinking)}
        try:
            from scout.adapters.web.routes.config import (
                build_thinking_extra,
                resolve_thinking_style,
            )
            _model = getattr(self.llm, "model", "") or ""
            style = resolve_thinking_style(self.model_provider, _model)
            extra, _ = build_thinking_extra(style, eff)
            return extra or {}
        except Exception:  # noqa: BLE001
            return {"enable_thinking": eff != "off"}

    def _vision_route(self) -> dict:
        """本轮的视觉路由判定 —— 唯一决策点见 `scout/llm/vision_route` 模块头.

        ★ 2026-09-26：此前本方法所在链路只问"模型能不能看图"(`resolve_model_vision`)，
        从不问路由，导致设置里的「视觉模型」对用户发的图片不起作用，而 vision 工具
        却按路由走另一个模型 —— 同一轮两个答案。现在附件与工具共用同一份决策；
        运行时模型（聊天中可切换）与 `vision_input` 显式覆盖都在核心函数里处理，
        本方法只负责取配置并转发，避免强制值语义在两处分叉。
        """
        try:
            from scout.config import ConfigManager
            from scout.llm.vision_route import route_for_agent

            return route_for_agent(self, ConfigManager().load())
        except Exception:  # noqa: BLE001 — 判定不可用时不拖累对话，退回按能力自动判断
            try:
                from scout.llm.vision_route import native_vision

                ok, src = native_vision(
                    getattr(self, "model_provider", ""),
                    getattr(self.llm, "model", "") or "",
                )
            except Exception:  # noqa: BLE001
                ok, src = False, ""
            forced = getattr(self, "vision_input", None)
            if isinstance(forced, bool):
                ok, src = forced, "forced"
            return {
                "path": "main" if ok else "none",
                "model": getattr(self.llm, "model", "") or "",
                "base_url": "",
                "source": src,
                "reason": "路由判定不可用，已退回模型能力自动判断",
                "native": bool(ok),
                "needs_choice": False,
            }

    def _vision_enabled(self) -> bool:
        """主模型是否**直接接收**图片（路由判 main 才是 True）."""
        return self._vision_route().get("path") == "main"

    def _image_content_parts(self, attachments: list, text: str) -> list[dict]:
        """把图片附件拼成 OpenAI 兼容的多模态 content 列表.

        ★ 2026-09-26（V2）：送模型前先过 `scout.llm.image_prep`（降采样 + 字节预算）。
        此前只有 vision 工具做降采样，附件这条路是原图直 base64 —— 实测 4 张常见附件
        （手机照片/2.5K 与 4K 截图/微信长图）请求体合计 10.5 MB，预处理后 4.0 MB；
        图像 token 按分辨率计费，降幅更大。更糟的是磁盘上 >5MB 的图此前被**静默跳过**，
        模型完全不知道用户发了图 —— 而降采样后它往往只有 1 MB，本可以正常送达。

        因此这里新增"未送达"明示：任何没送出的图（超张数、文件缺失、无法解码、超硬
        上限）都会以一行文本告诉模型，绝不让它以为自己看见了。
        """
        from scout.llm.image_prep import build_image_part

        parts: list[dict] = []
        if text.strip():
            parts.append({"type": "text", "text": text})

        imgs = [a for a in (attachments or []) if _is_image_attachment(a)]
        used = 0
        notes: list[str] = []
        for idx, att in enumerate(imgs):
            path = str(att.get("path") or "")
            name = str(att.get("name") or os.path.basename(path) or f"图片{idx + 1}")
            if used >= _IMAGE_MAX_COUNT:
                notes.append(f"另有 {len(imgs) - idx} 张图片超出单次 {_IMAGE_MAX_COUNT} 张上限，未送达")
                break
            part, prep = build_image_part(path)
            if part is None:
                notes.append(f"{name} 未送达（{prep.skipped or '无法处理'}）")
                continue
            parts.append(part)
            used += 1

        if notes:
            parts.append({
                "type": "text",
                "text": "[附件提示] " + "；".join(notes) + "。不要描述或推测未送达图片的内容。",
            })
        return parts if (used or notes) else []


    def _build_api_messages(self, session: Session) -> list[dict]:
        """构建发送给 LLM 的消息列表.



        - 使用 self.system_prompt 作为唯一 system 消息

        - 跳过所有 role=SYSTEM 的历史消息（动态内容已移入 runtime_context）

        """

        # ── 使用 Agent 构造时确定的 system prompt ──

        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]

        # runtime_context 注入治理（2026-09-05 token 优化）：
        # 仅“最近一条带 runtime_context 的 user 消息（当轮）”需要注入，且注入到该消息自身位置，
        # 取代旧版“每步追加到最后一条 user 消息”的方案。旧版问题：
        #   1) 每次 API 请求都把整段技能/记忆/摘要全文重复挂到动态尾部 → 每步多付一份完整注入；
        #   2) 注入位置随步数漂移（看门狗等新 user 消息插在前面）→ 前缀缓存无法命中。
        # 新版注入点固定在该 user 消息的历史位置，后续各步重发内容逐字节一致：
        #   前缀缓存命中时近乎免费；无缓存时也仅保留一份而非每步重复追加。
        # ★ 2026-09-14（P0 真相/视图分离）：构造 API 消息一律基于「视图」——
        # 治理（压缩摘要 / 工具裁剪）只作用于视图，session.messages 保持完整真相
        # （持久化与 UI 因此不再丢历史）。
        _llm_msgs = self._llm_view(session)

        _last_rt_idx = -1
        for _i, _m in enumerate(_llm_msgs):
            if _m.role == Role.USER and _m.metadata.get("runtime_context"):
                _last_rt_idx = _i

        # 图片直收（2026-09-24）：只对**最近一条带图片附件的 user 消息**注入图像
        # 内容。历史轮次的图片不再逐轮重发（一张图上千 token，长会话会撑爆窗口），
        # 模型仍可通过落盘路径用文件工具按需回看。
        _last_img_idx = -1
        if self._vision_enabled():
            for _i, _m in enumerate(_llm_msgs):
                _atts = (_m.metadata or {}).get("attachments") if _m.role == Role.USER else None
                if _atts and any(_is_image_attachment(a) for a in _atts):
                    _last_img_idx = _i

        for _idx, msg in enumerate(_llm_msgs):
            # ── SYSTEM 消息：仅保留压缩器生成的 [对话摘要]，其余动态内容已移入 runtime_context ──

            if msg.role == Role.SYSTEM:
                # ★ 2026-09-09：放行 [运行笔记] —— 此前只放行 [对话摘要]，运行笔记
                # 从不进入 API 消息，"防剪枝失忆"完全无效（被剪工具结论真丢，
                # 长任务后半程重复搜索/重做）。
                if msg.content and (
                    msg.content.startswith("[对话摘要]")
                    or msg.content.startswith("[运行笔记]")
                ):
                    messages.append({"role": "system", "content": msg.content})
                continue

            elif msg.role == Role.USER:
                _uc = msg.content or ""
                if _idx == _last_rt_idx:
                    _rt_now = msg.metadata.get("runtime_context") or ""
                    if _rt_now:
                        _uc = _uc + "\n\n" + _rt_now
                if _idx == _last_img_idx:
                    _parts = self._image_content_parts(
                        msg.metadata.get("attachments") or [], _uc
                    )
                    if _parts:
                        messages.append({"role": "user", "content": _parts})
                        continue
                messages.append({"role": "user", "content": _uc})

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

        # ── v3-Final P0: 断言仅 1 条非摘要 system 消息（[对话摘要] 不计入，保证长对话压缩后上下文不丢失） ──

        system_count = sum(
            1
            for m in messages
            if m["role"] == "system"
            and not (
                m["content"].startswith("[对话摘要]")
                or m["content"].startswith("[运行笔记]")
            )
        )

        assert system_count == 1, f"检测到 {system_count} 条主 system 消息，破坏缓存前缀！"

        return messages

    async def cleanup(self):
        """清理资源（沙箱容器等）."""

        if hasattr(self, "sandbox_mgr") and self.sandbox_mgr:
            try:
                await self.sandbox_mgr.cleanup()

            except Exception as e:

                logging.getLogger(__name__).debug(f"Sandbox cleanup failed: {e}")
