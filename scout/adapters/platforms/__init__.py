"""Scout Agent 平台适配器."""

from scout.adapters.platforms.base import ChannelAdapter, PlatformMessage, PlatformResponse
from scout.adapters.platforms.telegram import TelegramAdapter
from scout.adapters.platforms.wechat import WeChatAdapter
from scout.adapters.platforms.feishu import FeishuAdapter
from scout.adapters.platforms.dingtalk import DingTalkAdapter
from scout.adapters.platforms.discord import DiscordAdapter
from scout.adapters.platforms.slack import SlackAdapter
from scout.adapters.platforms.qq import QQAdapter
from scout.adapters.platforms.wecom_bot import WecomBotAdapter
from scout.adapters.platforms.wechatmp import WechatMPAdapter
from scout.adapters.platforms.wechatcom import WechatComAdapter
from scout.adapters.platforms.wechat_kf import WechatKfAdapter
from scout.adapters.platforms.weixin import WeixinAdapter

__all__ = [
    "ChannelAdapter",
    "PlatformMessage",
    "PlatformResponse",
    "TelegramAdapter",
    "WeChatAdapter",
    "FeishuAdapter",
    "DingTalkAdapter",
    "DiscordAdapter",
    "SlackAdapter",
    "QQAdapter",
    "WecomBotAdapter",
    "WechatMPAdapter",
    "WechatComAdapter",
    "WechatKfAdapter",
    "WeixinAdapter",
]
