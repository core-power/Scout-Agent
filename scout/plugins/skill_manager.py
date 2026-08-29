"""第三方技能包管理 — 支持安装、卸载与版本隔离."""

import json
import shutil
from pathlib import Path
from typing import List, Optional

from scout.config.settings import ScoutConfig


class SkillPackage:
    """技能包元数据."""
    def __init__(self, name: str, version: str, description: str = "", author: str = ""):
        self.name = name
        self.version = version
        self.description = description
        self.author = author

    def to_dict(self):
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
        }

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


class SkillManager:
    """管理第三方技能包的生命周期."""
    
    def __init__(self, config: ScoutConfig):
        self.config = config
        self.skills_dir = config.skills_dir
        self.manifest_file = self.skills_dir / "installed_skills.json"
        self._ensure_manifest()

    def _ensure_manifest(self):
        if not self.manifest_file.exists():
            self.manifest_file.write_text(json.dumps([]))

    def install(self, source_path: str, package: SkillPackage) -> bool:
        """从源码路径安装技能包."""
        target_dir = self.skills_dir / package.name
        if target_dir.exists():
            # 简单的版本覆盖逻辑，实际可扩展为多版本共存
            shutil.rmtree(target_dir)
        
        shutil.copytree(source_path, target_dir)
        
        # 更新清单
        installed = self.list_installed()
        installed.append(package.to_dict())
        self.manifest_file.write_text(json.dumps(installed, indent=2))
        return True

    def uninstall(self, name: str) -> bool:
        """卸载技能包."""
        target_dir = self.skills_dir / name
        if not target_dir.exists():
            return False
        
        shutil.rmtree(target_dir)
        
        installed = [s for s in self.list_installed() if s["name"] != name]
        self.manifest_file.write_text(json.dumps(installed, indent=2))
        return True

    def list_installed(self) -> List[dict]:
        """列出已安装的包."""
        try:
            return json.loads(self.manifest_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def get_skill_path(self, name: str) -> Optional[Path]:
        """获取技能包的运行时路径."""
        path = self.skills_dir / name
        return path if path.exists() else None
