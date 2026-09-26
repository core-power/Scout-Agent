"""渠道与平台 Webhook 路由组（/api/channels/* + 飞书/企微/微信/QQ webhook）.

W2 拆分（2026-09-14）：自 adapter.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, Response
from scout.core.types import Message, Role, Session

# logger 归一：保持与原 web.py 相同的日志器名（行为不变）
import logging

logger = logging.getLogger("scout.adapters.web")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scout.adapters.web.adapter import WebAdapter


class ChannelRoutes:
    """渠道与平台 Webhook 路由组（/api/channels/* + 飞书/企微/微信/QQ webhook）（mixin）."""

    def _setup_channel_routes(self):
        """渠道管理 API."""

        # ── 渠道管理 API ──

        @self.app.get("/api/channels")
        async def list_channels():
            """列出所有渠道."""
            channels = self._channel_manager.list_channels()
            return {"channels": channels}

        @self.app.post("/api/channels")
        async def add_channel(req: Request):
            """添加渠道."""
            from scout.adapters.platforms.feishu import FeishuAdapter
            from scout.adapters.platforms.wechat import WeChatAdapter
            from scout.adapters.platforms.telegram import TelegramAdapter
            from scout.adapters.platforms.dingtalk import DingTalkAdapter
            from scout.adapters.platforms.discord import DiscordAdapter
            from scout.adapters.platforms.slack import SlackAdapter
            from scout.adapters.platforms.qq import QQAdapter
            from scout.adapters.platforms.wecom_bot import WecomBotAdapter
            from scout.adapters.platforms.wechatmp import WechatMPAdapter
            from scout.adapters.platforms.wechatcom import WechatComAdapter
            from scout.adapters.platforms.wechat_kf import WechatKfAdapter
            from scout.adapters.platforms.weixin import WeixinAdapter

            data = await req.json()
            name = data.get("name")
            channel_type = data.get("type")
            config = data.get("config", {})

            if not name or not channel_type:
                return JSONResponse({"error": "缺少必要参数"}, status_code=400)

            # 根据类型创建适配器
            adapter_map = {
                "feishu": FeishuAdapter,
                "wechat": WeChatAdapter,
                "telegram": TelegramAdapter,
                "dingtalk": DingTalkAdapter,
                "discord": DiscordAdapter,
                "slack": SlackAdapter,
                "qq": QQAdapter,
                "wecom_bot": WecomBotAdapter,
                "wechatmp": WechatMPAdapter,
                "wechatcom": WechatComAdapter,
                "wechat_kf": WechatKfAdapter,
                "weixin": WeixinAdapter,
            }

            adapter_class = adapter_map.get(channel_type)
            if not adapter_class:
                return JSONResponse({"error": f"不支持的渠道类型: {channel_type}"}, status_code=400)

            try:
                # 将 name 添加到 config 中
                config["name"] = name
                adapter = adapter_class(config)
                self._channel_manager.register(name, adapter)
                self._channel_manager.save_config()
                return {"status": "ok", "channel": name}
            except Exception as e:
                logger.error(f"添加渠道失败: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        @self.app.delete("/api/channels/{name}")
        async def delete_channel(name: str):
            """删除渠道."""
            if self._channel_manager.unregister(name):
                self._channel_manager.save_config()
                return {"status": "ok", "channel": name}
            else:
                return JSONResponse({"error": f"渠道不存在: {name}"}, status_code=404)

        @self.app.post("/api/channels/{name}/start")
        async def start_channel(name: str):
            """启动渠道."""
            # 设置 Agent 处理函数
            if self._agent:
                async def handle_message(message):
                    import copy
                    from scout.adapters.channel_callbacks import ChannelCallbacks
                    from scout.session.store import get_session_store
                    from scout.tools.registry import ToolRegistry

                    sid = f"channel_{name}_{message.sender}"
                    # 载入持久会话（IM 多轮上下文；澄清「问完等下一条消息作答」依赖它）；
                    # 无则新建。此前每轮都用全新空 Session → IM 多轮无上下文。
                    try:
                        session = get_session_store().load_session(sid)
                    except Exception:
                        session = None
                    if session is None:
                        session = Session(id=sid)

                    # 每条消息用 agent 浅拷贝挂 IM 专属回调（race-free，对齐 ws.py 模式）：
                    # ask_user 澄清发到渠道、危险操作默认拒绝（IM 无法可靠交互批准）。
                    channel_id = message.session_id or name
                    adapter = self._channel_manager.get_adapter(name)

                    async def _send(cid, text, **kw):
                        if adapter is None:
                            return False
                        return await adapter.send_message(cid, text, **kw)

                    agent_copy = copy.copy(self._agent)
                    agent_copy.callbacks = ChannelCallbacks(
                        _send,
                        channel_id=channel_id,
                        user_id=message.sender or "",
                        reply_to=(message.metadata or {}).get("message_id"),
                    )
                    ToolRegistry._main_agent = agent_copy

                    # ★ 修复：Agent 没有 .chat 方法（此前每条 IM 消息都 AttributeError →
                    #   被 _handle_message 吞成「处理失败」，IM 对话实际不可用）。
                    #   正确入口 run_conversation(content, session) -> {"response", "session", ...}。
                    result = await agent_copy.run_conversation(
                        message.content, session, attachments=message.attachments
                    )
                    # 持久化会话，供下一条消息（含澄清回答）续上下文
                    try:
                        get_session_store().save_session((result or {}).get("session") or session)
                    except Exception:
                        pass
                    return (result or {}).get("response", "") or ""

                self._channel_manager.set_agent_handler(handle_message)

            success = await self._channel_manager.start_channel(name)
            if success:
                return {"status": "ok", "channel": name, "running": True}
            else:
                return JSONResponse({"error": "启动失败"}, status_code=500)

        @self.app.post("/api/channels/{name}/stop")
        async def stop_channel(name: str):
            """停止渠道."""
            success = await self._channel_manager.stop_channel(name)
            if success:
                return {"status": "ok", "channel": name, "running": False}
            else:
                return JSONResponse({"error": "停止失败"}, status_code=500)

        # ── 平台 Webhook 回调 ──
        # 微信/公众号/企业微信/飞书/QQ 的服务器回调统一在这里挂载，
        # 回调收到的消息经适配器解析后入队，由 ChannelManager 消费并交由 Agent 处理。
        # 2026-08-27: 此前平台回调从未挂载，webhook 模式（receive_webhook）形同虚设。

        @self.app.get("/wechatmp/webhook")
        async def wechatmp_webhook_verify(request: Request):
            adapter = self._channel_manager.get_adapter("wechatmp")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatmp 渠道未注册"}, status_code=404)
            result = await adapter.receive_webhook(dict(request.query_params), b"")
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/wechatmp/webhook")
        async def wechatmp_webhook_post(request: Request):
            adapter = self._channel_manager.get_adapter("wechatmp")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatmp 渠道未注册"}, status_code=404)
            body = await request.body()
            result = await adapter.receive_webhook(dict(request.query_params), body)
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.get("/wechatcom/webhook")
        async def wechatcom_webhook_verify(request: Request):
            adapter = self._channel_manager.get_adapter("wechatcom")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatcom 渠道未注册"}, status_code=404)
            result = await adapter.receive_webhook(dict(request.query_params), b"")
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/wechatcom/webhook")
        async def wechatcom_webhook_post(request: Request):
            adapter = self._channel_manager.get_adapter("wechatcom")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechatcom 渠道未注册"}, status_code=404)
            body = await request.body()
            result = await adapter.receive_webhook(dict(request.query_params), body)
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.get("/wechat/webhook")
        async def wechat_webhook_verify(request: Request):
            adapter = self._channel_manager.get_adapter("wechat")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechat 渠道未注册"}, status_code=404)
            result = await adapter.receive_webhook(dict(request.query_params), b"")
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/wechat/webhook")
        async def wechat_webhook_post(request: Request):
            adapter = self._channel_manager.get_adapter("wechat")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "wechat 渠道未注册"}, status_code=404)
            body = await request.body()
            result = await adapter.receive_webhook(dict(request.query_params), body)
            return Response(content=result.get("body", ""), media_type=result.get("content_type", "text/plain"))

        @self.app.post("/feishu/webhook")
        async def feishu_webhook(request: Request):
            adapter = self._channel_manager.get_adapter("feishu")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "feishu 渠道未注册"}, status_code=404)
            body = await request.json()
            result = await adapter.receive_webhook(body)
            return JSONResponse(result)

        @self.app.post("/qq/webhook")
        async def qq_webhook(request: Request):
            adapter = self._channel_manager.get_adapter("qq")
            if not adapter or not hasattr(adapter, "receive_webhook"):
                return JSONResponse({"error": "qq 渠道未注册"}, status_code=404)
            if hasattr(adapter, "_verify_webhook") and not adapter._verify_webhook(dict(request.headers), await request.body()):
                return JSONResponse({"error": "signature check failed"}, status_code=403)
            body = await request.json()
            result = await adapter.receive_webhook(body)
            return JSONResponse(result)
