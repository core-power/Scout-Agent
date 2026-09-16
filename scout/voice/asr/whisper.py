"""
Whisper ASR - 基于 OpenAI Whisper 本地模型的语音识别
"""

import asyncio
from pathlib import Path
from typing import Optional
import logging

from scout.voice.asr.base import ASRBase

logger = logging.getLogger(__name__)


class WhisperASR(ASRBase):
    """
    基于 Whisper 的本地语音识别
    
    支持多种模型大小：
    - tiny: 最快，准确率较低
    - base: 平衡速度和准确率
    - small: 较高准确率
    - medium: 高准确率
    - large: 最高准确率，但最慢
    """
    
    def __init__(
        self,
        model_name: str = "base",
        device: str = "auto",
        compute_type: str = "float32",
        **kwargs
    ):
        """
        初始化 Whisper ASR
        
        Args:
            model_name: 模型名称 (tiny/base/small/medium/large)
            device: 计算设备 (cpu/cuda/auto)
            compute_type: 计算类型 (float32/float16/int8)
            **kwargs: 其他参数
        """
        super().__init__(model_name=model_name, **kwargs)
        self.device = device
        self.compute_type = compute_type
        self._model = None
    
    def _load_model(self):
        """加载 Whisper 模型"""
        if self._model is not None:
            return
        
        try:
            import whisper
        except ImportError:
            raise ImportError(
                "Whisper 未安装。请运行: pip install openai-whisper"
            )
        
        logger.info(f"加载 Whisper 模型: {self.model_name}")
        
        # 自动选择设备
        device = self.device
        if device == "auto":
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        
        self._model = whisper.load_model(
            self.model_name,
            device=device,
        )
        
        logger.info(f"Whisper 模型加载完成 (设备: {device})")
    
    async def transcribe(
        self,
        audio_path: str | Path,
        language: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        转录音频文件
        
        Args:
            audio_path: 音频文件路径
            language: 语言代码（如 "zh", "en"）
            **kwargs: Whisper 的其他参数
        
        Returns:
            转录的文本
        """
        audio_path = Path(audio_path)
        
        if not self.validate_audio(audio_path):
            raise FileNotFoundError(f"音频文件无效: {audio_path}")
        
        # 加载模型（首次调用时）
        self._load_model()
        
        logger.info(f"开始转录: {audio_path}")
        
        # 在线程池中执行（避免阻塞异步事件循环）
        loop = asyncio.get_event_loop()
        
        result = await loop.run_in_executor(
            None,
            lambda: self._model.transcribe(
                str(audio_path),
                language=language,
                **kwargs
            )
        )
        
        text = result.get("text", "").strip()
        
        logger.info(f"转录完成: {len(text)} 字符")
        
        return text
    
    async def transcribe_stream(
        self,
        audio_stream,
        language: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        从音频流转录（Whisper 不直接支持流式，需要先保存为临时文件）
        """
        import tempfile
        import os
        
        # 保存流到临时文件
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            
            # 读取流数据并写入文件
            async for chunk in audio_stream:
                tmp.write(chunk)
        
        try:
            # 转录临时文件
            result = await self.transcribe(tmp_path, language=language, **kwargs)
            return result
        finally:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except Exception as e:
                logger.warning(f"无法删除临时文件 {tmp_path}: {e}")
    
    def get_supported_languages(self) -> list[str]:
        """Whisper 支持的语言列表"""
        # Whisper 支持 99 种语言，这里列出常用的
        return [
            "zh", "en", "ja", "ko", "fr", "de", "es", "it", "pt", "ru",
            "ar", "nl", "pl", "tr", "sv", "da", "no", "fi", "el", "he",
            "hi", "th", "vi", "id", "ms", "uk", "cs", "ro", "hu", "bg",
        ]
