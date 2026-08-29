"""统一文件工具 — 合并 8 个文件工具为 1 个，减少 ~1200 tokens/轮.

Actions:
- read:    读取文件内容（支持行范围）
- write:   写入文件（覆盖）
- list:    列出目录内容
- replace: 精确替换文本
- insert:  在指定行后插入内容
- delete:  删除指定行范围
- edit:    diff/patch 模式编辑（支持 SEARCH/REPLACE 块）

合并自: read_file, write_file, list_dir, file_read, file_replace,
        file_insert, file_delete, file_edit

路径沙箱（2026-08-27 新增）:
- 所有操作先经 _resolve_path() 归一化（expanduser + abspath，防 `..` 穿越）。
- 禁止访问系统敏感目录（scout.security.policy.SYSTEM_DIRS）：
  /etc /usr /bin /sbin /lib /lib64 /boot /sys /proc /dev /var /root。
- 仅允许访问白名单前缀（ALLOWED_PATH_PREFIXES）：
  /tmp /home /data /opt /srv /mnt /media /workspace。
- 与 shell 工具的 cwd/路径策略保持一致。
"""

from __future__ import annotations

import os
import shutil
import stat
from datetime import datetime
from pathlib import Path

import json
from pathlib import Path

from scout.core.annotations import ToolAnnotations
from scout.core.types import Observation
from scout.security.policy import ALLOWED_PATH_PREFIXES, SYSTEM_DIRS
from scout.tools.base import ToolDefinition


