# Scout Agent 语音交互系统

## 概述

Scout Agent 提供完整的语音交互能力，支持语音识别（ASR）和语音合成（TTS），可以让 Agent 通过语音与用户进行自然交互。

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                    VoiceHandler                         │
│              (统一的语音交互接口)                         │
└─────────────────────────────────────────────────────────┘
         │                              │
         │                              │
    ┌────▼────┐                   ┌────▼────┐
    │   ASR   │                   │   TTS   │
    │ 语音识别 │                   │ 语音合成 │
    └─────────┘                   └─────────┘
         │                              │
    ┌────▼─────────────┐     ┌─────────▼──────────┐
    │ - Whisper (本地)  │     │ - OpenAI TTS       │
    │ - OpenAI Whisper  │     │ - 阿里云 TTS (计划) │
    │ - 阿里云 ASR (计划)│     │ - Edge TTS (计划)   │
    └──────────────────┘     └────────────────────┘
```

## 快速开始

### 1. 语音识别（ASR）

#### 使用 OpenAI Whisper API

```python
from scout.voice.asr import OpenAIASR

# 初始化
asr = OpenAIASR(api_key="your-api-key")

# 转录音频文件
text = await asr.transcribe("audio.wav", language="zh")
print(text)  # "你好，世界"
```

#### 使用本地 Whisper 模型

```python
from scout.voice.asr import WhisperASR

# 初始化（首次运行会下载模型）
asr = WhisperASR(model_name="base")  # tiny/base/small/medium/large

# 转录
text = await asr.transcribe("audio.wav")
```

### 2. 语音合成（TTS）

#### 使用 OpenAI TTS

```python
from scout.voice.tts import OpenAITTS

# 初始化
tts = OpenAITTS(api_key="your-api-key")

# 合成语音
audio_path = await tts.synthesize(
    "你好，我是 Scout Agent",
    output_path="output.mp3",
    voice="nova"  # alloy/echo/fable/onyx/nova/shimmer
)
```

### 3. 完整的语音交互

```python
from scout.voice import VoiceHandler
from scout.voice.asr import OpenAIASR
from scout.voice.tts import OpenAITTS

# 创建处理器
handler = VoiceHandler(
    asr=OpenAIASR(api_key="your-api-key"),
    tts=OpenAITTS(api_key="your-api-key"),
    default_language="zh"
)

# 语音识别
text = await handler.speech_to_text("audio.wav")

# 语音合成
audio_path = await handler.text_to_speech("你好，世界")

# 语音对话（完整流程）
async def process(text: str) -> str:
    # 这里可以调用 LLM 生成回复
    return f"你说的是：{text}"

input_text, output_audio = await handler.voice_to_voice(
    "input.wav",
    processor=process,
    voice="nova"
)
```

## ASR 实现

### OpenAIASR

基于 OpenAI Whisper API 的云端语音识别。

**优点：**
- 识别准确率高
- 支持 99 种语言
- 无需本地 GPU

**缺点：**
- 需要 API Key
- 有文件大小限制（25MB）
- 依赖网络

```python
asr = OpenAIASR(
    api_key="your-api-key",
    base_url="https://api.openai.com/v1",  # 可选，用于代理
    timeout=60  # 可选
)
```

### WhisperASR

基于 OpenAI Whisper 的本地语音识别。

**优点：**
- 完全离线
- 无文件大小限制
- 隐私性好

**缺点：**
- 需要下载模型（75MB - 3GB）
- 需要一定的计算资源
- 首次加载较慢

```python
asr = WhisperASR(
    model_name="base",  # tiny/base/small/medium/large
    device="auto",      # cpu/cuda/auto
    compute_type="float16"  # float32/float16/int8
)
```

**模型选择指南：**

| 模型 | 大小 | 显存需求 | 速度 | 准确率 |
|------|------|---------|------|--------|
| tiny | 75MB | ~1GB | 最快 | 一般 |
| base | 140MB | ~1GB | 快 | 较好 |
| small | 460MB | ~2GB | 中等 | 好 |
| medium | 1.5GB | ~5GB | 慢 | 很好 |
| large | 3GB | ~10GB | 最慢 | 最佳 |

## TTS 实现

### OpenAITTS

基于 OpenAI TTS API 的语音合成。

**特点：**
- 6 种高质量语音
- 自然流畅
- 支持多种输出格式

```python
tts = OpenAITTS(
    api_key="your-api-key",
    model="tts-1",  # tts-1 (快) / tts-1-hd (高质量)
    default_voice="nova"
)

# 支持的语音角色
voices = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]

# 支持的输出格式
formats = ["mp3", "opus", "aac", "flac", "wav", "pcm"]
```

## VoiceHandler

VoiceHandler 是统一的语音交互接口，整合了 ASR 和 TTS。

### 基本用法

```python
handler = VoiceHandler(
    asr=asr_instance,
    tts=tts_instance,
    default_language="zh"
)

# 查看支持的能力
capabilities = handler.get_capabilities()
print(capabilities)
# {
#     "asr": True,
#     "tts": True,
#     "asr_model": "whisper-1",
#     "tts_model": "tts-1",
#     "asr_languages": ["zh", "en", ...],
#     "tts_voices": ["alloy", "echo", ...]
# }
```

### 高级功能

#### 流式语音响应

适用于实时对话场景：

```python
async def text_generator():
    """模拟 LLM 流式生成文本"""
    yield "你好，"
    yield "我是 "
    yield "Scout Agent"

