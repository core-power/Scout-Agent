"""文件系统事件监听 — 主动感知能力（标准库实现，无第三方依赖）.

场景：让 Agent 感知工作目录/数据目录中的文件变化（新增/修改/删除），
从而触发自动化任务。例如：
- 有新的上传文件落到 data/ 目录 → 触发处理
- 配置文件被修改 → 触发重新加载
- 日志出现新错误 → 触发告警

实现：
- 基于 asyncio + 轮询，记录目录内文件的 mtime/size/存在性 快照
- 变化时通过 EventBus 广播 "fs.event" 事件（载荷含 event_type / path / size）
- 配合 TriggerManager 的 event 触发器（event_name="fs.event"）即可实现
  "文件变化 → Agent 执行" 的自动化闭环
- 去抖：同一文件在 debounce 窗口内只发一次事件

配置持久化在 ~/.scout/watchers.json，可管理多个监听目录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("scout.automation.watcher")

_CONFIG_PATH = Path.home() / ".scout" / "watchers.json"


@dataclass
class WatchTarget:
    """单个监听目录配置."""
    path: str = ""
    patterns: list = field(default_factory=list)   # glob 模式，空 = 全部
    recursive: bool = True
    emit_create: bool = True
    emit_modify: bool = True
    emit_delete: bool = True
    debounce_seconds: float = 1.0
    poll_interval: float = 2.0
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "WatchTarget":
        return cls(
            path=d.get("path", ""),
            patterns=d.get("patterns", []),
            recursive=bool(d.get("recursive", True)),
            emit_create=bool(d.get("emit_create", True)),
            emit_modify=bool(d.get("emit_modify", True)),
            emit_delete=bool(d.get("emit_delete", True)),
            debounce_seconds=float(d.get("debounce_seconds", 1.0)),
            poll_interval=float(d.get("poll_interval", 2.0)),
            enabled=bool(d.get("enabled", True)),
        )


def _matches(path: Path, patterns: list[str]) -> bool:
    if not patterns:
        return True
    return any(path.match(p) or path.name == p for p in patterns)


class FileWatcher:
    """文件系统监听器 — 轮询目录并广播 fs.event 事件."""

    def __init__(
        self,
        bus: Any = None,
        config_path: str | Path | None = None,
    ):
        self.bus = bus
        self._config_path = Path(config_path) if config_path else _CONFIG_PATH
        self._targets: list[WatchTarget] = []
        self._snapshots: dict[str, dict[str, dict]] = {}   # path -> {rel: {mtime,size}}
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._last_emit: dict[str, float] = {}
        self._load()

    # ── 持久化 ──

    def _load(self) -> None:
        try:
            if self._config_path.exists():
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                self._targets = [WatchTarget.from_dict(t) for t in data]
        except Exception as e:
            logger.warning(f"watchers.json 加载失败: {e}")

    def save(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            self._config_path.write_text(
                json.dumps([t.to_dict() for t in self._targets], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"watchers.json 保存失败: {e}")

    # ── 配置管理 ──

    def add_target(self, target: WatchTarget) -> WatchTarget:
        self._targets = [t for t in self._targets if t.path != target.path]
        self._targets.append(target)
        self.save()
        if self._running and target.enabled:
            self._restart()
        return target

    def remove_target(self, path: str) -> bool:
        before = len(self._targets)
        self._targets = [t for t in self._targets if t.path != path]
        if len(self._targets) != before:
            self.save()
            self._restart()
            return True
        return False

    def list_targets(self) -> list[dict]:
        return [t.to_dict() for t in self._targets]

    # ── 生命周期 ──

    async def start(self) -> None:
        """启动所有启用目录的监听任务."""
        if self._running:
            return
        self._running = True
        for target in self._targets:
            if not target.enabled:
                continue
            self._tasks.append(asyncio.create_task(self._watch_loop(target)))
            logger.info(f"文件监听启动: {target.path}")
        # 没有任务时也要保持 running 状态为 True，以便热更新
        if not self._tasks:
            self._running = True

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _restart(self) -> None:
        """配置变更后重启监听（仅在 running 时有效）."""
        if not self._running:
            return
        self._tasks.clear()
        for target in self._targets:
            if not target.enabled:
                continue
            self._tasks.append(asyncio.create_task(self._watch_loop(target)))

    # ── 监听循环 ──

    def _snapshot_dir(self, target: WatchTarget) -> dict[str, dict]:
        """扫描目录生成快照 {relpath: {mtime, size}}."""
        snap: dict[str, dict] = {}
        root = Path(target.path)
        if not root.exists():
            return snap
        try:
            if target.recursive:
                for p in root.rglob("*"):
                    if p.is_file() and _matches(p, target.patterns):
                        try:
                            st = p.stat()
                            rel = str(p.relative_to(root))
                            snap[rel] = {"mtime": st.st_mtime, "size": st.st_size}
                        except OSError:
                            continue
            else:
                for p in root.iterdir():
                    if p.is_file() and _matches(p, target.patterns):
                        try:
                            st = p.stat()
                            snap[p.name] = {"mtime": st.st_mtime, "size": st.st_size}
                        except OSError:
                            continue
        except Exception as e:
            logger.warning(f"扫描目录异常 {target.path}: {e}")
        return snap

    async def _watch_loop(self, target: WatchTarget) -> None:
        root = target.path
        self._snapshots[root] = self._snapshot_dir(target)

        while self._running:
            await asyncio.sleep(target.poll_interval)
            current = self._snapshot_dir(target)
            previous = self._snapshots.get(root, {})

            # 新增
            if target.emit_create:
                for rel in set(current) - set(previous):
                    self._emit(target, "created", root, rel, current[rel])
            # 修改
            if target.emit_modify:
                for rel in set(current) & set(previous):
                    cur, old = current[rel], previous[rel]
                    if cur["mtime"] > old["mtime"] or cur["size"] != old["size"]:
                        self._emit(target, "modified", root, rel, cur)
            # 删除
            if target.emit_delete:
                for rel in set(previous) - set(current):
                    self._emit(target, "deleted", root, rel, previous[rel])

            self._snapshots[root] = current

    def _emit(self, target: WatchTarget, event_type: str, root: str, rel: str, meta: dict) -> None:
        """去抖并广播 fs.event 事件."""
        key = f"{event_type}:{root}:{rel}"
        now = time.time()
        if now - self._last_emit.get(key, 0) < target.debounce_seconds:
            return
        self._last_emit[key] = now

        if not self.bus:
            logger.debug(f"[fs.event] {event_type} {root}/{rel}")
            return
        try:
            payload = {
                "event_type": event_type,   # created / modified / deleted
                "path": str(Path(root) / rel),
                "root": root,
                "relative": rel,
                "size": meta.get("size", 0),
                "mtime": meta.get("mtime", 0),
            }
            # 同步 fire-and-forget，避免阻塞监听循环
            asyncio.ensure_future(self.bus.emit("fs.event", payload))
            logger.info(f"[fs.event] {event_type}: {root}/{rel}")
        except Exception as e:
            logger.warning(f"fs.event 广播失败: {e}")


# ── 全局单例 ──

_watcher: FileWatcher | None = None


def get_watcher(bus: Any = None) -> FileWatcher:
    global _watcher
    if _watcher is None:
        _watcher = FileWatcher(bus=bus)
    if bus is not None:
        _watcher.bus = bus
    return _watcher


def reset_watcher():
    global _watcher
    _watcher = None
