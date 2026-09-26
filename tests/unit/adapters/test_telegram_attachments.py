"""Telegram 适配器入站附件处理单测 — mock httpx，不依赖真实网络."""

from __future__ import annotations

import os

from scout.adapters.platforms import base as base_mod
from scout.adapters.platforms import telegram as telegram_mod
from scout.adapters.platforms.telegram import TelegramAdapter

TOKEN = "TEST-TOKEN"
API_BASE = f"https://api.telegram.org/bot{TOKEN}"
FILE_BASE = f"https://api.telegram.org/file/bot{TOKEN}"


class _FakeResponse:
    def __init__(self, json_data=None, content=b"", status_code=200):
        self._json = json_data
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._json


class _FakeAsyncClient:
    """按 URL 规则路由的 httpx.AsyncClient 替身."""

    def __init__(self, get_handler):
        self._get = get_handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, **kwargs):
        return self._get(url, params or {})


def _install_httpx_mock(monkeypatch, get_handler):
    """替换 telegram 模块使用的 httpx.AsyncClient（自动忽略 timeout 等构造参数）."""
    monkeypatch.setattr(
        telegram_mod.httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(get_handler)
    )


def _make_adapter(tmp_path, monkeypatch):
    adapter = TelegramAdapter({"platform": "telegram", "bot_token": TOKEN})
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    # 附件落盘目录重定向到测试临时目录，避免污染系统临时目录
    monkeypatch.setattr(base_mod, "attachment_upload_dir", lambda: str(upload_dir))
    return adapter, upload_dir


def _routes(getfile_results: dict, contents: dict):
    """构造 getFile / 文件下载两条路由.

    getfile_results: file_id → file_path（缺省视为 getFile 失败）
    contents: file_path → 响应字节（缺省视为 404）
    """

    def handler(url, params):
        if url.startswith(f"{API_BASE}/getFile"):
            file_path = getfile_results.get(params.get("file_id"))
            if file_path:
                return _FakeResponse(
                    json_data={"ok": True, "result": {"file_id": params.get("file_id"), "file_path": file_path}}
                )
            return _FakeResponse(json_data={"ok": False, "description": "file not found"})
        if url.startswith(FILE_BASE):
            for suffix, data in contents.items():
                if url.endswith(suffix):
                    return _FakeResponse(content=data)
            return _FakeResponse(status_code=404)
        return _FakeResponse(status_code=404)

    return handler


async def test_photo_message_downloaded_and_attached(tmp_path, monkeypatch):
    """photo 消息：取最高分辨率一张，下载落盘并填充 attachments."""
    adapter, upload_dir = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(
        monkeypatch,
        _routes(
            getfile_results={"p2": "photos/file_2.jpg"},
            contents={"photos/file_2.jpg": b"JPGDATA"},
        ),
    )
    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 42},
            "chat": {"id": 99},
            "caption": "看看这张图",
            "photo": [
                {"file_id": "p1", "file_size": 1000},
                {"file_id": "p2", "file_size": 5000},
            ],
        },
    }

    async def fake_get_updates():
        adapter._polling = False  # 取到一批后结束监听循环
        return [update]

    monkeypatch.setattr(adapter, "_get_updates", fake_get_updates)

    msgs = [m async for m in adapter.listen()]
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.attachments and len(msg.attachments) == 1
    att = msg.attachments[0]
    assert att["name"].endswith(".jpg")
    assert att["type"] == "image/jpeg"
    assert att["size"] == 5000
    assert att["path"].startswith(str(upload_dir))
    assert os.path.isfile(att["path"])
    with open(att["path"], "rb") as f:
        assert f.read() == b"JPGDATA"
    # 文本：caption 保留 + 附件提示（含文件名与路径）
    assert "看看这张图" in msg.content
    assert "[附件:" in msg.content and att["name"] in msg.content and att["path"] in msg.content


