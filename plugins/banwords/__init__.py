"""Banwords 插件 - 敏感词过滤"""

from scout.plugins import Plugin
import logging
import json
import re

logger = logging.getLogger(__name__)


class BanwordsPlugin(Plugin):
    """过滤敏感词，替换为星号"""
    
    name = "banwords"
    version = "1.0.0"
    author = "Scout Team"
    description = "自动过滤消息中的敏感词，替换为星号"
    priority = 95  # 高优先级，在关键词之前就处理
    enabled = False  # 默认禁用，需要手动启用
    
    async def on_load(self) -> None:
        """加载敏感词列表"""
        # 默认敏感词（示例）
        default_banwords = [
            "脏话1", "脏话2", "脏话3"  # 示例，实际使用时替换
        ]
        
        # 尝试从配置加载
        try:
            config_path = self.data_dir / "banwords.json"
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.banwords = config.get("words", default_banwords)
                logger.info(f"从配置加载了 {len(self.banwords)} 个敏感词")
            else:
                self.banwords = default_banwords
                # 保存默认配置
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump({"words": self.banwords}, f, ensure_ascii=False, indent=2)
                logger.info(f"已创建默认敏感词配置")
        except Exception as e:
            logger.error(f"加载敏感词配置失败: {e}")
            self.banwords = default_banwords
        
        # 构建正则表达式（用于高效匹配）
        if self.banwords:
            pattern = '|'.join(re.escape(word) for word in self.banwords)
            self.regex = re.compile(pattern, re.IGNORECASE)
        else:
            self.regex = None
    
    def _filter_message(self, message: str) -> str:
        """过滤消息中的敏感词"""
        if not self.regex:
            return message
        
        def replace(match):
            word = match.group(0)
            return '*' * len(word)
        
        return self.regex.sub(replace, message)
    
    async def before_chat(self, message: str, session_id: str) -> str | None:
        """过滤用户消息中的敏感词"""
        filtered = self._filter_message(message)
        if filtered != message:
            logger.debug(f"已过滤敏感词")
            return filtered
        return None
    
    async def after_chat(self, message: str, response: str, session_id: str) -> str | None:
        """过滤助手回复中的敏感词"""
        filtered = self._filter_message(response)
        if filtered != response:
            logger.debug(f"已过滤回复中的敏感词")
            return filtered
        return None
    
    async def on_unload(self) -> None:
        """卸载时清理"""
        logger.info("Banwords 插件已卸载")


__all__ = ["BanwordsPlugin"]
