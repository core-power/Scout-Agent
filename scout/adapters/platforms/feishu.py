"""飞书适配器 — 通过飞书开放平台 Bot Webhook 接入."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from scout.adapters.platforms.base import ChannelAdapter
from scout.core.types import Message, Role


class FeishuAdapter(ChannelAdapter):
    """飞书 Bot 适配器."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.verification_token = config.get("verification_token", "")
        self.webhook_url = config.get("webhook_url", "/feishu/webhook")
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._connected = bool(self.app_id and self.app_secret)
        self._tenant_access_token: str = ""

    async def receive_webhook(self, data: dict) -> dict:
        """接收飞书事件回调."""
        # URL 验证挑战
        if data.get("type") == "url_verification":
            return {"challenge": data.get("challenge", "")}

        # 验证 token
        header = data.get("header", {})
        token = header.get("token", "")
        if self.verification_token and token != self.verification_token:
            return {"error": "invalid token"}

        # 处理消息事件
        event = data.get("event", {})
        if event.get("message_type") == "text":
            content = json.loads(event.get("content", "{}"))
            text = content.get("text", "")
            sender = event.get("sender", {}).get("sender_id", {}).get("open_id", "")

            if text:
                await self._message_queue.put(Message(
                    role=Role.USER,
                    content=text,
                    sender=sender,
                    session_id=sender,
                    source="feishu",
                ))

        return {"code": 0}

    async def listen(self) -> AsyncIterator[Message]:
        """从消息队列读取."""
        while True:
            msg = await self._message_queue.get()
            yield msg

    async def send(self, message: Message) -> None:
        """发送消息到飞书."""
        target = message.session_id or message.sender
        if target and message.content:
            await self.send_message(target, message.content)

    async def _get_tenant_token(self) -> None:
        """获取 tenant_access_token."""
        if not self.app_id or not self.app_secret:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                )
                data = resp.json()
                self._tenant_access_token = data.get("tenant_access_token", "")
        except Exception:
            pass

    async def connect(self) -> bool:
        """连接飞书."""
        try:
            await self._get_tenant_token()
            self._connected = bool(self._tenant_access_token)
            return self._connected
        except Exception:
            return False

    async def disconnect(self) -> None:
        """断开连接."""
        self._connected = False
        self._tenant_access_token = ""

    async def send_message(self, channel_id: str, content: str, **kwargs) -> bool:
        """发送消息到飞书."""
        try:
            if not self._tenant_access_token:
                await self._get_tenant_token()
            
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                    headers={
                        "Authorization": f"Bearer {self._tenant_access_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json={
                        "receive_id": channel_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": content}),
                    },
                )
                return resp.status_code == 200
        except Exception:
            return False

    async def send_file(self, channel_id: str, file_path: str, **kwargs) -> bool:
        """发送文件 — 先上传到飞书拿 file_key，再发送 file 消息.

        参考 CowAgent feishu_channel：POST /open-apis/im/v1/files 上传，
        用返回的 file_key 发送 file 类型消息。
        """
        try:
            import os

            if not self._tenant_access_token:
                await self._get_tenant_token()
            if not self._tenant_access_token:
                return False
            if not os.path.exists(file_path):
                self.config["_last_error"] = f"文件不存在: {file_path}"
                return False

            file_name = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                file_bytes = f.read()

            async with httpx.AsyncClient(timeout=60) as client:
                headers = {"Authorization": f"Bearer {self._tenant_access_token}"}
                # 1. 上传文件
                up_resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/files",
                    headers=headers,
                    data={"file_type": "stream", "file_name": file_name},
                    files={"file": (file_name, file_bytes)},
                )
                up_data = up_resp.json()
                file_key = up_data.get("data", {}).get("file_key", "")
                if not file_key:
                    self.config["_last_error"] = f"飞书文件上传失败: {up_data}"
                    return False
                # 2. 发送文件消息
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                    headers={**headers, "Content-Type": "application/json; charset=utf-8"},
                    json={
                        "receive_id": channel_id,
                        "msg_type": "file",
                        "content": json.dumps({"file_key": file_key}),
                    },
                )
                return resp.status_code == 200
        except Exception as e:
            self.config["_last_error"] = f"飞书发送文件异常: {e}"
            return False

    async def health_check(self) -> dict:
        """健康检查."""
        return {
            "connected": self._connected,
            "platform": "feishu",
            "has_token": bool(self._tenant_access_token),
        }

    async def start(self) -> None:
        """启动适配器."""
        await self._get_tenant_token()
        self._connected = True

    def get_routes(self) -> list[dict]:
        return [
            {
                "method": "POST",
                "path": self.webhook_url,
                "handler": self.receive_webhook,
            },
        ]