async def test_document_and_voice_attachments(tmp_path, monkeypatch):
    """document / voice 分别下载，mime 与文件名正确."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(
        monkeypatch,
        _routes(
            getfile_results={"d1": "documents/report.pdf", "v1": "voice/file_1.oga"},
            contents={"documents/report.pdf": b"%PDF-1.4", "voice/file_1.oga": b"OGGDATA"},
        ),
    )
    atts, notices = await adapter._collect_attachments(
        {
            "document": {"file_id": "d1", "file_name": "report.pdf", "mime_type": "application/pdf", "file_size": 2048},
            "voice": {"file_id": "v1", "mime_type": "audio/ogg", "file_size": 512},
        }
    )
    assert not notices
    assert len(atts) == 2
    by_name = {a["name"]: a for a in atts}
    assert by_name["report.pdf"]["type"] == "application/pdf"
    assert by_name["report.pdf"]["size"] == 2048
    voice = [a for a in atts if a["type"] == "audio/ogg"][0]
    assert voice["name"].endswith(".oga")
    assert voice["size"] == 512


async def test_oversize_file_skipped_with_notice(tmp_path, monkeypatch):
    """单文件超过 20MB：跳过下载，文本提示."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    called = []

    def handler(url, params):
        called.append(url)
        return _FakeResponse(status_code=404)

    _install_httpx_mock(monkeypatch, handler)
    atts, notices = await adapter._collect_attachments(
        {
            "document": {
                "file_id": "big1",
                "file_name": "huge.zip",
                "mime_type": "application/zip",
                "file_size": 21 * 1024 * 1024,
            }
        }
    )
    assert atts == []
    assert len(notices) == 1 and "huge.zip" in notices[0] and "20MB" in notices[0]
    assert not called  # 超限文件不应发起任何网络请求


async def test_attachment_count_capped_at_five(tmp_path, monkeypatch):
    """附件数超过 5 个：仅保留前 5 个并提示."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(
        monkeypatch,
        _routes(
            getfile_results={f"f{i}": f"documents/file_{i}.bin" for i in range(7)},
            contents={f"documents/file_{i}.bin": b"x" for i in range(7)},
        ),
    )
    msg = {
        "photo": [{"file_id": "f0", "file_size": 10}],
        "document": {"file_id": "f1", "file_size": 10},
        "video": {"file_id": "f2", "file_size": 10},
        "animation": {"file_id": "f3", "file_size": 10},
        "video_note": {"file_id": "f4", "file_size": 10},
        "voice": {"file_id": "f5", "file_size": 10},
        "audio": {"file_id": "f6", "file_size": 10},
    }
    atts, notices = await adapter._collect_attachments(msg)
    assert len(atts) == 5
    assert len(notices) == 2
    assert all("已跳过" in n for n in notices)


async def test_download_failure_yields_notice_not_error(tmp_path, monkeypatch):
    """getFile 失败 / 下载 404：仅生成提示，不抛异常."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    _install_httpx_mock(
        monkeypatch,
        _routes(getfile_results={"ok1": "docs/a.pdf", "bad1": None}, contents={}),
    )
    atts, notices = await adapter._collect_attachments(
        {
            "document": {"file_id": "bad1", "file_name": "a.pdf", "file_size": 10},
            "video": {"file_id": "ok1", "file_size": 10},
        }
    )
    assert atts == []
    assert len(notices) == 2
    assert any("a.pdf" in n and "获取文件信息" in n for n in notices)
    assert any("下载失败" in n for n in notices)


async def test_text_only_message_untouched(tmp_path, monkeypatch):
    """纯文本消息：attachments 为 None，内容不变."""
    adapter, _ = _make_adapter(tmp_path, monkeypatch)
    from scout.adapters.platforms.base import format_attachment_hints

    atts, notices = await adapter._collect_attachments({"text": "hi"})
    assert atts == [] and notices == []
    assert format_attachment_hints(atts, notices) == ""


async def test_message_dataclass_field_roundtrip():
    """Message.attachments 字段可填充（此前被 pydantic 静默丢弃）."""
    from scout.core.types import Message, Role

    m = Message(
        role=Role.USER,
        content="x",
        attachments=[{"name": "a.png", "type": "image/png", "size": 1, "path": "/tmp/a.png"}],
    )
    assert m.attachments and m.attachments[0]["name"] == "a.png"
