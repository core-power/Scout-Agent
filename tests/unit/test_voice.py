"""
语音交互模块单元测试
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch
import tempfile

from scout.voice.asr.base import ASRBase
from scout.voice.asr.whisper import WhisperASR
from scout.voice.asr.openai_asr import OpenAIASR
from scout.voice.tts.base import TTSBase
from scout.voice.tts.openai_tts import OpenAITTS
from scout.voice.handler import VoiceHandler


class TestASRBase:
    """测试 ASR 基类"""
    
    def test_validate_audio_valid(self, tmp_path):
        """测试有效音频文件验证"""
        # 创建测试音频文件
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake audio data")
        
        # 创建 ASR 实例（使用 Mock）
        asr = Mock(spec=ASRBase)
        asr.validate_audio = ASRBase.validate_audio.__get__(asr, ASRBase)
        
        # 验证
        assert asr.validate_audio(audio_file) is True
    
    def test_validate_audio_not_exists(self, tmp_path):
        """测试不存在的音频文件"""
        audio_file = tmp_path / "not_exist.wav"
        
        asr = Mock(spec=ASRBase)
        asr.validate_audio = ASRBase.validate_audio.__get__(asr, ASRBase)
        
        assert asr.validate_audio(audio_file) is False
    
    def test_validate_audio_invalid_format(self, tmp_path):
        """测试无效格式"""
        audio_file = tmp_path / "test.txt"
        audio_file.write_text("not audio")
        
        asr = Mock(spec=ASRBase)
        asr.validate_audio = ASRBase.validate_audio.__get__(asr, ASRBase)
        
        assert asr.validate_audio(audio_file) is False


class TestTTSBase:
    """测试 TTS 基类"""
    
    def test_validate_text_valid(self):
        """测试有效文本验证"""
        tts = Mock(spec=TTSBase)
        tts.config = {"max_text_length": 4096}
        tts.validate_text = TTSBase.validate_text.__get__(tts, TTSBase)
        
        assert tts.validate_text("Hello, world!") is True
    
    def test_validate_text_empty(self):
        """测试空文本"""
        tts = Mock(spec=TTSBase)
        tts.config = {"max_text_length": 4096}
        tts.validate_text = TTSBase.validate_text.__get__(tts, TTSBase)
        
        assert tts.validate_text("") is False
        assert tts.validate_text("   ") is False
    
    def test_validate_text_too_long(self):
        """测试过长文本"""
        tts = Mock(spec=TTSBase)
        tts.config = {"max_text_length": 100}
        tts.validate_text = TTSBase.validate_text.__get__(tts, TTSBase)
        
        long_text = "a" * 200
        assert tts.validate_text(long_text) is False


class TestVoiceHandler:
    """测试 VoiceHandler"""
    
    @pytest.fixture
    def mock_asr(self):
        """创建 Mock ASR"""
        asr = Mock(spec=ASRBase)
        asr.model_name = "test-asr"
        asr.transcribe = AsyncMock(return_value="识别的文本")
        asr.transcribe_stream = AsyncMock(return_value="流式识别的文本")
        asr.get_supported_languages = Mock(return_value=["zh", "en"])
        return asr
    
    @pytest.fixture
    def mock_tts(self):
        """创建 Mock TTS"""
        tts = Mock(spec=TTSBase)
        tts.model_name = "test-tts"
        tts.synthesize = AsyncMock(return_value=Path("/tmp/test.mp3"))
        tts.synthesize_stream = AsyncMock()
        tts.get_supported_voices = Mock(return_value=["alloy", "echo"])
        return tts
    
    @pytest.fixture
    def handler(self, mock_asr, mock_tts):
        """创建 VoiceHandler"""
        return VoiceHandler(asr=mock_asr, tts=mock_tts)
    
    def test_init(self, handler, mock_asr, mock_tts):
        """测试初始化"""
        assert handler.asr == mock_asr
        assert handler.tts == mock_tts
        assert handler.default_language == "zh"
    
    def test_set_asr(self, handler, mock_asr):
        """测试设置 ASR"""
        new_asr = Mock(spec=ASRBase)
        handler.set_asr(new_asr)
        assert handler.asr == new_asr
    
    def test_set_tts(self, handler, mock_tts):
        """测试设置 TTS"""
        new_tts = Mock(spec=TTSBase)
        handler.set_tts(new_tts)
        assert handler.tts == new_tts
    
    @pytest.mark.asyncio
    async def test_speech_to_text(self, handler, mock_asr, tmp_path):
        """测试语音识别"""
        audio_file = tmp_path / "test.wav"
        audio_file.write_bytes(b"fake audio")
        
        result = await handler.speech_to_text(audio_file, language="en")
        
        assert result == "识别的文本"
        mock_asr.transcribe.assert_called_once_with(
            audio_file,
            language="en"
        )
    
    @pytest.mark.asyncio
    async def test_speech_to_text_no_asr(self, mock_tts):
        """测试未配置 ASR"""
        handler = VoiceHandler(tts=mock_tts)
        
        with pytest.raises(RuntimeError, match="ASR 未配置"):
            await handler.speech_to_text("/tmp/test.wav")
    
    @pytest.mark.asyncio
    async def test_text_to_speech(self, handler, mock_tts):
        """测试语音合成"""
        result = await handler.text_to_speech(
            "Hello, world!",
            voice="echo"
        )
        
        assert result == Path("/tmp/test.mp3")
        mock_tts.synthesize.assert_called_once()
        
        # 检查调用参数
        call_args = mock_tts.synthesize.call_args
        assert call_args[0][0] == "Hello, world!"  # text
        assert call_args[1]["voice"] == "echo"
    
    @pytest.mark.asyncio
    async def test_text_to_speech_with_output_path(self, handler, mock_tts, tmp_path):
        """测试指定输出路径"""
        output_path = tmp_path / "output.mp3"
        
        result = await handler.text_to_speech(
            "Hello",
            output_path=output_path
        )
        
        assert result == Path("/tmp/test.mp3")
        call_args = mock_tts.synthesize.call_args
        assert call_args[0][1] == output_path
    
    @pytest.mark.asyncio
    async def test_text_to_speech_no_tts(self, mock_asr):
        """测试未配置 TTS"""
        handler = VoiceHandler(asr=mock_asr)
        
        with pytest.raises(RuntimeError, match="TTS 未配置"):
            await handler.text_to_speech("Hello")
    
    @pytest.mark.asyncio
    async def test_voice_to_voice(self, handler, mock_asr, mock_tts, tmp_path):
        """测试语音对话"""
        audio_file = tmp_path / "input.wav"
        audio_file.write_bytes(b"fake audio")
        
        def processor(text: str) -> str:
            return f"处理后的: {text}"
        
        input_text, output_audio = await handler.voice_to_voice(
            audio_file,
            processor=processor,
            voice="nova"
        )
        
        assert input_text == "识别的文本"
        assert output_audio == Path("/tmp/test.mp3")
        
        # 验证调用顺序
        mock_asr.transcribe.assert_called_once()
        mock_tts.synthesize.assert_called_once()
        
        # 验证处理后的文本
        tts_call_args = mock_tts.synthesize.call_args
        assert tts_call_args[0][0] == "处理后的: 识别的文本"
    
    @pytest.mark.asyncio
    async def test_voice_to_voice_async_processor(self, handler, mock_asr, mock_tts, tmp_path):
        """测试异步处理器"""
        audio_file = tmp_path / "input.wav"
        audio_file.write_bytes(b"fake audio")
        
        async def async_processor(text: str) -> str:
            await asyncio.sleep(0.01)
            return f"异步处理: {text}"
        
        input_text, output_audio = await handler.voice_to_voice(
            audio_file,
            processor=async_processor
        )
        
        assert input_text == "识别的文本"
        tts_call_args = mock_tts.synthesize.call_args
        assert tts_call_args[0][0] == "异步处理: 识别的文本"
    
    @pytest.mark.asyncio
    async def test_process_audio_stream(self, handler, mock_asr):
        """测试音频流处理"""
        async def audio_stream():
            yield b"chunk1"
            yield b"chunk2"
        
        result = await handler.process_audio_stream(audio_stream())
        
        assert result == "流式识别的文本"
        mock_asr.transcribe_stream.assert_called_once()
    
    def test_get_capabilities(self, handler, mock_asr, mock_tts):
        """测试获取能力"""
        capabilities = handler.get_capabilities()
        
        assert capabilities["asr"] is True
        assert capabilities["tts"] is True
        assert capabilities["asr_model"] == "test-asr"
        assert capabilities["tts_model"] == "test-tts"
        assert "zh" in capabilities["asr_languages"]
        assert "alloy" in capabilities["tts_voices"]
    
    def test_get_capabilities_partial(self, mock_asr):
        """测试部分能力"""
        handler = VoiceHandler(asr=mock_asr)
        capabilities = handler.get_capabilities()
        
        assert capabilities["asr"] is True
        assert capabilities["tts"] is False
        assert capabilities["tts_model"] is None


class TestWhisperASR:
    """测试 WhisperASR"""
    
    def test_init(self):
        """测试初始化"""
        asr = WhisperASR(model_name="base")
        assert asr.model_name == "base"
        assert asr.device == "auto"
    
    def test_get_supported_languages(self):
        """测试支持的语音"""
        asr = WhisperASR()
        languages = asr.get_supported_languages()
        
        assert "en" in languages
        assert "zh" in languages


class TestOpenAIASR:
    """测试 OpenAIASR"""
    
    def test_init(self):
        """测试初始化"""
        asr = OpenAIASR(api_key="test-key")
        assert asr.api_key == "test-key"
        assert asr.model_name == "whisper-1"
        assert "openai.com" in asr.base_url
    
    def test_init_custom_config(self):
        """测试自定义配置"""
        asr = OpenAIASR(
            api_key="test-key",
            base_url="https://custom.api.com/v1",
            timeout=30
        )
        
        assert asr.base_url == "https://custom.api.com/v1"
        assert asr.config["timeout"] == 30


class TestOpenAITTS:
    """测试 OpenAITTS"""
    
    def test_init(self):
        """测试初始化"""
        tts = OpenAITTS(api_key="test-key")
        assert tts.api_key == "test-key"
        assert tts.model_name == "tts-1"
        assert tts.default_voice == "alloy"
    
    def test_get_supported_voices(self):
        """测试支持的语音"""
        tts = OpenAITTS(api_key="test-key")
        voices = tts.get_supported_voices()
        
        assert "alloy" in voices
        assert "echo" in voices
        assert "nova" in voices
    
    def test_get_supported_formats(self):
        """测试支持的格式"""
        tts = OpenAITTS(api_key="test-key")
        formats = tts.get_supported_formats()
        
        assert "mp3" in formats
        assert "wav" in formats


# 需要导入 asyncio
import asyncio
