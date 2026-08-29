"""Skill Manager — 管理内置与用户自定义技能的加载与隔离.

遵循 XDG Base Directory 规范，确保用户技能在代码更新时不受影响。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


class SkillManager:
    """技能管理器 — 负责发现、加载和隔离 Skills."""

    def __init__(self):
        # 1. 内置技能路径 (随代码发布)
        self.builtin_dir = Path(__file__).parent.parent / "skills" / "builtin"
        
        # 2. 用户技能路径 (遵循 XDG 标准，永久保存)
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", "")) / "Scout"
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "Scout"
        else:
            base = Path.home() / ".local" / "share" / "scout"
        
        self.user_dir = base / "skills"
        self.user_dir.mkdir(parents=True, exist_ok=True)

        self._skills_cache: dict[str, dict[str, Any]] = {}

    def discover_all(self) -> list[dict[str, Any]]:
        """发现所有可用的技能（内置 + 用户）。"""
        skills = []
        
        # 加载内置技能
        if self.builtin_dir.exists():
            skills.extend(self._scan_directory(self.builtin_dir, prefix="builtin"))
        
        # 加载用户技能 (优先级更高，可覆盖内置)
        if self.user_dir.exists():
            user_skills = self._scan_directory(self.user_dir)
            # 简单的去重逻辑：如果用户定义了同名技能，移除内置的
            user_names = {s["name"] for s in user_skills}
            skills = [s for s in skills if s["name"] not in user_names]
            skills.extend(user_skills)
            
        self._skills_cache = {s["name"]: s for s in skills}
        return skills

    def _scan_directory(self, path: Path, prefix: str = "") -> list[dict[str, Any]]:
        """扫描目录下的 skill.yaml 文件。"""
        skills = []
        for item in path.iterdir():
            if item.is_dir():
                yaml_file = item / "skill.yaml"
                if yaml_file.exists():
                    try:
                        with open(yaml_file, 'r', encoding='utf-8') as f:
                            meta = yaml.safe_load(f)
                        if meta:
                            meta["path"] = str(item)
                            meta["source"] = "user" if not prefix else "builtin"
                            meta["id"] = f"{prefix}.{meta['name']}" if prefix else meta["name"]
                            skills.append(meta)
                    except Exception as e:
                        print(f"[SkillManager] 加载失败 {yaml_file}: {e}")
        return skills

    def get_skill(self, name: str) -> dict[str, Any] | None:
        """根据名称获取技能元数据。"""
        return self._skills_cache.get(name)

    def migrate_old_skills(self, old_path: Path):
        """从旧版本的项目内目录迁移技能到标准用户目录。"""
        if old_path.exists() and old_path.is_dir():
            print(f"[SkillManager] 发现旧版技能目录: {old_path}")
            print(f"[SkillManager] 正在迁移至: {self.user_dir}")
            for item in old_path.iterdir():
                dest = self.user_dir / item.name
                if not dest.exists():
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
            print("[SkillManager] 迁移完成。")


# 全局单例
_manager = SkillManager()

def get_skill_manager() -> SkillManager:
    return _manager
