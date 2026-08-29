"""技能合成器 — 从自愈循环中提取可复用技能.

核心流程：
1. 监听自愈循环的成功修复事件
2. 提取"错误模式 -> 解决方案"对
3. 利用 LLM 泛化解决方案（替换具体变量为占位符）
4. 生成 SynthesizedSkill 并存入 VectorSkillStore
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

# VectorSkillStore imported lazily to avoid numpy dependency chain
from scout.engine.skill_types import SkillOrigin, SkillStatus, SynthesizedSkill

logger = logging.getLogger(__name__)


class SkillSynthesizer:
    """技能合成器 — 将成功的修复经验沉淀为可复用技能."""

    def __init__(
        self,
        skill_store: Any,  # VectorSkillStore
        llm_client: Any = None,
        min_confidence: float = 0.7,
    ):
        """
        Args:
            skill_store: 向量技能存储
            llm_client: LLM 客户端（用于泛化和摘要）
            min_confidence: 最低置信度，低于此值不沉淀
        """
        self.skill_store = skill_store
        self.llm = llm_client
        self.min_confidence = min_confidence
        self._pending_skills: list[dict] = []

    async def on_heal_success(
        self,
        tool_name: str,
        original_error: str,
        original_args: dict,
        fixed_args: dict,
        heal_attempts: int,
    ) -> SynthesizedSkill | None:
        """自愈成功时的回调 — 决定是否沉淀为新技能.

        Args:
            tool_name: 工具名称
            original_error: 原始错误信息
            original_args: 原始参数
            fixed_args: 修复后的参数
            heal_attempts: 修复尝试次数

        Returns:
            如果沉淀成功，返回 SynthesizedSkill；否则 None
        """
        # 1. 评估是否值得沉淀
        if not self._should_synthesize(tool_name, original_error, heal_attempts):
            logger.debug(f"Skipping synthesis for {tool_name}: low confidence")
            return None

        # 2. 提取错误模式
        error_pattern = self._extract_error_pattern(original_error)

        # 3. 泛化解决方案
        solution_template = await self._generalize_solution(
            tool_name, original_args, fixed_args
        )

        # 4. 生成技能描述
        intent = await self._generate_intent(tool_name, error_pattern, solution_template)

        # 5. 构造 SynthesizedSkill
        skill_id = f"skill_{uuid.uuid4().hex[:12]}"
        skill = SynthesizedSkill(
            id=skill_id,
            name=f"{tool_name}_fix_{error_pattern[:20]}",
            description=intent,
            trigger_pattern=self._error_to_regex(error_pattern),
            trigger_keywords=self._extract_keywords(error_pattern),
            trigger_tools=[tool_name],
            solution_template=solution_template,
            solution_type="code_fix" if tool_name == "execute_code" else "tool_sequence",
            intent=intent,
            context_tags=[tool_name, error_pattern.split(":")[0] if ":" in error_pattern else "general"],
            origin=SkillOrigin.SELF_HEAL,
            status=SkillStatus.ACTIVE,
            success_count=1,
            failure_count=0,
            usage_count=0,
            success_rate=1.0,
            created_at=time.time(),
            updated_at=time.time(),
            source_error=original_error,
            source_fix=json.dumps(fixed_args, ensure_ascii=False),
        )

        # 6. 存入向量数据库
        try:
            await self.skill_store.add_skill(skill)
            logger.info(f"Synthesized new skill: {skill.name} (id={skill_id})")
            return skill
        except Exception as e:
            logger.error(f"Failed to add skill to store: {e}")
            return None

    def _should_synthesize(
        self,
        tool_name: str,
        error: str,
        heal_attempts: int,
    ) -> bool:
        """评估是否值得沉淀.

        规则：
        - 修复尝试次数 <= 3（说明问题相对简单，容易复用）
        - 错误信息包含明确的模式（如 ImportError, SyntaxError）
        - 工具是常见的可复用类型（execute_code, shell）
        """
        # 工具白名单
        reusable_tools = {"execute_code", "shell", "browser"}
        if tool_name not in reusable_tools:
            return False

        # 修复次数过多说明问题复杂，不适合泛化
        if heal_attempts > 3:
            return False

        # 错误模式检测
        error_patterns = [
            r"ImportError",
            r"ModuleNotFoundError",
            r"NameError",
            r"SyntaxError",
            r"TypeError",
            r"AttributeError",
            r"KeyError",
            r"IndexError",
            r"FileNotFoundError",
            r"PermissionError",
        ]
        has_pattern = any(re.search(p, error) for p in error_patterns)
        return has_pattern

    def _extract_error_pattern(self, error: str) -> str:
        """从错误信息中提取关键模式.

        例如：
        "ImportError: No module named 'pandas'" -> "ImportError:pandas"
        "SyntaxError: invalid syntax (line 5)" -> "SyntaxError"
        """
        # 提取错误类型
        match = re.match(r"(\w+Error):?\s*(.+)", error)
        if match:
            error_type = match.group(1)
            detail = match.group(2).strip()[:50]  # 截取前50字符
            # 清理特殊字符
            detail = re.sub(r"[^\w\s]", "", detail).strip()
            detail = re.sub(r"\s+", "_", detail)
            return f"{error_type}:{detail}" if detail else error_type

        # 降级：返回错误信息的前30字符
        return re.sub(r"[^\w\s]", "", error[:30]).strip()

    def _error_to_regex(self, error_pattern: str) -> str:
        """将错误模式转换为正则表达式.

        例如：
        "ImportError:pandas" -> r"ImportError.*pandas"
        """
        # 转义特殊字符
        escaped = re.escape(error_pattern)
        # 将冒号替换为通配符
        regex = escaped.replace(":", ".*")
        return f".*{regex}.*"

    def _extract_keywords(self, text: str) -> list[str]:
        """从文本中提取关键词."""
        # 简单分词：按空格、冒号、下划线分割
        words = re.split(r"[\s:_]+", text.lower())
        # 过滤停用词和短词
        stop_words = {"error", "exception", "the", "a", "an", "in", "on", "at"}
        keywords = [w for w in words if w and len(w) > 2 and w not in stop_words]
        return list(set(keywords))[:10]  # 最多10个关键词

    async def _generalize_solution(
        self,
        tool_name: str,
        original_args: dict,
        fixed_args: dict,
    ) -> str:
        """泛化解决方案 — 将具体变量替换为占位符.

        如果有 LLM，使用 LLM 进行智能泛化；
        否则使用规则泛化（替换数字、字符串为占位符）。
        """
        if not self.llm:
            return self._rule_based_generalize(original_args, fixed_args)

        # 使用 LLM 泛化
        prompt = f"""你是一个代码专家。请将以下修复方案泛化为可复用的模板。

