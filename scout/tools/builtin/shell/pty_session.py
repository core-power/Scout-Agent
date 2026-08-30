"""PTY 交互式终端会话 — 持久 Shell 的交互式演进（对标 DSH 终端完整支持）.

管道模式（ShellSession）无法运行 vim/top/less 等交互式程序：
它们依赖终端（TTY）的原始模式、窗口尺寸与转义序列。
本模块提供基于伪终端（pty）的持久会话：

- 伪终端：进程 stdin/stdout/stderr 连接到 PTY，程序认为自己在真实终端。
- 窗口尺寸：TIOCSWINSZ 动态调整（cols/rows），vim/top 正确渲染。
- 交互式程序：run() 超时后发送 Ctrl-C 而非杀进程，会话保留，
  可继续用 send_keys() 注入按键（如 ':wq\\r'、'q'、'jj'）。
- 哨兵分帧：非交互命令仍以 SENTINEL 标记结束与退出码。

用法:
    sess = PtyShellSession()
    await sess.start()
    out, code, status = await sess.run("vim note.txt", timeout=10)
    # status="timeout" → vim 仍在前台
    out, status2 = await sess.send_keys(":wq\\r", timeout=5, wait_sentinel=True)
    # vim 退出 → bash 打印哨兵 → status2="done"
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import subprocess
import time

from scout.tools.builtin.shell.session import MAX_SESSIONS, SENTINEL

# ── 平台保护（2026-08-30）：fcntl/termios/pty 均为 Unix 专属模块，
# Windows 上 import 直接抛 ImportError，会导致 shell 工具 __session_reset__ 等
# 引用本模块的路径整体崩溃。改为条件导入 + PTY_SUPPORTED 标志。
try:
    import fcntl  # noqa: F401
    import termios  # noqa: F401
    import pty  # noqa: F401

    PTY_SUPPORTED = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    pty = None  # type: ignore[assignment]
    PTY_SUPPORTED = False

logger = logging.getLogger("scout.pty_session")


def _require_pty() -> None:
    """PTY 不可用（Windows）时抛出明确错误."""
    if not PTY_SUPPORTED:
        raise RuntimeError(
            "PTY 交互式终端仅支持 Linux/macOS（依赖 fcntl/termios/pty 模块），"
            "当前平台不支持。Windows 下请使用普通 shell 或 persistent 持久会话（cmd.exe）。"
        )


class PtyShellSession:
    """基于伪终端的持久交互式 bash 会话."""

    def __init__(self, cols: int = 120, rows: int = 40, cwd: str | None = None):
        self.cols = cols
        self.rows = rows
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.master_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    # ── 生命周期 ─────────────────────────────────────────

    def _set_winsize(self) -> None:
        if self.master_fd is None:
            return
        try:
            fcntl.ioctl(
                self.master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", self.rows, self.cols, 0, 0),
            )
        except OSError:
            pass

    async def start(self) -> None:
        """拉起 PTY bash（若已有则先清理）."""
        _require_pty()
        await self._kill()
        master, slave = pty_openpty()
        self.master_fd = master
        self._set_winsize()
        # start_new_session：bash 成为会话 leader 并把 PTY 作为控制终端，
        # 任务控制/前台进程组正常（Ctrl-C 才能路由到前台作业）。
        self.proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile"],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=self.cwd,
            close_fds=True,
            start_new_session=True,
        )
        os.close(slave)
        self._closed = False
        self._q = asyncio.Queue()
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._reader())
        # bash 检测到 stdin 是 TTY 会自动进入交互模式（任务控制/回显/提示符）。
        # 关闭回显与提示符：命令不再回显（哨兵只在执行输出出现，分帧可靠），
        # 交互程序（vim 等）启动时自行设置 raw 模式，不受影响。
        await self._write(
            b"stty -echo 2>/dev/null; PS1=''; unset PROMPT_COMMAND; "
            b"export TERM=xterm-256color\n"
        )
        await asyncio.sleep(0.1)
        await self._drain()  # 丢弃启动警告（终端进程组等）与初始化回显

    async def _reader(self) -> None:
        """后台读取 PTY 输出 → 队列（线程池避免阻塞事件循环）."""
        loop = asyncio.get_running_loop()
        try:
            while self.master_fd is not None and not self._closed:
                try:
                    data = await loop.run_in_executor(None, os.read, self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                await self._q.put(data)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                self._q.put_nowait(b"")  # EOF 哨兵
            except Exception:
                pass

    async def _write(self, data: bytes) -> None:
        if self.master_fd is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, os.write, self.master_fd, data)
        except OSError:
            pass

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    async def _kill(self) -> None:
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(None, self.proc.wait), timeout=2
                )
            except Exception:
                pass
            self.proc = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._kill()

    # ── 窗口尺寸 ─────────────────────────────────────────

    async def resize(self, cols: int, rows: int) -> None:
        async with self._lock:
            self.cols = max(10, cols)
            self.rows = max(5, rows)
            self._set_winsize()

    # ── 命令执行 ─────────────────────────────────────────

    async def _drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except Exception:
                break

    @staticmethod
    def _normalize(buf: bytearray) -> str:
        text = bytes(buf).decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    async def run(self, cmd: str, timeout: int = 60) -> tuple[str, int | None, str]:
        """执行命令并等待哨兵（退出码）.

        Returns: (输出, 退出码或 None, 状态)
          - status="done"：命令正常结束（含退出码）
          - status="timeout"：超时后发送 Ctrl-C；会话保留，可继续 send_keys
        """
        async with self._lock:
            if not self._alive():
                await self.start()
            await self._drain()
            # 单行拼接：交互 bash 会预读多行（read/cat 会吞掉后续行），
            # 必须用分号合成一行；超时挂起时上层可 send_keys 注入或 interrupt()。
            framed = f"stty -echo 2>/dev/null; {cmd}; printf '\\n{SENTINEL}=%s\\n' \"$?\"\n"
            await self._write(framed.encode("utf-8", errors="replace"))

            buf = bytearray()
            deadline = time.monotonic() + timeout
            timed_out = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    chunk = await asyncio.wait_for(self._q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                if chunk == b"":
                    break  # 会话退出
                buf.extend(chunk)
                if SENTINEL.encode() in buf:
                    break

            if timed_out:
                # 交互程序挂起：不自动中断（保留会话供注入按键继续），
                # 上层可 send_keys("...") 继续 或 send_keys("\\x03")/interrupt() 显式中断
                text = self._normalize(buf)
                if SENTINEL.encode() in buf:
                    code, text = self._parse_sentinel(text)
                    return text, code, "done"
                return text, None, "timeout"

            text = self._normalize(buf)
            code, text = self._parse_sentinel(text)
            return text, code, "done"

    @classmethod
    def _parse_sentinel(cls, text: str) -> tuple[int | None, str]:
        code: int | None = None
        idx = text.rfind(f"{SENTINEL}=")
        if idx != -1:
            tail = text[idx + len(SENTINEL) + 1:].splitlines()
            try:
                code = int(tail[0].strip()) if tail and tail[0].strip() else None
            except ValueError:
                code = None
            text = text[:idx].rstrip()
        return code, text

    async def interrupt(self) -> None:
        """发送 Ctrl-C 中断当前前台作业（显式中断挂起命令）."""
        async with self._lock:
            if self._alive():
                await self._write(b"\x03")

    async def send_keys(
        self, keys: str, timeout: float = 3.0, wait_sentinel: bool = False
    ) -> tuple[str, str]:
        """向当前会话发送按键（vim 操作、Ctrl-C 等）.

        Args:
            keys: 按键序列（如 ":wq\\r"、'q'、"jj"、"\\x03"）
            timeout: 等待时长
            wait_sentinel: True 时等待哨兵（交互程序退出后 bash 打印），超时返回 "timeout"

        Returns: (输出, 状态)  status ∈ {"sent", "done", "timeout"}
        """
        async with self._lock:
            if not self._alive():
                await self.start()
            await self._drain()
            await self._write(keys.encode("utf-8", errors="replace"))

            if not wait_sentinel:
                # 短读收集已有输出
                buf = bytearray()
                while not self._q.empty():
                    try:
                        buf.extend(self._q.get_nowait())
                    except Exception:
                        break
                return self._normalize(buf), "sent"

            buf = bytearray()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._normalize(buf), "timeout"
                try:
                    chunk = await asyncio.wait_for(self._q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    return self._normalize(buf), "timeout"
                if chunk == b"":
                    break
                buf.extend(chunk)
                if SENTINEL.encode() in buf:
                    break
            text = self._normalize(buf)
            code, text = self._parse_sentinel(text)
            return text, ("done" if code is not None else "timeout")


def pty_openpty() -> tuple[int, int]:
    """创建 PTY 对（master, slave）."""
    _require_pty()
    master, slave = pty.openpty()
    return master, slave


class PtyShellSessionManager:
    """PTY 会话注册表 — 按 session_key 管理交互式 bash."""

    _sessions: dict[str, PtyShellSession] = {}

    @classmethod
    async def get(
        cls, key: str = "default", cwd: str | None = None
    ) -> PtyShellSession:
        key = key or "default"
        sess = cls._sessions.get(key)
        if sess is None:
            if len(cls._sessions) >= MAX_SESSIONS:
                oldest_key = next(iter(cls._sessions))
                await cls.close(oldest_key)
            sess = PtyShellSession(cwd=cwd)
            cls._sessions[key] = sess
        elif cwd and cwd != sess.cwd:
            sess.cwd = os.path.abspath(cwd)
        if not sess._alive():
            await sess.start()
        return sess

    @classmethod
    async def close(cls, key: str | None = None) -> None:
        if key is None:
            for k in list(cls._sessions):
                await cls._sessions[k].close()
            cls._sessions.clear()
        else:
            sess = cls._sessions.pop(key, None)
            if sess:
                await sess.close()

    @classmethod
    async def resize(cls, key: str, cols: int, rows: int) -> None:
        sess = cls._sessions.get(key or "default")
        if sess:
            await sess.resize(cols, rows)

    @classmethod
    def alive_count(cls) -> int:
        return sum(1 for s in cls._sessions.values() if s._alive())
