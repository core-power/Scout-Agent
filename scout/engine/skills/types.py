"""技能沉淀数据模型 — 向量化的可复用技能定义.

与 scout/context/skills.py 的区别：
- skills.py: 文件驱动的静态技能（SKILL.md），基于关键词匹配
- skill_types.py: 自愈循环沉淀的动态技能，基于向量语义检索
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SkillOrigin(str, Enum):
    """技能来源."""
    SELF_HEAL = "self_heal"        # 自愈循环沉淀
    USER_DEFINED = "user_defined"  # 用户手动定义
    SYNTHESIS = "synthesis"        # LLM 合成/泛化
    IMPORTED = "imported"          # 从外部导入


class SkillStatus(str, Enum):
    """技能状态."""
    ACTIVE = "active"         # 正常使用
    DEPRECATED = "deprecated" # 已弃用（成功率过低）
    PENDING = "pending"       # 待审核
    ARCHIVED = "archived"     # 已归档


@dataclass
class SynthesizedSkill:
    """一个沉淀的技能."""

    id: str
    name: str
    description: str

    # 触发条件
    trigger_pattern: str = ""           # 正则表达式（兼容旧系统）
    trigger_keywords: list[str] = field(default_factory=list)
    trigger_tools: list[str] = field(default_factory=list)  # 关联的工具名

    # 解决方案
    solution_template: str = ""         # 泛化后的代码/指令模板
    solution_type: str = "code_fix"     # code_fix | tool_sequence | prompt_hint

    # 语义信息（用于向量检索）
    intent: str = ""                    # 自然语言描述解决的问题
    context_tags: list[str] = field(default_factory=list)

    # 统计与进化
    origin: SkillOrigin = SkillOrigin.SELF_HEAL
    status: SkillStatus = SkillStatus.ACTIVE
    success_count: int = 0
    failure_count: int = 0
    usage_count: int = 0
    success_rate: float = 1.0

    # 元数据
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used_at: float = 0.0
    source_error: str = ""              # 原始错误信息
    source_fix: str = ""                # 原始修复方案
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
        if not self.updated_at:
            self.updated_at = self.created_at

    @property
    def total_attempts(self) -> int:
        return self.success_count + self.failure_count

    def record_usage(self, success: bool) -> None:
        """记录一次使用结果."""
        self.usage_count += 1
        self.last_used_at = time.time()
        self.updated_at = time.time()
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
        # 贝叶斯平滑：避免极端值
        total = max(self.total_attempts, 1)
        self.success_rate = (self.success_count + 1) / (total + 2)

    def should_deprecate(self, min_rate: float = 0.3, min_attempts: int = 5) -> bool:
        """是否应该弃用."""
        return (
            self.total_attempts >= min_attempts
            and self.success_rate < min_rate
        )

    def to_embedding_text(self) -> str:
        """生成用于向量化的文本表示.

        将技能的关键语义信息拼接为一段文本，用于 Embedding。
        """
        parts = [
            f"技能: {self.name}",
            f"意图: {self.intent}",
            f"描述: {self.description}",
        ]
        if self.trigger_keywords:
            parts.append(f"关键词: {', '.join(self.trigger_keywords)}")
        if self.trigger_tools:
            parts.append(f"工具: {', '.join(self.trigger_tools)}")
        if self.context_tags:
            parts.append(f"标签: {', '.join(self.context_tags)}")
        if self.source_error:
            parts.append(f"错误模式: {self.source_error[:200]}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（存储用）."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "trigger_pattern": self.trigger_pattern,
            "trigger_keywords": self.trigger_keywords,
            "trigger_tools": self.trigger_tools,
            "solution_template": self.solution_template,
            "solution_type": self.solution_type,
            "intent": self.intent,
            "context_tags": self.context_tags,
            "origin": self.origin.value,
            "status": self.status.value,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "usage_count": self.usage_count,
            "success_rate": self.success_rate,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
            "source_error": self.source_error,
            "source_fix": self.source_fix,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SynthesizedSkill:
        """从字典反序列化."""
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            trigger_pattern=data.get("trigger_pattern", ""),
            trigger_keywords=data.get("trigger_keywords", []),
            trigger_tools=data.get("trigger_tools", []),
            solution_template=data.get("solution_template", ""),
            solution_type=data.get("solution_type", "code_fix"),
            intent=data.get("intent", ""),
            context_tags=data.get("context_tags", []),
            origin=SkillOrigin(data.get("origin", "self_heal")),
            status=SkillStatus(data.get("status", "active")),
            success_count=data.get("success_count", 0),
            failure_count=data.get("failure_count", 0),
            usage_count=data.get("usage_count", 0),
            success_rate=data.get("success_rate", 1.0),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            last_used_at=data.get("last_used_at", 0.0),
            source_error=data.get("source_error", ""),
            source_fix=data.get("source_fix", ""),
            metadata=data.get("metadata", {}),
        )
