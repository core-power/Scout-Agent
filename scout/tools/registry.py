"""工具注册表 — 按需加载 + import 时自动注册.

P0 优化: 工具按需加载，减少 50%+ 工具定义 token 消耗。
- 核心工具 (files, shell, code_exec): 始终加载
- 常用工具 (web, memory, knowledge, ...): 始终加载（无重依赖）
- 可选工具 (browser, vision, image_gen): 仅在依赖可用时加载
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import sys
import time
from typing import Any

from scout.core.types import Action, Observation, ToolCall
from scout.tools.base import (
    ERROR_INTERNAL,
    ERROR_INVALID_ARGS,
    ERROR_UNKNOWN_TOOL,
    ToolDefinition,
)

logger = logging.getLogger(__name__)


def _compact_schema(schema: dict) -> None:
    """原地精简工具 schema 的描述文本（紧凑模式）.

    策略：
    - 工具级 description：截断到 90 字符（保留核心语义）
    - 参数级 description：截断到 60 字符
    只改文本，不动 name/type/required/enum 等结构字段 → 工具调用完全兼容.
    """
    MAX_FN_DESC = 60
    MAX_PARAM_DESC = 40
    fn = schema.get("function") or {}
    if isinstance(fn, dict) and fn.get("description"):
        d = fn["description"]
        if len(d) > MAX_FN_DESC:
            # 截断到句子边界，避免半句话
            cut = d[:MAX_FN_DESC]
            last = max(cut.rfind("。"), cut.rfind("."), cut.rfind("，"), cut.rfind(","))
            fn["description"] = (cut[: last + 1] if last > 20 else cut) + "…"
    params = fn.get("parameters") or {}
    props = params.get("properties") or {}
    if isinstance(props, dict):
        for p in props.values():
            if isinstance(p, dict) and p.get("description"):
                d = p["description"]
                if len(d) > MAX_PARAM_DESC:
                    p["description"] = d[:MAX_PARAM_DESC].rstrip() + "…"


class ToolRegistry:
    """工具注册表.

    工具在模块顶层调用 ToolRegistry.register() 自动注册。
    调用 discover() 自动发现并导入所有工具模块。
    """

    _tools: dict[str, ToolDefinition] = {}

    # ── 按需加载配置 ──────────────────────────────────────
    # 可选工具: 仅在依赖可用时加载
    _OPTIONAL_TOOLS: dict[str, list[str]] = {
        "browser": ["playwright"],
        "vision": [],       # 需要 API key，但无额外 pip 依赖
        "image_gen": [],    # 需要 API key，但无额外 pip 依赖
    }

    # 内置工具显式兜底清单（2026-08-30 修复）:
    # PyInstaller 打包后 .py 模块全部进入 PYZ 归档，pkgutil.iter_modules()
    # 无法枚举归档内子模块 → discover() 动态导入全部落空，只剩被主程序
    # 显式 import 的工具（如 knowledge）注册。此清单保证打包版工具完整加载；
    # 源码环境下与 iter_modules 结果做并集去重，无副作用。
    _BUILTIN_FALLBACK = [
        "browser", "code_exec", "delegate", "edit", "env_config",
        "files", "image_gen", "knowledge", "mcp", "memory",
        "parallel", "scheduler", "send_file", "shell", "vision", "web",
        "scout_report",
    ]

    @classmethod
    def register(cls, tool: ToolDefinition) -> None:
        """注册工具 — 在工具模块顶层调用."""
        cls._tools[tool.name] = tool

    @classmethod
    def get_tool(cls, name: str) -> ToolDefinition | None:
        """按名称获取工具."""
        return cls._tools.get(name)

    @classmethod
    def all_tools(cls) -> dict[str, ToolDefinition]:
        """获取所有已注册工具."""
        return cls._tools

    @classmethod
    def schemas(
        cls,
        exclude: set[str] | None = None,
        compact: bool = False,
        allow: set[str] | None = None,
    ) -> list[dict]:
        """生成工具的 JSON Schema（给 LLM 看的）.

        按工具名排序保证顺序稳定 — 工具定义是 prompt 前缀的一部分，
        顺序稳定才能最大化 prompt cache 命中。

        Args:
            exclude: 要排除的工具名集合（子代理不应拥有委派类工具，防止无限递归）
            compact: 紧凑模式，精简 description 减小上下文占用
                （保留结构/参数完整性，只压缩描述文本；调用兼容不受影响）
            allow: 白名单模式 — 仅返回此集合内的工具（None=全部）
                与 exclude 同时提供时，先按 allow 过滤，再剔除 exclude
        """
        names = sorted(cls._tools)
        if allow is not None:
            names = [n for n in names if n in allow]
        if exclude:
            names = [n for n in names if n not in exclude]
        # 条件禁用过滤：工具可定义 is_enabled() 返回 False，表示当前未启用
        # （如未配置搜索引擎时 web_search 不暴露给 LLM）
        names = [n for n in names if cls._tool_is_enabled(cls._tools[n])]
        # 平台过滤（2026-08-30）：工具声明 platforms 且不含当前系统时，
        # 不暴露给 LLM —— 保证「不同的系统只看到适合该系统的工具」。
        names = [n for n in names if cls._tool_supported_on_platform(cls._tools[n])]
        schemas = [cls._tools[name].to_schema() for name in names]
        if compact:
            for s in schemas:
                _compact_schema(s)
        return schemas

    @staticmethod
    def _tool_is_enabled(tool: ToolDefinition) -> bool:
        """条件启用判断 — 工具提供 is_enabled() 且返回 False 时不暴露."""
        fn = getattr(tool, "is_enabled", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                return True
        return True

    @staticmethod
    def _tool_supported_on_platform(tool: ToolDefinition) -> bool:
        """平台过滤 — 工具声明 platforms 且不含当前系统时不暴露（2026-08-30）."""
        platforms = getattr(tool, "platforms", None)
        if not platforms:
            return True  # None = 全平台可用
        current = "windows" if os.name == "nt" else (
            "darwin" if sys.platform == "darwin" else "linux"
        )
        return current in platforms

    @classmethod
    async def execute(cls, call: ToolCall | Action, **kwargs) -> Observation:
        """执行工具调用. 额外 kwargs 透传给 tool.execute()."""
        # 兼容 ToolCall 和 Action
        if isinstance(call, Action):
            tool_name = call.tool_name or ""
            args = call.tool_args
        else:
            tool_name = call.name
            args = call.arguments

        tool = cls.get_tool(tool_name)
        if not tool:
            return Observation(
                tool_name=tool_name,
                success=False,
                output=f"未知工具: {tool_name}",
                error_code=ERROR_UNKNOWN_TOOL,
            )

        start = time.time()

        # ── 工具契约校验（2026-08-27）：入参先行校验，失败不再进入 execute ──
        cleaned_args, validate_err = tool.validate_args(args)
        if validate_err:
            return Observation(
                tool_name=tool_name,
                success=False,
                output=validate_err,
                error_code=ERROR_INVALID_ARGS,
                duration_ms=int((time.time() - start) * 1000),
            )

        try:
            obs = await tool.execute(**cleaned_args, **kwargs)
            obs.duration_ms = int((time.time() - start) * 1000)
            if not obs.error_code and not obs.success:
                obs.error_code = ERROR_INTERNAL
            return obs
        except Exception as e:
            return Observation(
                tool_name=tool_name,
                success=False,
                output=str(e),
                error_code=ERROR_INTERNAL,
                duration_ms=int((time.time() - start) * 1000),
            )

    @classmethod
    def discover(cls) -> None:
        """自动发现并导入所有工具模块 — 支持按需加载.

        可选工具仅在依赖可用时加载，减少工具定义 token 消耗。
        """
        from scout.tools import builtin

        skipped = []

        # 源码环境枚举物理目录；打包环境枚举为空 → 用显式清单兜底（并集去重）
        discovered = {name for _, name, _ in pkgutil.iter_modules(builtin.__path__)}
        modules = sorted(discovered | set(cls._BUILTIN_FALLBACK))

        for name in modules:
            # 检查是否为可选工具
            if name in cls._OPTIONAL_TOOLS:
                deps = cls._OPTIONAL_TOOLS[name]
                if not cls._check_deps(deps):
                    skipped.append(name)
                    continue

            try:
                importlib.import_module(f"{builtin.__name__}.{name}")
            except Exception as e:
                logger.warning(f"工具模块 {name} 加载失败: {e}")

        if skipped:
            logger.info(f"按需跳过工具: {', '.join(skipped)} (依赖不可用)")

    @classmethod
    def _check_deps(cls, deps: list[str]) -> bool:
        """检查依赖是否可用."""
        for dep in deps:
            try:
                importlib.import_module(dep)
            except ImportError:
                return False
        return True

    @classmethod
    def loaded_tools(cls) -> list[str]:
        """返回已加载的工具名列表."""
        return sorted(cls._tools.keys())
