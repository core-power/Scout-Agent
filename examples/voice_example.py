#!/usr/bin/env python3
"""
语音交互示例

演示如何使用 Scout Agent 的语音交互功能
"""

import asyncio
from pathlib import Path
import tempfile

# 示例 1: 语音识别（ASR）
async def example_asr():
    """语音识别示例"""
    print("=" * 60)
    print("示例 1: 语音识别")
    print("=" * 60)
    
    # 使用 OpenAI Whisper API
    from scout.voice.asr import OpenAIASR
    
    # 从环境变量获取 API Key
    import os
    api_key = os.getenv("OPENAI_API_KEY")
    
    if not api_key:
        print("❌ 请设置 OPENAI_API_KEY 环境变量")
        return
    
    asr = OpenAIASR(api_key=api_key)
    
    # 假设有一个音频文件
    audio_path = "sample_audio.wav"  # 替换为实际的音频文件
    
    if not Path(audio_path).exists():
        print(f"⚠️  音频文件 {audio_path} 不存在，跳过此示例")
        print("   提示：你可以录制一段语音并保存为 sample_audio.wav")
        return
    
    try:
        text = await asr.transcribe(audio_path, language="zh")
        print(f"✅ 识别结果: {text}")
    except Exception as e:
        print(f"❌ 识别失败: {e}")


# 示例 2: 语音合成（TTS）
async def example_tts():
    """语音合成示例"""
    print("\n" + "=" * 60)
    print("示例 2: 语音合成")
    print("=" * 60)
    
    from scout.voice.tts import OpenAITTS
    import os
    
    api_key = os.getenv("OPENAI_API_KEY")
    
    if not api_key:
        print("❌ 请设置 OPENAI_API_KEY 环境变量")
        return
    
    tts = OpenAITTS(api_key=api_key)
    
    text = "你好，我是 Scout Agent，很高兴为你服务！"
    
    # 使用临时文件
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        output_path = Path(tmp.name)
    
    try:
        result_path = await tts.synthesize(
            text,
            output_path=output_path,
            voice="nova"  # 尝试不同的语音: alloy, echo, fable, onyx, nova, shimmer
        )
        
        print(f"✅ 合成成功: {result_path}")
        print(f"   文件大小: {result_path.stat().st_size} bytes")
        print(f"   文本内容: {text}")
        
        # 保持文件 10 秒供播放
        print("   文件将在 10 秒后自动删除")
        await asyncio.sleep(10)
        
    except Exception as e:
        print(f"❌ 合成失败: {e}")
    finally:
        # 清理临时文件
        if output_path.exists():
            output_path.unlink()


# 示例 3: 语音对话（完整流程）
async def example_voice_conversation():
    """语音对话示例"""
    print("\n" + "=" * 60)
    print("示例 3: 语音对话（语音 → 文本 → 处理 → 文本 → 语音）")
    print("=" * 60)
    
    from scout.voice import VoiceHandler
    from scout.voice.asr import OpenAIASR
    from scout.voice.tts import OpenAITTS
    import os
    
    api_key = os.getenv("OPENAI_API_KEY")
    
    if not api_key:
        print("❌ 请设置 OPENAI_API_KEY 环境变量")
        return
    
    # 创建语音处理器
    handler = VoiceHandler(
        asr=OpenAIASR(api_key=api_key),
        tts=OpenAITTS(api_key=api_key),
        default_language="zh"
    )
    
    # 查看能力
    capabilities = handler.get_capabilities()
    print(f"✅ 语音能力:")
    print(f"   ASR: {capabilities['asr']} (模型: {capabilities['asr_model']})")
    print(f"   TTS: {capabilities['tts']} (模型: {capabilities['tts_model']})")
    print(f"   支持的语言: {', '.join(capabilities['asr_languages'][:5])}...")
    print(f"   支持的语音: {', '.join(capabilities['tts_voices'])}")
    
    # 模拟一个处理器（实际应用中可以调用 LLM）
    async def process_text(text: str) -> str:
        """处理识别的文本"""
        # 这里可以调用 LLM 生成回复
        # 示例：简单的回声处理
        return f"你说的是：{text}。我理解你的意思了。"
    
    # 假设有一个输入音频
    input_audio = "input_audio.wav"
    
    if not Path(input_audio).exists():
        print(f"\n⚠️  输入音频 {input_audio} 不存在，跳过此示例")
        print("   提示：你可以录制一段语音并保存为 input_audio.wav")
        return
    
    try:
        # 执行语音对话
        input_text, output_audio = await handler.voice_to_voice(
            input_audio,
            processor=process_text,
            voice="nova"
        )
        
        print(f"\n✅ 对话完成:")
        print(f"   输入文本: {input_text}")
        print(f"   输出音频: {output_audio}")
        
        # 清理
        if output_audio.exists():
            output_audio.unlink()
            
    except Exception as e:
        print(f"❌ 对话失败: {e}")