# 流式合成
audio_stream = handler.stream_voice_response(
    text_generator(),
    voice="nova"
)

async for chunk in audio_stream:
    # 实时播放或保存
    play_audio(chunk)
```

#### 音频流识别

处理实时音频流：

```python
async def audio_stream():
    """从麦克风读取音频"""
    while True:
        chunk = await read_microphone()
        yield chunk

text = await handler.process_audio_stream(
    audio_stream(),
    language="zh"
)
```

## 集成到 Scout Agent

### 作为工具使用

将语音功能暴露为 Agent 工具：

```python
from scout.tools.base import ToolDefinition

class SpeechToTextTool(ToolDefinition):
    name = "speech_to_text"
    description = "将音频文件转换为文本"
    
    parameters = {
        "type": "object",
        "properties": {
            "audio_path": {
                "type": "string",
                "description": "音频文件路径"
            },
            "language": {
                "type": "string",
                "description": "语言代码（如 zh, en）",
                "default": "zh"
            }
        },
        "required": ["audio_path"]
    }
    
    async def execute(self, audio_path: str, language: str = "zh"):
        handler = get_voice_handler()
        text = await handler.speech_to_text(audio_path, language)
        return {"text": text}

class TextToSpeechTool(ToolDefinition):
    name = "text_to_speech"
    description = "将文本转换为语音"
    
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "要转换的文本"
            },
            "voice": {
                "type": "string",
                "description": "语音角色",
                "default": "nova"
            }
        },
        "required": ["text"]
    }
    
    async def execute(self, text: str, voice: str = "nova"):
        handler = get_voice_handler()
        audio_path = await handler.text_to_speech(text, voice=voice)
        return {"audio_path": str(audio_path)}
```

### 配置管理

通过环境变量配置：

```bash
# OpenAI API
export OPENAI_API_KEY="your-api-key"
export OPENAI_BASE_URL="https://api.openai.com/v1"

# 语音配置
export SCOUT_ASR_PROVIDER="openai"  # openai / whisper
export SCOUT_TTS_PROVIDER="openai"  # openai
export SCOUT_TTS_VOICE="nova"
export SCOUT_DEFAULT_LANGUAGE="zh"

# Whisper 本地模型
export SCOUT_WHISPER_MODEL="base"
```

## 最佳实践

### 1. 错误处理

```python
try:
    text = await handler.speech_to_text("audio.wav")
except FileNotFoundError:
    print("音频文件不存在")
except ValueError as e:
    print(f"音频格式无效: {e}")
except RuntimeError as e:
    print(f"识别失败: {e}")
```

### 2. 性能优化

```python
# 使用较小的模型提高速度
asr = WhisperASR(model_name="base")  # 而不是 large

# 使用 tts-1 而不是 tts-1-hd 提高速度
tts = OpenAITTS(model="tts-1")

# 对于长文本，分段合成
async def synthesize_long_text(text: str):
    chunks = split_text(text, max_length=500)
    audio_paths = []
    for chunk in chunks:
        path = await handler.text_to_speech(chunk)
        audio_paths.append(path)
    return merge_audio(audio_paths)
```

### 3. 资源管理

```python
# 使用临时文件
import tempfile
import os

with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
    tmp_path = tmp.name

try:
    await handler.text_to_speech("Hello", output_path=tmp_path)
    # 使用音频文件
finally:
    os.unlink(tmp_path)  # 清理
```

## 测试

运行语音模块测试：

```bash
# 运行所有语音测试
pytest tests/unit/test_voice.py -v

# 运行特定测试
pytest tests/unit/test_voice.py::TestVoiceHandler -v
```

## 未来计划

- [ ] 阿里云 ASR 支持
- [ ] 阿里云 TTS 支持
- [ ] Edge TTS 支持（免费）
- [ ] 实时语音对话（WebSocket）
- [ ] 语音情感识别
- [ ] 多语言自动检测
- [ ] 语音命令识别

## 故障排除

### Whisper 模型下载失败

```bash
# 手动下载模型
python -c "import whisper; whisper.load_model('base')"

# 或使用镜像
export HF_ENDPOINT="https://hf-mirror.com"
```

### OpenAI API 超时

```python
# 增加超时时间
asr = OpenAIASR(api_key="...", timeout=120)
tts = OpenAITTS(api_key="...", timeout=120)
```

### 音频格式不支持

```bash
# 使用 ffmpeg 转换格式
ffmpeg -i input.m4a -acodec pcm_s16le -ar 16000 output.wav
```

## API 参考

详见各模块的文档字符串：

- `scout.voice.asr.base.ASRBase` - ASR 基类
- `scout.voice.asr.whisper.WhisperASR` - Whisper ASR
- `scout.voice.asr.openai_asr.OpenAIASR` - OpenAI ASR
- `scout.voice.tts.base.TTSBase` - TTS 基类
- `scout.voice.tts.openai_tts.OpenAITTS` - OpenAI TTS
- `scout.voice.handler.VoiceHandler` - 语音处理器