工具: {tool_name}
原始参数: {json.dumps(original_args, ensure_ascii=False, indent=2)}
修复后参数: {json.dumps(fixed_args, ensure_ascii=False, indent=2)}

要求：
1. 将具体的变量名、路径、数字替换为占位符（如 {{variable}}, {{path}}, {{number}}）
2. 保留核心逻辑结构
3. 添加注释说明关键步骤
4. 输出泛化后的代码模板（只输出代码，不要解释）

泛化模板:"""

        try:
            resp = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                temperature=0.3,
            )
            template = resp.content.strip()
            # 清理 markdown 代码块标记
            template = re.sub(r"^```[\w]*\n?", "", template)
            template = re.sub(r"\n?```$", "", template)
            return template
        except Exception as e:
            logger.warning(f"LLM generalization failed: {e}")
            return self._rule_based_generalize(original_args, fixed_args)

    def _rule_based_generalize(self, original_args: dict, fixed_args: dict) -> str:
        """基于规则的泛化 — 简单的字符串替换."""
        if "code" in fixed_args:
            code = fixed_args["code"]
            # 替换数字为占位符
            code = re.sub(r"\b\d+\b", "{{number}}", code)
            # 替换长字符串为占位符
            code = re.sub(r"'[^']{10,}'", "'{{string}}'", code)
            code = re.sub(r'"[^"]{10,}"', '"{{string}}"', code)
            return code
        elif "command" in fixed_args:
            cmd = fixed_args["command"]
            # 替换路径为占位符
            cmd = re.sub(r"/[\w/]+", "{{path}}", cmd)
            return cmd
        else:
            return json.dumps(fixed_args, ensure_ascii=False, indent=2)

    async def _generate_intent(
        self,
        tool_name: str,
        error_pattern: str,
        solution_template: str,
    ) -> str:
        """生成技能的自然语言意图描述."""
        if not self.llm:
            # 降级：使用模板生成
            return f"解决 {tool_name} 工具中的 {error_pattern} 错误"

        prompt = f"""用一句话描述这个技能解决的问题：

工具: {tool_name}
错误模式: {error_pattern}
解决方案: {solution_template[:200]}

一句话描述（不超过30字）:"""

        try:
            resp = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                temperature=0.3,
            )
            intent = resp.content.strip()
            return intent[:50]  # 限制长度
        except Exception:
            return f"解决 {tool_name} 中的 {error_pattern}"

    async def synthesize_from_session(
        self,
        session_metadata: dict,
    ) -> list[SynthesizedSkill]:
        """从会话元数据中批量合成技能.

        解析 session.extra["heal_attempts"] 中的成功修复记录。
        """
        heal_attempts = session_metadata.get("heal_attempts", [])
        synthesized = []

        for attempt in heal_attempts:
            if not attempt.get("success"):
                continue

            skill = await self.on_heal_success(
                tool_name=attempt.get("tool_name", "unknown"),
                original_error=attempt.get("error", ""),
                original_args=attempt.get("original_args", {}),
                fixed_args=attempt.get("fixed_args", {}),
                heal_attempts=attempt.get("attempt", 1),
            )
            if skill:
                synthesized.append(skill)

        return synthesized
