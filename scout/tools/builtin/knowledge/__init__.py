"""知识库工具 — 管理个人知识库，读写知识页面.

优化 (2026-08-01):
- 搜索使用倒排索引（内存缓存），避免暴力全文扫描
- 索引更新使用原子操作（先写临时文件，再 os.replace）
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from datetime import datetime

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.tools.base import ToolDefinition
from scout.tools.registry import ToolRegistry

# 知识库根目录
KNOWLEDGE_DIR = Path.home() / ".scout" / "knowledge"
# 倒排索引缓存文件
_INDEX_CACHE_PATH = Path.home() / ".scout" / "knowledge_index.json"


class _InvertedIndex:
    """简单的倒排索引 — keyword → [(path, score_hint), ...]."""

    def __init__(self):
        self._index: dict[str, list[str]] = {}  # keyword → [path, ...]
        self._built = False

    def build(self, knowledge_dir: Path) -> None:
        """从所有 .md 文件构建倒排索引."""
        self._index.clear()
        if not knowledge_dir.exists():
            self._built = True
            return

        for md_file in knowledge_dir.rglob("*.md"):
            if md_file.name == "index.md":
                continue
            try:
                content = md_file.read_text(encoding="utf-8").lower()
                rel_path = str(md_file.relative_to(knowledge_dir))
                # 提取关键词（中文 2 字以上 + 英文单词）
                words = set(re.findall(r'[a-z]{2,}|[\u4e00-\u9fff]{2,}', content))
                for word in words:
                    if word not in self._index:
                        self._index[word] = []
                    self._index[word].append(rel_path)
            except Exception:
                continue

        self._built = True

    def search(self, query: str, knowledge_dir: Path) -> list[tuple[str, int, str]]:
        """搜索 — 返回 [(path, score, summary), ...]."""
        if not self._built:
            self.build(knowledge_dir)

        query_words = set(re.findall(r'[a-z]{2,}|[\u4e00-\u9fff]{2,}', query.lower()))
        if not query_words:
            # 回退到简单匹配
            query_words = {query.lower()}

        # 统计每个 path 的匹配度
        path_scores: dict[str, int] = {}
        for word in query_words:
            # 精确匹配
            if word in self._index:
                for p in self._index[word]:
                    path_scores[p] = path_scores.get(p, 0) + 1
            # 前缀匹配（中文友好）
            for kw, paths in self._index.items():
                if kw.startswith(word) or word.startswith(kw):
                    for p in paths:
                        path_scores[p] = path_scores.get(p, 0) + 1

        if not path_scores:
            return []

        # 按分数排序，提取摘要
        results = []
        for path, score in sorted(path_scores.items(), key=lambda x: -x[1])[:10]:
            full_path = knowledge_dir / path
            try:
                content = full_path.read_text(encoding="utf-8")
                first_para = content.split("\n\n")[0][:100]
            except Exception:
                first_para = ""
            results.append((path, score, first_para))

        return results

    def invalidate(self) -> None:
        """标记索引需要重建."""
        self._built = False


# 全局倒排索引实例
_global_index = _InvertedIndex()


class KnowledgeTool(ToolDefinition):
    """知识库管理 — 创建、读取、搜索知识页面."""

    name = "knowledge"
    description = "管理个人知识库。可以创建知识页面、读取内容、搜索知识、列出索引。适合保存重要结论、方案、概念。"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作: create(创建页面), read(读取页面), search(搜索), list(列出索引), update(更新页面)",
                "enum": ["create", "read", "search", "list", "update"],
            },
            "path": {"type": "string", "description": "知识页面路径（如 concepts/ai-agent.md）"},
            "content": {"type": "string", "description": "页面内容（create/update 时使用）"},
            "query": {"type": "string", "description": "搜索关键词（search 时使用）"},
        },
        "required": ["action"],
    }
    annotations = ToolAnnotations(read_only=False, open_world=False)

    async def execute(self, action: str, path: str = "", content: str = "",
                      query: str = "") -> Observation:
        try:
            KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

            if action == "create":
                return self._create(path, content)
            elif action == "read":
                return self._read(path)
            elif action == "search":
                return self._search(query)
            elif action == "list":
                return self._list()
            elif action == "update":
                return self._update(path, content)
            else:
                return Observation(tool_name="knowledge", success=False, output=f"未知操作: {action}")
        except Exception as e:
            return Observation(tool_name="knowledge", success=False, output=str(e))

    def _create(self, path: str, content: str) -> Observation:
        if not path:
            return Observation(tool_name="knowledge", success=False, output="需要提供 path")
        if not content:
            return Observation(tool_name="knowledge", success=False, output="需要提供 content")

        # 安全检查 — 不允许路径穿越
        full_path = (KNOWLEDGE_DIR / path).resolve()
        if not str(full_path).startswith(str(KNOWLEDGE_DIR.resolve())):
            return Observation(tool_name="knowledge", success=False, output="路径不合法")

        full_path.parent.mkdir(parents=True, exist_ok=True)

        # 原子写入 — 先写临时文件，再 os.replace
        tmp_path = full_path.with_suffix(".tmp")
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, full_path)

        # 更新索引
        self._update_index(path)

        # 使倒排索引失效
        _global_index.invalidate()

        return Observation(tool_name="knowledge", success=True, output=f"✅ 知识页面已创建: {path}")

    def _read(self, path: str) -> Observation:
        if not path:
            return Observation(tool_name="knowledge", success=False, output="需要提供 path")

        full_path = (KNOWLEDGE_DIR / path).resolve()
        if not full_path.exists():
            return Observation(tool_name="knowledge", success=False, output=f"页面不存在: {path}")

        content = full_path.read_text(encoding="utf-8")
        if len(content) > 5000:
            content = content[:5000] + f"\n\n... [截断，共 {len(content)} 字符]"
        return Observation(tool_name="knowledge", success=True, output=content)

    def _search(self, query: str) -> Observation:
        if not query:
            return Observation(tool_name="knowledge", success=False, output="需要提供 query")

        # 使用倒排索引搜索
        results = _global_index.search(query, KNOWLEDGE_DIR)

        if not results:
            return Observation(tool_name="knowledge", success=True, output="未找到匹配的知识")

        lines = [f"🔍 搜索 '{query}' — 找到 {len(results)} 条结果\n"]
        for path, score, summary in results:
            lines.append(f"- {path} (相关度: {score})")
            lines.append(f"  {summary}\n")

        return Observation(tool_name="knowledge", success=True, output="\n".join(lines))

    def _list(self) -> Observation:
        index_path = KNOWLEDGE_DIR / "index.md"
        if index_path.exists():
            content = index_path.read_text(encoding="utf-8")
            return Observation(tool_name="knowledge", success=True, output=content)

        # 如果没有索引，扫描所有文件
        files = list(KNOWLEDGE_DIR.rglob("*.md"))
        if not files:
            return Observation(tool_name="knowledge", success=True, output="知识库为空")

        lines = ["# 知识库索引\n"]
        for f in sorted(files):
            if f.name == "index.md":
                continue
            rel_path = str(f.relative_to(KNOWLEDGE_DIR))
            title = f.stem.replace("-", " ").title()
            lines.append(f"- [{title}]({rel_path})")

        return Observation(tool_name="knowledge", success=True, output="\n".join(lines))

    def _update(self, path: str, content: str) -> Observation:
        return self._create(path, content)  # 覆盖写入

    def _update_index(self, new_path: str) -> None:
        """更新索引文件 — 原子操作."""
        index_path = KNOWLEDGE_DIR / "index.md"
        title = Path(new_path).stem.replace("-", " ").title()
        entry = f"- [{title}]({new_path})"

        if index_path.exists():
            content = index_path.read_text(encoding="utf-8")
            if new_path not in content:
                content = content.rstrip() + f"\n{entry}\n"
                # 原子写入
                tmp_path = index_path.with_suffix(".tmp")
                tmp_path.write_text(content, encoding="utf-8")
                os.replace(tmp_path, index_path)
        else:
            tmp_path = index_path.with_suffix(".tmp")
            tmp_path.write_text(f"# 知识库索引\n\n{entry}\n", encoding="utf-8")
            os.replace(tmp_path, index_path)


ToolRegistry.register(KnowledgeTool())
