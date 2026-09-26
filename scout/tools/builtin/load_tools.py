"""load_tools — 两阶段工具加载的「第二阶段」：按需展开工具完整 schema.

设计动机（2026-09-24）：27 个工具的完整 JSON schema 全量注入约占 3.7k token
（compact 后），且旧「渐进式加载」靠关键词命中 + 会话内单调累积，长会话下
激活集仍会涨到接近全量。改为两阶段：

  阶段一（目录）：system/runtime_context 只注入所有工具的「name + 一句话用途」
                 目录（≈740 token，稳定利于前缀缓存）+ 极小核心集的完整 schema；
  阶段二（展开）：LLM 需要某个非核心工具时，先调 load_tools(names=[...])，
                 本工具把其完整参数 schema 追加进当前 Agent 的 _active_tool_schemas
                 并回显给模型 —— ReAct 循环每轮迭代重读 _active_tool_schemas，
                 故同轮的下一次 LLM 调用即可直接使用刚加载的工具，无需等下一轮。

跨轮持久：加载过的工具名写入 session.extra["lazy_loaded"]，下一轮
_select_progressive_tools 会并回，避免每轮重复 load。作用域限本会话，
不做全局累积（区别于旧的关键词单调累积）。

降级行为（不抛错）：
- 无 _main_agent（脱离 Agent 直接调用）→ 仅回显 schema，不做注入；
- 未知工具名 → 在输出中列出，不影响其余合法工具的加载；
- 未启用懒加载（SCOUT_TOOL_LAZY_LOAD=0）→ 工具仍可调用，但注入是 no-op
  （此时全量 schema 本就常驻）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 单次最多展开的工具数：防 LLM 一次性 load 全部工具，退化成全量注入。
_MAX_LOAD_PER_CALL = 8


class LoadToolsTool(ToolDefinition):
    """按需加载工具的完整参数定义 — 使用目录中列出的非核心工具前先调用."""

    name = "load_tools"
    description = (
        "加载工具的完整参数 schema。系统提示的 <available_tools> 目录里列出的工具"
        "当前只有名称与用途、没有参数定义；要调用其中某个工具前，先用本工具按名加载"
        "（如 names=[\"desktop\"]），加载成功后即可正常调用它。已在场的核心工具无需加载。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要加载的工具名列表（取自 <available_tools> 目录），最多 8 个",
            },
        },
        "required": ["names"],
    }
    # 纯读：只改本进程内的 schema 注入集，无外部副作用，可与其他只读工具并行
    pure_read = True

    async def execute(self, names: Any = None, **kwargs: Any) -> Observation:
        # 入参规整：兼容 list / 逗号分隔字符串 / 单个字符串
        raw: list[str] = []
        if isinstance(names, str):
            raw = [p for p in names.replace("，", ",").split(",")]
        elif isinstance(names, (list, tuple)):
            raw = [str(p) for p in names]
        requested: list[str] = []
        for n in raw:
            n = (n or "").strip()
            if n and n not in requested:
                requested.append(n)

        if not requested:
            return Observation(
                tool_name=self.name,
                success=False,
                output="",
                error="names 不能为空：请给出要加载的工具名（取自 <available_tools> 目录）",
                error_code="INVALID_ARGS",
            )

        truncated = False
        if len(requested) > _MAX_LOAD_PER_CALL:
            requested = requested[:_MAX_LOAD_PER_CALL]
            truncated = True

        agent = getattr(ToolRegistry, "_main_agent", None)
        # 全量 schema 池：优先用 Agent 已构建的 _tool_schemas（与「技能联动」注入
        # 复用同一批 compact schema 对象，保持一致）；脱离 Agent 时回退到注册表现取。
        pool: list[dict] = list(getattr(agent, "_tool_schemas", []) or [])

        loaded: list[str] = []
        unknown: list[str] = []
        schemas_to_inject: list[dict] = []
        for n in requested:
            src = next(
                (s for s in pool if s.get("function", {}).get("name", "") == n),
                None,
            )
            if src is None:
                # 池里没有（可能平台/依赖不可见，或名字写错）→ 向注册表求证
                fetched = ToolRegistry.schema_for(n, compact=True)
                if fetched is None:
                    unknown.append(n)
                    continue
                src = fetched
            loaded.append(n)
            schemas_to_inject.append(src)

        # 注入当前 Agent 的活跃工具集（去重、按名排序保持前缀稳定）
        if agent is not None and schemas_to_inject:
            try:
                active = list(getattr(agent, "_active_tool_schemas", []) or [])
                have = {s.get("function", {}).get("name", "") for s in active}
                added = False
                for s in schemas_to_inject:
                    nm = s.get("function", {}).get("name", "")
                    if nm and nm not in have:
                        active.append(s)
                        have.add(nm)
                        added = True
                if added:
                    agent._active_tool_schemas = sorted(
                        active, key=lambda x: x.get("function", {}).get("name", "")
                    )
                # 跨轮持久：写入当前会话 extra（作用域限本会话，不做全局累积）
                sess = getattr(agent, "_current_session", None)
                extra = getattr(sess, "extra", None) if sess is not None else None
                if isinstance(extra, dict):
                    prev = extra.get("lazy_loaded") or []
                    merged = sorted({x for x in prev if isinstance(x, str)} | set(loaded))
                    extra["lazy_loaded"] = merged
            except Exception:  # noqa: BLE001  # 注入失败不应阻断——已回显 schema 供本轮参考
                logger.debug("load_tools 注入活跃工具集失败（忽略）", exc_info=True)

        # 回显完整 schema：即便注入 no-op（脱离 Agent / 未启用懒加载），
        # 模型也能从本返回值读到参数定义，当轮即可正确构造调用。
        payload = {
            "loaded": loaded,
            "schemas": schemas_to_inject,
        }
        notes: list[str] = []
        if unknown:
            notes.append(f"未找到（名称有误或当前平台不可用）：{', '.join(unknown)}")
        if truncated:
            notes.append(f"单次最多加载 {_MAX_LOAD_PER_CALL} 个，超出部分已忽略，可再次调用。")
        if loaded:
            notes.append("以下工具已就绪，可直接调用：" + ", ".join(loaded))

        output = json.dumps(payload, ensure_ascii=False)
        if notes:
            output = "\n".join(notes) + "\n" + output

        return Observation(
            tool_name=self.name,
            success=bool(loaded),
            output=output,
            metadata={"loaded": loaded, "unknown": unknown, "truncated": truncated},
        )


ToolRegistry.register(LoadToolsTool())
