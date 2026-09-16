"""热重载 — 借鉴 OpenClaw 的 hybrid 模式.

安全变更热应用（工具/技能/记忆），需重启的变更自动重启。
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Callable

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR


class HotReloader:
    """配置热重载器 — 监听文件变化."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        poll_interval: float = 2.0,
    ):
        self.config_path = Path(
            config_path
            if config_path is not None
            else str(_SCOUT_DATA_DIR / "config.json")
        ).expanduser()
        self.poll_interval = poll_interval
        self._last_mtime: float = 0
        self._last_content: str = ""
        self._running = False
        self._task: asyncio.Task | None = None

        # 变更回调
        self._hot_callbacks: list[Callable] = []  # 热应用回调
        self._reload_callbacks: list[Callable] = []  # 需重启回调

        # 安全变更字段（热应用）
        self._hot_fields = {
            "max_turns", "temperature", "system_prompt",
            "auto_approve", "allow_tools", "deny_tools",
            "deep_thinking", "agent_mode",
        }

        # 需重启变更字段
        self._reload_fields = {
            "provider", "model", "base_url", "api_key",
        }

    def on_hot_change(self, callback: Callable):
        """注册热应用回调 — 配置变更不重启."""
        self._hot_callbacks.append(callback)

    def on_reload_change(self, callback: Callable):
        """注册重启回调 — 配置变更需要重启."""
        self._reload_callbacks.append(callback)

    async def start(self):
        """启动文件监听."""
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self):
        """停止监听."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _watch_loop(self):
        """轮询监听文件变化."""
        while self._running:
            try:
                if self.config_path.exists():
                    mtime = self.config_path.stat().st_mtime
                    if mtime > self._last_mtime:
                        await self._check_changes()
                        self._last_mtime = mtime
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval)

    async def _check_changes(self):
        """检查配置变更并触发回调."""
        try:
            content = self.config_path.read_text()
        except Exception:
            return

        if content == self._last_content:
            return

        import json
        try:
            old = json.loads(self._last_content) if self._last_content else {}
            new = json.loads(content)
        except json.JSONDecodeError:
            return

        self._last_content = content

        # 找出变更字段
        changed_fields = set()
        for key in set(list(old.keys()) + list(new.keys())):
            if old.get(key) != new.get(key):
                changed_fields.add(key)

        if not changed_fields:
            return

        # 分类变更
        hot_changes = changed_fields & self._hot_fields
        reload_changes = changed_fields & self._reload_fields

        # 触发热应用回调
        if hot_changes:
            for cb in self._hot_callbacks:
                try:
                    result = cb({f: new.get(f) for f in hot_changes})
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

        # 触发重启回调
        if reload_changes:
            for cb in self._reload_callbacks:
                try:
                    result = cb({f: new.get(f) for f in reload_changes})
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
