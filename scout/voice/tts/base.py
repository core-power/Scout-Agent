"""
TTS 基类 - 定义语音合成接口
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, AsyncIterator
import logging

logger = logging.getLogger(__name__)


class TTSBase(ABC):
    """语音合成基类"""
    
    def __init__(self, model_name: str = "default", **kwargs):
        """
        初始化 TTS
        
        Args:
            model_name: 模型名称
            **kwargs: 其他配置参数
        """
        self.model_name = model_name
        self.config = kwargs
    
    @abstractmethod
    async def synthesize(
        self,
        text: str,
        output_path: str | Path,
        voice: Optional[str] = None,
        **kwargs
    ) -> Path:
        """
        将文本合成为音频文件
        
        Args:
            text: 要合成的文本
            output_path: 输出音频文件路径
            voice: 语音角色
            **kwargs: 其他参数
        
        Returns:
            生成的音频文件路径
        
        Raises:
            RuntimeError: 合成失败
        """
        pass
    
    @abstractmethod
    async def synthesize_stream(
        self,
        text: str,
        voice: Optional[str] = None,
        **kwargs
    ) -> AsyncIterator[bytes]:
        """
        流式合成音频
        
        Args:
            text: 要合成的文本
            voice: 语音角色
            **kwargs: 其他参数
        
        Yields:
            音频数据块
        """
        pass
    
    def get_supported_voices(self) -> list[str]:
        """获取支持的语音角色列表"""
        # 默认实现，子类可以覆盖
        return ["default"]
    
    def get_supported_formats(self) -> list[str]:
        """获取支持的输出格式列表"""
        return [".mp3", ".wav", ".opus", ".ogg"]
    
    def validate_text(self, text: str) -> bool:
        """
        验证文本是否有效
        
        Args:
            text: 要验证的文本
        
        Returns:
            True 如果有效，否则 False
        """
        if not text or not text.strip():
            logger.error("文本为空")
            return False
        
        # 检查长度限制（大多数 TTS 服务有字符数限制）
        max_length = self.config.get("max_text_length", 4096)
        if len(text) > max_length:
            logger.error(f"文本过长: {len(text)} 字符 (限制 {max_length})")
            return False
        
        return True
