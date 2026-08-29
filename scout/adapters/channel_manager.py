"""多渠道适配器管理器 — 统一管理多个 IM 平台接入.

支持:
- 动态注册/注销渠道
- 渠道健康检查
- 消息路由到 Agent
- 渠道配置持久化
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse
from scout.core.types import Message, Role, Session

logger = logging.getLogger("scout.adapters")


class ChannelManager:
    """渠道管理器 — 管理多个 IM 平台适配器."""

    def __init__(self, config_dir: str | Path | None = None):
        self._adapters: dict[str, ChannelAdapter] = {}
        self._running: dict[str, asyncio.Task] = {}
        self._message_queue: asyncio.Queue[PlatformMessage] = asyncio.Queue()
        self._agent_handler: Any = None  # Agent 处理函数
        self._config_dir = Path(config_dir) if config_dir else Path("data/channels")
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._stats: dict[str, dict] = {}  # 渠道统计

    def register(self, name: str, adapter: ChannelAdapter) -> None:
        """注册渠道适配器."""
        self._adapters[name] = adapter
        self._stats[name] = {
            "messages_in": 0,
            "messages_out": 0,
            "errors": 0,
            "started_at": None,
        }
        logger.info(f"渠道已注册: {name} ({adapter.__class__.__name__})")

    def unregister(self, name: str) -> bool:
        """注销渠道适配器."""
        if name in self._running:
            self._running[name].cancel()
            del self._running[name]
        if name in self._adapters:
            del self._adapters[name]
            logger.info(f"渠道已注销: {name}")
            return True
        return False

    def set_agent_handler(self, handler: Any) -> None:
        """设置 Agent 消息处理函数.

        handler 签名: async (message: PlatformMessage) -> str
        """
        self._agent_handler = handler

    # ── 启动/停止 ──

    async def start_channel(self, name: str) -> bool:
        """启动指定渠道."""
        adapter = self._adapters.get(name)
        if not adapter:
            logger.error(f"渠道不存在: {name}")
            return False

        if name in self._running:
            logger.warning(f"渠道已在运行: {name}")
            return True

        try:
            await adapter.start()
            task = asyncio.create_task(self._run_channel(name, adapter))
            self._running[name] = task
            self._stats[name]["started_at"] = __import__("datetime").datetime.now().isoformat()
            logger.info(f"渠道已启动: {name}")
            return True
        except Exception as e:
            logger.error(f"渠道启动失败 {name}: {e}")
            self._stats[name]["errors"] += 1
            return False

    async def stop_channel(self, name: str) -> bool:
        """停止指定渠道."""
        adapter = self._adapters.get(name)
        task = self._running.get(name)

        if task:
            task.cancel()
            del self._running[name]

        if adapter:
            try:
                await adapter.stop()
            except Exception as e:
                logger.warning(f"渠道停止异常 {name}: {e}")

        logger.info(f"渠道已停止: {name}")
        return True

    async def start_all(self) -> dict[str, bool]:
        """启动所有已注册的渠道."""
        results = {}
        for name in self._adapters:
            results[name] = await self.start_channel(name)
        return results

    async def stop_all(self) -> None:
        """停止所有运行中的渠道."""
        for name in list(self._running.keys()):
            await self.stop_channel(name)

    # ── 消息处理循环 ──

    async def _run_channel(self, name: str, adapter: ChannelAdapter) -> None:
        """单个渠道的消息处理循环."""
        logger.info(f"渠道消息循环开始: {name}")

        try:
            async for message in adapter.listen():
                # 2026-08-20: 兼容老方言适配器（telegram/wechat/feishu），
                # 其 listen() 产出 core.types.Message 而非 PlatformMessage
                if not isinstance(message, PlatformMessage):
                    message = self._legacy_to_platform_message(name, message)
                self._stats[name]["messages_in"] += 1

                try:
                    # 处理消息
                    response = await self._handle_message(name, message)

                    if response:
                        # 回复用户
                        reply = PlatformResponse(
                            success=True,
                            message_id=message.message_id,
                            metadata={"content": response, "reply_to": message.message_id},
                        )
                        await adapter.send_message(message.channel_id, response, reply_to=message.message_id)
                        self._stats[name]["messages_out"] += 1

                except Exception as e:
                    logger.error(f"消息处理失败 [{name}]: {e}")
                    self._stats[name]["errors"] += 1

                    # 发送错误提示
                    try:
                        await adapter.send_message(
                            message.channel_id,
                            "⚠️ 处理消息时出错，请稍后重试。",
                            reply_to=message.message_id,
                        )
                    except Exception:
                        pass

        except asyncio.CancelledError:
            logger.info(f"渠道消息循环取消: {name}")
        except Exception as e:
            logger.error(f"渠道消息循环异常 {name}: {e}")
            self._stats[name]["errors"] += 1

    @staticmethod
    def _legacy_to_platform_message(channel: str, msg: Message) -> PlatformMessage:
        """把老方言适配器产出的 core.types.Message 转换为 PlatformMessage.

        2026-08-20: 老方言（telegram/wechat/feishu）的 listen() 产出
        scout.core.types.Message，其字段语义与 PlatformMessage 不同
        （sender/attachments 为位置字段、群聊 ID 在 metadata），统一转换后再路由。
        """
        meta = getattr(msg, "metadata", None) or {}
        session_id = getattr(msg, "session_id", None)
        return PlatformMessage(
            platform=channel,
            channel_id=str(meta.get("group_id") or meta.get("channel_id") or session_id or channel),
            user_id=str(msg.sender or ""),
            user_name=str(meta.get("sender_name") or ""),
            content=msg.content or "",
            message_id=str(meta.get("message_id") or ""),
            timestamp=0.0,
            attachments=getattr(msg, "attachments", None),
            reply_to=meta.get("reply_to"),
            metadata=dict(meta),
        )

    async def _handle_message(self, channel: str, message: PlatformMessage) -> str:
        """处理来自渠道的消息."""
        if not self._agent_handler:
            return "Agent 未就绪，请稍后重试。"

        # 构建统一的 Message 对象（2026-08-20 补 role/session_id：
        # 此前缺 role 必填字段，渠道消息无法进入 Agent 链路）
        unified_msg = Message(
            role=Role.USER,
            content=message.content,
            sender=message.user_id,
            session_id=message.channel_id,
            source=channel,
            attachments=message.attachments,
            metadata={
                "channel": channel,
                "message_id": message.message_id,
                "reply_to": message.reply_to,
                "sender_name": message.user_name,
                "group_id": message.channel_id,
            },
        )

        # 调用 Agent 处理
        try:
            response = await self._agent_handler(unified_msg)
            return response
        except Exception as e:
            logger.error(f"Agent 处理失败: {e}")
            return f"处理失败: {e}"

    # ── 主动发送 ──

    async def send_to(self, channel: str, target: str, content: str, **kwargs) -> bool:
        """主动向指定渠道的用户/群组发送消息.

        Args:
            channel: 渠道名
            target: 目标用户/群组 ID
            content: 消息内容
            **kwargs: 额外参数（如 reply_to, attachments）
        """
        adapter = self._adapters.get(channel)
        if not adapter:
            logger.error(f"渠道不存在: {channel}")
            return False

        try:
            await adapter.send_message(target, content, **kwargs)
            self._stats[channel]["messages_out"] += 1
            return True
        except Exception as e:
            logger.error(f"发送失败 [{channel}]: {e}")
            self._stats[channel]["errors"] += 1
            return False

    async def broadcast(self, content: str, channels: list[str] | None = None, **kwargs) -> dict[str, bool]:
        """广播消息到多个渠道.

        Args:
            content: 消息内容
            channels: 目标渠道列表（None = 所有渠道）
            **kwargs: 额外参数（需包含 target）
        """
        target_channels = channels or list(self._adapters.keys())
        results = {}

        for ch in target_channels:
            target = kwargs.get("target", "default")
            results[ch] = await self.send_to(ch, target, content, **kwargs)

        return results

    # ── 状态查询 ──

    def list_channels(self) -> list[dict]:
        """列出所有渠道及其状态."""
        result = []
        for name, adapter in self._adapters.items():
            stats = self._stats.get(name, {})
            result.append({
                "name": name,
                "type": adapter.__class__.__name__,
                "running": name in self._running,
                "messages_in": stats.get("messages_in", 0),
                "messages_out": stats.get("messages_out", 0),
                "errors": stats.get("errors", 0),
                "started_at": stats.get("started_at"),
            })
        return result

    def get_adapter(self, name: str) -> ChannelAdapter | None:
        """获取渠道适配器实例."""
        return self._adapters.get(name)

    def get_channel(self, name: str) -> dict | None:
        """获取指定渠道的详细信息."""
        adapter = self._adapters.get(name)
        if not adapter:
            return None

        stats = self._stats.get(name, {})
        return {
            "name": name,
            "type": adapter.__class__.__name__,
            "running": name in self._running,
            "config": adapter.config,
            "stats": stats,
        }

    # ── 配置持久化 ──

    def save_config(self) -> None:
        """保存渠道配置到文件."""
        config = {}
        for name, adapter in self._adapters.items():
            config[name] = {
                "type": adapter.__class__.__name__,
                "config": adapter.config,
                "enabled": name not in self._running or True,
            }

        config_file = self._config_dir / "channels.json"
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    def load_config(self) -> dict:
        """加载渠道配置."""
        config_file = self._config_dir / "channels.json"
        if not config_file.exists():
            return {}

        with open(config_file, encoding="utf-8") as f:
            return json.load(f)

    # ── 工厂方法 ──

    @classmethod
    def from_config(cls, config: dict, config_dir: str | None = None) -> "ChannelManager":
        """从配置创建渠道管理器.

        Args:
            config: 渠道配置字典
            config_dir: 配置目录
        """
        manager = cls(config_dir=config_dir)

        for name, ch_config in config.items():
            adapter_type = ch_config.get("type", "")
            adapter_config = ch_config.get("config", {})

            adapter = cls._create_adapter(adapter_type, adapter_config)
            if adapter:
                manager.register(name, adapter)

        return manager

    @staticmethod
    def _create_adapter(adapter_type: str, config: dict) -> ChannelAdapter | None:
        """根据类型名创建适配器实例."""
        adapter_map = {
            "TelegramAdapter": "scout.adapters.platforms.telegram",
            "WeChatAdapter": "scout.adapters.platforms.wechat",
            "FeishuAdapter": "scout.adapters.platforms.feishu",
            "DiscordAdapter": "scout.adapters.platforms.discord",
            "SlackAdapter": "scout.adapters.platforms.slack",
            "DingTalkAdapter": "scout.adapters.platforms.dingtalk",
            "QQAdapter": "scout.adapters.platforms.qq",
            "WecomBotAdapter": "scout.adapters.platforms.wecom_bot",
            "WechatMPAdapter": "scout.adapters.platforms.wechatmp",
            "WechatComAdapter": "scout.adapters.platforms.wechatcom",
            "WechatKfAdapter": "scout.adapters.platforms.wechat_kf",
            "WeixinAdapter": "scout.adapters.platforms.weixin",
        }

        module_path = adapter_map.get(adapter_type)
        if not module_path:
            logger.warning(f"未知的适配器类型: {adapter_type}")
            return None

        try:
            import importlib
            module = importlib.import_module(module_path)
            adapter_class = getattr(module, adapter_type)
            return adapter_class(config)
        except ImportError as e:
            logger.warning(f"适配器模块导入失败 {adapter_type}: {e}")
            return None
        except AttributeError:
            logger.warning(f"适配器类不存在: {adapter_type}")
            return None
