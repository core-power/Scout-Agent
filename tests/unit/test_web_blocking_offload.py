"""Web 层 I/O 下沉与响应压缩的回归测试（2026-09-25 Windows 性能 P1 批次 B/C/E）.

覆盖三件事：
- B：`/api/skills/install-from-url` 的长阻塞（codeload 下载 / git clone / 目录导入）
  必须移出事件循环——否则一次安装可把整个服务冻住 40~45 s。
- C：`create_web_app()` 挂上 GZipMiddleware，静态资源与 HTML 实际被压缩，
  而 `text/event-stream` 保持不压缩（SSE 不能被缓冲）。
- E：`_PROGRESSIVE_TOOL_KEYWORDS` 的键必须都能在注册表解析（漂移项 mcp_tool 已删）。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import scout.security.auth as auth_mod
from scout.web.server import create_web_app

# 隔离凭证/配置路径，避免读写用户真实 D:\.scout（见项目安全铁律）
_TARBALL_URL = "https://github.com/example/skill-repo"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "SECRET_PATH", tmp_path / "jwt_secret")
    monkeypatch.setattr(auth_mod.AuthManager, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    from scout.config import manager as config_manager_mod

    monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    return create_web_app()


async def _drive_asgi(app, path: str) -> tuple[dict[str, str], list[tuple[float, bytes]]]:
    """裸 ASGI 驱动：逐条记录 http.response.body 的到达时刻与内容.

    为什么不用 httpx.ASGITransport —— 它会把整个响应体收完再一次性交给客户端，
    根本无法观察"是否逐条下发"，而 SSE 的回归点恰恰是这个。
    """
    headers = {"host": "127.0.0.1", "accept-encoding": "gzip"}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 80),
    }
    msgs: list[tuple[float, dict]] = []
    t0 = time.perf_counter()
    first = True

    async def receive():
        # 语义要贴真服务器：首次给一个空请求体，之后**等一会儿**才报 disconnect。
        # 若立刻返回 http.disconnect，starlette 的 listen_for_disconnect 会在第一条
        # 分片后就取消生成器（实测只能收到 1 条 body）；若永不返回，则调用方挂死。
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(1.0)
        return {"type": "http.disconnect"}

    async def send(msg):
        msgs.append((time.perf_counter() - t0, msg))

    await app(scope, receive, send)
    start = next(m for _, m in msgs if m["type"] == "http.response.start")
    hdrs = {k.decode(): v.decode() for k, v in start.get("headers", [])}
    body_parts = [(ts, m.get("body", b"")) for ts, m in msgs if m["type"] == "http.response.body"]
    return hdrs, [(ts, b) for ts, b in body_parts if b]



async def _heartbeat(stop: asyncio.Event, ticks: list[float], interval: float = 0.01) -> None:
    """事件循环探针：只有循环没被占住才会持续 tick."""
    while not stop.is_set():
        await asyncio.sleep(interval)
        ticks.append(time.perf_counter())


# ── B：长阻塞移出事件循环 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_skill_install_download_does_not_block_event_loop(app, monkeypatch):
    """下载耗时 0.6 s 时，事件循环仍应每秒 tick 数十次（而非冻死）."""
    from scout.adapters.web.adapter import WebAdapter

    def _slow_tarball(url: str, target_dir: str, timeout: int = 40) -> bool:
        time.sleep(0.6)  # 同步阻塞，模拟 codeload 下载 + 解压
        return False

    monkeypatch.setattr(WebAdapter, "_fetch_github_tarball", staticmethod(_slow_tarball), raising=False)

    stop = asyncio.Event()
    ticks: list[float] = []
    probe = asyncio.create_task(_heartbeat(stop, ticks))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        r = await client.post("/api/skills/install-from-url", json={"url": _TARBALL_URL})
    stop.set()
    await probe

    assert r.status_code == 400, f"下载失败应回 400，实际 {r.status_code}: {r.text[:120]}"
    # 0.6 s 里按 10ms 间隔理论上约 60 次；被阻塞则只有个位数
    assert len(ticks) >= 25, f"事件循环在下载期间被占住（仅 {len(ticks)} 次 tick）"


@pytest.mark.asyncio
async def test_git_clone_communicate_runs_in_thread(app, monkeypatch):
    """Gitee 分支走 git clone：communicate(timeout=45) 也必须在线程池里等."""
    import subprocess

    from scout.adapters.web import routes as _r  # noqa: F401  — 确保路由模块已加载
    from scout.adapters.web.adapter import WebAdapter

    captured: dict[str, object] = {}

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            time.sleep(0.5)  # 若仍在事件循环里就会冻住它
            return ("", "")

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    # 走到 git 分支需要 URL 不含 github.com
    stop = asyncio.Event()
    ticks: list[float] = []
    probe = asyncio.create_task(_heartbeat(stop, ticks))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        r = await client.post(
            "/api/skills/install-from-url", json={"url": "https://gitee.com/x/y"}
        )
    stop.set()
    await probe

    assert captured.get("timeout") == 45, f"未走 clone 分支或超时参数丢失: {captured} {r.text[:120]}"
    assert len(ticks) >= 20, f"communicate 期间事件循环被占住（仅 {len(ticks)} 次 tick）"


# ── C：响应压缩 ──────────────────────────────────────────────────


def test_gzip_middleware_is_wired(app):
    from starlette.middleware.gzip import GZipMiddleware

    classes = [m.cls for m in app.user_middleware]
    assert GZipMiddleware in classes, f"GZipMiddleware 未挂载: {classes}"
    entry = next(m for m in app.user_middleware if m.cls is GZipMiddleware)
    assert entry.kwargs.get("minimum_size") == 1024


@pytest.mark.asyncio
async def test_static_asset_and_html_are_gzipped(app):
    headers = {"Accept-Encoding": "gzip"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        r_js = await client.get("/static/js/shell-extras.js", headers=headers)
        r_html = await client.get("/chat", headers=headers)

    assert r_js.status_code == 200 and r_html.status_code == 200
    assert r_js.headers.get("content-encoding") == "gzip", "静态 JS 未被压缩"
    assert r_html.headers.get("content-encoding") == "gzip", "/chat HTML 未被压缩"
    # httpx 会自动解压 .content，所以"真的省了字节"要用传输层字节数比：
    # num_bytes_downloaded = 实际收到的压缩体大小
    from pathlib import Path as _P

    from scout.config.paths import PROJECT_ROOT

    disk = _P(PROJECT_ROOT) / "scout" / "web" / "static" / "js" / "shell-extras.js"
    plain = r_js.content
    assert len(plain) == disk.stat().st_size, "解压后内容与磁盘原件不一致（内容被改坏）"
    assert r_js.num_bytes_downloaded < len(plain) * 0.6, (
        f"压缩效果异常：传输 {r_js.num_bytes_downloaded} B / 原文 {len(plain)} B"
    )
    assert b"hljs" in plain, "解压内容不是预期的 shell-extras.js"



@pytest.mark.asyncio
async def test_event_stream_not_compressed_and_not_buffered():
    """SSE 既不能被压缩，也不能被攒成一次性下发（用裸 ASGI 驱动看逐条到达时刻）."""
    from fastapi import FastAPI
    from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware
    from starlette.responses import StreamingResponse

    assert "text/event-stream" in DEFAULT_EXCLUDED_CONTENT_TYPES, "starlette 升级后需重新确认旁路"

    inner = FastAPI()

    @inner.get("/sse")
    async def _sse():
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n".encode()
                await asyncio.sleep(0.08)

        return StreamingResponse(gen(), media_type="text/event-stream")

    inner.add_middleware(GZipMiddleware, minimum_size=1)  # 与生产同参数

    hdrs, parts = await _drive_asgi(inner, "/sse")
    assert hdrs.get("content-encoding") is None, f"SSE 被压缩了: {hdrs}"
    assert len(parts) >= 3, f"SSE 分片被合并成 {len(parts)} 条（应逐条下发）"
    span = parts[-1][0] - parts[0][0]
    assert span >= 0.14, f"分片到达过于集中，疑似被缓冲: {span:.3f}s"

    # 对照组：同样大小起步的普通响应在 minimum_size=1 下确实会被压缩，
    # 说明上面的"未压缩"是 content-type 旁路生效，而不是压缩器整体没跑。
    from starlette.responses import Response

    @inner.get("/plain")
    async def _plain():
        return Response(b"x" * 2048)

    hdrs2, _ = await _drive_asgi(inner, "/plain")
    assert hdrs2.get("content-encoding") == "gzip", "压缩器本身未生效，对照组失败"



# ── E：关键词表与注册表一致 ──────────────────────────────────────


# 依赖门控导致的"本机可能不注册"，属预期，不算漂移
_DEP_GATED = {"desktop", "browser"}


def test_progressive_keyword_keys_resolve_to_tools():
    from scout.engine.agent import Agent
    from scout.tools.registry import ToolRegistry

    ToolRegistry.discover()
    registered = set(ToolRegistry.all_tools())
    keys = set(Agent._PROGRESSIVE_TOOL_KEYWORDS)
    drift = sorted(keys - registered - _DEP_GATED)
    assert not drift, f"关键词表存在漂移键（永不生效）: {drift}"


def test_mcp_tool_drift_entry_removed_but_keywords_kept():
    """漂移键已删，但它承载的每个关键词都必须仍被 "mcp" 覆盖（无损删除）."""
    from scout.engine.agent import Agent

    kw = Agent._PROGRESSIVE_TOOL_KEYWORDS
    assert "mcp_tool" not in kw
    for word in ("mcp", "外部服务", "model context protocol", "external service"):
        assert word in kw["mcp"], f"关键词 {word!r} 随 mcp_tool 一起丢了"
