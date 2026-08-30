"""版本管理 API"""
from fastapi import APIRouter
from pathlib import Path
import subprocess
import json
import os
import re
import urllib.request
from typing import Optional

router = APIRouter(prefix="/api/version", tags=["version"])

# 官方仓库（检查更新用）
REPO = "core-power/scout-agent"
RELEASES_URL = f"https://github.com/{REPO}/releases"


def get_local_version() -> str:
    """获取本地版本号：优先 VERSION 文件（源码仓库 / 打包后 _internal/VERSION）"""
    version_file = Path(__file__).parent.parent.parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    try:
        from scout import __version__

        return __version__
    except Exception:
        return "unknown"


def get_git_info() -> dict:
    """获取 Git 信息（桌面版无 git 时返回 unknown）"""
    try:
        base = Path(__file__).parent.parent.parent.parent
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=base,
            stderr=subprocess.DEVNULL,
        ).decode().strip()

        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=base,
            stderr=subprocess.DEVNULL,
        ).decode().strip()

        commit_time = subprocess.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=iso"],
            cwd=base,
            stderr=subprocess.DEVNULL,
        ).decode().strip()

        return {
            "branch": branch,
            "commit": commit,
            "commit_time": commit_time,
        }
    except Exception:
        return {
            "branch": "unknown",
            "commit": "unknown",
            "commit_time": "unknown",
        }


def _is_desktop() -> bool:
    """是否桌面绿色版（launcher 注入 SCOUT_DESKTOP=1）"""
    return os.environ.get("SCOUT_DESKTOP") == "1"


def _parse_version(version: str) -> tuple:
    """把 'v1.0.0.0' / '1.0.0' 解析成可比较的数字元组 (1,0,0,0)"""
    return tuple(int(x) for x in re.findall(r"\d+", version or ""))


def _fetch_latest_release() -> Optional[dict]:
    """从 GitHub Releases API 拉取最新发布信息（3 秒超时）"""
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Scout-Agent/1.0.0",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


@router.get("/info")
async def version_info():
    """获取版本信息"""
    return {
        "version": get_local_version(),
        "git": get_git_info(),
    }


@router.get("/check")
async def check_update():
    """检查更新（GitHub Releases）"""
    current = get_local_version()
    try:
        data = _fetch_latest_release()
    except Exception as e:
        return {
            "update_available": False,
            "current_version": current,
            "latest_version": current,
            "html_url": RELEASES_URL,
            "download_url": "",
            "desktop": _is_desktop(),
            "message": f"检查更新失败: {e}",
        }

    latest_tag = (data.get("tag_name") or "").lstrip("v") or current
    update_available = (
        _parse_version(latest_tag) > _parse_version(current)
        if latest_tag != current
        else False
    )

    download_url = ""
    for asset in data.get("assets", []):
        if "win-x64" in asset.get("name", ""):
            download_url = asset.get("browser_download_url", "")
            break

    return {
        "update_available": update_available,
        "current_version": current,
        "latest_version": latest_tag,
        "html_url": data.get("html_url", RELEASES_URL),
        "download_url": download_url,
        "desktop": _is_desktop(),
        "message": "ok",
    }
