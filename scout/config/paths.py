"""统一路径解析 — 用户配置与运行时数据目录.

设计原则 (2026-08-20):
- 所有用户产生的配置（config.json / credentials.json / jwt_secret / secret_key /
  automation_policy.json）与运行时数据（数据库、日志、技能、向量库等）
  默认统一保存在【项目根目录下的 ./.scout/】，随项目迁移：
  用户 git clone 后无需任何额外初始化，首次启动自动生成配置与数据目录。
- 环境变量覆盖：
    SCOUT_CONFIG_DIR — 配置文件目录（默认 <项目根>/.scout）
    SCOUT_DATA_DIR   — 运行时数据目录（默认同配置文件目录）
  覆盖后配置文件与数据目录可分离（如 Docker volume 挂载）。
"""

import os
from pathlib import Path

# 项目根目录 — 基于本文件位置推导:
# scout/config/paths.py -> config -> scout -> <项目根>
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def get_config_dir() -> Path:
    """用户配置文件目录 — 默认 <项目根>/.scout，可用 SCOUT_CONFIG_DIR 覆盖."""
    env_dir = os.getenv("SCOUT_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return PROJECT_ROOT / ".scout"


def get_data_dir() -> Path:
    """运行时数据目录 — 默认与配置文件目录一致，可用 SCOUT_DATA_DIR 覆盖."""
    env_dir = os.getenv("SCOUT_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return get_config_dir()


# 配置目录（模块加载时解析，便于各模块直接引用；测试可通过环境变量覆盖后重新加载）
CONFIG_DIR = get_config_dir()
DATA_DIR = get_data_dir()

# 各配置/密钥文件的路径
CONFIG_PATH = CONFIG_DIR / "config.json"
CREDENTIALS_PATH = CONFIG_DIR / "credentials.json"
JWT_SECRET_PATH = CONFIG_DIR / "jwt_secret"
SECRET_KEY_PATH = CONFIG_DIR / "secret_key"
AUTOMATION_POLICY_PATH = CONFIG_DIR / "automation_policy.json"
