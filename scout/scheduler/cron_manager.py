"""Cron 调度管理器 — 管理周期性任务."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
# croniter 类名兼容：5.x 及以前为小写 croniter，6.x 起改名为大写 Croniter。
# 本工程锁定 croniter<6，因此走小写分支；保留 fallback 以兼容未来升级。
try:
    from croniter import croniter as Croniter  # croniter<6（class croniter）
except ImportError:  # pragma: no cover - croniter>=6（class Croniter）
    from croniter import Croniter  # type: ignore[no-redef]


@dataclass
class CronJob:
    """一个定时任务描述对象."""

    name: str
    cron: str
    callback: Callable[[], Any]
    last_run: datetime | None = field(default=None, init=False)


class CronManager:
    """基于 Croniter 的轻量级调度器.

    负责解析 Cron 表达式并触发定时任务。
    """

    def __init__(self):
        self.tasks = {}
        self._running = False

    async def add_task(self, name: str, cron_expr: str, callback):
        """添加一个定时任务."""
        if name in self.tasks:
            raise ValueError(f"任务 {name} 已存在")
        
        self.tasks[name] = {
            "cron": cron_expr,
            "callback": callback,
            "last_run": None,
        }

    async def start(self):
        """启动调度循环."""
        self._running = True
        while self._running:
            now = datetime.now()
            for name, task in self.tasks.items():
                try:
                    cron = Croniter(task["cron"], now)
                    if cron.is_trigger(now):
                        await task["callback"]()
                        task["last_run"] = now
                except Exception as e:
                    print(f"[Scheduler] Error in task {name}: {e}")
            
            # 每秒检查一次（生产环境可优化为更高效的睡眠逻辑）
            await asyncio.sleep(1)

    def stop(self):
        """停止调度器."""
        self._running = False
