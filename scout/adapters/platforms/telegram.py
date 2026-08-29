"""Telegram 适配器 — 通过 Telegram Bot API 接入."""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx

from scout.adapters.platforms.base import ChannelAdapter
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
                        yield Message(
                            role=Role.USER,
                            content=text,
                            sender=str(msg["from"]["id"]),
                            session_id=str(msg["chat"]["id"]),
                            source="telegram",
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5)

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
