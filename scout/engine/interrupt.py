"""可中断执行器 — 借鉴 Hermes 的可中断设计.

API 调用和工具执行可被用户中途取消。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class InterruptibleExecutor:
    """可中断的异步执行器."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancelled: set[str] = set()

    async def execute(
        self,
        task_id: str,
        coro: Coroutine[Any, Any, T],
        timeout: float | None = None,
    ) -> T:
        """执行一个可中断的协程.

        Args:
            task_id: 任务唯一标识
            coro: 要执行的协程
            timeout: 超时时间（秒）

        Returns:
            协程返回值

        Raises:
            asyncio.CancelledError: 被用户取消
            asyncio.TimeoutError: 超时
        """
        task = asyncio.create_task(coro)
        self._tasks[task_id] = task

        try:
            if timeout:
                return await asyncio.wait_for(task, timeout=timeout)
            return await task
        except asyncio.CancelledError:
            self._cancelled.add(task_id)
            raise
        finally:
            self._tasks.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        """取消指定任务."""
        task = self._tasks.get(task_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def cancel_all(self) -> int:
        """取消所有正在执行的任务."""
        count = 0
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
                count += 1
        return count

    @property
    def active_tasks(self) -> list[str]:
        """当前活跃的任务 ID 列表."""
        return [tid for tid, t in self._tasks.items() if not t.done()]
