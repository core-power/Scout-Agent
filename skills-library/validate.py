#!/usr/bin/env python3
"""skills-library 结构验证脚本.

校验标准与运行时加载器 scout/context/skills.py:SkillManager 保持一致
（直接复用其 frontmatter 解析逻辑，不另造一套），并在其之上叠加
skills-library/README.md 记载的库内格式要求：

  1. 每个技能目录必须有 SKILL.md，且带完整 YAML frontmatter（--- 包裹）
  2. frontmatter 必填字段非空：name / description / trigger / version / author
  3. name 必须与目录名一致（运行时缺 name 会静默回退目录名，库内不允许歧义）
  4. version 必须是 X.Y.Z 语义化格式
  5. 运行时加载器能成功解析该文件（round-trip 一致）
  6. 正文中 markdown 图片/链接引用的相对资源文件必须真实存在

用法（仓库内任意位置均可）:
    python skills-library/validate.py

退出码: 0 = 全部 PASS; 1 = 存在 FAIL; 2 = 环境错误（无法导入运行时加载器）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# ── 路径 ──
LIB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = LIB_DIR.parent

# ── 库内格式要求（对应 README.md "格式" 一节）──
REQUIRED_FIELDS = ("name", "description", "trigger", "version", "author")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# markdown 图片 ![alt](target) 与普通链接 [text](target)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "data:", "#")
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _load_runtime_loader() -> type:
    """导入运行时技能加载器（复用其解析逻辑，保证标准一致）."""
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from scout.context.skills import SkillManager  # noqa: PLC0415

    return SkillManager


def _split_frontmatter(content: str) -> tuple[str, str] | None:
    """切分 frontmatter / 正文 — 与 SkillManager._parse_skill_md 相同的算法."""
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end <= 0:
        return None
    return content[3:end].strip(), content[end + 4:].strip()


def _is_external_or_absolute(target: str) -> bool:
    """链接目标是否为外链/锚点/绝对路径（无需本地存在）."""
    return (
        target.startswith(_EXTERNAL_PREFIXES)
        or target.startswith(("/", "\\"))
        or bool(_WIN_ABS_RE.match(target))
    )


def _missing_resources(skill_dir: Path, body: str) -> list[str]:
    """收集正文中引用了但磁盘上不存在的相对资源."""
    missing: list[str] = []
    for target in [m.group(1) for m in _MD_IMAGE_RE.finditer(body)] + [
        m.group(1) for m in _MD_LINK_RE.finditer(body)
    ]:
        if _is_external_or_absolute(target):
            continue
        # 去掉 #fragment、解码 %20 等转义后按技能目录解析
        rel = unquote(target.split("#", 1)[0])
        if not rel:
            continue
        resolved = (skill_dir / rel).resolve()
        try:
            resolved.relative_to(skill_dir.resolve())
        except ValueError:
            missing.append(f"资源引用越出技能目录: {target}")
            continue
        if not resolved.exists():
            missing.append(f"引用的相对资源不存在: {target}")
    return missing


def validate_skill_dir(
    skill_dir: Path, skill_manager_cls: type | None = None
) -> list[str]:
    """校验单个技能目录，返回问题清单（空列表 = 通过）."""
    problems: list[str] = []
    if skill_manager_cls is None:
        skill_manager_cls = _load_runtime_loader()

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return [f"缺少 {skill_md.name}"]

    content = skill_md.read_text(encoding="utf-8")
    parts = _split_frontmatter(content)
    if parts is None:
        return ["缺少 YAML frontmatter（须以 --- 开始并闭合）"]
    frontmatter, body = parts

    meta: dict[str, Any] = skill_manager_cls._parse_frontmatter(frontmatter)
    if not isinstance(meta, dict) or not meta:
        return ["frontmatter 解析结果为空（不是合法的 key: value 结构）"]

    # 1) 必填字段非空
    for field in REQUIRED_FIELDS:
        value = meta.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            problems.append(f"frontmatter 缺少必填字段或为空: {field}")
        elif not isinstance(value, str):
            problems.append(f"frontmatter 字段 {field} 应为字符串，实际为 {type(value).__name__}")

    # 2) name 与目录名一致
    name = meta.get("name")
    if isinstance(name, str) and name.strip() and name.strip() != skill_dir.name:
        problems.append(f"name ({name.strip()!r}) 与目录名 ({skill_dir.name!r}) 不一致")

    # 3) version 格式
    version = meta.get("version")
    if isinstance(version, str) and version.strip() and not VERSION_RE.match(version.strip()):
        problems.append(f"version ({version.strip()!r}) 不符合 X.Y.Z 格式")

    # 4) 运行时加载器 round-trip（与 SkillManager._parse_skill_md 一致）
    loader = skill_manager_cls.__new__(skill_manager_cls)
    skill = loader._parse_skill_md(skill_md, scope="repo")
    if skill is None:
        problems.append("运行时加载器解析失败（_parse_skill_md 返回 None）")
    else:
        if skill.name != skill_dir.name:
            problems.append(f"运行时加载得到的 name ({skill.name!r}) 与目录名不一致")
        if not skill.instructions.strip():
            problems.append("正文（指令）为空")
        if not skill.trigger_keywords:
            problems.append("trigger 解析后无有效关键词（无法被触发命中）")

    # 5) 相对资源引用存在性
    problems.extend(_missing_resources(skill_dir, body))

    return problems


def iter_skill_dirs(lib_dir: Path = LIB_DIR) -> list[Path]:
    """列出库内全部技能目录（跳过隐藏/下划线开头的非技能目录）."""
    return sorted(
        d for d in lib_dir.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    )


def validate_library(lib_dir: Path = LIB_DIR) -> list[tuple[str, list[str]]]:
    """校验整个技能库，返回 [(技能目录名, 问题清单)]，按目录名排序."""
    skill_manager_cls = _load_runtime_loader()
    return [
        (d.name, validate_skill_dir(d, skill_manager_cls))
        for d in iter_skill_dirs(lib_dir)
    ]


def main(argv: list[str] | None = None) -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        results = validate_library()
    except Exception as exc:  # 环境问题：无法导入 scout 运行时加载器等
        print(f"ERROR: 无法执行校验（环境问题）: {exc}")
        print("请在仓库内运行，例如: python skills-library/validate.py")
        return 2

    print(f"skills-library 校验（{len(results)} 个技能包）")
    print("=" * 60)
    failed = 0
    for name, problems in results:
        if problems:
            failed += 1
            print(f"FAIL  {name}")
            for p in problems:
                print(f"      - {p}")
        else:
            print(f"PASS  {name}")

    print("=" * 60)
    passed = len(results) - failed
    print(f"结果: {passed} PASS / {failed} FAIL，共 {len(results)} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
