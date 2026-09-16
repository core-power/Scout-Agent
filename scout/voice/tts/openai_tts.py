"""
OpenAI TTS - 基于 OpenAI TTS API 的语音合成
"""

from pathlib import Path
from typing import Optional, AsyncIterator
import logging
import aiohttp

from scout.voice.tts.base import TTSBase

logger = logging.getLogger(__name__)


class OpenAITTS(TTSBase):
    """
    基于 OpenAI TTS API 的语音合成
    
    支持的语音角色：
    - alloy: 中性声音
    - echo: 男性声音
    - fable: 英国男性声音
    - onyx: 深沉男性声音
    - nova: 女性声音
    - shimmer: 温暖女性声音
    
    支持的模型：
    - tts-1: 快速，质量较低
    - tts-1-hd: 高质量，较慢
    """
    
    def __init__(
        self,
        api_key: str,
        model: str = "tts-1",
        base_url: str = "https://api.openai.com/v1",
        default_voice: str = "alloy",
        **kwargs
    ):
        """
        初始化 OpenAI TTS
        
        Args:
            api_key: OpenAI API Key
            model: 模型名称 (tts-1/tts-1-hd)
            base_url: API 基础 URL
            default_voice: 默认语音角色
            **kwargs: 其他参数
        """
        super().__init__(model_name=model, **kwargs)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/audio/speech"
        self.default_voice = default_voice
    
    async def synthesize(
        self,
        text: str,
        output_path: str | Path,
        voice: Optional[str] = None,
        **kwargs
    ) -> Path:
        """
        合成音频并保存到文件
        
        Args:
            text: 要合成的文本
            output_path: 输出文件路径
            voice: 语音角色
            **kwargs: 其他参数 (speed, response_format)
        
        Returns:
            生成的音频文件路径
        """
        if not self.validate_text(text):
            raise ValueError("文本无效")
        
        output_path = Path(output_path)
        voice = voice or self.default_voice
        
        logger.info(f"开始合成: {len(text)} 字符, 语音: {voice}")
        
        # 构建请求
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        # 确定输出格式
        output_format = kwargs.get("response_format", "mp3")
        if output_path.suffix:
            # 从文件扩展名推断格式
            ext = output_path.suffix.lower().lstrip(".")
            if ext in ["mp3", "opus", "aac", "flac", "wav", "pcm"]:
                output_format = ext
        
        data = {
            "model": self.model_name,
            "input": text,
            "voice": voice,
            "response_format": output_format,
        }
        
        if "speed" in kwargs:
            data["speed"] = kwargs["speed"]
        
        # 发送请求
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.endpoint,
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(
                        f"OpenAI TTS 请求失败 ({response.status}): {error_text}"
                    )
                
                # 保存音频文件
                audio_data = await response.read()
                
                # 确保输出目录存在
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(output_path, "wb") as f:
                    f.write(audio_data)
                
                logger.info(f"合成完成: {output_path} ({len(audio_data)} bytes)")
                
                return output_path
    
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
        if not self.validate_text(text):
            raise ValueError("文本无效")
        
        voice = voice or self.default_voice
        
        logger.info(f"开始流式合成: {len(text)} 字符, 语音: {voice}")
        
        # 构建请求
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        data = {
            "model": self.model_name,
            "input": text,
            "voice": voice,
            "response_format": kwargs.get("response_format", "mp3"),
        }
        
        if "speed" in kwargs:
            data["speed"] = kwargs["speed"]
        
        # 发送请求并流式返回
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.endpoint,
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(
                        f"OpenAI TTS 请求失败 ({response.status}): {error_text}"
                    )
                
                # 流式读取音频数据
                async for chunk in response.content.iter_chunked(8192):
                    if chunk:
                        yield chunk
    
    def get_supported_voices(self) -> list[str]:
        """获取支持的语音角色"""
        return ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
    
    def get_supported_formats(self) -> list[str]:
        """获取支持的输出格式"""
        return ["mp3", "opus", "aac", "flac", "wav", "pcm"]
