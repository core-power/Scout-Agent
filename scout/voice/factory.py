"""VoiceHandler 工厂 — 根据环境变量/配置构建语音能力.

支持的后端:
- ASR: "openai" (Whisper API) | "whisper" (本地模型) | "none"
- TTS: "openai" | "none"

环境变量:
- SCOUT_VOICE_ASR: asr 后端, 默认 "openai"
- SCOUT_VOICE_TTS: tts 后端, 默认 "openai"
- SCOUT_ASR_API_KEY / SCOUT_TTS_API_KEY: 独立密钥 (缺省时回退 SCOUT_LLM_API_KEY)
- SCOUT_ASR_MODEL / SCOUT_TTS_MODEL: 模型名
- SCOUT_TTS_VOICE: 默认语音角色
"""

import logging
import os
from typing import Optional

from scout.voice.handler import VoiceHandler

logger = logging.getLogger(__name__)


def _pick_key(*names: str) -> Optional[str]:
    """按顺序取第一个非空环境变量."""
    for name in names:
        val = os.getenv(name)
        if val and val.strip():
            return val.strip()
    return None


def build_voice_handler() -> VoiceHandler:
    """根据环境变量构建 VoiceHandler.

    任何后端配置不可用时返回仅含 capabilities 的空处理器
    (capabilities 中 asr/tts 均为 False, 不会抛错)。
    """
    from scout.voice.asr.base import ASRBase
    from scout.voice.tts.base import TTSBase

    handler = VoiceHandler()

    asr_backend = os.getenv("SCOUT_VOICE_ASR", "openai").strip().lower()
    if asr_backend != "none":
        try:
            asr: Optional[ASRBase] = None
            if asr_backend == "openai":
                api_key = _pick_key("SCOUT_ASR_API_KEY", "SCOUT_LLM_API_KEY")
                if api_key:
                    from scout.voice.asr.openai_asr import OpenAIASR

                    asr = OpenAIASR(
                        api_key=api_key,
                        model=os.getenv("SCOUT_ASR_MODEL", "whisper-1"),
                        base_url=os.getenv(
                            "SCOUT_ASR_BASE_URL",
                            os.getenv("SCOUT_LLM_BASE_URL", "https://api.openai.com/v1"),
                        ),
                    )
                else:
                    logger.warning("OpenAI ASR 未配置 API Key, 语音识别不可用")
            elif asr_backend == "whisper":
                from scout.voice.asr.whisper import WhisperASR

                asr = WhisperASR(
                    model_name=os.getenv("SCOUT_ASR_MODEL", "base"),
                    device=os.getenv("SCOUT_ASR_DEVICE", "auto"),
                    compute_type=os.getenv("SCOUT_ASR_COMPUTE_TYPE", "float32"),
                )
            else:
                logger.warning(f"未知 ASR 后端: {asr_backend}, 语音识别不可用")

            if asr is not None:
                handler.set_asr(asr)
        except Exception as e:  # noqa: BLE001 — 后端初始化失败不阻塞服务
            logger.error(f"ASR 初始化失败: {e}")

    tts_backend = os.getenv("SCOUT_VOICE_TTS", "openai").strip().lower()
    if tts_backend != "none":
        try:
            tts: Optional[TTSBase] = None
            if tts_backend == "openai":
                api_key = _pick_key("SCOUT_TTS_API_KEY", "SCOUT_LLM_API_KEY")
                if api_key:
                    from scout.voice.tts.openai_tts import OpenAITTS

                    tts = OpenAITTS(
                        api_key=api_key,
                        model=os.getenv("SCOUT_TTS_MODEL", "tts-1"),
                        base_url=os.getenv(
                            "SCOUT_TTS_BASE_URL",
                            os.getenv("SCOUT_LLM_BASE_URL", "https://api.openai.com/v1"),
                        ),
                        default_voice=os.getenv("SCOUT_TTS_VOICE", "alloy"),
                    )
                else:
                    logger.warning("OpenAI TTS 未配置 API Key, 语音合成不可用")
            else:
                logger.warning(f"未知 TTS 后端: {tts_backend}, 语音合成不可用")

            if tts is not None:
                handler.set_tts(tts)
        except Exception as e:  # noqa: BLE001
            logger.error(f"TTS 初始化失败: {e}")

    return handler
