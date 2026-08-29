"""Hello 插件 - 问候用户"""

from scout.plugins import Plugin
import logging

logger = logging.getLogger(__name__)


class HelloPlugin(Plugin):
    """当用户打招呼时回复问候"""
    
    name = "hello"
    version = "1.0.0"
    author = "Scout Team"
    description = "当用户打招呼时回复友好的问候"
    priority = 100
    
    async def on_load(self) -> None:
        """插件加载时调用"""
        logger.info("Hello 插件已加载")
    
    async def on_unload(self) -> None:
        """插件卸载时调用"""
        logger.info("Hello 插件已卸载")
    
    async def on_event(self, event) -> bool:
        """通用事件处理器 — 拦截问候语并直接回复"""
        from scout.plugins import EventType
        
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").strip().lower()
            greetings = ["你好", "hello", "hi", "嗨", "您好", "hey"]
            
            if message in greetings:
                # 设置直接响应，绕过 AI
                event.data["direct_response"] = self._get_greeting()
                event.stop_propagation = True
                logger.debug(f"检测到问候语，直接回复")
                return True
        
        return False
    
    def _get_greeting(self) -> str:
        """根据时间生成问候语"""
        from datetime import datetime
        
        hour = datetime.now().hour
        
        if hour < 6:
            return "夜深了，注意休息！有什么我可以帮助你的吗？ 🌙"
        elif hour < 12:
            return "早上好！很高兴见到你，有什么我可以帮助你的吗？ ☀️"
        elif hour < 14:
            return "中午好！吃过午饭了吗？有什么我可以帮助你的吗？ 🍜"
        elif hour < 18:
            return "下午好！有什么我可以帮助你的吗？ 🌤️"
        else:
            return "晚上好！有什么我可以帮助你的吗？ 🌆"


# 导出插件类（插件管理器会查找这个）
__all__ = ["HelloPlugin"]

