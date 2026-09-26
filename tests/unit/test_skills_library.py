"""skills-library 结构校验测试 — 复用 skills-library/validate.py 的校验逻辑.

验证标准与运行时加载器 scout/context/skills.py:SkillManager 一致：
校验实现只有一份（skills-library/validate.py），本测试通过 importlib
加载它（skills-library 目录名含连字符，无法作为包导入），参数化遍历
全部技能包跑同样断言，另附校验器自身的负例自测。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT_DIR / "skills-library"
VALIDATE_PY = LIB_DIR / "validate.py"


def _load_validate_module():
    """加载 skills-library/validate.py（目录名含 '-'，不能常规 import）."""
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    spec = importlib.util.spec_from_file_location(
        "skills_library_validate", VALIDATE_PY
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate = _load_validate_module()

SKILL_DIRS = validate.iter_skill_dirs(LIB_DIR)


def _write_skill(skill_dir: Path, frontmatter: str, body: str = "# 配方\n\n正文。\n") -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return path


# ── 参数化遍历库内全部技能包 ──

@pytest.mark.unit
@pytest.mark.parametrize(
    "skill_dir",
    SKILL_DIRS,
    ids=lambda d: d.name,
)
def test_skill_package_valid(skill_dir: Path):
    """每个技能包：必填字段/name 一致/description 非空/version 格式/资源引用 全部通过."""
    problems = validate.validate_skill_dir(skill_dir)
    assert problems == [], f"{skill_dir.name} 校验失败:\n" + "\n".join(
        f"  - {p}" for p in problems
    )


@pytest.mark.unit
def test_library_has_skill_packages():
    """技能库至少应发现技能包（防止目录扫描逻辑失效导致参数化空跑）."""
    assert len(SKILL_DIRS) >= 1
    assert all((d / "SKILL.md").exists() for d in SKILL_DIRS)


# ── 校验器自身逻辑的负例/正例自测 ──

@pytest.mark.unit
def test_valid_minimal_skill_passes(tmp_path: Path):
    _write_skill(
        tmp_path / "demo-control",
        'name: demo-control\n'
        'description: "演示技能"\n'
        'trigger: 演示,demo\n'
        'version: 1.2.3\n'
        'author: tester\n',
    )
    assert validate.validate_skill_dir(tmp_path / "demo-control") == []


@pytest.mark.unit
def test_missing_skill_md_fails(tmp_path: Path):
    (tmp_path / "no-skill").mkdir()
    problems = validate.validate_skill_dir(tmp_path / "no-skill")
    assert any("缺少 SKILL.md" in p for p in problems)


@pytest.mark.unit
def test_missing_frontmatter_fails(tmp_path: Path):
    skill_dir = tmp_path / "raw-control"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 只有正文\n", encoding="utf-8")
    problems = validate.validate_skill_dir(skill_dir)
    assert any("frontmatter" in p for p in problems)


@pytest.mark.unit
def test_missing_required_fields_fail(tmp_path: Path):
    _write_skill(
        tmp_path / "sparse-control",
        'name: sparse-control\n'
        'description: ""\n'
        'trigger: ""\n'
        'version: ""\n'
        'author: ""\n',
    )
    problems = validate.validate_skill_dir(tmp_path / "sparse-control")
    for field in ("description", "trigger", "version", "author"):
        assert any(field in p for p in problems), f"未检出缺失字段 {field}"


@pytest.mark.unit
def test_name_mismatch_fails(tmp_path: Path):
    _write_skill(
        tmp_path / "real-name",
        'name: other-name\n'
        'description: "x"\n'
        'trigger: x\n'
        'version: 1.0.0\n'
        'author: t\n',
    )
    problems = validate.validate_skill_dir(tmp_path / "real-name")
    assert any("不一致" in p for p in problems)


@pytest.mark.unit
def test_bad_version_fails(tmp_path: Path):
    _write_skill(
        tmp_path / "ver-control",
        'name: ver-control\n'
        'description: "x"\n'
        'trigger: x\n'
        'version: "v1"\n'
        'author: t\n',
    )
    problems = validate.validate_skill_dir(tmp_path / "ver-control")
    assert any("version" in p and "X.Y.Z" in p for p in problems)


@pytest.mark.unit
def test_dangling_relative_resource_fails(tmp_path: Path):
    _write_skill(
        tmp_path / "res-control",
        'name: res-control\n'
        'description: "x"\n'
        'trigger: x\n'
        'version: 1.0.0\n'
        'author: t\n',
        body="# 配方\n\n![截图](assets/shot.png)\n[说明](references/guide.md)\n",
    )
    problems = validate.validate_skill_dir(tmp_path / "res-control")
    assert any("assets/shot.png" in p for p in problems)
    assert any("references/guide.md" in p for p in problems)


@pytest.mark.unit
def test_existing_relative_resource_passes(tmp_path: Path):
    skill_dir = tmp_path / "res2-control"
    _write_skill(
        skill_dir,
        'name: res2-control\n'
        'description: "x"\n'
        'trigger: x\n'
        'version: 1.0.0\n'
        'author: t\n',
        body="# 配方\n\n![截图](assets/shot.png)\n[外链](https://example.com/a.png)\n"
              "[锚点](#section)\n[绝对路径](C:/abs/path.png)\n",
    )
    (skill_dir / "assets").mkdir()
    (skill_dir / "assets" / "shot.png").write_bytes(b"png")
    assert validate.validate_skill_dir(skill_dir) == []
