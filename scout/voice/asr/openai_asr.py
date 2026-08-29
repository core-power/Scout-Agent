"""
OpenAI ASR - 基于 OpenAI Whisper API 的语音识别
"""

from pathlib import Path
from typing import Optional
import logging
import aiohttp

from scout.voice.asr.base import ASRBase

logger = logging.getLogger(__name__)


class OpenAIASR(ASRBase):
    """
    基于 OpenAI Whisper API 的语音识别
    
    优点：
    - 无需本地 GPU
    - 快速响应
    - 支持多种语言
    
    限制：
    - 需要 API Key
    - 文件大小限制 25MB
    - 需要网络连接
    """
    
    def __init__(
        self,
        api_key: str,
        model: str = "whisper-1",
        base_url: str = "https://api.openai.com/v1",
        **kwargs
    ):
        """
        初始化 OpenAI ASR
        
        Args:
            api_key: OpenAI API Key
            model: 模型名称 (whisper-1)
            base_url: API 基础 URL
            **kwargs: 其他参数
        """
        super().__init__(model_name=model, **kwargs)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/audio/transcriptions"
    
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
            **kwargs: 其他参数
        
        Returns:
            转录的文本
        """
        audio_path = Path(audio_path)
        
        if not self.validate_audio(audio_path):
            raise FileNotFoundError(f"音频文件无效: {audio_path}")
        
        # 检查文件大小（限制 25MB）
        file_size = audio_path.stat().st_size
        if file_size > 25 * 1024 * 1024:
            raise ValueError(f"音频文件过大: {file_size / 1024 / 1024:.1f}MB (限制 25MB)")
        
        logger.info(f"开始转录: {audio_path}")
        
        # 构建 multipart/form-data 请求
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        
        data = {
            "model": self.model_name,
            "response_format": kwargs.get("response_format", "text"),
        }
        
        if language:
            data["language"] = language
        
        if "prompt" in kwargs:
            data["prompt"] = kwargs["prompt"]
        
        if "temperature" in kwargs:
            data["temperature"] = str(kwargs["temperature"])
        
        # 读取音频文件
        with open(audio_path, "rb") as f:
            audio_data = f.read()
        
        # 发送请求
        async with aiohttp.ClientSession() as session:
            form_data = aiohttp.FormData()
            form_data.add_field("file", audio_data, filename=audio_path.name)
            
            for key, value in data.items():
                form_data.add_field(key, value)
            
            async with session.post(
                self.endpoint,
                headers=headers,
                data=form_data,
                timeout=aiohttp.ClientTimeout(total=60)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise RuntimeError(
                        f"OpenAI ASR 请求失败 ({response.status}): {error_text}"
                    )
                
                # 解析响应
                response_format = data["response_format"]
                if response_format == "text":
                    text = await response.text()
                elif response_format == "json":
                    result = await response.json()
                    text = result.get("text", "")
                else:
                    text = await response.text()
                
                text = text.strip()
                
                logger.info(f"转录完成: {len(text)} 字符")
                
                return text
    
    async def transcribe_stream(
        self,
        audio_stream,
        language: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        从音频流转录（需要先保存为临时文件）
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
