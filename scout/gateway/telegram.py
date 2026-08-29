"""Telegram 网关 — 将 Scout Agent 接入 Telegram."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters


class TelegramGateway:
    """Telegram Bot 适配器.

    负责处理来自 Telegram 的消息并将其转发给 Agent，同时将 Agent 的回复推送到 Telegram。
    """

    def __init__(self, agent_instance: Any):
        self.agent = agent_instance
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not self.token:
            raise ValueError("请设置环境变量 TELEGRAM_BOT_TOKEN")
        
        self.app = Application.builder().token(self.token).build()
        self._setup_handlers()

    def _setup_handlers(self):
        """注册消息处理器."""
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start(self, update: Update, context):
        """处理 /start 命令."""
        await update.message.reply_text(
            "👋 你好！我是 Scout Agent。\n"
            "你可以向我提问、让我执行任务或管理你的知识库。"
        )

    async def handle_message(self, update: Update, context):
        """处理普通文本消息."""
        user_id = update.effective_user.id
        message = update.message.text
        
        # 发送“正在思考”状态
        thinking_msg = await update.message.reply_text("🤔 正在思考...")
        
        try:
            # 调用 Agent：run_conversation 签名为 (user_message: str, session: Session)
            from scout.core.types import Session

            session = Session(id=f"tg-{user_id}")
            result = await self.agent.run_conversation(message, session)
            response_text = result.get("response", "抱歉，我遇到了一些问题。")

            # 编辑消息为最终回复
            await thinking_msg.edit_text(response_text)
        except Exception as e:
            await thinking_msg.edit_text(f"❌ 发生错误: {str(e)}")

    def run(self):
        """启动 Bot."""
        print(f"🚀 Telegram Bot 已启动 (ID: {self.app.bot.id})")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
