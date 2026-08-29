"""模型监控 API — 提供 token 用量统计."""

from fastapi import APIRouter, Query

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("/summary")
async def get_summary(period: str = Query("day", pattern="^(day|week|month|year)$")):
    """获取指定时间范围的统计摘要."""
    from scout.llm.tracker import token_tracker
    summary = token_tracker.get_summary(period)
    return summary


@router.get("/daily")
async def get_daily(days: int = Query(30, ge=1, le=365)):
    """获取每日 token 消耗趋势."""
    from scout.llm.tracker import token_tracker
    data = token_tracker.get_daily(days)
    return {"data": data}


@router.get("/recent")
async def get_recent(limit: int = Query(20, ge=1, le=100)):
    """获取最近的调用记录."""
    from scout.llm.tracker import token_tracker
    data = token_tracker.get_recent(limit)
    return {"data": data}
