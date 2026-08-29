"""文件监听管理 API 路由 — 配置目录监听、查看状态、启动/停止."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watcher", tags=["watcher"])


def _get_watcher():
    from scout.automation.watcher import get_watcher
    return get_watcher()


@router.get("/targets")
async def list_targets():
    """列出所有监听目录配置."""
    try:
        return {"targets": _get_watcher().list_targets()}
    except Exception as e:
        logger.warning(f"获取监听目录失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/targets")
async def add_target(req: Request):
    """添加监听目录."""
    try:
        from scout.automation.watcher import WatchTarget
        body = await req.json()
        target = WatchTarget.from_dict(body)
        if not target.path:
            return JSONResponse({"error": "path 不能为空"}, status_code=400)
        _get_watcher().add_target(target)
        return {"status": "ok", "target": target.to_dict()}
    except Exception as e:
        logger.warning(f"添加监听目录失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/targets")
async def remove_target(req: Request):
    """删除监听目录."""
    try:
        body = await req.json()
        path = body.get("path", "")
        if _get_watcher().remove_target(path):
            return {"status": "ok", "path": path}
        return JSONResponse({"error": "监听目录不存在"}, status_code=404)
    except Exception as e:
        logger.warning(f"删除监听目录失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/start")
async def start_watcher():
    """启动所有监听."""
    try:
        await _get_watcher().start()
        return {"status": "ok", "running": True}
    except Exception as e:
        logger.warning(f"启动监听失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/stop")
async def stop_watcher():
    """停止所有监听."""
    try:
        await _get_watcher().stop()
        return {"status": "ok", "running": False}
    except Exception as e:
        logger.warning(f"停止监听失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
