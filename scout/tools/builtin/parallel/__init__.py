"""并行子代理委派 — 一次性派发多个子任务并行执行.

与 delegate_task 的区别：
- delegate_task: 串行，一次一个子任务
- parallel_delegate: 并行，一次多个子任务，asyncio.gather 并发

适用于：可分解为多个独立子任务的复杂场景。

修复记录 (2026-08-01):
- 子代理继承主 Agent 的工具（排除委派类工具防止无限递归）
- 新增委派深度限制（max_delegate_depth），防止无限嵌套
- 子代理优先使用 executor 模型（省钱）
- 子代理不覆盖 ToolRegistry._main_agent

接线记录 (2026-08-21):
- 复用 delegate.build_sub_agent 共享函数
- 子代理注册进全局 AgentRouter，执行完毕自动注销
"""

from __future__ import annotations

import asyncio
import uuid

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation, Session
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

from scout.tools.builtin.delegate import build_sub_agent, unregister_sub_agent


class ParallelDelegateTool(ToolDefinition):
    """并行子代理委派工具 — 同时派发多个子任务."""

    name = "parallel_delegate"
    description = (
        "将多个独立子任务并行委派给隔离子代理执行。所有子任务同时启动，"
        "全部完成后返回汇总结果。比逐个调用 delegate_task 快数倍。\n\n"
        "适用场景：\n"
        "- 同时调研多个主题\n"
        "- 并行处理多个文件\n"
        "- 同时搜索多个信息源\n"
        "- 任何可以拆分为独立子任务的场景"
    )
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "子任务描述",
                        },
                        "label": {
                            "type": "string",
                            "description": "子任务标签（便于识别结果）",
                        },
                    },
                    "required": ["task"],
                },
                "description": "子任务列表，每个任务包含 task 描述和可选的 label 标签",
                "minItems": 1,
                "maxItems": 10,
            },
            "context": {
                "type": "string",
                "description": "所有子任务共享的上下文信息（可选）",
            },
            "max_turns": {
                "type": "integer",
                "description": "每个子代理的最大迭代次数（默认 6）",
            },
            "timeout": {
                "type": "integer",
                "description": "每个子任务的超时秒数（默认 75）",
            },
        },
        "required": ["tasks"],
    }
    annotations = ToolAnnotations(read_only=False, open_world=False)

    async def execute(
        self,
        tasks: list[dict],
        context: str = "",
        max_turns: int = 6,
        timeout: int = 75,
    ) -> Observation:
        agent = getattr(ToolRegistry, "_main_agent", None)
        if not agent:
            return Observation(
                tool_name="parallel_delegate",
                success=False,
                output="无法委派：主 Agent 未注册",
            )

        # ── 深度限制检查 ──
        current_depth = getattr(agent, "delegate_depth", 0)
        max_depth = getattr(agent, "max_delegate_depth", 2)
        if current_depth >= max_depth:
            return Observation(
                tool_name="parallel_delegate",
                success=False,
                output=f"无法委派：已达到最大委派深度 ({max_depth})，请直接使用工具完成任务",
            )

        if not tasks:
            return Observation(
                tool_name="parallel_delegate",
                success=False,
                output="任务列表为空",
            )

        # 委派批次 ID：同一次并行委派所有子代理共享，前端聚合为并行卡片组
        delegation_id = f"pl_{uuid.uuid4().hex[:8]}"

        # 为每个子任务创建独立的子 Agent 并并发执行
        async def run_sub_task(idx: int, task_spec: dict) -> dict:
            task_desc = task_spec.get("task", "")
            label = task_spec.get("label", f"task_{idx}")

            sub_session = Session(
                id=str(uuid.uuid4()),
                parent_id=None,
                lineage_id="parallel_delegated",
            )

            prompt = task_desc
            if context:
                prompt = f"上下文: {context}\n\n任务: {task_desc}"

            sub_system_prompt = (
                "You are a focused sub-agent. Complete the assigned task efficiently using available tools.\n"
                "Use tools (shell, web_search, read_file, etc.) to gather information and produce results.\n"
                "EFFICIENCY: Complete the task in at MOST 1-2 tool calls. STRICT RULES:\n"
                "  - Use web_search at most ONCE. Do NOT re-search, do NOT expand scope, do NOT verify.\n"
                "  - Do NOT call web_fetch unless the search result explicitly requires it. Prefer answering from the search snippets.\n"
                "  - After the first tool result, IMMEDIATELY produce the final answer. No extra rounds.\n"
                "  - If the first search returns enough info, answer right away without any further tool calls.\n"
                "Be concise and return a clear, actionable result.\n"
                "Respond in the same language as the task description.\n"
            )

            # 子代理名（前端展示）：用任务标签，如 子代理-调研A
            _sub_name = f"子代理-{label}" if label != f"task_{idx}" else f"子代理-{idx}"
            sub_agent = build_sub_agent(
                agent,
                task_context=context,
                sub_name=_sub_name,
                delegation_id=delegation_id,
                current_depth=current_depth,
                max_depth=max_depth,
                max_turns=max_turns,
                system_prompt=sub_system_prompt,
                router_suffix=str(idx),
            )

            try:
                result = await asyncio.wait_for(
                    sub_agent.run_conversation(prompt, sub_session),
                    timeout=timeout,
                )
                return {
                    "label": label,
                    "success": True,
                    "steps": result["steps"],
                    # 结果截断到 600 字符：主 Agent 汇总只需结论要点，
                    # 过长输入会显著拖慢汇总 LLM 调用（4个子代理 × 2000 = 8k 输入）
                    "response": result["response"][:600],
                }
            except asyncio.TimeoutError:
                return {
                    "label": label,
                    "success": False,
                    "steps": 0,
                    "response": f"超时: 未在 {timeout} 秒内完成",
                }
            except Exception as e:
                return {
                    "label": label,
                    "success": False,
                    "steps": 0,
                    "response": f"执行失败: {e}",
                }
            finally:
                unregister_sub_agent(delegation_id, router_suffix=str(idx))

        # 并发执行所有子任务（信号量限流，防止 LLM 突发请求触发 429 limit_burst_rate）
        # 并发量按子任务数自适应：≤4 全并行，>4 限 4 并发，避免 DashScope 限流
        max_concurrent = min(len(tasks), 4)
        _sem = asyncio.Semaphore(max_concurrent)

        async def _bounded(idx: int, task_spec: dict) -> dict:
            async with _sem:
                return await run_sub_task(idx, task_spec)

        coros = [_bounded(i, t) for i, t in enumerate(tasks)]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # 汇总结果
        output_parts = []
        success_count = 0
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                label = tasks[i].get("label", f"task_{i}")
                output_parts.append(f"## [{label}]\n❌ 异常: {r}\n")
            elif isinstance(r, dict):
                if r["success"]:
                    success_count += 1
                status = "✅" if r["success"] else "❌"
                output_parts.append(
                    f"## [{r['label']}]\n"
                    f"{status} (步数: {r['steps']})\n"
                    f"{r['response']}\n"
                )

        total = len(tasks)
        summary = f"并行委派完成: {success_count}/{total} 成功\n\n"
        full_output = summary + "\n---\n\n".join(output_parts)

        return Observation(
            tool_name="parallel_delegate",
            success=True,
            output=full_output[:5000],
        )


# import 时自动注册
ToolRegistry.register(ParallelDelegateTool())
