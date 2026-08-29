"""DAG 任务规划器 — 将复杂目标拆解为有向无环图."""

from __future__ import annotations

import json
from typing import Any

from scout.llm.base import LLMClient


class DAGPlanner:
    """基于 LLM 的显式任务规划器.

    负责将用户的高层目标拆解为一系列可执行的步骤，并识别步骤间的依赖关系。
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def plan(self, goal: str) -> list[dict[str, Any]]:
        """生成任务计划.

        Args:
            goal: 用户的目标描述

        Returns:
            包含 id, description, depends_on (可选) 的步骤列表
        """
        prompt = f"""
你是一个专业的任务规划专家。请将以下复杂目标拆解为具体的执行步骤。
每个步骤应该是一个原子操作，可以被 Agent 的工具直接执行。

目标：{goal}

要求：
1. 输出格式必须是 JSON 数组。
2. 每个对象包含 "id" (字符串), "description" (字符串), "depends_on" (可选，字符串数组)。
3. 确保步骤之间有清晰的逻辑顺序和依赖关系。
4. 如果任务是简单的，可以只返回一个步骤。

示例输出：
[
  {{"id": "step_1", "description": "搜索 Python 3.12 新特性", "depends_on": []}},
  {{"id": "step_2", "description": "编写演示代码", "depends_on": ["step_1"]}}
]
"""
        response = await self.llm.complete(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        
        try:
            # 尝试从响应中提取 JSON
            content = response.content.strip()
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            return json.loads(content)
        except Exception:
            # 如果解析失败，返回一个默认的单步计划
            return [{"id": "default_step", "description": goal, "depends_on": []}]
