"""持久 Shell 会话 — 进程级 shell 长驻（Linux/macOS 用 bash，Windows 用 cmd.exe）.

背景：默认 shell 工具每次调用都是独立 subprocess，`cd` / 环境变量 / 后台任务
跨调用全部丢失。本模块提供进程级持久 shell 会话：

- 一个会话 = 一个常驻 shell 进程（bash --norc --noprofile / cmd.exe /Q，非交互）。
- 命令通过 stdin 注入，输出用哨兵行（SENTINEL）分帧，保证每次调用
  只取回本条命令的输出与退出码。
- 命令天然保留 cwd / 导出变量 / 后台任务。
- 超时 → 杀掉会话并自动重建；会话退出（exit / 崩溃）→ 下次调用自动拉起。
- 按 session_key 隔离（默认 "default"），并发命令经 per-session 锁串行化。

Windows 适配（2026-08-30）：bash 在 Windows 默认不存在（除非装 Git Bash/WSL），
持久会话改用 cmd.exe /Q /d；哨兵改用 echo 输出 %errorlevel%；编解码走
跨平台 decode_output（UTF-8→GBK→latin-1），解决中文系统 GBK 输出乱码。
"""

from __future__ import annotations

import asyncio
import logging
import os

from scout.security.sandbox import _decode as decode_output  # noqa: E402

logger = logging.getLogger("scout.shell_session")

SENTINEL = "__SCOUT_SESSION_END__"
MAX_SESSIONS = 16

IS_WINDOWS = os.name == "nt"


class ShellSession:
    """单条持久 bash 会话."""

    def __init__(self, cwd: str | None = None):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """拉起 shell 子进程（Windows 用 cmd.exe，其余用 bash；若已存在则先关闭）."""
        await self._kill_proc()
        if IS_WINDOWS:
            # /Q 关闭回显，/d 忽略 AutoRun（避免注册表配置干扰命令分帧）
            self.proc = await asyncio.create_subprocess_exec(
                "cmd.exe", "/Q", "/d",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
            )
            # 丢弃启动横幅（若有）
            try:
                await asyncio.wait_for(self.proc.stdout.read(512), timeout=0.5)
            except Exception:
                pass
        else:
            self.proc = await asyncio.create_subprocess_exec(
                "bash", "--norc", "--noprofile",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
            )
        self._closed = False

    async def _kill_proc(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except Exception:
                pass
        self.proc = None

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def run(self, cmd: str, timeout: int = 60) -> tuple[str, int]:
        """执行单条命令，返回 (输出, 退出码).

        - 输出按哨兵分帧，只返回本条命令的 stdout+stderr。
        - 超时：杀掉会话（后续调用自动重建），返回部分输出 + 退出码 124。
        - 会话中途退出：自动重启并重放一次。
        """
        async with self._lock:
            if not self._alive():
                await self.start()

            if IS_WINDOWS:
                # cmd 哨兵：echo 输出退出码；%errorlevel% 在本行解析时反映上一命令退出码
                framed = f"{cmd}\r\necho.&echo {SENTINEL}=%errorlevel%\r\n"
                payload = framed.encode("gbk", errors="replace")  # 中文系统 cmd 默认 GBK 代码页
            else:
                framed = f"{cmd}\nprintf '\\n{SENTINEL}=%s\\n' \"$?\"\n"
                payload = framed.encode("utf-8", errors="replace")
            self.proc.stdin.write(payload)
            try:
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                await self.start()
                self.proc.stdin.write(payload)
                await self.proc.stdin.drain()

            out = bytearray()
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            timed_out = False
            try:
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        timed_out = True
                        break
                    chunk = await asyncio.wait_for(
                        self.proc.stdout.read(4096), timeout=remaining
                    )
                    if not chunk:
                        break  # 会话已退出（如命令内 exit）
                    out.extend(chunk)
                    if SENTINEL.encode() in out:
                        break
            except asyncio.TimeoutError:
                timed_out = True
            except Exception:
                timed_out = True

            if timed_out:
                await self._kill_proc()  # 卡死会话，整体重建
                text = decode_output(bytes(out))
                return text + f"\n[超时] 命令超过 {timeout}s，会话已重置（后续调用将自动重建）", 124

            text = decode_output(bytes(out))
            code = 0
            idx = text.rfind(f"{SENTINEL}=")
            if idx != -1:
                tail = text[idx + len(SENTINEL) + 1:].splitlines()
                try:
                    code = int(tail[0].strip()) if tail else 0
                except ValueError:
                    code = 0
                text = text[:idx].rstrip()
            return text, code

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._kill_proc()


class ShellSessionManager:
    """会话注册表 — 按 session_key 管理持久 bash 进程."""

    _sessions: dict[str, ShellSession] = {}

    @classmethod
    async def get(cls, key: str = "default", cwd: str | None = None) -> ShellSession:
        key = key or "default"
        sess = cls._sessions.get(key)
        if sess is None:
            # 简单 LRU：超过上限时清理最旧的存活会话
            if len(cls._sessions) >= MAX_SESSIONS:
                oldest_key = next(iter(cls._sessions))
                await cls.close(oldest_key)
            sess = ShellSession(cwd)
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
    def alive_count(cls) -> int:
        return sum(1 for s in cls._sessions.values() if s._alive())
