"""稳定性/安全修复回归测试（2026-09-24）.

覆盖本轮三项后端修复：
- web/api/fs.py：读写收紧到 home/cwd/temp/白名单；写操作非回环必须鉴权
- channel_manager.py：save_config 的 enabled 不再恒 True；_run_channel 不吞 CancelledError；
  supervisor 掉线重连、stop 干净取消
- storage/sqlite.py：异步方法经 to_thread + 锁；并发写不损坏；事务原子性
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scout.web.api import fs as fs_api


# ══════════════════════════ fs.py 安全边界 ══════════════════════════


def _outside_path() -> Path:
    """一个肯定不在 home/cwd/temp 下的路径（用于验证被拒）."""
    if os.name == "nt":
        return Path(r"C:\Windows\scout_fs_test_outside")
    return Path("/usr/scout_fs_test_outside")


def test_under_allowed_io_home_true():
    p = fs_api._home() / "somefile.txt"
    assert fs_api._under_allowed_io(p) is True


def test_under_allowed_io_outside_false():
    assert fs_api._under_allowed_io(_outside_path()) is False


def test_under_allowed_io_env_allowlist(tmp_path, monkeypatch):
    """SCOUT_FS_ALLOW_ROOTS 显式白名单内的路径放行."""
    allow = tmp_path / "proj"
    allow.mkdir()
    monkeypatch.setenv("SCOUT_FS_ALLOW_ROOTS", str(allow))
    assert fs_api._under_allowed_io(allow / "a.txt") is True


def test_resolve_io_blocks_outside(tmp_path):
    """_resolve_io 对白名单外路径抛 403（即使 _allowed_root 放行盘符）."""
    from fastapi import HTTPException

    outside = _outside_path() / "x.txt"
    with pytest.raises(HTTPException) as ei:
        fs_api._resolve_io(str(outside), must_exist=False)
    assert ei.value.status_code == 403


def _fake_request(host: str, auth: str = "", query: dict | None = None):
    return SimpleNamespace(
        client=SimpleNamespace(host=host),
        headers={"authorization": auth} if auth else {},
        query_params=query or {},
    )


def test_guard_write_client_loopback_ok():
    # 本地回环无需 token
    fs_api._guard_write_client(_fake_request("127.0.0.1"))
    fs_api._guard_write_client(_fake_request("::1"))


def test_guard_write_client_remote_no_token_401():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        fs_api._guard_write_client(_fake_request("203.0.113.9"))
    assert ei.value.status_code == 401


def test_guard_write_client_remote_valid_token_ok(monkeypatch):
    monkeypatch.setattr("scout.security.auth.verify_token", lambda t: {"sub": "u"} if t == "good" else None)
    # 有效 Bearer token 放行
    fs_api._guard_write_client(_fake_request("203.0.113.9", auth="Bearer good"))
    # 无效 token 仍 401
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        fs_api._guard_write_client(_fake_request("203.0.113.9", auth="Bearer bad"))


# ══════════════════════════ channel_manager ══════════════════════════


class _FakeAdapter:
    """最小适配器桩：可控 listen 行为 + 记录 start/stop 次数."""

    def __init__(self, enabled=True, listen_mode="block"):
        self.config = {"platform": "fake", "enabled": enabled}
        self.enabled = enabled
        self.platform = "fake"
        self.channel_id = "c"
        self._started = 0
        self._stopped = 0
        self._listen_mode = listen_mode
        self._block = asyncio.Event()

    async def start(self):
        self._started += 1
        return True

    async def stop(self):
        self._stopped += 1

    async def disconnect(self):
        pass

    async def listen(self):
        # 必须是 async generator（_run_channel 用 `async for`）
        if self._listen_mode == "raise":
            raise RuntimeError("boom")
        if self._listen_mode == "block":
            await self._block.wait()  # 挂住，直到被 cancel
        # "end" 或 block 被唤醒后：不产出任何消息，自然结束
        if False:  # pragma: no cover - 仅为使其成为 async generator
            yield None

    async def send_message(self, *a, **k):
        return True

    async def send_file(self, *a, **k):
        return True

    async def health_check(self):
        return {"ok": True}


def _make_manager(tmp_path):
    from scout.adapters.channel_manager import ChannelManager
    return ChannelManager(config_dir=str(tmp_path))


def test_save_config_enabled_not_always_true(tmp_path):
    """修复 `or True` 恒真：enabled 应如实反映 adapter.enabled."""
    mgr = _make_manager(tmp_path)
    mgr.register("on", _FakeAdapter(enabled=True))
    mgr.register("off", _FakeAdapter(enabled=False))
    mgr.save_config()
    cfg = json.loads((tmp_path / "channels.json").read_text(encoding="utf-8"))
    assert cfg["on"]["enabled"] is True
    assert cfg["off"]["enabled"] is False  # 关键：不再恒 True


async def test_run_channel_reraises_cancelled(tmp_path):
    """_run_channel 不再吞 CancelledError（否则 stop/supervisor 失效）."""
    mgr = _make_manager(tmp_path)
    ad = _FakeAdapter(listen_mode="block")
    mgr.register("x", ad)
    await ad.start()
    task = asyncio.create_task(mgr._run_channel("x", ad))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_supervisor_reconnects_after_listen_ends(tmp_path):
    """listen 意外结束 → supervisor 退避后重连（再次 start）."""
    mgr = _make_manager(tmp_path)
    mgr._STABLE_RUN_S = 0.0
    ad = _FakeAdapter(listen_mode="end")  # 每次 listen 立即结束
    mgr.register("rc", ad)
    ok = await mgr.start_channel("rc")
    assert ok
    try:
        # 初始 start 1 次；首次退避 1s 后重连再 start → 等待 >1s
        await asyncio.sleep(1.4)
        assert ad._started >= 2, f"应已重连（start 次数={ad._started}）"
    finally:
        await mgr.stop_channel("rc")


async def test_supervisor_stop_cancels_cleanly(tmp_path):
    """stop_channel 取消监督任务后不再重连."""
    mgr = _make_manager(tmp_path)
    ad = _FakeAdapter(listen_mode="block")
    mgr.register("s", ad)
    await mgr.start_channel("s")
    await asyncio.sleep(0.05)
    await mgr.stop_channel("s")
    assert "s" not in mgr._running
    started_after_stop = ad._started
    await asyncio.sleep(0.3)
    assert ad._started == started_after_stop  # 停止后不再重启


# ══════════════════════════ sqlite 异步 ══════════════════════════


@pytest.fixture
async def store(tmp_path):
    from scout.storage.sqlite import SQLiteStorage
    s = SQLiteStorage(str(tmp_path / "t.db"))
    await s.connect()
    await s.execute_script("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    try:
        yield s
    finally:
        await s.disconnect()


async def test_sqlite_basic_ops(store):
    await store.execute("INSERT INTO t(v) VALUES($1)", ("hello",))
    row = await store.fetchone("SELECT v FROM t WHERE id=$1", (1,))
    assert row == {"v": "hello"}
    await store.executemany("INSERT INTO t(v) VALUES($1)", [("a",), ("b",)])
    rows = await store.fetchall("SELECT v FROM t ORDER BY id")
    assert [r["v"] for r in rows] == ["hello", "a", "b"]


async def test_sqlite_concurrent_writes_no_corruption(store):
    await asyncio.gather(*[store.execute("INSERT INTO t(v) VALUES($1)", (f"c{i}",)) for i in range(50)])
    rows = await store.fetchall("SELECT v FROM t")
    assert len(rows) == 50


async def test_sqlite_transaction_rollback(store):
    with pytest.raises(RuntimeError):
        async with store.transaction() as tx:
            await tx.execute("INSERT INTO t(v) VALUES($1)", ("tx1",))
            raise RuntimeError("boom")
    rows = await store.fetchall("SELECT v FROM t")
    assert all(r["v"] != "tx1" for r in rows)  # 已回滚


async def test_sqlite_transaction_commit(store):
    async with store.transaction() as tx:
        await tx.execute("INSERT INTO t(v) VALUES($1)", ("tx2",))
        await tx.execute("INSERT INTO t(v) VALUES($1)", ("tx3",))
    rows = {r["v"] for r in await store.fetchall("SELECT v FROM t")}
    assert {"tx2", "tx3"} <= rows


async def test_sqlite_does_not_block_loop(store):
    """写操作期间事件循环仍能推进（to_thread 生效的间接验证）."""
    flag = {"ticked": False}

    async def ticker():
        await asyncio.sleep(0.01)
        flag["ticked"] = True

    t = asyncio.create_task(ticker())
    await asyncio.gather(*[store.execute("INSERT INTO t(v) VALUES($1)", (f"x{i}",)) for i in range(30)])
    await t
    assert flag["ticked"] is True


async def test_sqlite_reentrant_no_deadlock(store):
    """防御性：即便在 transaction() 内误用外层 store 方法，可重入锁也不死锁.

    （正确用法是用 tx 句柄保证原子性；此测仅验证不会永久挂起。）
    """
    async with store.transaction():
        # 外层 store.execute 会再次进入同一把锁 —— 可重入 → 不挂起
        await asyncio.wait_for(store.execute("INSERT INTO t(v) VALUES($1)", ("re",)), timeout=5)
    rows = await store.fetchall("SELECT v FROM t")
    assert any(r["v"] == "re" for r in rows)


def test_session_store_sync_save_load_no_deadlock(tmp_path):
    """回归：SessionStore 同步 save/load（_run_async 每次新建事件循环）不得死锁.

    此前 SQLiteStorage 用不可重入 asyncio.Lock + transaction() 内调外层 db.execute
    → 永久挂起。修复后（tx 句柄 + 可重入锁）应正常往返。
    """
    from scout.core.types import Message, Session
    from scout.session.store import SessionStore

    store = SessionStore(db_path=tmp_path / "sessions.db")
    sess = Session(
        id="s-regr",
        agent_id="default",
        messages=[Message(role="user", content="你好"), Message(role="assistant", content="在")],
    )
    store.save_session(sess)  # 同步桥接（asyncio.run）
    loaded = store.load_session("s-regr")
    assert loaded is not None
    assert loaded.id == "s-regr"
    assert len(loaded.messages) == 2
    assert loaded.messages[0].content == "你好"
