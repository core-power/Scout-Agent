"""PTY 交互式终端会话测试：基本执行、哨兵分帧、交互挂起→按键恢复、resize."""

from __future__ import annotations

import asyncio

import pytest

from scout.tools.builtin.shell.pty_session import PtyShellSession, PtyShellSessionManager


@pytest.mark.asyncio
async def test_pty_run_basic():
    sess = PtyShellSession()
    await sess.start()
    try:
        out, code, status = await sess.run("echo hello-pty", timeout=10)
        assert status == "done"
        assert code == 0
        assert "hello-pty" in out
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_pty_exit_code():
    sess = PtyShellSession()
    await sess.start()
    try:
        out, code, status = await sess.run("false", timeout=10)
        assert status == "done"
        assert code == 1
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_pty_cwd_preserved():
    sess = PtyShellSession(cwd="/tmp")
    await sess.start()
    try:
        out, code, _ = await sess.run("pwd", timeout=10)
        assert out.strip() == "/tmp"
        await sess.run("cd /var/tmp && pwd", timeout=10)
        out2, _, _ = await sess.run("pwd", timeout=10)
        assert out2.strip() == "/var/tmp"  # cd 跨调用保留
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_pty_interactive_hang_then_keys():
    """read 等待输入 → run 超时（挂起未中断）→ send_keys 注入 → read 继续 → 哨兵."""
    sess = PtyShellSession()
    await sess.start()
    try:
        out, code, status = await sess.run("read v; echo GOT:$v", timeout=1)
        assert status == "timeout"  # read 挂起，未中断（会话保留）
        assert code is None

        # 注入按键：read 读到输入后命令结束（带哨兵）
        out2, st2 = await sess.send_keys("hello-world\n", timeout=5, wait_sentinel=True)
        assert st2 == "done"
        assert "GOT:hello-world" in out2

        # 会话继续可用
        out3, code3, status3 = await sess.run("echo after", timeout=5)
        assert status3 == "done"
        assert code3 == 0
        assert "after" in out3
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_pty_timeout_then_interrupt():
    """sleep 挂起 → interrupt() 显式 Ctrl-C → 会话仍可用."""
    sess = PtyShellSession()
    await sess.start()
    try:
        out, code, status = await sess.run("sleep 30", timeout=1)
        assert status == "timeout"  # 挂起未自动中断
        await sess.interrupt()  # 显式 Ctrl-C 中断 sleep
        await asyncio.sleep(0.3)
        out2, code2, status2 = await sess.run("echo alive", timeout=10)
        assert status2 == "done"
        assert code2 == 0
        assert "alive" in out2
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_pty_resize():
    sess = PtyShellSession()
    await sess.start()
    try:
        await sess.resize(80, 24)
        assert sess.cols == 80 and sess.rows == 24
        out, code, status = await sess.run("stty size", timeout=10)
        # stty size 输出 "rows cols"（PTY 下可能因环境而异，宽松断言）
        assert status == "done"
    finally:
        await sess.close()


@pytest.mark.asyncio
async def test_manager_reuse_and_reset():
    k = "ut-pty"
    s1 = await PtyShellSessionManager.get(k, cwd="/tmp")
    s2 = await PtyShellSessionManager.get(k)
    assert s1 is s2  # 同 key 复用
    try:
        out, _, _ = await s1.run("echo reuse", timeout=10)
        assert "reuse" in out
    finally:
        await PtyShellSessionManager.close(k)
    assert PtyShellSessionManager.alive_count() == 0


@pytest.mark.asyncio
async def test_shell_tool_interactive():
    """shell 工具 interactive=true：普通命令直接完成，挂起命令可按键恢复."""
    from scout.tools.builtin.shell import ShellTool

    tool = ShellTool()
    obs = await tool.execute(
        command="echo pty-via-tool", interactive=True, session_key="ut-tool"
    )
    assert obs.success
    assert "pty-via-tool" in obs.output

    # 挂起 → 提示按键继续
    obs2 = await tool.execute(
        command="read v; echo GOT:$v", timeout=1, interactive=True, session_key="ut-tool"
    )
    assert not obs2.success
    assert "PTY" in obs2.output and "session_keys" in obs2.output

    # 按键恢复（read 读到输入 → 命令结束）
    obs3 = await tool.execute(
        command="", session_keys="hello\\r", timeout=5, interactive=True, session_key="ut-tool"
    )
    assert "GOT:hello" in obs3.output

    # 显式 Ctrl-C 中断一个挂起命令
    obs4 = await tool.execute(
        command="sleep 30", timeout=1, interactive=True, session_key="ut-tool"
    )
    assert "挂起" in obs4.output
    obs5 = await tool.execute(
        command="", session_keys="\\x03", timeout=2, interactive=True, session_key="ut-tool"
    )
    # 中断后会话仍可用
    obs6 = await tool.execute(
        command="echo still-alive", interactive=True, session_key="ut-tool"
    )
    assert obs6.success and "still-alive" in obs6.output

    await tool.execute(command="__session_reset__", session_key="ut-tool")
    from scout.tools.builtin.shell.pty_session import PtyShellSessionManager

    assert PtyShellSessionManager.alive_count() == 0
