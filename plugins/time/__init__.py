"""
时间查询插件 - 示例插件

当用户询问时间或日期时，自动回复当前时间
"""

from scout.plugins import Plugin, EventType
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class TimePlugin(Plugin):
    name = "time"
    version = "1.0.0"
    author = "Scout Team"
    description = "当用户询问时间或日期时自动回复"
    priority = 85  # 较高优先级
    
    # 触发关键词
    keywords = ["时间", "几点", "日期", "今天", "星期", "time", "date"]
    
    async def on_event(self, event):
        """处理事件"""
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").lower()
            
            # 检查是否包含时间相关关键词
            for keyword in self.keywords:
                if keyword in message:
                    response = self._get_time_response(keyword)
                    if response:
                        logger.info(f"触发时间查询: {keyword}")
                        event.data["direct_response"] = response
                        event.stop_propagation = True
                        return True
        
        return False
    
    def _get_time_response(self, keyword):
        """生成时间响应"""
        now = datetime.now()
        
        if keyword in ["时间", "几点", "time"]:
            return f"当前时间是：{now.strftime('%H:%M:%S')}"
        
        elif keyword in ["日期", "今天", "date"]:
            return f"今天是：{now.strftime('%Y年%m月%d日')}"
        
        elif keyword == "星期":
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            return f"今天是：{weekdays[now.weekday()]}"
        
        return None
