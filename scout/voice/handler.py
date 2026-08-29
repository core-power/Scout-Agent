"""
VoiceHandler - 语音交互处理器

整合 ASR 和 TTS，提供统一的语音交互接口。
"""

from pathlib import Path
from typing import Optional, AsyncIterator
import logging
import tempfile
import asyncio

from scout.voice.asr.base import ASRBase
from scout.voice.tts.base import TTSBase

logger = logging.getLogger(__name__)


class VoiceHandler:
    """
    语音交互处理器
    
    提供完整的语音交互能力：
    - 语音识别（ASR）：音频 → 文本
    - 语音合成（TTS）：文本 → 音频
    - 语音对话：音频 → 文本 → 处理 → 文本 → 音频
    """
    
    def __init__(
        self,
        asr: Optional[ASRBase] = None,
        tts: Optional[TTSBase] = None,
        default_language: str = "zh",
        **kwargs
    ):
        """
        初始化语音处理器
        
        Args:
            asr: ASR 实例
            tts: TTS 实例
            default_language: 默认语言
            **kwargs: 其他配置
        """
        self.asr = asr
        self.tts = tts
        self.default_language = default_language
        self.config = kwargs
    
    def set_asr(self, asr: ASRBase):
        """设置 ASR 实例"""
        self.asr = asr
        logger.info(f"设置 ASR: {type(asr).__name__}")
    
    def set_tts(self, tts: TTSBase):
        """设置 TTS 实例"""
        self.tts = tts
        logger.info(f"设置 TTS: {type(tts).__name__}")
    
    async def speech_to_text(
        self,
        audio_path: str | Path,
        language: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        语音识别：音频 → 文本
        
        Args:
            audio_path: 音频文件路径
            language: 语言代码
            **kwargs: 其他参数
        
        Returns:
            识别的文本
        """
        if not self.asr:
            raise RuntimeError("ASR 未配置")
        
        language = language or self.default_language
        
        logger.info(f"语音识别: {audio_path}, 语言: {language}")
        
        text = await self.asr.transcribe(audio_path, language=language, **kwargs)
        
        logger.info(f"识别结果: {len(text)} 字符")
        
        return text
    
    async def text_to_speech(
        self,
        text: str,
        output_path: Optional[str | Path] = None,
        voice: Optional[str] = None,
        **kwargs
    ) -> Path:
        """
        语音合成：文本 → 音频
        
        Args:
            text: 要合成的文本
            output_path: 输出文件路径（None 则使用临时文件）
            voice: 语音角色
            **kwargs: 其他参数
        
        Returns:
            生成的音频文件路径
        """
        if not self.tts:
            raise RuntimeError("TTS 未配置")
        
        # 如果未指定输出路径，使用临时文件
        if output_path is None:
            suffix = f".{kwargs.get('response_format', 'mp3')}"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                output_path = Path(tmp.name)
        else:
            output_path = Path(output_path)
        
        logger.info(f"语音合成: {len(text)} 字符, 语音: {voice or 'default'}")
        
        result_path = await self.tts.synthesize(
            text,
            output_path,
            voice=voice,
            **kwargs
        )
        
        logger.info(f"合成完成: {result_path}")
        
        return result_path
    
    async def voice_to_voice(
        self,
        audio_path: str | Path,
        processor: callable,
        voice: Optional[str] = None,
        language: Optional[str] = None,
        **kwargs
    ) -> tuple[str, Path]:
        """
        语音对话：音频 → 文本 → 处理 → 文本 → 音频
        
        Args:
            audio_path: 输入音频文件路径
            processor: 文本处理函数 (input_text: str) -> str
            voice: 输出语音角色
            language: 语言代码
            **kwargs: 其他参数
        
        Returns:
            (识别的文本, 生成的音频路径)
        """
        # 1. 语音识别
        input_text = await self.speech_to_text(audio_path, language=language)
        
        # 2. 文本处理
        logger.info("处理文本...")
        if asyncio.iscoroutinefunction(processor):
            output_text = await processor(input_text)
        else:
            output_text = processor(input_text)
        
        logger.info(f"处理结果: {len(output_text)} 字符")
        
        # 3. 语音合成
        output_audio = await self.text_to_speech(
            output_text,
            voice=voice,
            **kwargs
        )
        
        return input_text, output_audio
    
    async def stream_voice_response(
        self,
        text_generator: AsyncIterator[str],
        voice: Optional[str] = None,
        **kwargs
    ) -> AsyncIterator[bytes]:
        """
        流式语音响应：文本流 → 音频流
        
        适用于实时对话场景，边生成文本边合成语音。
        
        Args:
            text_generator: 文本生成器（异步迭代器）
            voice: 语音角色
            **kwargs: 其他参数
        
        Yields:
            音频数据块
        """
        if not self.tts:
            raise RuntimeError("TTS 未配置")
        
        # 收集完整的文本（OpenAI TTS 目前不支持真正的流式输入）
        full_text = []
        async for chunk in text_generator:
            full_text.append(chunk)
        
        text = "".join(full_text)
        
        logger.info(f"流式合成: {len(text)} 字符")
        
        # 流式合成
        async for audio_chunk in self.tts.synthesize_stream(
            text,
            voice=voice,
            **kwargs
        ):
            yield audio_chunk
    
    async def process_audio_stream(
        self,
        audio_stream,
        language: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        处理音频流（实时语音识别）
        
        Args:
            audio_stream: 音频流
            language: 语言代码
            **kwargs: 其他参数
        
        Returns:
            识别的文本
        """
        if not self.asr:
            raise RuntimeError("ASR 未配置")
        
        language = language or self.default_language
        
        logger.info("处理音频流...")
        
        text = await self.asr.transcribe_stream(
            audio_stream,
            language=language,
            **kwargs
        )
        
        logger.info(f"识别结果: {len(text)} 字符")
        
        return text
    
    def get_capabilities(self) -> dict:
        """获取当前支持的语音能力"""
        capabilities = {
            "asr": False,
            "tts": False,
            "asr_model": None,
            "tts_model": None,
            "asr_languages": [],
            "tts_voices": [],
        }
        
        if self.asr:
            capabilities["asr"] = True
            capabilities["asr_model"] = self.asr.model_name
            capabilities["asr_languages"] = self.asr.get_supported_languages()
        
        if self.tts:
            capabilities["tts"] = True
            capabilities["tts_model"] = self.tts.model_name
            capabilities["tts_voices"] = self.tts.get_supported_voices()
        
        return capabilities
