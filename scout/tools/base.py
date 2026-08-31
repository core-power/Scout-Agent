"""工具定义基类 — Action-Observation 模式.

工具契约（2026-08-27 增强，对标 DeepSeek Harness 的 Tool Contracts）：
- 无歧义入参：execute() 的类型注解可自动推导 JSON Schema（ensure_schema），
  手写 parameters 优先；运行时统一 validate_args 校验（必填缺失 / 类型纠正）。
- 统一错误码：Observation.error_code 区分 UNKNOWN_TOOL / INVALID_ARGS / INTERNAL
  与工具细化的 NOT_FOUND / PERMISSION / TIMEOUT / NETWORK / SANDBOX 等，
  让 Agent 对失败原因可编程处理，而非仅解析文本。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation

# ── 统一错误码（工具契约）────────────────────────────────
ERROR_UNKNOWN_TOOL = "UNKNOWN_TOOL"   # 工具不存在
ERROR_INVALID_ARGS = "INVALID_ARGS"   # 参数缺失 / 类型错误
ERROR_INTERNAL = "INTERNAL"           # 工具内部异常（注册表兜底）
ERROR_NOT_FOUND = "NOT_FOUND"         # 资源不存在
ERROR_PERMISSION = "PERMISSION"       # 权限不足
ERROR_TIMEOUT = "TIMEOUT"             # 超时
ERROR_NETWORK = "NETWORK"             # 网络错误
ERROR_SANDBOX = "SANDBOX"             # 沙箱拒绝
ERROR_UNAUTHORIZED = "UNAUTHORIZED"   # 未授权


class ToolDefinition(ABC):
    """工具定义基类.

    每个工具继承此类，实现 execute() 方法。
    在模块顶层调用 ToolRegistry.register() 自动注册。
    """

    name: str = "base_tool"
    description: str = "Base tool"
    parameters: dict[str, Any] = {}  # JSON Schema
    annotations: ToolAnnotations = ToolAnnotations()
    # 纯读工具标记（2026-08-19）：无副作用、可安全并行的工具置 True。
    # agent 主循环据此把多个独立纯读工具调用用 asyncio.gather 并发执行，
    # 减少多工具轮次的等待时间（参考 CowAgent 简洁 ReAct + 更进一步）。
    pure_read: bool = False

    # 支持的平台（2026-08-30）：("windows", "linux", "darwin") 元组；
    # None = 全平台可用。registry 暴露 schema 时据此过滤，
    # 保证「不同的系统只看到适合该系统的工具」。
    platforms: tuple[str, ...] | None = None

    # 类型注解 → JSON Schema type 的映射（尽力而为）
    _TYPE_MAP: dict[Any, dict[str, str]] = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        list: {"type": "array"},
        dict: {"type": "object"},
        bytes: {"type": "string"},
    }

    # 签名推导 schema 缓存（按类缓存，避免子类互相覆盖）
    _auto_schema: dict[str, Any] | None = None
    _schema_cache: dict[type, dict[str, Any]] = {}

    @abstractmethod
    async def execute(self, **kwargs) -> Observation:
        """执行工具. 未知的 kwargs 应被忽略."""
        ...

    # ── 契约：schema ─────────────────────────────────────

    @classmethod
    def _resolve_type(cls, ann: Any) -> dict[str, str] | None:
        """Python 注解 → JSON Schema type（处理 Optional/Union/List/Dict 等）. """
        origin = getattr(ann, "__origin__", None)
        args = getattr(ann, "__args__", ())
        if origin is not None:
            if origin is list:
                return {"type": "array"}
            if origin is dict:
                return {"type": "object"}
            if origin is tuple:
                return {"type": "array"}
            if origin in (Any,):
                return None
            if origin is None or str(origin).startswith("typing.Union"):
                # Optional[X] → X
                non_none = [a for a in args if a is not type(None)]
                if len(non_none) == 1:
                    return cls._resolve_type(non_none[0])
                return None
        return cls._TYPE_MAP.get(ann)

    @classmethod
    def _derive_schema(cls) -> dict[str, Any]:
        """从 execute() 签名推导 JSON Schema.

        规则：跳过 self / kwargs / 下划线开头的参数（如 _role）；
        有默认值 → 非必填；无默认值 → 必填。
        失败时返回空 schema（不抛出，保证容错）。
        注意：模块级 `from __future__ import annotations` 使注解为字符串，
        必须用 typing.get_type_hints() 解析为真实类型。
        """
        try:
            sig = inspect.signature(cls.execute)
        except (TypeError, ValueError):
            return {"type": "object", "properties": {}, "required": []}

        try:
            import typing

            hints = typing.get_type_hints(cls.execute)
        except Exception:
            hints = {}

        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, param in sig.parameters.items():
            if name in ("self", "kwargs", "args") or name.startswith("_"):
                continue
            ann = hints.get(name, param.annotation)
            ptype = cls._resolve_type(ann)
            prop: dict[str, Any] = dict(ptype or {"type": "string"})
            if param.default is not inspect.Parameter.empty:
                prop["default"] = param.default
            else:
                required.append(name)
            properties[name] = prop
        return {"type": "object", "properties": properties, "required": required}

    def ensure_schema(self) -> dict[str, Any]:
        """返回有效 JSON Schema：手写 parameters 优先，否则签名推导（按类缓存）."""
        if self.parameters:
            return self.parameters
        cls = type(self)
        if cls not in self._schema_cache:
            self._schema_cache[cls] = cls._derive_schema()
        return self._schema_cache[cls]

    # ── 契约：入参校验 ────────────────────────────────────

    def validate_args(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """校验并规范化入参，返回 (清洗后的参数, 错误信息).

        错误信息为空 = 校验通过。校验规则（宽松策略，避免误伤）：
        - 必填缺失 → INVALID_ARGS 错误；
        - 数字型参数收到字符串 → 尝试数值转换；
        - 数组型参数收到单个值 → 包装为单元素列表；
        - 其余保持原样透传（未知参数忽略，与 execute 约定一致）。
        """
        schema = self.ensure_schema()
        props = schema.get("properties") or {}
        required = schema.get("required") or []

        for r in required:
            if r not in kwargs or kwargs[r] is None:
                return {}, f"缺少必填参数: {r}（工具 {self.name}）"

        cleaned: dict[str, Any] = dict(kwargs)
        for name, val in cleaned.items():
            prop = props.get(name)
            if not prop:
                continue
            t = prop.get("type")
            if t in ("integer", "number") and not isinstance(val, (int, float)):
                try:
                    cleaned[name] = int(val) if t == "integer" else float(val)
                except (TypeError, ValueError):
                    return {}, f"参数 {name} 需要 {t} 类型，收到 {type(val).__name__}（工具 {self.name}）"
            elif t == "array" and not isinstance(val, (list, tuple)):
                cleaned[name] = [val]
            elif t == "boolean" and isinstance(val, str):
                cleaned[name] = val.strip().lower() in ("true", "1", "yes")
        return cleaned, ""

    def to_schema(self) -> dict:
        """生成 OpenAI function-calling 格式."""
        schema = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.ensure_schema(),
            },
        }
        return self.adapt_schema(schema)

    def adapt_schema(self, schema: dict) -> dict:
        """平台自适应钩子（2026-08-30）：根据当前操作系统微调给 LLM 的 schema.

        默认原样返回。跨平台工具可覆写本方法，在 Windows / Linux / macOS 上
        提供各自合适的命令示例与参数说明——例如 shell 工具在 Windows 下应
        展示 dir/type/findstr 而非 ls/cat/grep，并标注 PTY 仅 Unix 可用，
        避免 LLM 在 Windows 上尝试不存在的命令与行为。
        """
        return schema


# 向后兼容别名：重构前的类名为 BaseTool，
# plugins/tool_loader.py、tools/cron_tool.py、tools/mcp_client.py 尚未迁移。
BaseTool = ToolDefinition
