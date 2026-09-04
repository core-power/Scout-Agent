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
    """默认数据根目录（2026-09-04 按用户要求改回盘符根 .scout）:
    - Windows: <项目/exe 所在盘符>\\.scout（如 D:\\.scout）——用户指定的工作目录；
      盘符根不可写时回退 %APPDATA%\\Scout（始终可写）。
    - 其他平台: 项目根目录下的 .scout（避免写入 / 根目录）
    """
    if os.name == "nt":
        anchor = PROJECT_ROOT.anchor  # 如 "D:\\"
        if anchor:
            try:
                d = Path(anchor) / ".scout"
                d.mkdir(parents=True, exist_ok=True)
                probe = d / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                return d
            except Exception:  # noqa: BLE001 — 盘符根不可写（无权限/只读盘）
                pass
        try:
            base = os.getenv("APPDATA") or str(Path.home())
            if base:
                return Path(base) / "Scout"
        except Exception:  # noqa: BLE001
            pass
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

# Agent 产物目录（2026-09-04）：Agent 生成的文件/中间产物统一收纳于此，
# 系统提示词引导 LLM 默认写这里（用户明确指定路径时除外）。
# 桌面版 = %APPDATA%\Scout\outputs；源码版 = <项目根>/.scout/outputs。
# 不放项目源码 scout/ 包内：打包后为 _internal 只读且更新即清空（见文件头设计原则）。
OUTPUTS_DIR = DATA_DIR / "outputs"


def legacy_data_dirs() -> list[Path]:
    """历史版本可能使用过的数据/配置目录（按优先级排序）.

    用途 (2026-09-01 修复"更新后 API Key/配置丢失"):
    - 旧版本把数据写在 <盘符>:\\.scout、exe 旁 data/、~/.scout 等位置;
      新版本改用 %APPDATA%\\Scout 后,若迁移不完整会导致旧密文与新密钥
      不配对 → 解密失败 → 用户被迫重新填写 API Key。
    - 本函数列出候选旧目录,供 secret/manager 在解密失败或配置为空时
      自动从旧目录恢复密钥与配置（自愈），与 launcher._migrate_old_data
      的候选清单保持一致。

    注意: 打包(frozen)时 PROJECT_ROOT 指向 onedir 的 _internal,
    程序根目录实际是其父目录。
    """
    import sys

    dirs: list[Path] = []
    try:
        if getattr(sys, "frozen", False):
            app_root = PROJECT_ROOT.parent  # 打包: onedir 根（含 exe）
        else:
            app_root = PROJECT_ROOT  # 源码: 项目根
        try:
            anchor = app_root.resolve().anchor
        except OSError:
            anchor = None
        if anchor:
            dirs.append(Path(anchor) / ".scout")  # 旧版默认: 程序所在盘符根
        dirs.append(app_root / "data")  # 更早版本: exe 旁 data/
        dirs.append(app_root / ".scout")
    except Exception:  # noqa: BLE001
        pass
    dirs.append(Path.home() / ".scout")

    # 去重并排除当前数据目录自身
    out: list[Path] = []
    seen: set[Path] = set()
    try:
        current = DATA_DIR.resolve()
    except OSError:
        current = DATA_DIR
    for d in dirs:
        try:
            rp = d.resolve()
        except OSError:
            continue
        if rp in seen or rp == current:
            continue
        seen.add(rp)
        out.append(rp)
    return out
