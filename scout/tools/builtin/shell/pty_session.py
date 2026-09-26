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
import re
import struct
import subprocess
import threading
import time

from scout.tools.builtin.shell.session import MAX_SESSIONS, SENTINEL

# ── 平台保护（2026-08-30）：fcntl/termios/pty 均为 Unix 专属模块，
# Windows 上 import 直接抛 ImportError，会导致 shell 工具 __session_reset__ 等
# 引用本模块的路径整体崩溃。改为条件导入 + PTY_SUPPORTED 标志。
try:
    import fcntl  # noqa: F401
    import termios  # noqa: F401
    import pty  # noqa: F401

    _UNIX_PTY = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    termios = None  # type: ignore[assignment]
    pty = None  # type: ignore[assignment]
    _UNIX_PTY = False

# ── Windows ConPTY 支持（2026-09-24）：pywinpty 提供 ConPTY 绑定，
# 让主打 Windows 的本项目也能跑交互式 PTY 终端（vim/top 的 Windows 等价场景）。
# pywinpty 为 Windows 可选依赖（requirements 里 sys_platform=='win32' 才装）；
# 缺失时 WINPTY_AVAILABLE=False，Windows 上 PTY 仍降级到 persistent cmd 会话。
WINPTY_AVAILABLE = False
if os.name == "nt":  # pragma: no cover - 仅 Windows
    try:
        from winpty import PtyProcess as _WinPtyProcess  # noqa: F401

        WINPTY_AVAILABLE = True
    except Exception:  # pragma: no cover - pywinpty 未安装
        _WinPtyProcess = None  # type: ignore[assignment]
        WINPTY_AVAILABLE = False
else:  # pragma: no cover - 非 Windows
    _WinPtyProcess = None  # type: ignore[assignment]

# PTY 是否在本平台可用：Unix 原生 pty，或 Windows 且 pywinpty 就绪。
PTY_SUPPORTED = _UNIX_PTY or WINPTY_AVAILABLE

logger = logging.getLogger("scout.pty_session")

# ConPTY 会把控制台输出翻译成大量 VT 转义序列（光标定位 \x1b[8;40H、标题
# \x1b]0;...\x1b\\、配色 \x1b[90m 等），且可能插在正文字符之间，导致哨兵串被
# 切断而匹配失败。检测/解析前先剥离这些序列，得到干净的文本流。
_ANSI_RE = re.compile(
    r"""
    \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)   # OSC ... (BEL 或 ST 结尾，如窗口标题)
    | \x1b[\[\]()#;?]*[0-9;]*[A-Za-z]     # CSI / 字符集 / 私有序列
    | \x1b[=>]                            # 键盘模式
    | [\x00-\x08\x0b\x0c\x0e-\x1f]        # 其余控制字符（保留 \t \n \r）
    """,
    re.VERBOSE,
)


def _strip_ansi(text: str) -> str:
    """剥离 VT/ANSI 转义序列与多余控制字符，保留可读正文."""
    return _ANSI_RE.sub("", text)



def _require_pty() -> None:
    """PTY 不可用时抛出明确错误（区分平台给出可行替代）."""
    if _UNIX_PTY or WINPTY_AVAILABLE:
        return
    if os.name == "nt":
        raise RuntimeError(
            "Windows PTY 交互式终端依赖 pywinpty（ConPTY），当前未安装。"
            "请运行 `pip install pywinpty` 后重试；或改用 persistent 持久会话（cmd.exe）。"
        )
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
    """创建 PTY 对（master, slave）— 仅 Unix."""
    if not _UNIX_PTY:
        raise RuntimeError(
            "pty.openpty() 仅 Unix 可用；Windows 走 ConPTY（WindowsPtySession）。"
        )
    master, slave = pty.openpty()
    return master, slave


