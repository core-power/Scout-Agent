"""
Scout Agent 语音交互模块

提供语音识别（ASR）和语音合成（TTS）能力，支持多种服务后端。

支持的 ASR 服务：
- Whisper（本地模型）
- OpenAI Whisper API
- 阿里云 ASR

支持的 TTS 服务：
- OpenAI TTS API
- 阿里云 TTS
- pyttsx3（本地）
"""

from scout.voice.asr.base import ASRBase
from scout.voice.asr.whisper import WhisperASR
from scout.voice.asr.openai_asr import OpenAIASR
from scout.voice.tts.base import TTSBase
from scout.voice.tts.openai_tts import OpenAITTS
from scout.voice.handler import VoiceHandler

__all__ = [
    "ASRBase",
    "WhisperASR",
    "OpenAIASR",
    "TTSBase",
    "OpenAITTS",
    "VoiceHandler",
]
