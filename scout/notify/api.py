"""通知管理 API 路由 — 偏好配置 / 历史查看 / 手动测试推送."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notify", tags=["notify"])


def _get_dispatcher():
    from scout.notify.dispatcher import get_dispatcher
    return get_dispatcher()


@router.get("/prefs")
async def get_prefs():
    """获取通知偏好配置."""
    try:
        return {"prefs": _get_dispatcher().get_prefs()}
    except Exception as e:
        logger.warning(f"获取通知偏好失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/prefs")
async def update_prefs(req: Request):
    """更新通知偏好配置."""
    try:
        body = await req.json()
        prefs = _get_dispatcher().update_prefs(body)
        return {"status": "ok", "prefs": prefs}
    except Exception as e:
        logger.warning(f"更新通知偏好失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/history")
async def get_history(limit: int = 50):
    """查看推送历史."""
    limit = max(1, min(limit, 500))
    try:
        return {"history": _get_dispatcher().get_history(limit)}
    except Exception as e:
        logger.warning(f"获取通知历史失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.delete("/history")
async def clear_history():
    """清空推送历史."""
    try:
        n = _get_dispatcher().clear_history()
        return {"status": "ok", "cleared": n}
    except Exception as e:
        logger.warning(f"清除通知历史失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/test")
async def test_push():
    """发送一条测试通知，验证渠道配置."""
    try:
        result = await _get_dispatcher().test_push()
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.warning(f"测试通知失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