# 示例 4: 使用本地 Whisper 模型
async def example_local_whisper():
    """本地 Whisper 模型示例"""
    print("\n" + "=" * 60)
    print("示例 4: 使用本地 Whisper 模型（无需 API Key）")
    print("=" * 60)
    
    try:
        from scout.voice.asr import WhisperASR
    except ImportError:
        print("⚠️  Whisper 未安装")
        print("   安装命令: pip install openai-whisper")
        return
    
    # 使用小模型（更快）
    asr = WhisperASR(model_name="base", device="cpu")
    
    print(f"✅ 初始化本地 Whisper 模型: {asr.model_name}")
    print(f"   设备: {asr.device}")
    print(f"   支持的语言: {', '.join(asr.get_supported_languages()[:10])}...")
    
    # 如果有音频文件，可以进行识别
    audio_path = "sample_audio.wav"
    
    if Path(audio_path).exists():
        try:
            print(f"\n正在识别: {audio_path}")
            text = await asr.transcribe(audio_path)
            print(f"✅ 识别结果: {text}")
        except Exception as e:
            print(f"❌ 识别失败: {e}")
    else:
        print(f"\n⚠️  音频文件 {audio_path} 不存在")


# 示例 5: 流式语音合成
async def example_streaming_tts():
    """流式语音合成示例"""
    print("\n" + "=" * 60)
    print("示例 5: 流式语音合成（适用于实时对话）")
    print("=" * 60)
    
    from scout.voice.tts import OpenAITTS
    import os
    
    api_key = os.getenv("OPENAI_API_KEY")
    
    if not api_key:
        print("❌ 请设置 OPENAI_API_KEY 环境变量")
        return
    
    tts = OpenAITTS(api_key=api_key)
    
    # 模拟 LLM 流式生成文本
    async def text_stream():
        words = ["你好，", "我是 ", "Scout ", "Agent，", "很高兴 ", "为你 ", "服务！"]
        for word in words:
            yield word
            await asyncio.sleep(0.1)  # 模拟生成延迟
    
    print("✅ 开始流式合成...")
    
    # 收集音频数据
    audio_chunks = []
    try:
        async for chunk in tts.synthesize_stream(
            text_stream(),
            voice="nova"
        ):
            audio_chunks.append(chunk)
            print(f"   接收到 {len(chunk)} bytes 音频数据")
        
        total_size = sum(len(chunk) for chunk in audio_chunks)
        print(f"\n✅ 流式合成完成:")
        print(f"   总大小: {total_size} bytes")
        print(f"   数据块数: {len(audio_chunks)}")
        
    except Exception as e:
        print(f"❌ 流式合成失败: {e}")


# 主函数
async def main():
    """运行所有示例"""
    print("\n" + "=" * 60)
    print("🎙️  Scout Agent 语音交互示例")
    print("=" * 60)
    
    # 检查环境变量
    import os
    if not os.getenv("OPENAI_API_KEY"):
        print("\n⚠️  警告: OPENAI_API_KEY 未设置")
        print("   部分示例需要 API Key 才能运行")
        print("   设置方法: export OPENAI_API_KEY='your-api-key'\n")
    
    # 运行示例
    await example_local_whisper()  # 不需要 API Key
    await example_asr()
    await example_tts()
    await example_voice_conversation()
    await example_streaming_tts()
    
    print("\n" + "=" * 60)
    print("✅ 所有示例运行完成")
    print("=" * 60)
    print("\n📚 更多信息请查看: docs/voice.md")


if __name__ == "__main__":
    asyncio.run(main())
