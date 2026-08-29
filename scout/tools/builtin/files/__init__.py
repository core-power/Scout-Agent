"""统一文件工具 — 合并 8 个文件工具为 1 个.

合并自: read_file, write_file, list_dir, file_read, file_replace,
        file_insert, file_delete, file_edit

节省 ~1200 tokens/轮 的工具定义开销。
"""

from scout.tools.builtin.files.unified import UnifiedFileTool
from scout.tools.registry import ToolRegistry

# 注册统一文件工具（替代原来的 8 个独立文件工具）
ToolRegistry.register(UnifiedFileTool())
