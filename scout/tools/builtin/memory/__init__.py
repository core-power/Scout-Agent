"""记忆工具 — 让 Agent 自己能读写记忆.

优化 (2026-08-01):
- 添加最大记忆条数限制（默认 1000 条，超出时自动清理低分记忆）
- 添加 TTL（Time To Live）支持，过期记忆自动清理

优化 (2026-08-04):
- 集成向量嵌入，保存时自动生成 embedding
- 搜索时使用混合检索（向量语义 + FTS5 文本）
"""

from __future__ import annotations

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry


# 最大记忆条数（超出时自动清理低分记忆）
MAX_MEMORIES = 1000


def _get_store():
    """获取全局 MemoryStore 实例（带 embedding provider）."""
    from scout.memory.store import MemoryStore

    # 尝试从 Agent 获取已注入 embedding 的 store
    try:
        from scout.tools.registry import ToolRegistry
        agent = getattr(ToolRegistry, '_main_agent', None)
        if agent and hasattr(agent, 'memory_store') and agent.memory_store:
            return agent.memory_store
    except Exception:
        pass

    # 回退：创建新实例
    return MemoryStore()


class MemorySaveTool(ToolDefinition):
    """保存记忆."""

    name = "memory_save"
    description = "将一条信息保存到长期记忆中，以便将来引用。用于记住用户偏好、重要决策、事实等。"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "要记住的内容"},
            "category": {"type": "string", "description": "记忆类别（如: preference/decision/fact/todo）", "default": "general"},
            "importance": {"type": "number", "description": "重要性 0.0~1.0（默认 0.5）", "default": 0.5},
        },
        "required": ["content"],
    }
    annotations = ToolAnnotations(destructive=True)

    async def execute(self, content: str, category: str = "general", importance: float = 0.5) -> Observation:
        store = _get_store()

        # 检查是否超出最大条数，如果是则清理低分记忆
        recent = store.list_recent(limit=MAX_MEMORIES + 10)
        if len(recent) >= MAX_MEMORIES:
            deleted = store.decay_cleanup(min_score=0.1)
            if deleted > 0:
                pass  # 静默清理

        # 使用异步方法，自动生成 embedding
        mid = await store.add_async(content=content, category=category, importance=importance)
        return Observation(
            tool_name="memory_save",
            success=True,
            output=f"已保存记忆 (id={mid}, category={category}, importance={importance})",
        )


class MemorySearchTool(ToolDefinition):
    """搜索记忆."""

    name = "memory_search"
    pure_read = True
    description = "搜索长期记忆，查找之前保存的信息。用于回忆用户偏好、过往决策等。如果搜索不到，会返回最近的记忆供参考。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词（可以用多个词）"},
            "limit": {"type": "integer", "description": "返回结果数量（默认 5）", "default": 5},
        },
        "required": ["query"],
    }
    annotations = ToolAnnotations(read_only=True)

    async def execute(self, query: str, limit: int = 5) -> Observation:
        store = _get_store()

        # 使用异步混合检索（向量语义 + FTS5 文本）
        results = await store.search_async(query, limit=limit)

        if not results:
            # 搜索无结果时，返回最近记忆供参考
            recent = store.list_recent(limit=limit)
            if recent:
                lines = [f"未找到匹配 '{query}' 的记忆。最近保存的记忆:\n"]
                for m in recent:
                    lines.append(f"[{m.id}] ({m.category}) {m.content[:80]}")
                return Observation(tool_name="memory_search", success=True, output="\n".join(lines))
            return Observation(tool_name="memory_search", success=True, output="记忆库为空")

        lines = [f"找到 {len(results)} 条记忆:\n"]
        for m in results:
            lines.append(f"[{m.id}] ({m.category}, 重要性={m.importance:.1f}) {m.content}")
        return Observation(tool_name="memory_search", success=True, output="\n".join(lines))


class MemoryListTool(ToolDefinition):
    """列出最近的记忆."""

    name = "memory_list"
    description = "列出最近保存的记忆。可用于回顾之前记住的内容。"
    parameters = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "按类别筛选（可选）"},
            "limit": {"type": "integer", "description": "返回数量（默认 10）", "default": 10},
        },
    }
    annotations = ToolAnnotations(read_only=True)

    async def execute(self, category: str = "", limit: int = 10) -> Observation:
        store = _get_store()
        results = store.list_recent(category=category or None, limit=limit)
        if not results:
            return Observation(tool_name="memory_list", success=True, output="暂无记忆")
        lines = [f"最近 {len(results)} 条记忆:\n"]
        for m in results:
            lines.append(f"[{m.id}] ({m.category}) {m.content[:80]}")
        return Observation(tool_name="memory_list", success=True, output="\n".join(lines))


# import 时自动注册
ToolRegistry.register(MemorySaveTool())
ToolRegistry.register(MemorySearchTool())
ToolRegistry.register(MemoryListTool())
