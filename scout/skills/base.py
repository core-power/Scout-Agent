"""技能定义基类 — 支持 YAML 元数据驱动的自动注册."""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation


class SkillMetadata(BaseModel):
    """技能元数据模型.

    从 YAML 文件中读取，描述技能的基本信息。
    """

    name: str
    description: str = ""
    version: str = "1.0.0"
    author: str = ""
    tags: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)  # JSON Schema
    annotations: ToolAnnotations = Field(default_factory=ToolAnnotations)
    enabled: bool = True
    priority: int = 0


class SkillDefinition(ABC):
    """技能定义基类.

    每个技能继承此类，实现 execute() 方法。
    支持从 YAML 文件加载元数据，或通过类属性直接定义。

    使用方式:
        1. YAML 驱动: 在 YAML 中定义 metadata，Python 文件实现逻辑
        2. 纯 Python: 直接在类中定义 name, description 等属性
    """

    # 类级别的默认元数据（可被 YAML 覆盖）
    name: str = "base_skill"
    description: str = "Base skill"
    version: str = "1.0.0"
    parameters: dict[str, Any] = {}
    annotations: ToolAnnotations = ToolAnnotations()
    enabled: bool = True
    priority: int = 0

    def __init__(self, metadata: SkillMetadata | None = None) -> None:
        """初始化技能.

        Args:
            metadata: 可选的元数据对象，用于覆盖类级别默认值
        """
        if metadata is not None:
            self._apply_metadata(metadata)

    def _apply_metadata(self, metadata: SkillMetadata) -> None:
        """应用元数据到当前实例."""
        self.name = metadata.name or self.name
        self.description = metadata.description or self.description
        self.version = metadata.version or self.version
        self.parameters = metadata.parameters or self.parameters
        self.annotations = metadata.annotations or self.annotations
        self.enabled = metadata.enabled
        self.priority = metadata.priority

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Observation:
        """执行技能逻辑.

        Args:
            **kwargs: 执行参数

        Returns:
            Observation: 执行结果
        """
        ...

    def to_schema(self) -> dict[str, Any]:
        """生成 OpenAI function-calling 格式.

        Returns:
            dict: 符合 OpenAI API 规范的 schema
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {
                    "type": "object",
                    "properties": {},
                },
            },
        }

    def get_metadata(self) -> dict[str, Any]:
        """获取技能的完整元数据.

        Returns:
            dict: 包含所有元数据字段的字典
        """
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "parameters": self.parameters,
            "annotations": self.annotations.model_dump(),
            "enabled": self.enabled,
            "priority": self.priority,
        }

    def validate_parameters(self, params: dict[str, Any]) -> tuple[bool, str | None]:
        """验证参数是否符合 schema 定义.

        Args:
            params: 待验证的参数

        Returns:
            tuple: (是否有效, 错误信息)
        """
        if not self.parameters:
            return True, None

        required = self.parameters.get("required", [])
        properties = self.parameters.get("properties", {})

        for key in required:
            if key not in params:
                return False, f"缺少必需参数: {key}"

        for key, value in params.items():
            if key not in properties:
                continue  # 允许额外参数

            prop_schema = properties[key]
            expected_type = prop_schema.get("type")

            if expected_type == "string" and not isinstance(value, str):
                return False, f"参数 '{key}' 应为字符串类型"
            elif expected_type == "integer" and not isinstance(value, int):
                return False, f"参数 '{key}' 应为整数类型"
            elif expected_type == "number" and not isinstance(value, (int, float)):
                return False, f"参数 '{key}' 应为数字类型"
            elif expected_type == "boolean" and not isinstance(value, bool):
                return False, f"参数 '{key}' 应为布尔类型"
            elif expected_type == "array" and not isinstance(value, list):
                return False, f"参数 '{key}' 应为数组类型"
            elif expected_type == "object" and not isinstance(value, dict):
                return False, f"参数 '{key}' 应为对象类型"

        return True, None

    def __repr__(self) -> str:
        return f"<SkillDefinition name={self.name!r} version={self.version!r}>"
