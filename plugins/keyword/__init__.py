"""Keyword 插件 - 关键词触发"""

from scout.plugins import Plugin
import logging
import json

logger = logging.getLogger(__name__)


class KeywordPlugin(Plugin):
    """检测关键词并触发预定义响应"""
    
    name = "keyword"
    version = "1.0.0"
    author = "Scout Team"
    description = "检测消息中的关键词并返回预定义的响应"
    priority = 90
    
    async def on_load(self) -> None:
        """加载关键词配置"""
        # 默认关键词配置
        default_keywords = {
            "帮助": "你可以问我任何问题！试试说'你能做什么'。",
            "你是谁": "我是 Scout，一个智能助手，可以帮你解答问题、完成任务。",
            "版本": "Scout Agent v1.0.0",
        }
        
        # 尝试从配置加载
        try:
            config_path = self.data_dir / "keywords.json"
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    self.keywords = json.load(f)
                logger.info(f"从配置加载了 {len(self.keywords)} 个关键词")
            else:
                self.keywords = default_keywords
                # 保存默认配置
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(self.keywords, f, ensure_ascii=False, indent=2)
                logger.info(f"已创建默认关键词配置")
        except Exception as e:
            logger.error(f"加载关键词配置失败: {e}")
            self.keywords = default_keywords
    
    async def on_event(self, event) -> bool:
        """通用事件处理器 — 检测关键词并直接回复"""
        from scout.plugins import EventType
        
        if event.event_type == EventType.BEFORE_CHAT:
            message = event.data.get("message", "").strip().lower()
            
            for keyword, response in self.keywords.items():
                if keyword in message:
                    logger.debug(f"检测到关键词 '{keyword}'")
                    event.data["direct_response"] = response
                    event.stop_propagation = True
                    return True
        
        return False
    
    async def on_unload(self) -> None:
        """卸载时清理"""
        logger.info("Keyword 插件已卸载")


__all__ = ["KeywordPlugin"]