class UnifiedFileTool(ToolDefinition):
    """统一文件操作工具 — 一个工具完成所有文件操作."""

    name = "file"
    description = (
        "文件操作工具 — 读取、写入、编辑文件.\n\n"
        "1. 替换 (replace):\n"
        "   <<<<<<< SEARCH\n   原代码\n   =======\n   新代码\n   >>>>>>> REPLACE\n\n"
        "2. 插入 (insert):\n"
        "   <<<<<<< SEARCH\n   =======\n   新代码\n   >>>>>>> REPLACE\n\n"
        "3. 删除 (delete):\n"
        "   <<<<<<< SEARCH\n   要删除的代码\n   =======\n   >>>>>>> REPLACE\n"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "操作类型",
                "enum": ["read", "write", "list", "replace", "insert", "delete", "edit"],
            },
            "path": {
                "type": "string",
                "description": "文件路径",
            },
            "content": {
                "type": "string",
                "description": "要写入/插入的内容（write/insert 时使用）",
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号，从1开始（read/delete 时使用）",
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号，包含（read/delete 时使用，默认与 start_line 相同）",
            },
            "line": {
                "type": "integer",
                "description": "在此行后插入，0 表示在文件开头插入（insert 时使用）",
            },
            "old_text": {
                "type": "string",
                "description": "要被替换的文本，必须完全匹配（replace 时使用）",
            },
            "new_text": {
                "type": "string",
                "description": "替换后的文本（replace 时使用）",
            },
            "patch": {
                "type": "string",
                "description": "Diff/patch 格式的内容（edit 时使用）",
            },
        },
        "required": ["action"],
    }
    annotations = ToolAnnotations(
        read_only=False,
        destructive=True,
        idempotent=False,
        open_world=True,
    )

    async def execute(
        self,
        action: str,
        path: str = "",
        content: str = "",
        start_line: int = 1,
        end_line: int = -1,
        line: int = 0,
        old_text: str = "",
        new_text: str = "",
        patch: str = "",
    ) -> Observation:
        """分发到具体操作."""
        try:
            if action == "read":
                return self._read(path, start_line, end_line)
            elif action == "write":
                return self._write(path, content)
            elif action == "list":
                return self._list(path or ".")
            elif action == "replace":
                return self._replace(path, old_text, new_text)
            elif action == "insert":
                return self._insert(path, line, content)
            elif action == "delete":
                return self._delete(path, start_line, end_line)
            elif action == "edit":
                return self._edit(path, patch)
            else:
                return Observation(
                    tool_name="file",
                    success=False,
                    output=f"未知操作: {action}。支持: read, write, list, replace, insert, delete, edit",
                )
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"操作失败: {e}")

    # ── 路径沙箱 ──────────────────────────────────────────

    @staticmethod
    def _resolve_path(path: str) -> tuple[str, str]:
        """归一化并校验路径，返回 (绝对路径, 错误信息).

        规则:
        1. expanduser + abspath 归一化，防 `..` 相对路径穿越。
        2. 命中 SYSTEM_DIRS 系统敏感目录 → 拒绝。
        3. 未命中 ALLOWED_PATH_PREFIXES 白名单前缀 → 拒绝。
        """
        if not path:
            return "", "缺少 path 参数"

        abs_path = os.path.abspath(os.path.expanduser(path))

        for d in SYSTEM_DIRS:
            if abs_path == d or abs_path.startswith(d + os.sep):
                return "", f"⛔ 禁止访问系统目录: {d}（file 工具路径沙箱）"

        for prefix in ALLOWED_PATH_PREFIXES:
            if abs_path == prefix or abs_path.startswith(prefix + os.sep):
                return abs_path, ""

        return "", f"⛔ 路径不在允许的访问范围内: {abs_path}（file 工具路径沙箱，白名单: {ALLOWED_PATH_PREFIXES}）"

    # ── read ──────────────────────────────────────────────

    def _read(self, path: str, start_line: int = 1, end_line: int = -1) -> Observation:
        """读取文件内容（支持行范围）."""
        if not path:
            return Observation(tool_name="file", success=False, output="缺少 path 参数")

        path, err = self._resolve_path(path)
        if err:
            return Observation(tool_name="file", success=False, output=err, error_code="SANDBOX")
        if not os.path.exists(path):
            return Observation(tool_name="file", success=False, output=f"文件不存在: {path}", error_code="NOT_FOUND")
        if os.path.isdir(path):
            return Observation(tool_name="file", success=False, output=f"路径是目录，请使用 list 操作: {path}")

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"读取失败: {e}")

        total = len(lines)

        # 行范围处理
        if start_line < 1:
            start_line = 1
        if end_line == -1 or end_line > total:
            end_line = total

        selected = lines[start_line - 1 : end_line]

        # 带行号输出
        numbered = []
        for i, line_content in enumerate(selected, start=start_line):
            numbered.append(f"{i:4d} | {line_content.rstrip()}")

        header = f"📄 {path} (行 {start_line}-{end_line}/{total})"
        output = header + "\n" + "\n".join(numbered)

        return Observation(
            tool_name="file",
            success=True,
            output=output,
            metadata={"total_lines": total, "start": start_line, "end": end_line},
        )

    # ── write ─────────────────────────────────────────────

    def _write(self, path: str, content: str) -> Observation:
        """写入文件（覆盖）."""
        if not path:
            return Observation(tool_name="file", success=False, output="缺少 path 参数")
        if not content:
            return Observation(tool_name="file", success=False, output="缺少 content 参数")

        path, err = self._resolve_path(path)
        if err:
            return Observation(tool_name="file", success=False, output=err, error_code="SANDBOX")

        # 创建目录
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        # 备份已有文件
        if os.path.exists(path):
            bak_path = path + ".bak"
            try:
                shutil.copy2(path, bak_path)
            except Exception:
                pass  # 备份失败不阻断

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"写入失败: {e}")

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return Observation(
            tool_name="file",
            success=True,
            output=f"✅ 已写入 {path} ({line_count} 行, {len(content)} 字节)",
        )

    # ── list ──────────────────────────────────────────────

    def _list(self, path: str) -> Observation:
        """列出目录内容."""
        path, err = self._resolve_path(path)
        if err:
            return Observation(tool_name="file", success=False, output=err, error_code="SANDBOX")
        if not os.path.exists(path):
            return Observation(tool_name="file", success=False, output=f"目录不存在: {path}", error_code="NOT_FOUND")
        if not os.path.isdir(path):
            return Observation(tool_name="file", success=False, output=f"路径不是目录: {path}")

        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return Observation(tool_name="file", success=False, output=f"权限不足: {path}")

        lines = [f"📁 {path}/\n"]
        dirs = []
        files = []

        for entry in entries:
            full = os.path.join(path, entry)
            try:
                st = os.stat(full)
                size = st.st_size
                mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except (OSError, PermissionError):
                size = 0
                mtime = "unknown"

            if os.path.isdir(full):
                dirs.append(f"  📁 {entry}/")
            else:
                size_str = self._format_size(size)
                files.append(f"  📄 {entry}  ({size_str}, {mtime})")

        lines.extend(dirs)
        if dirs and files:
            lines.append("")
        lines.extend(files)
        lines.append(f"\n共 {len(dirs)} 个目录, {len(files)} 个文件")

        return Observation(tool_name="file", success=True, output="\n".join(lines))

    # ── replace ───────────────────────────────────────────

    def _replace(self, path: str, old_text: str, new_text: str) -> Observation:
        """精确替换文件中的文本."""
        if not path:
            return Observation(tool_name="file", success=False, output="缺少 path 参数")
        if not old_text:
            return Observation(tool_name="file", success=False, output="缺少 old_text 参数")

        path, err = self._resolve_path(path)
        if err:
            return Observation(tool_name="file", success=False, output=err, error_code="SANDBOX")
        if not os.path.exists(path):
            return Observation(tool_name="file", success=False, output=f"文件不存在: {path}", error_code="NOT_FOUND")

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"读取失败: {e}")

        if old_text not in content:
            # 尝试给出有用的错误信息
            hint = self._find_similar(content, old_text)
            return Observation(
                tool_name="file",
                success=False,
                output=f"未找到匹配的文本。\n{hint}",
            )

        count = content.count(old_text)
        if count > 1:
            return Observation(
                tool_name="file",
                success=False,
                output=f"找到 {count} 处匹配，请提供更多上下文以确保唯一匹配。",
            )

        # 备份
        self._backup(path)

        new_content = content.replace(old_text, new_text, 1)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"写入失败: {e}")

        # Python 语法检查
        syntax_result = self._check_syntax(path, new_content)
        msg = f"✅ 已替换 {path}"
        if syntax_result:
            msg += f"\n⚠️ {syntax_result}"

        return Observation(tool_name="file", success=True, output=msg)

    # ── insert ────────────────────────────────────────────

    def _insert(self, path: str, line: int, content: str) -> Observation:
        """在指定行后插入内容."""
        if not path:
            return Observation(tool_name="file", success=False, output="缺少 path 参数")
        if not content:
            return Observation(tool_name="file", success=False, output="缺少 content 参数")

        path, err = self._resolve_path(path)
        if err:
            return Observation(tool_name="file", success=False, output=err, error_code="SANDBOX")

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception as e:
                return Observation(tool_name="file", success=False, output=f"读取失败: {e}")
        else:
            lines = []

        # 备份
        if os.path.exists(path):
            self._backup(path)

        # 确保 content 以换行结尾
        if content and not content.endswith("\n"):
            content += "\n"

        insert_lines = content.splitlines(True)

        if line == 0:
            # 在文件开头插入
            new_lines = insert_lines + lines
        elif line >= len(lines):
            # 在文件末尾插入
            new_lines = lines + insert_lines
        else:
            # 在指定行后插入
            new_lines = lines[:line] + insert_lines + lines[line:]

        try:
            # 创建目录
            dir_path = os.path.dirname(path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"写入失败: {e}")

        return Observation(
            tool_name="file",
            success=True,
            output=f"✅ 已在第 {line} 行后插入 {len(insert_lines)} 行到 {path}",
        )

    # ── delete ────────────────────────────────────────────

    def _delete(self, path: str, start_line: int, end_line: int = -1) -> Observation:
        """删除指定行范围."""
        if not path:
            return Observation(tool_name="file", success=False, output="缺少 path 参数")
        if start_line < 1:
            return Observation(tool_name="file", success=False, output="start_line 必须 >= 1")

        path, err = self._resolve_path(path)
        if err:
            return Observation(tool_name="file", success=False, output=err, error_code="SANDBOX")
        if not os.path.exists(path):
            return Observation(tool_name="file", success=False, output=f"文件不存在: {path}", error_code="NOT_FOUND")

        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"读取失败: {e}")

        total = len(lines)
        if end_line == -1 or end_line < start_line:
            end_line = start_line
        if end_line > total:
            end_line = total

        # 备份
        self._backup(path)

        # 删除行（start_line 从 1 开始）
        del lines[start_line - 1 : end_line]

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"写入失败: {e}")

        deleted_count = end_line - start_line + 1
        return Observation(
            tool_name="file",
            success=True,
            output=f"✅ 已删除 {path} 第 {start_line}-{end_line} 行 ({deleted_count} 行)",
        )

    # ── edit (diff/patch) ─────────────────────────────────

    def _edit(self, path: str, patch: str) -> Observation:
        """使用 diff/patch 模式编辑文件.

        支持三种操作:
        1. 替换: <<<<<<< SEARCH / 原代码 / ======= / 新代码 / >>>>>>> REPLACE
        2. 插入: <<<<<<< SEARCH / ======= / 新代码 / >>>>>>> REPLACE
        3. 删除: <<<<<<< SEARCH / 要删除的代码 / ======= / >>>>>>> REPLACE
        """
        if not path:
            return Observation(tool_name="file", success=False, output="缺少 path 参数")
        if not patch:
            return Observation(tool_name="file", success=False, output="缺少 patch 参数")

        path, err = self._resolve_path(path)
        if err:
            return Observation(tool_name="file", success=False, output=err, error_code="SANDBOX")

        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as e:
                return Observation(tool_name="file", success=False, output=f"读取失败: {e}")
        else:
            content = ""

        # 解析 patch 块
        blocks = self._parse_patch_blocks(patch)
        if not blocks:
            return Observation(
                tool_name="file",
                success=False,
                output="未找到有效的 SEARCH/REPLACE 块。格式:\n<<<<<<< SEARCH\n原代码\n=======\n新代码\n>>>>>>> REPLACE",
            )

        # 备份
        if os.path.exists(path):
            self._backup(path)

        applied = 0
        errors = []

        for i, (search, replace) in enumerate(blocks):
            if search and search in content:
                content = content.replace(search, replace, 1)
                applied += 1
            elif not search:
                # 纯插入（SEARCH 为空）— 追加到末尾
                content += replace
                applied += 1
            elif not replace:
                # 纯删除（REPLACE 为空）
                if search in content:
                    content = content.replace(search, "", 1)
                    applied += 1
                else:
                    errors.append(f"块 {i+1}: 未找到要删除的文本")
            else:
                errors.append(f"块 {i+1}: 未找到匹配的 SEARCH 文本")

        if errors and applied == 0:
            return Observation(
                tool_name="file",
                success=False,
                output="编辑失败:\n" + "\n".join(errors),
            )

        try:
            dir_path = os.path.dirname(path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return Observation(tool_name="file", success=False, output=f"写入失败: {e}")

        # Python 语法检查
        syntax_result = self._check_syntax(path, content)
        msg = f"✅ 已编辑 {path} (应用 {applied}/{len(blocks)} 个块)"
        if errors:
            msg += "\n⚠️ 部分块未应用:\n" + "\n".join(errors)
        if syntax_result:
            msg += f"\n⚠️ {syntax_result}"

        return Observation(tool_name="file", success=True, output=msg)

    # ── 辅助方法 ──────────────────────────────────────────

    def _backup(self, path: str) -> None:
        """创建文件备份."""
        bak_path = path + ".bak"
        try:
            shutil.copy2(path, bak_path)
        except Exception:
            pass

    def _parse_patch_blocks(self, patch: str) -> list[tuple[str, str]]:
        """解析 SEARCH/REPLACE 块."""
        import re

        blocks = []
        # 支持多种分隔符变体
        pattern = re.compile(
            r"<{7}\s*SEARCH\s*\n(.*?)\n={7}\s*\n(.*?)\n>{7}\s*REPLACE",
            re.DOTALL,
        )

        for match in pattern.finditer(patch):
            search = match.group(1)
            replace = match.group(2)
            blocks.append((search, replace))

        return blocks

    def _check_syntax(self, path: str, content: str) -> str:
        """检查 Python 文件语法."""
        if not path.endswith(".py"):
            return ""

        try:
            compile(content, path, "exec")
            return ""
        except SyntaxError as e:
            return f"Python 语法错误 (行 {e.lineno}): {e.msg}"

    def _find_similar(self, content: str, target: str) -> str:
        """尝试找到相似文本，给出有用的提示."""
        # 简单的行级匹配检查
        target_lines = target.strip().split("\n")
        if not target_lines:
            return ""

        first_line = target_lines[0].strip()
        content_lines = content.split("\n")

        matches = []
        for i, line in enumerate(content_lines, 1):
            if first_line in line or line.strip() in first_line:
                matches.append(f"  行 {i}: {line.strip()[:80]}")
                if len(matches) >= 3:
                    break

        if matches:
            return "可能的匹配位置:\n" + "\n".join(matches)
        return "请检查文本是否包含多余的空格或换行差异。"

    @staticmethod
    def _format_size(size: int) -> str:
        """格式化文件大小."""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / 1024 / 1024:.1f} MB"
        else:
            return f"{size / 1024 / 1024 / 1024:.1f} GB"
