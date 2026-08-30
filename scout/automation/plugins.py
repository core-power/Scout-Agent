"""插件系统 — 借鉴 Hermes 的 3 源发现机制.

3 个发现源: 用户目录 / 项目目录 / pip entry points
插件可以是: 工具包 / 技能 / 适配器 / 钩子
"""

from __future__ import annotations

import importlib
import os
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR


class Plugin:
    """插件信息."""

    def __init__(
        self,
        name: str,
        source: str,  # "user_dir" / "project_dir" / "pip"
        path: str = "",
        module: Any = None,
        error: str = "",
    ):
        self.name = name
        self.source = source
        self.path = path
        self.module = module
        self.error = error
        self.loaded = module is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "path": self.path,
            "loaded": self.loaded,
            "error": self.error,
        }


class PluginManager:
    """插件管理器 — 3 源发现 + 加载."""

    def __init__(
        self,
        user_dir: str | None = None,
        project_dir: str = "",
    ):
        self.user_dir = Path(
            user_dir if user_dir is not None else str(_SCOUT_DATA_DIR / "plugins")
        ).expanduser()
        self.project_dir = Path(project_dir) if project_dir else Path.cwd() / ".scout" / "plugins"
        self._plugins: list[Plugin] = []

    def discover(self) -> list[Plugin]:
        """3 个来源发现并加载插件."""
        self._plugins = []

        # 1. 用户目录: ~/.scout/plugins/
        self._load_from_dir(self.user_dir, "user_dir")

        # 2. 项目目录: .scout/plugins/
        self._load_from_dir(self.project_dir, "project_dir")

        # 3. pip entry points
        self._load_from_entry_points()

        return self._plugins

    def _load_from_dir(self, dir_path: Path, source: str) -> None:
        """从目录加载插件."""
        if not dir_path.exists():
            return

        for item in dir_path.iterdir():
            if not item.is_dir() or item.name.startswith("."):
                continue

            # 检查是否有 plugin.py 或 __init__.py
            plugin_py = item / "plugin.py"
            init_py = item / "__init__.py"

            if plugin_py.exists():
                self._load_plugin_module(item.name, str(plugin_py), source)
            elif init_py.exists():
                self._load_plugin_module(item.name, str(item), source)

    def _load_plugin_module(self, name: str, path: str, source: str) -> None:
        """加载插件模块."""
        try:
            # 添加到 sys.path
            parent_dir = str(Path(path).parent if Path(path).is_file() else Path(path))
            if parent_dir not in sys.path:
                sys.path.insert(0, parent_dir)

            module_name = f"scout_plugin_{name}"
            if Path(path).is_file():
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                else:
                    raise ImportError(f"无法加载: {path}")
            else:
                module = importlib.import_module(name)

            self._plugins.append(Plugin(
                name=name,
                source=source,
                path=path,
                module=module,
            ))
        except Exception as e:
            self._plugins.append(Plugin(
                name=name,
                source=source,
                path=path,
                error=str(e),
            ))

    def _load_from_entry_points(self) -> None:
        """从 pip entry points 加载."""
        try:
            eps = entry_points()
            # Python 3.10+ 兼容
            if hasattr(eps, "select"):
                scout_eps = eps.select(group="scout.plugins")
            else:
                scout_eps = eps.get("scout.plugins", [])

            for ep in scout_eps:
                try:
                    module = ep.load()
                    if callable(module) and not isinstance(module, type):
                        module = module()
                    self._plugins.append(Plugin(
                        name=ep.name,
                        source="pip",
                        path=f"entry_point:{ep.name}",
                        module=module,
                    ))
                except Exception as e:
                    self._plugins.append(Plugin(
                        name=ep.name,
                        source="pip",
                        path=f"entry_point:{ep.name}",
                        error=str(e),
                    ))
        except Exception:
            pass

    def list_plugins(self) -> list[dict]:
        """列出所有插件."""
        return [p.to_dict() for p in self._plugins]

    def get_plugin(self, name: str) -> Plugin | None:
        """按名称获取插件."""
        for p in self._plugins:
            if p.name == name:
                return p
        return None

    @property
    def loaded_count(self) -> int:
        return sum(1 for p in self._plugins if p.loaded)
