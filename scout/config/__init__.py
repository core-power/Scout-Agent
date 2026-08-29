"""Scout Agent 配置模块.

对外导出两套配置:
- ConfigManager / LLMConfig (pydantic): 运行时可修改的 Web 配置，
  持久化在 ~/.scout/config.json（原 scout/config.py，2026-08-03 从字节码还原）。
- ScoutConfig / get_config (settings.py): 基于环境变量的核心配置。

注意: 两套配置里各有一个 LLMConfig。包级别导出的是 pydantic 版
(manager.py)，与 web.py / gateway.py 的用法保持一致；
settings 版请通过 scout.config.settings 显式导入。
"""

from scout.config.manager import ConfigManager, LLMConfig
from scout.config.settings import ScoutConfig, get_config, reset_config

__all__ = [
    "ConfigManager",
    "LLMConfig",
    "ScoutConfig",
    "get_config",
    "reset_config",
]
