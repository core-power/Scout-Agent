"""统一路径解析 — 用户配置与运行时数据目录.

设计原则 (2026-08-31):
- Windows 桌面版默认使用 %APPDATA%\\Scout（始终可写、更新绿色版不会清空、
  升级/覆盖安装后配置仍在），彻底解决"每次更新都要重填 API Key/配置"问题。
- 旧版本曾把数据放在盘符根目录 <盘符>\\.scout（如 D:\\.scout）——该位置
  存在权限问题（盘符根目录可能不可写 → 回退到 exe 旁 data/，更新即丢），
  现仅作为一次性迁移来源，不再作为新默认位置。
- 其他平台默认保存在项目根目录下的 .scout/（避免写入 / 根目录）。
- 环境变量覆盖（桌面版 launcher 启动时强制设置，保持与 data_dir() 一致）：
    SCOUT_CONFIG_DIR — 配置文件目录
    SCOUT_DATA_DIR   — 运行时数据目录（默认同配置文件目录）
  覆盖后配置文件与数据目录可分离（如 Docker volume 挂载）。
"""

import os
from pathlib import Path

# 项目根目录 — 基于本文件位置推导:
# scout/config/paths.py -> config -> scout -> <项目根>
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _default_root() -> Path:
    """默认数据根目录（2026-08-31 起）:
    - Windows: %APPDATA%\\Scout（始终可写、不随程序更新丢失）;
      无 APPDATA 时回退 <盘符>\\.scout 兼容旧版。
    - 其他平台: 项目根目录下的 .scout（避免写入 / 根目录）
    """
    if os.name == "nt":
        try:
            base = os.getenv("APPDATA") or str(Path.home())
            if base:
                return Path(base) / "Scout"
        except Exception:  # noqa: BLE001
            pass
        anchor = PROJECT_ROOT.anchor  # 如 "D:\\"
        if anchor:
            return Path(anchor) / ".scout"
    return PROJECT_ROOT / ".scout"


def get_config_dir() -> Path:
    """用户配置文件目录 — 默认 <盘符>:\\.scout，可用 SCOUT_CONFIG_DIR 覆盖."""
    env_dir = os.getenv("SCOUT_CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return _default_root()


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

# 数据目录版本标识 — 记录数据格式版本与程序版本，供升级/迁移检测
MANIFEST_PATH = DATA_DIR / "manifest.json"
