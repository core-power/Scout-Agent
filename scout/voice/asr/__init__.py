"""
ASR（语音识别）模块
"""

from scout.voice.asr.base import ASRBase
from scout.voice.asr.whisper import WhisperASR
from scout.voice.asr.openai_asr import OpenAIASR

__all__ = ["ASRBase", "WhisperASR", "OpenAIASR"]
