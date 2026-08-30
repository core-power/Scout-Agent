"""数据目录 manifest — 记录数据格式版本与程序版本，支撑升级/迁移检测.

数据目录（$SCOUT_DATA_DIR）中的 ``manifest.json`` 保存：
- ``data_format_version``: 数据格式版本（数据库 schema / 目录结构变更时 +1）
- ``app_version``: 创建该目录的程序版本
- ``created_at`` / ``updated_at``: 创建与最近写入时间

用途：
- 升级后首次启动可判断数据目录是否需要迁移；
- 避免用旧版程序读取新版数据格式导致损坏。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from scout.config.paths import DATA_DIR, MANIFEST_PATH

logger = logging.getLogger(__name__)

# 当前数据格式版本：数据库表结构 / 数据目录布局变更时 +1
DATA_FORMAT_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_manifest() -> dict:
    """读取 manifest；文件缺失或损坏返回空 dict."""
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        logger.warning("manifest.json 读取失败（可能损坏），按空处理")
        return {}


def write_manifest(overrides: dict | None = None) -> dict:
    """写入 manifest（保留既有字段），返回合并后的内容."""
    data = read_manifest()
    data.update(overrides or {})
    data.setdefault("data_format_version", DATA_FORMAT_VERSION)
    data.setdefault("updated_at", _now_iso())
    try:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        logger.exception("manifest.json 写入失败")
    return data


def ensure_manifest(app_version: str | None = None) -> dict:
    """确保 manifest 存在并写入当前数据格式版本（幂等）.

    首次创建时记录 created_at；后续调用仅更新 app_version / updated_at。
    """
    data = read_manifest()
    if not data:
        data = {
            "data_format_version": DATA_FORMAT_VERSION,
            "app_version": app_version or "",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
    else:
        data["data_format_version"] = data.get(
            "data_format_version", DATA_FORMAT_VERSION
        )
        if app_version:
            data["app_version"] = app_version
        data["updated_at"] = _now_iso()
    return write_manifest(data)


def check_data_format() -> tuple[bool, str]:
    """检查数据目录格式版本兼容性.

    返回 (兼容, 说明)。数据格式版本高于当前程序支持版本时不兼容，
    提示升级程序或人工介入。
    """
    data = read_manifest()
    if not data:
        return True, "manifest 不存在（新目录）"
    version = data.get("data_format_version", 1)
    if version > DATA_FORMAT_VERSION:
        return (
            False,
            f"数据格式版本 {version} 高于当前程序支持的 {DATA_FORMAT_VERSION}，"
            f"请升级 scout 后再启动（{MANIFEST_PATH}）",
        )
    if version < DATA_FORMAT_VERSION:
        return (
            True,
            f"数据格式版本 {version} 低于当前 {DATA_FORMAT_VERSION}，启动时将自动迁移",
        )
    return True, f"数据格式版本 {version} 兼容"


def get_data_format_version() -> int:
    """返回 manifest 中记录的数据格式版本（无记录时视为 1）."""
    return int(read_manifest().get("data_format_version", 1) or 1)


if __name__ == "__main__":  # 供调试
    print(check_data_format())
    print(os.environ.get("SCOUT_DATA_DIR", DATA_DIR))
