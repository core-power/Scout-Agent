"""Discord 适配器入站附件处理单测 — mock httpx 与 discord 消息对象，不依赖真实网络/库."""

from __future__ import annotations

import os
from types import SimpleNamespace

from scout.adapters.platforms import base as base_mod
from scout.adapters.platforms import discord as discord_mod
from scout.adapters.platforms.discord import DiscordAdapter


class _FakeResponse:
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code


class _FakeAsyncClient:
    """按 URL 前缀路由的 httpx.AsyncClient 替身."""

    def __init__(self, contents, status=200):
        self._contents = contents
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        for prefix, data in self._contents.items():
            if url.startswith(prefix):
                return _FakeResponse(content=data, status_code=self._status)
        return _FakeResponse(status_code=404)


def _install_httpx_mock(monkeypatch, contents, status=200):
    monkeypatch.setattr(
        discord_mod.httpx,
        "AsyncClient",
        lambda *a, **kw: _FakeAsyncClient(contents, status),
    )


def _make_adapter(tmp_path, monkeypatch):
    adapter = DiscordAdapter({"platform": "discord", "bot_token": "TEST-TOKEN"})
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    # 附件落盘目录重定向到测试临时目录，避免污染系统临时目录
    monkeypatch.setattr(base_mod, "attachment_upload_dir", lambda: str(upload_dir))
    return adapter, upload_dir


def _fake_message(attachments, content="看这个文件"):
    return SimpleNamespace(
        attachments=attachments,
        content=content,
        channel=SimpleNamespace(id=12345),
        author=SimpleNamespace(id=67890),
        id=111,
        created_at=SimpleNamespace(timestamp=lambda: 1700000000.0),
    )


def _fake_attachment(filename, url, content_type=None, size=100):
    return SimpleNamespace(
        filename=filename, url=url, content_type=content_type, size=size
    )


async def test_attachment_downloaded_and_attached(tmp_path, monkeypatch):
    """附件下载落盘，填充 PlatformMessage.attachments 并在文本中提示."""
    adapter, upload_dir = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(monkeypatch, {"https://cdn.example/": b"PNGDATA"})

    msg = await adapter._to_platform_message(
        _fake_message([_fake_attachment("pic.png", "https://cdn.example/pic.png", "image/png", 7)])
    )

    assert msg.attachments and len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att == {
        "name": "pic.png",
        "type": "image/png",
        "size": 7,
        "path": att["path"],  # 路径含随机前缀，单独断言
    }
    assert att["path"].startswith(str(upload_dir))
    assert os.path.isfile(att["path"])
    with open(att["path"], "rb") as f:
        assert f.read() == b"PNGDATA"
    # 文本：原内容保留 + 附件提示
    assert "看这个文件" in msg.content
    assert "[附件: pic.png → " in msg.content
    # 其余 PlatformMessage 字段不受影响
    assert msg.channel_id == "12345" and msg.user_id == "67890" and msg.message_id == "111"


async def test_mime_guessed_when_content_type_missing(tmp_path, monkeypatch):
    """content_type 缺失时按文件名推断 mime."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(monkeypatch, {"https://cdn.example/": b"data"})

    atts, notices = await adapter._collect_attachments(
        _fake_message([_fake_attachment("notes.txt", "https://cdn.example/notes.txt")])
    )
    assert not notices
    assert atts[0]["type"] == "text/plain"


async def test_oversize_file_skipped_with_notice(tmp_path, monkeypatch):
    """单文件超过 20MB：跳过下载并提示."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(monkeypatch, {"https://cdn.example/": b"x"})

    msg = await adapter._to_platform_message(
        _fake_message(
            [_fake_attachment("huge.zip", "https://cdn.example/huge.zip", "application/zip", 21 * 1024 * 1024)]
        )
    )
    assert msg.attachments is None
    assert "huge.zip" in msg.content and "20MB" in msg.content and "已跳过" in msg.content


async def test_attachment_count_capped_at_five(tmp_path, monkeypatch):
    """附件数超过 5 个：仅保留前 5 个并提示其余跳过."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(monkeypatch, {"https://cdn.example/": b"x"})

    attachments = [
        _fake_attachment(f"file{i}.bin", f"https://cdn.example/file{i}.bin", "application/octet-stream", 10)
        for i in range(6)
    ]
    atts, notices = await adapter._collect_attachments(_fake_message(attachments))
    assert len(atts) == 5
    assert len(notices) == 1 and "file5.bin" in notices[0] and "5" in notices[0]


async def test_download_failure_yields_notice_not_error(tmp_path, monkeypatch):
    """下载返回非 200：生成提示，不抛异常，不影响消息构建."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(monkeypatch, {"https://cdn.example/": b""}, status=500)

    msg = await adapter._to_platform_message(
        _fake_message([_fake_attachment("bad.png", "https://cdn.example/bad.png", "image/png")])
    )
    assert msg.attachments is None
    assert "bad.png" in msg.content and "下载失败" in msg.content
    assert "看这个文件" in msg.content


async def test_message_without_attachments_untouched(tmp_path, monkeypatch):
    """无附件消息：attachments 为 None，内容原样."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)

    msg = await adapter._to_platform_message(_fake_message([], content="纯文本"))
    assert msg.attachments is None
    assert msg.content == "纯文本"


async def test_platform_message_enqueued_via_handle_incoming(tmp_path, monkeypatch):
    """转换结果可经 _handle_incoming 入队（ChannelManager listen() 消费路径）."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(monkeypatch, {"https://cdn.example/": b"x"})

    platform_msg = await adapter._to_platform_message(
        _fake_message([_fake_attachment("a.png", "https://cdn.example/a.png", "image/png")])
    )
    await adapter._handle_incoming(platform_msg)
    assert adapter._incoming_queue.qsize() == 1
    assert adapter._incoming_queue.get_nowait() is platform_msg
