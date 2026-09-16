"""通知推送系统 — 跨渠道主动通知闭环.

监听 EventBus 的 notification 事件，根据用户通知偏好，将通知主动推送到
IM 渠道（Telegram/飞书/钉钉等）与邮件（SMTP），并记录推送历史。

关键能力：
- 跨渠道推送：notification 事件 → IM 渠道 / 邮件
- 通知偏好：按类型控制发送渠道、开关、去重防打扰
- 推送历史：持久化留痕，便于审计与排障
- 去重/节流：同类通知在窗口期内不重复推送

用法：
    from scout.notify import emit_notification
    await emit_notification("reminder", "喝水提醒", "该喝水啦", level="info")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("scout.notify")


async def emit_notification(
    notif_type: str = "default",
    title: str = "通知",
    message: str = "",
    level: str = "info",
    **extra: Any,
) -> None:
    """便捷入口 — 向 EventBus 广播一条 notification 事件.

    通知分发器（NotifyDispatcher）收到后会根据偏好推送到 IM/邮件渠道，
    WebSocket 也会实时广播到前端。

    Args:
        notif_type: 通知类型（task_alert / task_complete / cron_triggered / system / reminder / default）
        title: 标题
        message: 内容
        level: 级别（critical / warning / info / debug）
        extra: 额外字段（如 run_id、trigger_id 等，随事件广播）
    """
    try:
        from scout.bus.hub import bus
    except Exception as e:
        logger.warning(f"无法获取 EventBus: {e}")
        return

    import time
    payload = {
        "type": notif_type,
        "title": title,
        "message": message,
        "level": level,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    payload.update(extra)
    try:
        await bus.emit("notification", payload)
    except Exception as e:
        logger.warning(f"通知广播失败: {e}")


def get_dispatcher(channel_manager: Any = None):
    """获取全局通知分发器实例."""
    from scout.notify.dispatcher import get_dispatcher as _gd
    return _gd(channel_manager)


__all__ = ["emit_notification", "get_dispatcher"]
