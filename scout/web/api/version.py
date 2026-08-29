"""版本管理 API"""
from fastapi import APIRouter, HTTPException
from pathlib import Path
import subprocess
import requests
from typing import Optional

router = APIRouter(prefix="/api/version", tags=["version"])


def get_local_version() -> str:
    """获取本地版本号"""
    version_file = Path(__file__).parent.parent.parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "unknown"


def get_git_info() -> dict:
    """获取 Git 信息"""
    try:
        # 获取当前分支
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=Path(__file__).parent.parent.parent.parent,
            stderr=subprocess.DEVNULL
        ).decode().strip()
        
        # 获取最新 commit
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent.parent.parent.parent,
            stderr=subprocess.DEVNULL
        ).decode().strip()
        
        # 获取 commit 时间
        commit_time = subprocess.check_output(
            ["git", "log", "-1", "--format=%cd", "--date=iso"],
            cwd=Path(__file__).parent.parent.parent.parent,
            stderr=subprocess.DEVNULL
        ).decode().strip()
        
        return {
            "branch": branch,
            "commit": commit,
            "commit_time": commit_time
        }
    except Exception:
        return {
            "branch": "unknown",
            "commit": "unknown",
            "commit_time": "unknown"
        }


@router.get("/info")
async def version_info():
    """获取版本信息"""
    return {
        "version": get_local_version(),
        "git": get_git_info()
    }


@router.get("/check")
async def check_update():
    """检查更新"""
    try:
        # 获取远程最新版本（假设使用 GitLab）
        # 这里需要根据实际仓库地址修改
        repo_url = "https://github.com/core-power/Scout-Agent"
        
        # 获取远程版本
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "origin"],
            cwd=Path(__file__).parent.parent.parent.parent,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            return {
                "has_update": False,
                "current_version": get_local_version(),
                "latest_version": get_local_version(),
                "message": "无法检查更新"
            }
        
        # 解析最新的 tag
        tags = []
        for line in result.stdout.strip().split('\n'):
            if line and 'refs/tags/v' in line:
                tag = line.split('refs/tags/v')[-1]
                tags.append(tag)
        
        if not tags:
            return {
                "has_update": False,
                "current_version": get_local_version(),
                "latest_version": get_local_version(),
                "message": "未找到版本标签"
            }
        
        # 简单的版本比较
        latest = sorted(tags)[-1]
        current = get_local_version()
        
        has_update = latest != current
        
        return {
            "has_update": has_update,
            "current_version": current,
            "latest_version": latest,
            "download_url": f"{repo_url}/-/archive/v{latest}/scout-agent-v{latest}.tar.gz"
        }
    except Exception as e:
        return {
            "has_update": False,
            "current_version": get_local_version(),
            "latest_version": get_local_version(),
            "message": f"检查更新失败: {str(e)}"
        }
