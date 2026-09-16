"""
TTS 模块
"""

from scout.voice.tts.base import TTSBase
from scout.voice.tts.openai_tts import OpenAITTS

__all__ = ["TTSBase", "OpenAITTS"]
