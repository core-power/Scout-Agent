"""定时任务 — 借鉴 OpenClaw 的一等公民 Cron 设计.

Cron 任务是 Agent 任务（不是 shell 任务），可以让 Agent 定时执行。

优化 (2026-08-05):
- 任务触发时通过 EventBus 广播 notification 事件，实现前端实时推送
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, time as dtime
from typing import Any, Callable, Coroutine


class CronTask:
    """定时任务定义."""

    def __init__(
        self,
        name: str,
        schedule: str,
        task: str,
        agent_id: str = "default",
    ):
        self.name = name
        self.schedule = schedule  # cron 表达式或自然语言
        self.task = task  # 任务描述（给 Agent 执行）
        self.agent_id = agent_id
        self.last_run: datetime | None = None
        self.next_run: datetime | None = None
        self.enabled = True

    def parse_interval(self) -> int | None:
        """解析简单间隔表达式，返回秒数."""
        # "每X分钟" / "每X小时" / "每X秒"
        m = re.match(r"每(\d+)?\s*(秒|分|小时|天)", self.schedule)
        if not m:
            return None
        num = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2)
        if unit == "秒":
            return num
        elif unit == "分":
            return num * 60
        elif unit == "小时":
            return num * 3600
        elif unit == "天":
            return num * 86400
        return None

    def parse_daily_time(self) -> dtime | None:
        """解析每日定点表达式，返回 time 对象.

        支持格式（2026-08-13）:
        - "每天09:00" / "每天 9:30" / "每日21:15"
        - "09:00" (裸时间也按每日定点)
        """
        m = re.match(r"^(?:每天|每日)?\s*(\d{1,2}):(\d{2})$", self.schedule.strip())
        if not m:
            return None
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return None
        return dtime(hour=hour, minute=minute)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "schedule": self.schedule,
            "task": self.task,
            "agent_id": self.agent_id,
            "enabled": self.enabled,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
        }


class CronManager:
    """定时任务管理器."""

    def __init__(self):
        self._tasks: dict[str, CronTask] = {}
        self._runner_task: asyncio.Task | None = None
        self._agent_callback: Callable[..., Coroutine] | None = None
        self._bus = None  # EventBus 引用

    def set_agent_callback(self, callback: Callable[..., Coroutine]):
        """设置 Agent 执行回调 — 当 Cron 任务触发时调用."""
        self._agent_callback = callback

    def set_bus(self, bus):
        """设置 EventBus 引用 — 用于广播通知事件."""
        self._bus = bus

    def add(self, task: CronTask) -> None:
        """添加定时任务."""
        interval = task.parse_interval()
        if interval:
            task.next_run = datetime.now() + timedelta(seconds=interval)
        elif task.parse_daily_time():
            task.next_run = self._next_daily_run(task.parse_daily_time())
        self._tasks[task.name] = task

    @staticmethod
    def _next_daily_run(t: dtime, after: datetime | None = None) -> datetime:
        """计算每日定点任务的下次运行时间."""
        base = after or datetime.now()
        candidate = base.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    def remove(self, name: str) -> None:
        """删除定时任务."""
        self._tasks.pop(name, None)

    def list_tasks(self) -> list[CronTask]:
        """列出所有任务."""
        return list(self._tasks.values())

    def get_task(self, name: str) -> CronTask | None:
        """获取任务."""
        return self._tasks.get(name)

    def enable(self, name: str) -> None:
        if name in self._tasks:
            self._tasks[name].enabled = True

    def disable(self, name: str) -> None:
        if name in self._tasks:
            self._tasks[name].enabled = False

    async def start(self) -> None:
        """启动 Cron 调度器."""
        self._runner_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """停止调度器."""
        if self._runner_task:
            self._runner_task.cancel()
            self._runner_task = None

    async def _run_loop(self) -> None:
        """调度循环 — 每秒检查."""
        # 延迟导入避免循环依赖
        from scout.bus.hub import bus

        while True:
            now = datetime.now()
            for task in self._tasks.values():
                if not task.enabled:
                    continue
                if task.next_run and now >= task.next_run:
                    # 触发任务
                    if self._agent_callback:
                        try:
                            await self._agent_callback(task)
                        except Exception:
                            pass
                    
                    # 更新运行时间
                    task.last_run = now
                    interval = task.parse_interval()
                    if interval:
                        task.next_run = now + timedelta(seconds=interval)
                    elif task.parse_daily_time():
                        task.next_run = self._next_daily_run(task.parse_daily_time(), now)
                    
                    # ★ 断裂点修复 1: 广播通知事件到 EventBus
                    # 使用全局 bus 或注入的 bus
                    event_bus = self._bus or bus
                    await event_bus.emit("notification", {
                        "type": "cron_triggered",
                        "title": f"定时任务触发: {task.name}",
                        "message": task.task,
                        "task_name": task.name,
                        "timestamp": now.isoformat(),
                    })
                    
            await asyncio.sleep(1)
