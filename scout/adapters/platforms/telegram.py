"""Telegram 适配器 — 通过 Telegram Bot API 接入."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from typing import AsyncIterator

import httpx

from scout.adapters.platforms.base import (
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_COUNT,
    ChannelAdapter,
    format_attachment_hints,
    save_inbound_attachment,
)
from scout.core.types import Message, Role


class TelegramAdapter(ChannelAdapter):
    """Telegram Bot 适配器."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.bot_token = config.get("bot_token", "")
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}" if self.bot_token else ""
        self._offset = 0
        self._connected = False
        self._polling = False

    async def connect(self) -> bool:
        """连接 Telegram API — 验证 bot token."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.api_base}/getMe")
                data = resp.json()
                if data.get("ok"):
                    self._connected = True
                    self._bot_info = data["result"]
                    return True
        except Exception:
            pass
        return False

    async def listen(self) -> AsyncIterator[Message]:
        """长轮询监听 Telegram 消息."""
        self._polling = True
        while self._polling:
            try:
                updates = await self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    if "message" in update:
                        msg = update["message"]
                        text = msg.get("text", "")
                        if text.startswith("/"):
                            # 命令处理
                            if text == "/start":
                                await self.send_text(str(msg["chat"]["id"]),
                                    "🧭 Scout Agent 已就绪！直接发消息开始对话。")
                                continue
                        # 入站媒体（photo/document/voice/video 等）→ 下载落盘并附到消息
                        attachments, notices = await self._collect_attachments(msg)
                        content = text or msg.get("caption") or ""
                        hint = format_attachment_hints(attachments, notices)
                        if hint:
                            content = f"{content}\n\n{hint}" if content.strip() else hint
                        yield Message(
                            role=Role.USER,
                            content=content,
                            sender=str(msg["from"]["id"]),
                            session_id=str(msg["chat"]["id"]),
                            source="telegram",
                            attachments=attachments or None,
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

    @staticmethod
    def _media_candidates(msg: dict) -> list[tuple[str, str, str, int]]:
        """从 Telegram 消息提取媒体候选 → [(file_id, 文件名, mime, 大小)].

        photo 取分辨率最高的一张；其余取消息体里的第一个媒体字段。
        """
        candidates: list[tuple[str, str, str, int]] = []
        photo = msg.get("photo")
        if isinstance(photo, list) and photo:
            best = max(photo, key=lambda p: p.get("file_size") or 0)
            if best.get("file_id"):
                candidates.append(
                    (best["file_id"], "", "image/jpeg", best.get("file_size") or 0)
                )
        for key, default_mime in (
            ("document", "application/octet-stream"),
            ("video", "video/mp4"),
            ("animation", "video/mp4"),
            ("video_note", "video/mp4"),
            ("voice", "audio/ogg"),
            ("audio", "audio/mpeg"),
        ):
            item = msg.get(key)
            if isinstance(item, dict) and item.get("file_id"):
                candidates.append(
                    (
                        item["file_id"],
                        item.get("file_name") or "",
                        item.get("mime_type") or default_mime,
                        item.get("file_size") or 0,
                    )
                )
        return candidates

    async def _collect_attachments(self, msg: dict) -> tuple[list[dict], list[str]]:
        """下载消息中的媒体附件到临时目录.

        Returns:
            (attachments, notices): 附件元数据列表（{name, type, size, path}）
            与需要附加到消息文本的提示（超限跳过 / 下载失败等，可为空）。
        """
        attachments: list[dict] = []
        notices: list[str] = []
        if not self.api_base:
            return attachments, notices

        candidates = self._media_candidates(msg)
        if not candidates:
            return attachments, notices

        async with httpx.AsyncClient(timeout=120) as client:
            for file_id, name, mime, size in candidates:
                display = name or "附件"
                if len(attachments) >= ATTACHMENT_MAX_COUNT:
                    notices.append(
                        f"[附件: {display} 已跳过 — 单条消息最多 {ATTACHMENT_MAX_COUNT} 个附件]"
                    )
                    continue
                if size and size > ATTACHMENT_MAX_BYTES:
                    notices.append(f"[附件: {display} 超过 20MB，已跳过]")
                    continue
                try:
                    info = await client.get(
                        f"{self.api_base}/getFile", params={"file_id": file_id}
                    )
                    file_path = (info.json().get("result") or {}).get("file_path", "")
                    if not file_path:
                        notices.append(f"[附件: {display} 获取文件信息失败，已跳过]")
                        continue
                    resp = await client.get(
                        f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
                    )
                    if resp.status_code != 200:
                        notices.append(f"[附件: {display} 下载失败，已跳过]")
                        continue
                    data = resp.content
                    final_name = name or file_path.rsplit("/", 1)[-1] or file_id
                    final_mime = mime or (
                        mimetypes.guess_type(final_name)[0] or "application/octet-stream"
                    )
                    path = save_inbound_attachment(final_name, data)
                    attachments.append(
                        {
                            "name": final_name,
                            "type": final_mime,
                            "size": size or len(data),
                            "path": path,
                        }
                    )
                except Exception:
                    notices.append(f"[附件: {display} 下载失败，已跳过]")
        return attachments, notices

    async def send(self, message: Message) -> None:
        """发送消息到 Telegram."""
        chat_id = message.session_id
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{self.api_base}/sendMessage", json={
                "chat_id": chat_id,
                "text": message.content,
                "parse_mode": "Markdown",
            })

    async def _get_updates(self) -> list[dict]:
        """获取更新 — 长轮询."""
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.get(f"{self.api_base}/getUpdates", params={
                "offset": self._offset,
                "timeout": 30,
            })
            data = resp.json()
            return data.get("result", [])

    async def stop(self):
        """停止监听."""
        self._polling = False

    async def disconnect(self) -> None:
        """断开连接."""
        self._polling = False
        self._connected = False

    async def send_message(self, channel_id: str, content: str, **kwargs) -> bool:
        """发送消息."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{self.api_base}/sendMessage", json={
                    "chat_id": channel_id,
                    "text": content,
                    "parse_mode": "Markdown",
                })
                return resp.status_code == 200
        except Exception:
            return False

    async def send_file(self, channel_id: str, file_path: str, **kwargs) -> bool:
        """发送文件."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                with open(file_path, "rb") as f:
                    resp = await client.post(f"{self.api_base}/sendDocument", data={
                        "chat_id": channel_id,
                    }, files={"document": f})
                return resp.status_code == 200
        except Exception:
            return False

    async def health_check(self) -> dict:
        """健康检查."""
        return {
            "connected": self._connected,
            "polling": self._polling,
            "bot_token": bool(self.bot_token),
        }

    async def start(self) -> None:
        """启动适配器."""
        await self.connect()
