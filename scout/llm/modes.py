"""LLM API 模式适配 — 借鉴 Hermes 的 3 种 API 模式.

所有模式最终统一到 OpenAI 消息格式。
1. chat_completions: 标准 OpenAI 兼容
2. anthropic_messages: Anthropic 原生格式
3. codex_responses: OpenAI Responses API
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from scout.core.types import LLMResponse, ToolCall


class APIMode(str, Enum):
    """API 调用模式."""
    CHAT_COMPLETIONS = "chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    CODEX_RESPONSES = "codex_responses"


class ModeAdapter:
    """API 模式适配器 — 统一不同 API 格式."""

    @staticmethod
    def detect_mode(provider: str, base_url: str = "", model: str = "") -> APIMode:
        """自动检测 API 模式."""
        provider = provider.lower()
        if provider == "claude" or "anthropic.com" in base_url:
            return APIMode.ANTHROPIC_MESSAGES
        if "codex" in model or "responses" in model:
            return APIMode.CODEX_RESPONSES
        return APIMode.CHAT_COMPLETIONS

    @staticmethod
    def format_messages(messages: list[dict], mode: APIMode) -> list[dict]:
        """将统一消息格式转换为目标 API 格式."""
        if mode == APIMode.CHAT_COMPLETIONS:
            return messages
        elif mode == APIMode.ANTHROPIC_MESSAGES:
            return AnthropicAdapter.convert_messages(messages)
        elif mode == APIMode.CODEX_RESPONSES:
            return CodexAdapter.convert_messages(messages)
        return messages

    @staticmethod
    def parse_response(raw: dict, mode: APIMode) -> LLMResponse:
        """将 API 响应解析为统一 LLMResponse."""
        if mode == APIMode.CHAT_COMPLETIONS:
            return ChatCompletionsParser.parse(raw)
        elif mode == APIMode.ANTHROPIC_MESSAGES:
            return AnthropicParser.parse(raw)
        elif mode == APIMode.CODEX_RESPONSES:
            return CodexParser.parse(raw)
        return LLMResponse(content=str(raw))


class AnthropicAdapter:
    """Anthropic 消息格式转换."""

    @staticmethod
    def convert_messages(messages: list[dict]) -> list[dict]:
        """OpenAI 格式 → Anthropic 格式."""
        result = []
        system_content = []
        for msg in messages:
            if msg["role"] == "system":
                system_content.append(msg["content"])
            elif msg["role"] == "user":
                result.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                result.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                # Anthropic 用 tool_result 格式
                result.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg["content"],
                    }],
                })

        # system 消息放在最前面
        if system_content:
            result.insert(0, {"role": "system", "content": "\n\n".join(system_content)})

        return result


class CodexAdapter:
    """OpenAI Responses API 格式转换."""

    @staticmethod
    def convert_messages(messages: list[dict]) -> list[dict]:
        """OpenAI Chat 格式 → Responses API 格式."""
        # Responses API 用 input 而非 messages
        result = []
        for msg in messages:
            result.append({
                "role": msg["role"],
                "content": msg.get("content", ""),
            })
        return result


class ChatCompletionsParser:
    """标准 Chat Completions 响应解析."""

    @staticmethod
    def parse(raw: dict) -> LLMResponse:
        choice = raw.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = []
        if message.get("tool_calls"):
            for tc in message["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"]) if tc["function"].get("arguments") else {}
                except (json.JSONDecodeError, KeyError):
                    args = {}
                tool_calls.append(ToolCall(name=tc["function"]["name"], arguments=args))
        return LLMResponse(
            content=message.get("content", "") or "",
            tool_calls=tool_calls,
            usage=raw.get("usage", {}),
            finish_reason=choice.get("finish_reason", ""),
        )


class AnthropicParser:
    """Anthropic 响应解析."""

    @staticmethod
    def parse(raw: dict) -> LLMResponse:
        content_blocks = raw.get("content", [])
        text_parts = []
        tool_calls = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                ))
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=raw.get("usage", {}),
            finish_reason=raw.get("stop_reason", ""),
        )


class CodexParser:
    """Responses API 响应解析."""

    @staticmethod
    def parse(raw: dict) -> LLMResponse:
        output = raw.get("output", [])
        text_parts = []
        tool_calls = []
        for item in output:
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        text_parts.append(content.get("text", ""))
            elif item.get("type") == "function_call":
                try:
                    args = json.loads(item.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(name=item.get("name", ""), arguments=args))
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            usage=raw.get("usage", {}),
            finish_reason=raw.get("status", ""),
        )
