"""Windows ConPTY 交互式终端会话测试（WindowsPtySession）.

仅在 Windows 且 pywinpty 可用时运行；其余平台整体跳过。这些用例会真实拉起
cmd.exe（ConPTY），验证与 Unix PtyShellSession 对齐的公开接口：run/退出码/
状态持久/resize/挂起超时/interrupt 恢复/send_keys，以及工厂与管理器的平台路由。

对应 Unix bash 版用例见 test_pty_session.py。
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from scout.tools.builtin.shell.pty_session import (
    PTY_SUPPORTED,
    WINPTY_AVAILABLE,
    PtyShellSessionManager,
    WindowsPtySession,
    create_pty_session,
)

# 仅 Windows + pywinpty 就绪时运行；其他平台跳过（Unix 走 test_pty_session.py）
pytestmark = pytest.mark.skipif(
    not (os.name == "nt" and WINPTY_AVAILABLE),
    reason="Windows ConPTY PTY 专属用例（需 Windows + pywinpty）",
)


@pytest.fixture
async def sess():
    s = create_pty_session()
    await s.start()
    try:
        yield s
    finally:
        await s.close()


def test_flags_and_factory_route_to_windows():
    """Windows 上 PTY_SUPPORTED 为真，工厂返回 ConPTY 实现."""
    assert PTY_SUPPORTED is True
    assert WINPTY_AVAILABLE is True
    s = create_pty_session()
    assert isinstance(s, WindowsPtySession)


async def test_win_pty_run_basic(sess):
    out, code, status = await sess.run("echo hello-winpty", timeout=15)
    assert status == "done"
    assert code == 0
    assert "hello-winpty" in out


async def test_win_pty_exit_code(sess):
    """外部命令退出码经 %errorlevel% 精确回传（两行分帧的关键收益）."""
    out, code, status = await sess.run("cmd /c exit 3", timeout=15)
    assert status == "done"
    assert code == 3


async def test_win_pty_env_persists_across_calls(sess):
    """会话状态跨 run() 保留（set 的变量在后续命令可见）."""
    await sess.run("set SCOUT_VAR=bar123", timeout=15)
    out, code, status = await sess.run("echo %SCOUT_VAR%", timeout=15)
    assert status == "done"
    assert "bar123" in out


async def test_win_pty_resize(sess):
    await sess.resize(100, 30)
    assert sess.cols == 100 and sess.rows == 30


async def test_win_pty_hang_then_timeout(sess):
    """长命令超时 → status=timeout、code=None（会话保留，可后续 interrupt）."""
    out, code, status = await sess.run("ping -n 20 127.0.0.1", timeout=2)
    assert status == "timeout"
    assert code is None


async def test_win_pty_interrupt_recovers(sess):
    """挂起后 interrupt：ConPTY 无法向子进程投递 CTRL_C，故重启会话恢复可用 shell."""
    await sess.run("ping -n 20 127.0.0.1", timeout=2)  # 挂起
    await sess.interrupt()  # 内部发 \x03 并强制重启会话
    out, code, status = await sess.run("echo recovered-ok", timeout=15)
    assert status == "done"
    assert code == 0
    assert "recovered-ok" in out
    assert sess._alive()


async def test_win_pty_send_keys(sess):
    """send_keys 非等待模式返回 'sent'."""
    out, status = await sess.send_keys("echo sent-keys-ok\r", timeout=5, wait_sentinel=False)
    assert status == "sent"


async def test_win_pty_manager_routes_and_reuses():
    """管理器经工厂拿到 ConPTY 会话；同 key 复用；close 后清零."""
    key = "ut-winpty"
    s1 = await PtyShellSessionManager.get(key)
    assert isinstance(s1, WindowsPtySession)
    s2 = await PtyShellSessionManager.get(key)
    assert s1 is s2
    try:
        out, code, status = await s1.run("echo mgr-ok", timeout=15)
        assert status == "done" and "mgr-ok" in out
    finally:
        await PtyShellSessionManager.close(key)
    assert PtyShellSessionManager.alive_count() == 0


# ── 2026-09-25 延迟改造（终端应答 + 带序号哨兵）的回归钉 ──────────────
# 改造前实测：start() 3372 ms、run('echo') 512 ms（ConPTY 不回答终端查询要空等
# ~3.05 s；每条命令前后各一个"排空到静默"窗 ~0.45 s）。上限取实测值的 ~9 倍，
# 既能挡住回归、又不至于在 CI 上因抖动误报。

START_BUDGET_MS = 2000.0
RUN_BUDGET_MS = 300.0


async def test_win_pty_start_within_budget():
    """start() 必须远快于旧的 3.4 s（终端查询若没人应答会退回到那个量级）."""
    s = WindowsPtySession()
    t0 = time.perf_counter()
    try:
        await s.start()
        dt = (time.perf_counter() - t0) * 1000
        assert dt < START_BUDGET_MS, f"start() 退化到 {dt:.0f} ms（上限 {START_BUDGET_MS:.0f}）"
    finally:
        await s.close()


async def test_win_pty_run_within_budget(sess):
    """单条命令往返应在几十毫秒量级，不再靠排空窗凑时间."""
    for i in range(4):
        t0 = time.perf_counter()
        out, code, status = await sess.run(f"echo tick{i}", timeout=15)
        dt = (time.perf_counter() - t0) * 1000
        assert status == "done" and code == 0
        assert f"tick{i}" in out
        assert dt < RUN_BUDGET_MS, f"run() 第 {i} 次耗时 {dt:.0f} ms 超预算"


async def test_win_pty_sentinel_tags_increment(sess):
    """每条命令一个独立哨兵序号 —— 跨命令错位在结构上不可能."""
    base = sess._seq
    await sess.run("echo one", timeout=15)
    await sess.run('cmd /c "exit /b 5"', timeout=15)
    assert sess._seq == base + 2, "哨兵序号未按命令递增"
    out, code, _ = await sess.run('cmd /c "exit /b 9"', timeout=15)
    assert code == 9, f"带序号哨兵后退出码解析错误: {code} / {out[:80]!r}"


async def test_win_pty_output_has_no_frame_echo(sess):
    """ConPTY 键入回显里的帧模板不能污染输出（既费 token 又误导模型）."""
    out, code, _ = await sess.run("echo clean-out", timeout=15)
    assert "clean-out" in out
    assert "%SENTA%%SENTB%" not in out, f"输出残留帧回显: {out[-120:]!r}"


def test_win_pty_answers_terminal_queries():
    """终端应答器的纯单元测试（不需要真 ConPTY）：识别查询、回标准响应、限次."""
    written: list[str] = []

    class _FakeProc:
        def write(self, data):
            written.append(data)

    s = WindowsPtySession()
    s._proc = _FakeProc()

    s._answer_queries("\x1b[c")            # DA1 请求
    assert any(r.startswith("\x1b[?") and r.endswith("c") for r in written), written
    n_after_da1 = len(written)

    s._answer_queries("\x1b[6n")           # DSR 光标位置请求
    assert len(written) > n_after_da1 and written[-1].endswith("R"), written

    s._answer_queries("\x1b[>0;0c")        # DA2 请求
    assert written[-1].startswith("\x1b[>"), written

    before = len(written)                  # 限次：同一类再来 50 次也不应无限应答
    for _ in range(50):
        s._answer_queries("\x1b[c")
    assert len(written) - before <= 4, "终端应答没有限次，自己的回显可能引发应答风暴"


def test_win_pty_write_is_serialized():
    """读线程应答与协程写命令共用一把锁，避免字节交错."""
    s = WindowsPtySession()
    assert hasattr(s, "_wlock") and hasattr(s, "_write_sync")
    assert s._wlock.locked() is False