class WindowsPtySession:
    """基于 Windows ConPTY（pywinpty）的持久交互式 cmd.exe 会话.

    与 Unix ``PtyShellSession`` 提供**相同的公开接口**（start/run/send_keys/
    interrupt/resize/close/_alive），使 shell 工具的 interactive 路径无需分平台。

    与 Unix 版的关键差异：
    - 载体是 cmd.exe（Windows 原生 shell；PowerShell 的 PSReadLine 会注入大量
      配色/光标转义序列，严重干扰哨兵分帧，且 Ctrl-C 易导致会话终止，故弃用）。
    - 哨兵分帧用延迟扩展 ``!errorlevel!``，对内建/外部命令退出码都精确。
    - 启动即 ``@echo off`` 关闭命令回显（对标 Unix 的 ``stty -echo``）；哨兵串
      经会话变量 ``!SENT!`` 间接输出 —— 即便某些 ConPTY 仍回显键入行，回显里
      也只有 ``!SENT!`` 而非连续哨兵串，避免累积循环提前 break。
    - ConPTY 下命令行以 ``\r`` 提交（``\n`` 不触发执行）。
    - pywinpty 的 ``read()`` 同步阻塞 → 放后台线程读，经 ``call_soon_threadsafe``
      喂给 asyncio 队列，不阻塞事件循环（对齐 Unix 版的 run_in_executor 思路）。
    """

    # 启动初始化：把哨兵串拆成两片存进会话变量 SENTA/SENTB。
    # ★ 两个关键约束（本机 ConPTY 实测）：
    #   1) `@echo off` 无法抑制 ConPTY 的**键入回显**（终端级 ENABLE_ECHO_INPUT），
    #      故任何键入行都不能含连续哨兵字面量 —— 用 %SENTA%%SENTB% 间接拼出；
    #   2) 交互式 cmd 下 `setlocal enabledelayedexpansion` 不稳（实测 !VAR! 不展开），
    #      故只用**普通** %VAR% 展开（SENTA/SENTB 是静态值，足够）。
    # 启动这几行的回显在 start() 的 _drain_until_quiet 里排空，不进入首次 run()。
    _half = len(SENTINEL) // 2
    _STARTUP = (
        "@echo off\r"
        f"set SENTA={SENTINEL[:_half]}\r"
        f"set SENTB={SENTINEL[_half:]}\r"
    )

    def __init__(self, cols: int = 120, rows: int = 40, cwd: str | None = None):
        self.cols = cols
        self.rows = rows
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self._proc = None
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._reader_thread: threading.Thread | None = None
        # 写 pty 的串行锁：读线程要"回答终端查询"，主协程要写命令，两路必须互斥，
        # 否则字节会交错（pywinpty 的 write 就是裸 send 到输入管道）。
        self._wlock = threading.Lock()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # ★ 2026-09-25 延迟改造：每条命令一个哨兵序号。旧实现靠"排空到静默"来避免
        # 读到上一条的残留哨兵（off-by-one），代价是每条命令白等 ~0.45 s。带上序号
        # 后跨命令错位在结构上不可能发生，那两个排空窗就可以去掉。
        self._seq = 0
        # 终端查询应答次数上限（防"自己的应答被回显后再应答"打转）
        self._query_answered: dict[str, int] = {}


    # ── 生命周期 ─────────────────────────────────────────

    async def start(self) -> None:
        """拉起 ConPTY cmd.exe（若已有则先清理）."""
        if not WINPTY_AVAILABLE:
            _require_pty()  # 抛出带 pip 提示的明确错误
        await self._kill()
        self._closed = False
        self._q = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        # spawn 是同步阻塞调用（创建进程 + ConPTY）→ 放线程池，避免卡事件循环
        self._proc = await self._loop.run_in_executor(
            None,
            lambda: _WinPtyProcess.spawn(
                "cmd.exe",
                cwd=self.cwd,
                dimensions=(self.rows, self.cols),
            ),
        )
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="winpty-reader", daemon=True
        )
        self._reader_thread.start()
        # 初始化哨兵变量 + 确定性就绪同步。
        await self._write(self._STARTUP)
        await self._wait_ready()

    async def _wait_ready(self, timeout: float = 6.0) -> None:
        """发就绪探针并等待，直到确认 SENTA/SENTB 已生效、shell 可响应.

        不靠「静默计时」猜测启动完成（cmd 的 conda AutoRun/横幅耗时不定），
        而是发 `echo %SENTA%%SENTB%_READY`，读到真实输出里的
        `__SCOUT_SESSION_END___READY` 才算就绪 —— 证明变量已展开、分帧可用。
        探针键入行只含 %SENTA%%SENTB%，回显不含连续哨兵串，不会误判。
        """
        await self._write("echo %SENTA%%SENTB%_READY\r")
        ready = f"{SENTINEL}_READY"
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(self._q.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if chunk == b"":
                break
            buf.extend(chunk)
            if ready in _strip_ansi(PtyShellSession._normalize(buf)):
                break
        # 清掉横幅/探针尾流，让首条 run() 的输出干净。这里必须是"静默窗"而不是
        # 瞬时排空（横幅分很多帧迟到），但也不需要 1.5s —— 终端查询已被即时应答，
        # 输出在 ~300ms 内就静下来了。
        await self._drain_until_quiet(quiet=0.1, cap=0.5)

    # ── 终端查询应答（★ 2026-09-25 start() 3.37s → ~0.4s 的关键）─────────
    # cmd.exe/conhost 一起来就向"终端"发 DA1(`CSI c`)、DSR(`CSI 6n`) 等查询。
    # 我们的对端不是真终端、没人回话，ConPTY 就一直等到它自己的内部超时——本机
    # 实测横幅要 3.05 s 才出现（裸 spawn 只要 28 ms）。手动回标准应答即可立刻解锁。
    # 实测对照：写入 `\x1b[?62;1;6c` + `\x1b[4;1R` 后，横幅在 309 ms 出现。
    _QUERY_RE = re.compile(r"\x1b\[([?>]?)([\d;]*)([cn])")

    def _answer_queries(self, text: str) -> None:
        """扫出终端查询并按标准应答写回（限次，避免自己的回显被当成新查询）."""
        for m in self._QUERY_RE.finditer(text):
            prefix, kind = m.group(1), m.group(3)  # group(2)=数字参数，应答里用不上
            if kind == "c":
                key = "da2" if prefix == ">" else "da1"
                reply = "\x1b[>0;12;0c" if key == "da2" else "\x1b[?62;1;6c"
            else:  # 'n' = DSR 光标位置查询
                key = "dsr?" if prefix == "?" else "dsr"
                reply = f"\x1b[{max(1, self.rows)};1R"
            n = self._query_answered.get(key, 0)
            if n >= 4:
                continue
            self._query_answered[key] = n + 1
            try:
                self._write_sync(reply)
            except Exception:
                logger.debug("终端查询应答写入失败（忽略）", exc_info=True)

    def _reader_loop(self) -> None:
        """后台线程：阻塞读 ConPTY 输出 → asyncio 队列（bytes）."""
        loop = self._loop
        try:
            while not self._closed and self._proc is not None:
                try:
                    data = self._proc.read(4096)
                except EOFError:
                    break
                except Exception:
                    break
                if not data:
                    continue
                self._answer_queries(data)  # 先回话，再入队（横幅能被提前 ~3s 解锁）
                if loop is None:
                    continue
                loop.call_soon_threadsafe(
                    self._q.put_nowait, data.encode("utf-8", "replace")
                )
        finally:
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(self._q.put_nowait, b"")  # EOF 哨兵
                except Exception:
                    pass

    def _write_sync(self, data: str) -> None:
        """同步写 pty（带锁）—— 供读线程应答终端查询与协程侧 executor 共用."""
        proc = self._proc
        if proc is None:
            return
        with self._wlock:
            proc.write(data)

    async def _write(self, data: str) -> None:
        if self._proc is None:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_sync, data)
        except Exception:
            pass

    def _alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    async def _kill(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: proc.close(force=True)
                )
            except Exception:
                try:
                    proc.close(force=True)
                except Exception:
                    pass
        t = self._reader_thread
        self._reader_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.0)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._kill()

    # ── 窗口尺寸 ─────────────────────────────────────────

    async def resize(self, cols: int, rows: int) -> None:
        async with self._lock:
            self.cols = max(10, cols)
            self.rows = max(5, rows)
            if self._proc is not None:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._proc.setwinsize, self.rows, self.cols
                    )
                except Exception:
                    pass

    # ── 命令执行 ─────────────────────────────────────────

    async def _drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except Exception:
                break

    async def _drain_until_quiet(self, quiet: float = 0.4, cap: float = 3.0) -> None:
        """循环排空队列，直到连续 `quiet` 秒无新数据（或超过 `cap` 秒）.

        ConPTY 的启动回显/buffer 常有尾流，一次性 drain 会漏掉稍后到达的字节；
        本方法等到输出真正静默，确保后续 run() 读到的是干净起点。
        """
        deadline = time.monotonic() + cap
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            got = False
            while not self._q.empty():
                try:
                    self._q.get_nowait()
                    got = True
                except Exception:
                    break
            if got:
                last_data = time.monotonic()
            elif time.monotonic() - last_data >= quiet:
                return
            await asyncio.sleep(0.05)

    async def run(self, cmd: str, timeout: int = 60) -> tuple[str, int | None, str]:
        """执行命令并等待哨兵（退出码）. 语义与 Unix 版一致.

        Returns: (输出, 退出码或 None, 状态)；status ∈ {"done", "timeout"}。
        """
        async with self._lock:
            if not self._alive():
                await self.start()
            # 序号化哨兵让"读到上一条残留哨兵"在结构上不可能，因此不再需要
            # 「排空到静默」（旧实现 pre 0.25s + post 0.2s，每条命令白等 ~0.45s）。
            # 这里只做一次瞬时排空，丢掉已经到达的上一条尾流。
            await self._drain()
            self._seq += 1
            tag = f"#{self._seq}"
            marker = f"{SENTINEL}{tag}="
            # cmd 分帧（两行）：
            #   行1 = 用户命令；行2 = echo 哨兵=%errorlevel%。
            # 分两行是关键 —— 同一行用 `&` 连接时 %errorlevel% 在**解析期**展开
            # （命令还没跑，取到上一条的码）；单独成行则在行1执行后才解析，退出码精确。
            # 哨兵用 %SENTA%%SENTB% 拼出，键入行回显不含连续哨兵串（防提前 break）。
            # 均以 `\r` 提交（ConPTY 语义）。
            framed = f"{cmd}\recho %SENTA%%SENTB%{tag}=%errorlevel%\r"
            await self._write(framed)

            buf = bytearray()
            clean = ""
            deadline = time.monotonic() + timeout
            timed_out = False
            found = False
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
                # 剥离 VT 序列后再匹配，防哨兵被光标/标题转义切断
                clean = _strip_ansi(PtyShellSession._normalize(buf))
                if marker in clean:
                    found = True
                    break

            if found:
                code, text = self._parse_tagged(clean, tag)
                # 尾流（提示符重绘等）留给下一次 run() 的瞬时排空处理
                return text, code, "done"
            if timed_out:
                return clean, None, "timeout"
            return clean, None, "done"

    # 任意版本的带序号哨兵（send_keys 等"等下一条哨兵"的场景用得上）
    _ANY_MARKER_RE = re.compile(re.escape(SENTINEL) + r"(#\d+)?=")

    @classmethod
    def _parse_tagged(cls, text: str, tag: str | None = None) -> tuple[int | None, str]:
        """解析（可选指定序号的）哨兵之后的退出码与输出，语义同 _parse_sentinel."""
        if tag is not None:
            idx = text.rfind(f"{SENTINEL}{tag}=")
            marker_len = len(SENTINEL) + len(tag) + 1
        else:
            hits = list(cls._ANY_MARKER_RE.finditer(text))
            if not hits:
                return None, text
            last = hits[-1]
            idx, marker_len = last.start(), last.end() - last.start()
        code: int | None = None
        if idx != -1:
            tail = text[idx + marker_len:].splitlines()
            try:
                code = int(tail[0].strip()) if tail and tail[0].strip() else None
            except ValueError:
                code = None
            text = text[:idx].rstrip()
        # ConPTY 的键入回显无法被 `@echo off` 抑制，帧第二行原样出现在输出里；
        # 它含未展开的 %SENTA%%SENTB%，既不是命令输出也占 token —— 稳定可识别，去掉。
        text = "\n".join(
            ln for ln in text.splitlines() if "%SENTA%%SENTB%" not in ln
        ).rstrip()
        return code, text

    async def interrupt(self) -> None:
        """中断当前前台作业.

        ★ Windows/ConPTY 限制（本机实测）：注入的 ``\\x03`` 与 pywinpty 的
        ``sendintr()`` 都**不会**生成 CTRL_C_EVENT 送达前台子进程 —— ping/vim 等
        外部程序无法被 ``\\x03`` 打断（CTRL 事件需由挂在同一控制台上的进程调
        GenerateConsoleCtrlEvent 产生，而本进程未挂载子控制台，pywinpty 亦未暴露）。

        故 Windows 版采取「先发 ``\\x03`` 尽力而为，再强制重启会话」策略，保证
        调用方一定能拿回可用提示符。代价：会话内 cwd/env 等状态丢失（中断挂起
        命令时通常可接受）。Unix 版则不同 —— bash 会把 SIGINT 转发给前台作业，
        会话本身保留。
        """
        async with self._lock:
            if self._alive():
                await self._write("\x03")
            # 强制重启，确保从挂起/卡死状态恢复到干净可用的 shell
            try:
                await self.start()
            except Exception:
                logger.debug("Windows PTY interrupt 重启失败", exc_info=True)

    async def send_keys(
        self, keys: str, timeout: float = 3.0, wait_sentinel: bool = False
    ) -> tuple[str, str]:
        """向当前会话发送按键. 语义与 Unix 版一致."""
        async with self._lock:
            if not self._alive():
                await self.start()
            await self._drain()
            await self._write(keys)

            if not wait_sentinel:
                # 短读收集已有输出（剥离 VT 序列）——这里 0.15s 是给按键回显留的
                # 真实等待，不是排空窗，保留。
                await asyncio.sleep(0.15)
                buf = bytearray()
                while not self._q.empty():
                    try:
                        buf.extend(self._q.get_nowait())
                    except Exception:
                        break
                return _strip_ansi(PtyShellSession._normalize(buf)), "sent"

            buf = bytearray()
            clean = ""
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return clean, "timeout"
                try:
                    chunk = await asyncio.wait_for(self._q.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    return clean, "timeout"
                if chunk == b"":
                    break
                buf.extend(chunk)
                clean = _strip_ansi(PtyShellSession._normalize(buf))
                # 带序号的哨兵也算命中（按键往往是去续上一条挂起命令的帧）
                if self._ANY_MARKER_RE.search(clean):
                    break
            code, text = self._parse_tagged(clean)
            return text, ("done" if code is not None else "timeout")


def create_pty_session(
    cols: int = 120, rows: int = 40, cwd: str | None = None
):
    """PTY 会话工厂 — 按平台返回 Unix(PtyShellSession) 或 Windows(ConPTY) 实现.

    调用方（管理器 / shell 工具）统一走本工厂，不直接 new 具体类，避免平台分叉。
    """
    if _UNIX_PTY:
        return PtyShellSession(cols=cols, rows=rows, cwd=cwd)
    if WINPTY_AVAILABLE:
        return WindowsPtySession(cols=cols, rows=rows, cwd=cwd)
    # 两者皆不可用：返回 Unix 类，其 start() 会经 _require_pty 抛明确错误
    return PtyShellSession(cols=cols, rows=rows, cwd=cwd)


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
            sess = create_pty_session(cwd=cwd)
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
