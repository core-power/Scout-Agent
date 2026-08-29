"""
ASR 基类 - 定义语音识别接口
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class ASRBase(ABC):
    """语音识别基类"""
    
    def __init__(self, model_name: str = "default", **kwargs):
        """
        初始化 ASR
        
        Args:
            model_name: 模型名称
            **kwargs: 其他配置参数
        """
        self.model_name = model_name
        self.config = kwargs
    
    @abstractmethod
    async def transcribe(
        self,
        audio_path: str | Path,
        language: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        将音频文件转录为文本
        
        Args:
            audio_path: 音频文件路径
            language: 语言代码（如 "zh", "en"），None 表示自动检测
            **kwargs: 其他参数
        
        Returns:
            转录的文本
        
        Raises:
            FileNotFoundError: 音频文件不存在
            ValueError: 音频格式不支持
            RuntimeError: 转录失败
        """
        pass
    
    @abstractmethod
    async def transcribe_stream(
        self,
        audio_stream,
        language: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        从音频流转录文本
        
        Args:
            audio_stream: 音频流对象
            language: 语言代码
            **kwargs: 其他参数
        
        Returns:
            转录的文本
        """
        pass
    
    def validate_audio(self, audio_path: str | Path) -> bool:
        """
        验证音频文件格式
        
        Args:
            audio_path: 音频文件路径
        
        Returns:
            True 如果格式支持，否则 False
        """
        audio_path = Path(audio_path)
        
        if not audio_path.exists():
            logger.error(f"音频文件不存在: {audio_path}")
            return False
        
        # 支持的格式
        supported_formats = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm"}
        if audio_path.suffix.lower() not in supported_formats:
            logger.error(f"不支持的音频格式: {audio_path.suffix}")
            return False
        
        return True
    
    def get_supported_formats(self) -> list[str]:
        """获取支持的音频格式列表"""
        return [".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm"]
    
    def get_supported_languages(self) -> list[str]:
        """获取支持的语言列表（ISO 639-1 代码）"""
        # 默认支持常见语言，子类可以覆盖
        return [
            "zh",  # 中文
            "en",  # 英语
            "ja",  # 日语
            "ko",  # 韩语
            "fr",  # 法语
            "de",  # 德语
            "es",  # 西班牙语
            "it",  # 意大利语
            "pt",  # 葡萄牙语
            "ru",  # 俄语
        ]
