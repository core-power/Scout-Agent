"""语音路由组（/api/voice/*：capabilities/asr/tts/chat）.

W1 拆分（2026-09-14）：自 adapters/web.py 原样下沉（WebAdapter mixin），
函数体零改动——行为不变原则。"""
import logging

logger = logging.getLogger("scout.adapters.web")
import os


from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, Response
from scout.core.callbacks import Callbacks, NullCallbacks
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from scout.core.types import Message, Role, Session
import uuid

if TYPE_CHECKING:  # 仅为类型检查，运行时无循环依赖
    from scout.adapters.web.adapter import WebAdapter


class VoiceRoutes:
    """语音路由组（/api/voice/*：capabilities/asr/tts/chat）（mixin）."""

    def _setup_voice_routes(self):
        """语音 API — ASR / TTS / 语音对话（voice 模块接线入口）."""
        import tempfile
        from pathlib import Path as _Path

        # ── 语音能力查询 ──

        @self.app.get("/api/voice/capabilities")
        async def voice_capabilities():
            return {"status": "ok", "capabilities": self._voice_handler.get_capabilities()}

        # ── 语音识别：上传音频 → 文本 ──

        @self.app.post("/api/voice/asr")
        async def voice_asr(request: Request):
            form = await request.form()
            audio = form.get("audio") or form.get("file")
            if not audio:
                return JSONResponse({"error": "缺少音频文件 (audio)"}, status_code=400)
            try:
                data = await audio.read()
            except Exception:
                return JSONResponse({"error": "读取音频失败"}, status_code=400)
            if not data:
                return JSONResponse({"error": "音频内容为空"}, status_code=400)

            suffix = _Path(audio.filename or "audio.webm").suffix or ".webm"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                language = (form.get("language") or None) if form.get("language") else None
                text = await self._voice_handler.speech_to_text(tmp_path, language=language)
            except RuntimeError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:  # noqa: BLE001
                logger.error(f"ASR 失败: {e}")
                return JSONResponse({"error": f"语音识别失败: {e}"}, status_code=500)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            return {"status": "ok", "text": text}

        # ── 语音合成：文本 → 音频 ──

        @self.app.post("/api/voice/tts")
        async def voice_tts(request: Request):
            body = await request.json()
            text = (body.get("text") or "").strip()
            if not text:
                return JSONResponse({"error": "缺少文本 (text)"}, status_code=400)
            if len(text) > 4000:
                return JSONResponse({"error": "文本过长，请控制在 4000 字符以内"}, status_code=400)

            response_format = (body.get("response_format") or "mp3").lower()
            try:
                audio_path = await self._voice_handler.text_to_speech(
                    text,
                    voice=body.get("voice"),
                    response_format=response_format,
                )
            except RuntimeError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:  # noqa: BLE001
                logger.error(f"TTS 失败: {e}")
                return JSONResponse({"error": f"语音合成失败: {e}"}, status_code=500)

            try:
                audio_bytes = _Path(audio_path).read_bytes()
            finally:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
            media_type = "audio/mpeg" if response_format == "mp3" else f"audio/{response_format}"
            return Response(content=audio_bytes, media_type=media_type)

        # ── 语音对话：音频 → 回复音频 ──

        @self.app.post("/api/voice/chat")
        async def voice_chat(request: Request):
            form = await request.form()
            audio = form.get("audio") or form.get("file")
            if not audio:
                return JSONResponse({"error": "缺少音频文件 (audio)"}, status_code=400)
            if not self._agent:
                return JSONResponse({"error": "请先在设置中配置 API Key"}, status_code=400)
            try:
                data = await audio.read()
            except Exception:
                return JSONResponse({"error": "读取音频失败"}, status_code=400)
            if not data:
                return JSONResponse({"error": "音频内容为空"}, status_code=400)

            suffix = _Path(audio.filename or "audio.webm").suffix or ".webm"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                language = (form.get("language") or None) if form.get("language") else None

                # 1. ASR：音频 → 文本
                text = await self._voice_handler.speech_to_text(tmp_path, language=language)

                # 2. 会话 → Agent 回复
                import copy
                session_id = str(uuid.uuid4())
                session = Session(id=session_id)
                agent_copy = copy.copy(self.agent)
                agent_copy.callbacks = NullCallbacks()
                result = await agent_copy.run_conversation(text, session)
                reply_text = result.get("response", "")

                # 3. TTS：回复文本 → 音频
                response_format = (form.get("response_format") or "mp3").lower()
                audio_path = await self._voice_handler.text_to_speech(
                    reply_text,
                    voice=(form.get("voice") or None) if form.get("voice") else None,
                    response_format=response_format,
                )
            except RuntimeError as e:
                return JSONResponse({"error": str(e)}, status_code=503)
            except Exception as e:  # noqa: BLE001
                logger.error(f"语音对话失败: {e}")
                return JSONResponse({"error": f"语音对话失败: {e}"}, status_code=500)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            audio_bytes = _Path(audio_path).read_bytes()
            try:
                os.unlink(audio_path)
            except OSError:
                pass
            media_type = "audio/mpeg" if response_format == "mp3" else f"audio/{response_format}"
            return Response(content=audio_bytes, media_type=media_type)
