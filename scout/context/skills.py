"""技能系统 — SKILL.md 文件驱动技能 + 多作用域 + 渐进式披露.

v2 (2026-08-13) 对标 Codex Skills / agentskills.io 开放标准：
1. **多作用域发现**（对标 Codex 四层 scope，就近优先）：
   - REPO: $CWD/.scout/skills 及父目录、项目根的 .scout/skills、.agents/skills
   - USER: ~/.scout/skills（个人跨项目技能）
   - ADMIN: /etc/scout/skills（机器级默认技能）
2. **agentskills.io 兼容**：标准 YAML frontmatter（name + description），
   同时支持 scout 扩展字段 trigger/pattern。
3. **渐进式披露预算**（对标 Codex 2% 上下文预算 / Hermes 三层加载）：
   - build_skills_index(): 只给模型看 name+description 的索引，总预算受限
   - to_prompt(): 命中的技能全文注入，其余技能元数据按预算附带
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# 渐进式披露默认预算（对标 Codex：未知窗口时 8000 字符）
DEFAULT_INDEX_BUDGET = 8000


class Skill(BaseModel):
    """技能定义."""

    name: str
    description: str = ""
    trigger_patterns: list[str] = Field(default_factory=list)
    trigger_keywords: list[str] = Field(default_factory=list)
    instructions: str = ""
    location: str = ""
    base_dir: str = ""
    scope: str = "user"  # repo | user | admin

    def matches(self, user_input: str) -> bool:
        """检查用户输入是否匹配此技能（显式触发：关键词/正则/名称）."""
        text = user_input.lower()

        # 显式点名调用（对标 Codex $skill-name / 提及技能名）
        if self.name.lower() in text:
            return True

        for kw in self.trigger_keywords:
            if kw.lower() in text:
                return True

        for pattern in self.trigger_patterns:
            try:
                if re.search(pattern, user_input, re.IGNORECASE):
                    return True
            except re.error:
                continue

        if self.description and self.description.lower() in text:
            return True

        return False

    def relevance(self, user_input: str) -> int:
        """匹配强度打分（用于多技能命中时排序）."""
        text = user_input.lower()
        score = 0
        if self.name.lower() in text:
            score += 10
        score += sum(3 for kw in self.trigger_keywords if kw.lower() in text)
        score += sum(2 for p in self.trigger_patterns
                     if _safe_search(p, user_input))
        return score

    def to_prompt(self) -> str:
        """生成技能的 prompt 片段（全文加载）."""
        parts = [f"## 技能: {self.name}"]
        if self.description:
            parts.append(f"描述: {self.description}")
        if self.instructions:
            parts.append(f"指令:\n{self.instructions}")
        return "\n".join(parts)

    def to_index_line(self) -> str:
        """索引行（渐进式披露第一层：仅 name + description）."""
        desc = self.description.strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:200] + "…"
        return f"- **{self.name}** ({self.location}): {desc}"


def _safe_search(pattern: str, text: str) -> bool:
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        return False


class SkillManager:
    """技能管理器 — 多作用域发现、匹配、渐进式披露."""

    def __init__(
        self,
        skills_dir: str | Path = "~/.scout/skills",
        enable_repo_scope: bool = True,
        enable_admin_scope: bool = True,
        cwd: str | Path | None = None,
    ):
        self.skills_dir = Path(skills_dir).expanduser()
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._enable_repo_scope = enable_repo_scope
        self._enable_admin_scope = enable_admin_scope
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._skills: dict[str, Skill] = {}
        self._load_skills()

    # ── 作用域发现 ──

    def _scope_dirs(self) -> list[tuple[str, Path]]:
        """返回 [(scope, dir)]，按加载优先级排序（后加载覆盖先加载 = 就近优先）."""
        dirs: list[tuple[str, Path]] = []

        # ADMIN（机器级，最先加载 = 最低优先级）
        if self._enable_admin_scope:
            admin = Path("/etc/scout/skills")
            if admin.exists():
                dirs.append(("admin", admin))

        # USER（个人级）
        dirs.append(("user", self.skills_dir))

        # REPO（项目级，最后加载 = 最高优先级）
        if self._enable_repo_scope:
            repo_dirs = self._find_repo_skill_dirs()
            for d in repo_dirs:
                dirs.append(("repo", d))

        return dirs

    def _find_repo_skill_dirs(self) -> list[Path]:
        """从 cwd 向上查找项目技能目录（.scout/skills 和 .agents/skills）."""
        found: list[Path] = []
        cur = self._cwd.resolve()
        checked = 0
        while cur != cur.parent and checked < 10:
            for sub in (".scout/skills", ".agents/skills"):
                d = cur / sub
                if d.exists() and d.is_dir():
                    found.append(d)
            if (cur / ".git").exists():
                break  # 到 git 根为止
            cur = cur.parent
            checked += 1
        # root-first：从远到近（近的覆盖远的）
        found.reverse()
        return found

    # ── 加载与解析 ──

    def _load_skills(self):
        """加载所有作用域的技能（就近覆盖同名）."""
        self._skills = {}
        for scope, dir_path in self._scope_dirs():
            if not dir_path.exists():
                continue
            for item in dir_path.iterdir():
                if not item.is_dir():
                    continue
                skill_md = item / "SKILL.md"
                if not skill_md.exists():
                    continue
                skill = self._parse_skill_md(skill_md, scope)
                if skill:
                    self._skills[skill.name] = skill  # 后加载覆盖

    def reload(self):
        """热重载 — 重新扫描所有作用域."""
        self._load_skills()

    def _parse_skill_md(self, path: Path, scope: str = "user") -> Skill | None:
        """解析 SKILL.md — 标准 YAML frontmatter（agentskills.io 兼容）."""
        try:
            content = path.read_text(encoding="utf-8")
            frontmatter = ""
            body = content
            if content.startswith("---"):
                end = content.find("\n---", 3)
                if end > 0:
                    frontmatter = content[3:end].strip()
                    body = content[end + 4:].strip()

            meta = self._parse_frontmatter(frontmatter)

            name = meta.get("name") or path.parent.name
            description = meta.get("description", "") or ""

            trigger_keywords: list[str] = []
            trigger_patterns: list[str] = []

            # scout 扩展字段
            trig = meta.get("trigger", "")
            if trig:
                trigger_keywords = [k.strip() for k in str(trig).split(",") if k.strip()]
            pat = meta.get("pattern", "")
            if pat:
                pats = pat if isinstance(pat, list) else [pat]
                trigger_patterns = [str(p) for p in pats]
            # agentskills.io 的 metadata.requires/triggers 等自由字段
            md_meta = meta.get("metadata")
            if isinstance(md_meta, dict):
                extra_kws = md_meta.get("triggers") or md_meta.get("keywords")
                if isinstance(extra_kws, list):
                    trigger_keywords.extend(str(k) for k in extra_kws)

            return Skill(
                name=str(name),
                description=str(description),
                trigger_patterns=trigger_patterns,
                trigger_keywords=trigger_keywords,
                instructions=body,
                location=str(path),
                base_dir=str(path.parent),
                scope=scope,
            )
        except Exception:
            return None

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, Any]:
        """解析 frontmatter — 优先 YAML，失败退回行解析."""
        if not text:
            return {}
        if _HAS_YAML:
            try:
                data = yaml.safe_load(text)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        # 回退：简单行解析 key: value
        result: dict[str, Any] = {}
        for line in text.split("\n"):
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                k, _, v = line.partition(":")
                result[k.strip()] = v.strip().strip("\"'")
        return result

    # ── 匹配与检索 ──

    def find_skill(self, user_input: str) -> Skill | None:
        """查找最佳匹配技能."""
        best: Skill | None = None
        best_score = 0
        for skill in self._skills.values():
            if skill.matches(user_input):
                score = skill.relevance(user_input)
                if best is None or score > best_score:
                    best = skill
                    best_score = score
        return best

    def find_all(self, user_input: str) -> list[Skill]:
        """所有匹配技能（按相关度降序）."""
        matched = [s for s in self._skills.values() if s.matches(user_input)]
        matched.sort(key=lambda s: s.relevance(user_input), reverse=True)
        return matched

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def add_skill(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    # ── 渐进式披露 ──

    def build_skills_index(self, budget_chars: int = DEFAULT_INDEX_BUDGET) -> str:
        """技能索引（第一层披露）— 仅 name+description，控制在预算内.

        预算策略对标 Codex：超预算时先截断 description，再省略技能并警告。
        """
        skills = self.list_skills()
        if not skills:
            return ""

        header = "## 可用技能（需要时按名称调用）\n"
        lines: list[str] = []
        used = len(header)
        skipped = 0

        for skill in skills:
            line = skill.to_index_line()
            if used + len(line) + 1 > budget_chars:
                # 尝试截断 description 到 60 字符再塞一次
                short = f"- **{skill.name}**: {skill.description[:60]}…"
                if used + len(short) + 1 <= budget_chars:
                    lines.append(short)
                    used += len(short) + 1
                    skipped += 1
                    continue
                skipped += 1
                continue
            lines.append(line)
            used += len(line) + 1

        if skipped:
            lines.append(f"（另有 {skipped} 个技能未列出，超出索引预算）")
        return header + "\n".join(lines)

    def to_prompt(self, user_input: str, budget_chars: int = DEFAULT_INDEX_BUDGET) -> str:
        """生成技能 prompt 片段（渐进式披露）.

        - 命中的技能：全文注入（第二层披露）
        - 其余技能：仅当总预算允许时附带索引
        """
        skill = self.find_skill(user_input)
        if skill:
            return skill.to_prompt()
        return ""

    # ── 创建 ──

    def create_skill(
        self,
        name: str,
        description: str,
        instructions: str,
        trigger_keywords: list[str] | None = None,
        trigger_patterns: list[str] | None = None,
        scope: str = "user",
    ) -> Skill:
        """创建新技能并保存为 SKILL.md."""
        base = self.skills_dir if scope != "repo" else (self._cwd / ".scout" / "skills")
        skill_dir = base / name
        skill_dir.mkdir(parents=True, exist_ok=True)

        frontmatter_lines = [
            "---",
            f'name: "{name}"',
            f'description: "{description}"',
        ]
        if trigger_keywords:
            frontmatter_lines.append(f'trigger: "{",".join(trigger_keywords)}"')
        if trigger_patterns:
            for p in trigger_patterns:
                frontmatter_lines.append(f'pattern: "{p}"')
        frontmatter_lines.append("---")

        content = "\n".join(frontmatter_lines) + "\n\n" + instructions
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(content, encoding="utf-8")

        skill = Skill(
            name=name,
            description=description,
            trigger_keywords=trigger_keywords or [],
            trigger_patterns=trigger_patterns or [],
            instructions=instructions,
            location=str(skill_md),
            base_dir=str(skill_dir),
            scope=scope,
        )
        self._skills[name] = skill
        return skill

    def import_agentskills_dir(self, source_dir: str | Path, scope: str = "user") -> int:
        """从外部 agentskills.io 兼容目录批量导入技能.

        支持两种仓库形态：
        - 子目录多技能：repo/技能A/SKILL.md, repo/技能B/SKILL.md
        - 根目录单技能：repo/SKILL.md（仓库根部就是单个技能）

        Returns: 导入数量
        """
        src = Path(source_dir).expanduser()
        if not src.exists():
            return 0

        # 根目录单技能形态：src/SKILL.md 直接作为单个技能
        root_skill_md = src / "SKILL.md"
        if root_skill_md.exists():
            skill = self._parse_skill_md(root_skill_md, scope)
            if skill:
                base = self.skills_dir if scope != "repo" else (self._cwd / ".scout" / "skills")
                target = base / skill.name
                if not target.exists():
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "SKILL.md").write_text(root_skill_md.read_text(encoding="utf-8"), encoding="utf-8")
                    # 顺带复制 scripts/references/assets 等子目录
                    for sub in ("scripts", "references", "assets", "tools", "prompts"):
                        sub_src = src / sub
                        if sub_src.exists() and sub_src.is_dir():
                            import shutil
                            shutil.copytree(sub_src, target / sub, dirs_exist_ok=True)
                self._skills[skill.name] = Skill(**{**skill.model_dump(), "location": str(target / "SKILL.md"), "base_dir": str(target)})
                return 1

        # 子目录多技能形态
        count = 0
        for item in src.iterdir():
            if not item.is_dir():
                continue
            skill_md = item / "SKILL.md"
            if not skill_md.exists():
                continue
            skill = self._parse_skill_md(skill_md, scope)
            if not skill:
                continue
            # 复制到目标作用域目录
            base = self.skills_dir if scope != "repo" else (self._cwd / ".scout" / "skills")
            target = base / item.name
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                (target / "SKILL.md").write_text(skill_md.read_text(encoding="utf-8"), encoding="utf-8")
                # 顺带复制 scripts/references/assets 子目录（如果存在）
                for sub in ("scripts", "references", "assets"):
                    sub_src = item / sub
                    if sub_src.exists() and sub_src.is_dir():
                        import shutil
                        shutil.copytree(sub_src, target / sub, dirs_exist_ok=True)
            self._skills[skill.name] = Skill(**{**skill.model_dump(), "location": str(target / "SKILL.md"), "base_dir": str(target)})
            count += 1
        return count

    def remove_skill(self, name: str, scope: str = "user") -> bool:
        """删除已安装的技能（从磁盘 + 内存索引）.

        Returns: 是否删除成功（技能不存在返回 False）
        """
        import shutil as _sh
        skill = self._skills.get(name)
        base = self.skills_dir if scope != "repo" else (self._cwd / ".scout" / "skills")
        target = base / name

        removed = False
        # 优先删除内存中的技能目录（用户作用域）
        if target.exists():
            _sh.rmtree(target, ignore_errors=True)
            removed = True
        # 也清理 repo 作用域同名目录（保险）
        if scope == "repo":
            repo_target = self._cwd / ".scout" / "skills" / name
            if repo_target.exists():
                _sh.rmtree(repo_target, ignore_errors=True)
                removed = True

        # 从内存索引移除
        self._skills.pop(name, None)
        return removed
