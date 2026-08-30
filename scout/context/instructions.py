"""分层指令链 — 借鉴 Codex AGENTS.md 的三级指令体系.

指令发现机制（每次运行重建，无缓存）：
1. **全局作用域**: $SCOUT_DATA_DIR/INSTRUCTIONS.override.md（若存在）否则 INSTRUCTIONS.md — 只取第一个非空
2. **项目作用域**: 从项目根到当前目录，每级目录依次检查 INSTRUCTIONS.override.md → INSTRUCTIONS.md — 每级至多用一个
3. **合并顺序**: root-first 拼接，离工作目录越近的文件越靠后出现（后出现的覆盖前面的语义）

硬边界：
- 空文件跳过
- 组合大小达到 max_bytes（默认 32KiB）即停止加载（对标 Codex project_doc_max_bytes）

兼容 Codex 生态：同时识别 AGENTS.md / AGENTS.override.md 文件名。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

logger = logging.getLogger(__name__)

# 每级目录按序检查的文件名（override 优先）
_INSTRUCTION_FILENAMES = [
    "INSTRUCTIONS.override.md",
    "INSTRUCTIONS.md",
    "AGENTS.override.md",
    "AGENTS.md",
]

DEFAULT_MAX_BYTES = 32 * 1024  # 32 KiB，对标 Codex project_doc_max_bytes


@dataclass
class InstructionSource:
    """一个被加载的指令文件."""
    scope: str          # "global" | "project"
    path: str
    content: str = ""
    truncated: bool = False


@dataclass
class InstructionChain:
    """指令链构建结果."""
    sources: list[InstructionSource] = field(default_factory=list)
    combined: str = ""
    stopped_at_limit: bool = False

    def describe(self) -> str:
        """人类可读的加载摘要（用于调试「加载了哪些指令」）."""
        if not self.sources:
            return "未加载任何指令文件"
        lines = [f"共加载 {len(self.sources)} 个指令文件:"]
        for i, s in enumerate(self.sources, 1):
            mark = " ⚠️后续文件因大小上限被跳过" if self.stopped_at_limit and i == len(self.sources) else ""
            lines.append(f"  {i}. [{s.scope}] {s.path} ({len(s.content)} chars){mark}")
        return "\n".join(lines)


class InstructionLoader:
    """分层指令加载器."""

    def __init__(
        self,
        global_dir: str | Path | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        custom_fallback_names: list[str] | None = None,
    ):
        """
        Args:
            global_dir: 全局指令目录（默认 $SCOUT_DATA_DIR）
            max_bytes: 组合大小上限
            custom_fallback_names: 自定义回退文件名（对标 Codex project_doc_fallback_filenames）
        """
        self.global_dir = Path(global_dir or _SCOUT_DATA_DIR).expanduser()
        self.max_bytes = max_bytes
        self._fallback_names = custom_fallback_names or []

    def _candidate_names(self) -> list[str]:
        """每级目录的检查顺序 = 标准名单 + 自定义回退名（追加在 override 之后、普通名之前无效——保持简单：追加到末尾）."""
        return _INSTRUCTION_FILENAMES + list(self._fallback_names)

    def _first_nonempty(self, directory: Path) -> Path | None:
        """在目录中按优先级找到第一个非空指令文件."""
        for name in self._candidate_names():
            p = directory / name
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                return p
        return None

    def build(self, working_dir: str | Path | None = None) -> InstructionChain:
        """构建指令链.

        Args:
            working_dir: 当前工作目录（项目作用域从 git 根/项目根到该目录）。
                         为 None 时只加载全局层。
        """
        chain = InstructionChain()
        used_bytes = 0

        # ── 1. 全局层 ──
        g_path = self._first_nonempty(self.global_dir)
        if g_path:
            content = g_path.read_text(encoding="utf-8", errors="ignore")
            chain.sources.append(InstructionSource(scope="global", path=str(g_path), content=content))
            used_bytes += len(content.encode("utf-8"))

        # ── 2. 项目层：从根到工作目录逐级收集 ──
        if working_dir:
            wd = Path(working_dir).expanduser().resolve()
            # 项目根：优先 git 根，否则取最深的含指令文件的祖先目录链顶端
            root = self._find_project_root(wd)
            if root:
                # 收集 root → wd 路径上每级目录（含两端）
                levels: list[Path] = []
                cur = wd
                while True:
                    levels.append(cur)
                    if cur == root:
                        break
                    if cur.parent == cur:  # 到文件系统根仍未到 root
                        break
                    cur = cur.parent
                levels.reverse()  # root-first

                for d in levels:
                    f = self._first_nonempty(d)
                    if not f:
                        continue
                    content = f.read_text(encoding="utf-8", errors="ignore")
                    size = len(content.encode("utf-8"))
                    if used_bytes + size > self.max_bytes:
                        chain.stopped_at_limit = True
                        logger.info(
                            f"指令链达到大小上限 {self.max_bytes}B，跳过 {f} 及后续文件"
                        )
                        break
                    chain.sources.append(
                        InstructionSource(scope="project", path=str(f), content=content)
                    )
                    used_bytes += size

        # ── 3. 拼接（root-first，近的靠后 = 语义上覆盖前面的）──
        parts = []
        for s in chain.sources:
            parts.append(f"<!-- instruction source: {s.path} -->\n{s.content.strip()}")
        chain.combined = "\n\n".join(parts)
        return chain

    def _find_project_root(self, wd: Path) -> Path | None:
        """找项目根：向上找 .git；找不到则用最深含指令文件的目录."""
        cur = wd
        git_root = None
        while cur != cur.parent:
            if (cur / ".git").exists():
                git_root = cur
                break
            cur = cur.parent

        if git_root:
            # git 根到 wd 之间任一层有指令文件才启用项目层
            return git_root

        # 无 .git：从 wd 向上找"最高的"含指令文件的目录作为根
        # （只要父目录还有指令文件就继续向上，保证链上所有层级都被收集）
        root_found: Path | None = None
        cur = wd
        while cur != cur.parent:
            if self._first_nonempty(cur):
                root_found = cur
            cur = cur.parent
        return root_found
