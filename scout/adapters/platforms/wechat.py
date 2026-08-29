"""微信适配器 — 通过 Webhook 接入（微信公众平台）.

接入方式：微信公众平台 → 服务器配置 → Webhook 接收消息；
发送使用客服消息接口（需 app_id / app_secret，参考 CowAgent 公众号实现）。

2026-08-21 修复：此前 send/send_message/send_file 均为 TODO 占位
（直接返回 True，实际不发送），现实现为真实微信客服消息 API 调用。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import httpx

from scout.adapters.platforms.base import ChannelAdapter
from scout.core.types import Message, Role

# 微信 access_token 有效期（秒）
_TOKEN_TTL = 7000


class WeChatAdapter(ChannelAdapter):
    """微信适配器 — 通过 Webhook 接入."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.webhook_url = config.get("webhook_url", "/wechat/webhook")
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._connected = True  # Webhook 模式始终连接
        self._access_token: str = ""
        self._token_expire_at: float = 0.0

    async def connect(self) -> bool:
        """连接微信."""
        # Webhook 模式始终连接
        self._connected = True
        return True

    async def receive_webhook(self, data: dict) -> dict:
        """接收 Webhook 推送 — 供 FastAPI 路由调用.

        Args:
            data: 微信消息数据

        Returns:
            响应给微信服务器的 JSON
        """
        # 微信服务器配置校验（首次接入时）
        if data.get("echostr"):
            return {"echostr": data.get("echostr")}

        # 解析消息
        msg_type = data.get("MsgType", "text")
        content = data.get("Content", "")
        from_user = data.get("FromUserName", "")

        if msg_type == "text" and content:
            await self._message_queue.put(Message(
                role=Role.USER,
                content=content,
                sender=from_user,
                session_id=from_user,
                source="wechat",
            ))

        # 被动回复（需要在 5 秒内返回，超时后微信服务器重试）
        if from_user and content:
            reply = self._build_reply(from_user, content)
            if reply:
                return reply

        return {"msg": "ok"}

    def _build_reply(self, to_user: str, content: str) -> dict:
        """构造微信被动回复 XML（文本消息）."""
        import time as _time
        timestamp = int(_time.time())
        return {
            "ToUserName": to_user,
            "FromUserName": self.config.get("bot_id", ""),
            "CreateTime": timestamp,
            "MsgType": "text",
            "Content": content,
        }

    async def listen(self) -> AsyncIterator[Message]:
        """从消息队列读取消息."""
        while True:
            msg = await self._message_queue.get()
            yield msg

    async def _get_access_token(self) -> str:
        """获取微信 access_token（带缓存）."""
        import time as _time
        now = _time.time()
        if self._access_token and now < self._token_expire_at:
            return self._access_token
        if not self.app_id or not self.app_secret:
            return ""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.weixin.qq.com/cgi-bin/token",
                    params={
                        "grant_type": "client_credential",
                        "appid": self.app_id,
                        "secret": self.app_secret,
                    },
                )
                data = resp.json()
                token = data.get("access_token", "")
                expires_in = data.get("expires_in", 7200)
                if token:
                    self._access_token = token
                    self._token_expire_at = now + min(expires_in, _TOKEN_TTL)
                return token
        except Exception as e:
            self.config["_last_error"] = f"获取 access_token 失败: {e}"
            return ""

    async def send(self, message: Message) -> None:
        """发送消息 — 通过客服消息接口回复."""
        target = message.session_id or message.sender
        if target and message.content:
            await self.send_message(target, message.content)

    async def disconnect(self) -> None:
        """断开连接."""
        self._connected = False
        self._access_token = ""

    async def send_message(self, channel_id: str, content: str, **kwargs) -> bool:
        """发送文本消息（客服消息接口）."""
        try:
            token = await self._get_access_token()
            if not token:
                self.config["_last_error"] = "未配置 app_id/app_secret 或获取 token 失败"
                return False
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://api.weixin.qq.com/cgi-bin/message/custom/send",
                    params={"access_token": token},
                    json={
                        "touser": channel_id,
                        "msgtype": "text",
                        "text": {"content": content},
                    },
                )
                data = resp.json()
                if data.get("errcode") not in (0, None):
                    self.config["_last_error"] = f"微信发送失败: {data}"
                    return False
                return True
        except Exception as e:
            self.config["_last_error"] = f"微信发送异常: {e}"
            return False

    async def send_file(self, channel_id: str, file_path: str, **kwargs) -> bool:
        """发送文件 — 先上传素材再发图片消息."""
        try:
            token = await self._get_access_token()
            if not token:
                self.config["_last_error"] = "未配置 app_id/app_secret 或获取 token 失败"
                return False
            if not os.path.exists(file_path):
                self.config["_last_error"] = f"文件不存在: {file_path}"
                return False
            with open(file_path, "rb") as f:
                file_data = f.read()
            async with httpx.AsyncClient(timeout=30) as client:
                # 1. 上传临时素材（media/upload），type 按扩展名推断
                media_type = "image"
                ext = os.path.splitext(file_path)[1].lower()
                if ext in (".mp4", ".avi", ".mov"):
                    media_type = "video"
                elif ext in (".mp3", ".wav", ".amr"):
                    media_type = "voice"
                upload = await client.post(
                    "https://api.weixin.qq.com/cgi-bin/media/upload",
                    params={"access_token": token, "type": media_type},
                    files={"media": (os.path.basename(file_path), file_data)},
                )
                up_data = upload.json()
                media_id = up_data.get("media_id")
                if not media_id:
                    self.config["_last_error"] = f"微信素材上传失败: {up_data}"
                    return False
                # 2. 发送 media 消息
                msgtype_map = {"video": "video", "voice": "voice"}
                msgtype = msgtype_map.get(media_type, "image")
                msg_body = {"media_id": media_id}
                if msgtype == "video":
                    msg_body = {"media_id": media_id, "title": os.path.basename(file_path), "description": ""}
                resp = await client.post(
                    "https://api.weixin.qq.com/cgi-bin/message/custom/send",
                    params={"access_token": token},
                    json={"touser": channel_id, "msgtype": msgtype, msgtype: msg_body},
                )
                data = resp.json()
                if data.get("errcode") not in (0, None):
                    self.config["_last_error"] = f"微信发送文件失败: {data}"
                    return False
                return True
        except Exception as e:
            self.config["_last_error"] = f"微信发送文件异常: {e}"
            return False

    async def health_check(self) -> dict:
        """健康检查."""
        has_creds = bool(self.app_id and self.app_secret)
        return {
            "connected": self._connected,
            "platform": "wechat",
            "can_send": has_creds,
            "last_error": self.config.get("_last_error"),
        }

    async def start(self) -> None:
        """启动适配器."""
        self._connected = True
