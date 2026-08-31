"""资源管理 — 确保资源正确释放，防止泄漏.

管理:
- 子进程（Shell、Browser、MCP）
- 文件句柄
- 网络连接
- 临时文件
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import tempfile
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator


class ResourceManager:
    """资源管理器 — 跟踪和清理所有资源."""

    def __init__(self):
        self._processes: weakref.WeakSet[asyncio.subprocess.Process] = weakref.WeakSet()
        self._temp_files: set[Path] = set()
        self._cleanup_registered = False
        
        # 注册退出清理
        if not self._cleanup_registered:
            atexit.register(self._sync_cleanup)
            self._cleanup_registered = True

    def track_process(self, process: asyncio.subprocess.Process):
        """跟踪子进程."""
        self._processes.add(process)

    def untrack_process(self, process: asyncio.subprocess.Process):
        """取消跟踪子进程."""
        self._processes.discard(process)

    def create_temp_file(self, suffix: str = "", prefix: str = "scout_") -> Path:
        """创建临时文件并跟踪."""
        fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
        os.close(fd)
        temp_path = Path(path)
        self._temp_files.add(temp_path)
        return temp_path

    def create_temp_dir(self, suffix: str = "", prefix: str = "scout_") -> Path:
        """创建临时目录并跟踪."""
        path = Path(tempfile.mkdtemp(suffix=suffix, prefix=prefix))
        self._temp_files.add(path)
        return path

    async def cleanup_process(self, process: asyncio.subprocess.Process, timeout: float = 5.0):
        """安全清理子进程."""
        if process.returncode is not None:
            # 进程已结束
            self.untrack_process(process)
            return
        
        # 尝试优雅终止
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # 强制终止
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                pass
        
        self.untrack_process(process)

    async def cleanup_all_processes(self, timeout: float = 10.0):
        """清理所有跟踪的进程."""
        processes = list(self._processes)
        if not processes:
            return
        
        # 并发清理
        tasks = [self.cleanup_process(p, timeout=timeout / len(processes)) for p in processes]
        await asyncio.gather(*tasks, return_exceptions=True)

    def cleanup_temp_files(self):
        """清理临时文件."""
        for path in list(self._temp_files):
            try:
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    import shutil
                    shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass
            self._temp_files.discard(path)

    async def cleanup_all(self):
        """清理所有资源."""
        await self.cleanup_all_processes()
        self.cleanup_temp_files()

    def _sync_cleanup(self):
        """同步清理（atexit 回调）."""
        # 清理临时文件
        self.cleanup_temp_files()
        
        # 尝试终止进程（同步方式）
        for process in list(self._processes):
            try:
                if process.returncode is None:
                    process.terminate()
                    try:
                        process._transport.close()  # type: ignore
                    except Exception:
                        pass
            except Exception:
                pass


# 全局资源管理器
_resource_manager = ResourceManager()


def get_resource_manager() -> ResourceManager:
    """获取全局资源管理器."""
    return _resource_manager


def no_window_kwargs() -> dict:
    """Windows 下隐藏子进程控制台窗口的 kwargs.

    GUI 程序（console=False 打包的 exe 无控制台）用 subprocess 启动控制台
    子进程（cmd.exe / git / python / ruff）时，若不指定 CREATE_NO_WINDOW，
    Windows 会为每个子进程新建一个可见的黑色 cmd 窗口。此函数返回
    需要追加到 create_subprocess_exec / subprocess.run 的 kwargs。
    """
    if os.name == "nt":
        import subprocess

        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


@asynccontextmanager
async def managed_process(*args, **kwargs) -> AsyncIterator[asyncio.subprocess.Process]:
    """托管子进程上下文管理器 — 自动清理.
    
    用法:
        async with managed_process("ls", "-l") as process:
            stdout, stderr = await process.communicate()
    """
    manager = get_resource_manager()
    kwargs = {**no_window_kwargs(), **kwargs}
    process = await asyncio.create_subprocess_exec(*args, **kwargs)
    manager.track_process(process)
    
    try:
        yield process
    finally:
        await manager.cleanup_process(process)


@asynccontextmanager
async def temp_file(suffix: str = "", prefix: str = "scout_") -> AsyncIterator[Path]:
    """临时文件上下文管理器 — 自动清理.
    
    用法:
        async with temp_file(suffix=".txt") as path:
            path.write_text("content")
            # 使用文件...
        # 自动删除
    """
    manager = get_resource_manager()
    path = manager.create_temp_file(suffix=suffix, prefix=prefix)
    
    try:
        yield path
    finally:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
        manager._temp_files.discard(path)


@asynccontextmanager
async def temp_dir(suffix: str = "", prefix: str = "scout_") -> AsyncIterator[Path]:
    """临时目录上下文管理器 — 自动清理."""
    manager = get_resource_manager()
    path = manager.create_temp_dir(suffix=suffix, prefix=prefix)
    
    try:
        yield path
    finally:
        try:
            if path.exists():
                import shutil
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
        manager._temp_files.discard(path)
