"""自修复循环 — 自动处理工具执行失败并进行修复.

核心逻辑：
1. 捕获工具执行的 Observation。
2. 如果失败且包含可修复的错误信息（如代码报错），触发 LLM 反思。
3. 生成修复后的参数并重新执行。
4. 记录修复过程到 Session 元数据中。

测试反馈闭环（2026-08-27，对标 DeepSeek Harness DSBench）：
- execute_code 失败时自动运行项目 pytest，把失败堆栈提取为结构化信息
  注入修复 prompt，让 healer 依据真实失败原因自纠错而非盲猜。
- 由环境变量 SCOUT_TEST_FEEDBACK 控制（默认 "1" 开启，"0" 关闭）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from scout.core.types import Message, Observation, Role, ToolCall
from scout.engine.self_heal import VerifyPipeline

logger = logging.getLogger("scout.heal_loop")


class SelfHealLoop:
    """自修复状态机."""

    def __init__(self, llm: Any, max_retries: int = 3, test_feedback: bool | None = None):
        self.llm = llm
        self.max_retries = max_retries
        self.verifier = VerifyPipeline()
        # 测试反馈开关：显式参数优先，否则读环境变量，默认开启
        if test_feedback is None:
            test_feedback = os.environ.get("SCOUT_TEST_FEEDBACK", "1") != "0"
        self.test_feedback = test_feedback
        # 缓存已修复的代码，避免重复校验
        self._last_verified_code: dict[str, str] = {}

    async def should_heal(self, obs: Observation) -> bool:
        """判断是否需要触发自修复."""
        if obs.success:
            return False

        # 仅针对代码执行和 Shell 错误进行修复尝试
        if obs.tool_name in ["execute_code", "shell"]:
            return True

        return False

    async def _collect_test_feedback(self, error_obs: Observation) -> str:
        """execute_code 失败时运行项目测试，返回结构化失败反馈（空串=无需附加）."""
        if not self.test_feedback:
            return ""
        if error_obs.tool_name != "execute_code":
            return ""  # shell 失败场景异构，不套用测试反馈，避免误导 healer

        try:
            from scout.engine.test_feedback import build_test_feedback, run_tests

            result = await run_tests()
            return build_test_feedback(result)
        except Exception as e:
            logger.debug(f"测试反馈收集失败（忽略）: {e}")
            return ""

    async def generate_fix(
        self,
        original_tool_call: ToolCall,
        error_obs: Observation,
        context: list[dict],
    ) -> ToolCall | None:
        """基于错误信息生成修复后的工具调用."""
        test_feedback = await self._collect_test_feedback(error_obs)

        prompt = (
            f"之前的工具调用失败了。\n"
            f"工具: {original_tool_call.name}\n"
            f"原始参数: {original_tool_call.arguments}\n"
            f"错误信息:\n{error_obs.output}\n\n"
        )
        if test_feedback:
            prompt += (
                f"另外，项目测试套件也失败了（很可能是同一根因）:\n"
                f"{test_feedback}\n\n"
            )
        prompt += (
            "请分析错误原因，并提供一个修复后的 JSON 格式的工具调用参数。"
            "只输出 JSON，不要包含其他文字。"
        )

        try:
            response = await self.llm.complete(
                messages=context + [{"role": "user", "content": prompt}],
                tools=None,
                temperature=0.1,  # 修复需要确定性
                _role="healer",
            )

            import json
            # 尝试从响应中提取 JSON
            content = response.content or ""
            if "{" in content:
                start = content.find("{")
                end = content.rfind("}") + 1
                fixed_args = json.loads(content[start:end])
                # ★ 2026-09-01 修复：LLM 偶发输出非对象 JSON（字符串/数组/嵌套引号），
                # 若不校验类型，非 dict 参数会存入 session.metadata.tool_calls，
                # 后续每次请求 json.dumps 双重编码 → DashScope 400
                # "Can only get item pairs from a mapping"，整会话被永久毒化。
                if not isinstance(fixed_args, dict):
                    logger.warning(f"自修复生成失败: LLM 输出的 JSON 不是对象 (type={type(fixed_args).__name__})")
                    return None

                return ToolCall(
                    name=original_tool_call.name,
                    arguments=fixed_args,
                )
        except Exception as e:
            logger.warning(f"自修复生成失败: {e}")

        return None

    async def verify_and_fix_code(self, file_path: str, code_content: str) -> tuple[bool, str]:
        """对代码进行校验并返回修复建议."""
        result = await self.verifier.verify_code(file_path)
        if result.success:
            self._last_verified_code[file_path] = code_content
            return True, ""
        return False, result.summary
