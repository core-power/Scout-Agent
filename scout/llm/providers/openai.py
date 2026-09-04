"""OpenAI 兼容 LLM Provider — 支持 OpenAI / OpenRouter / DashScope 等所有兼容端点.

超时与重试策略 (2026-08-04):
- 可重试错误（超时/限流/网络/5xx）自动重试，指数退避
- 429 限流使用更长退避（5s 起步）
- 流式响应：连接建立阶段可重试，首 token 输出后不再重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from urllib.parse import urlparse

from openai import AsyncOpenAI

from scout.core.types import Delta, LLMResponse, ToolCall
from scout.llm.base import LLMClient
from scout.llm.prompt_cache import get_prompt_cache_optimizer

logger = logging.getLogger("scout.llm.openai")

# 可重试错误的关键词（借鉴 CowAgent）
_RETRYABLE_KEYWORDS = (
    "timeout", "timed out", "connection", "network",
    "rate limit", "rate_limit", "429", "overloaded",
    "500", "502", "503", "504", "internal_error",
    "server_error", "service_unavailable", "temporarily",
)


def _is_retryable(error: BaseException) -> bool:
    """判断错误是否值得重试."""
    msg = str(error).lower()
    return any(kw in msg for kw in _RETRYABLE_KEYWORDS)


def _normalize_base_url(url: str | None) -> str | None:
    """OpenAI 兼容端点规范化 (2026-09-04).

    - 去首尾空白
    - 仅 scheme://host[:port]（无路径）时自动补 /v1：SDK 会在 base_url 后直接拼
      /chat/completions，漏写 /v1 是自定义模型 401 "Authorization failed." 的高频根因
    - 已带路径（/v1、/compatible-mode/v1、/api/v3 等）保持原样，不越权改写
    """
    if not url:
        return url
    url = url.strip()
    if not url:
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    if not p.scheme:
        return url  # 无协议（如 localhost:8000）不处理，交 SDK 报清晰错误
    if not p.path or p.path == "/":
        return url.rstrip("/") + "/v1"
    return url


class OpenAIProvider(LLMClient):
    """OpenAI 兼容客户端 — 支持所有兼容 OpenAI API 的服务."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        # ─── 重试与超时 ───
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        retry_backoff_max: float = 30.0,
        stream_timeout: int = 180,
        request_timeout: int = 90,
        # ─── 全局限流（防 DashScope 429 limit_burst_rate）───
        min_request_interval: float = 1.0,  # 每请求最小间隔秒数（令牌桶速率）
    ):
        # 2026-09-04：Key/Model/URL 规范化 —— 首尾空白是 401 "Authorization failed."
        # 高频根因（复制粘贴带空格/换行）；bare host 自动补 /v1。
        # 收敛在此根治层，聊天/测试连接/fallback/vision 全部路径统一受益。
        if api_key is not None:
            api_key = api_key.strip()
        self.model = (model or "").strip()
        base_url = _normalize_base_url(base_url)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.retry_backoff_max = retry_backoff_max
        self._stream_timeout = stream_timeout
        self._request_timeout = request_timeout
        self.min_request_interval = min_request_interval
        # 滑动窗口限流：窗口内允许的突发请求数（默认 5，Multi-Agent 并行子代理更多也能扛）
        self._burst_size = 5
        self._req_times = []
        self._rate_lock = asyncio.Lock()

        # 默认 read timeout 600s 太长，用配置的 request_timeout 防止卡死
        from openai import Timeout
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=Timeout(
                connect=5.0,
                read=float(request_timeout),
                write=60.0,
                pool=60.0,
            ),
            max_retries=0,  # 由我们自己控制重试逻辑
        )

    def _backoff_delay(self, attempt: int, error: BaseException) -> float:
        """计算退避等待时间.

        429/限流: 5s → 10s → 15s（线性，上限 backoff_max）
        其他:     base × (attempt+1)，即 2s → 4s → 6s（上限 backoff_max）
        """
        msg = str(error).lower()
        if "429" in msg or "rate" in msg:
            delay = 5.0 * (attempt + 1)
        else:
            delay = self.retry_backoff_base * (attempt + 1)
        return min(delay, self.retry_backoff_max)

    @staticmethod
    def _convert_message_tool_calls(sdk_calls) -> list[ToolCall]:
        """把非流式 SDK 的 tool_calls 转成内部 ToolCall.

        SDK 返回 ChoiceDeltaToolCall/ChatCompletionMessageToolCall 对象，
        arguments 是 JSON 字符串，需解析成 dict，否则 pydantic 校验失败。
        """
        out: list[ToolCall] = []
        for tc in sdk_calls or []:
            fn = getattr(tc, "function", None)
            if not fn:
                continue
            try:
                args = json.loads(fn.arguments) if fn.arguments else {}
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {"_raw": args}
            out.append(ToolCall(name=fn.name or "", arguments=args))
        return out

    # 各类实现中"缓存命中 token 数"的常见字段名（按优先级尝试）
    _CACHED_FIELD_NAMES = (
        "cached_tokens",              # OpenAI / 部分 DashScope 兼容
        "prompt_cache_hit_tokens",    # 部分国产实现
        "cache_hit_tokens",           # 部分兼容网关
        "cached_input_tokens",        # 部分 DeepSeek 网关
        "input_cache_tokens",         # Anthropic 风格 / 部分代理
        "cache_read_tokens",          # 部分 Moonshot/Kimi 风格
    )

    @staticmethod
    def _extract_cached(usage_obj) -> int:
        """从 usage 提取缓存命中 token 数（健壮版，兼容 dict 与对象、多种字段命名）.

        支持:
        - OpenAI 标准: usage.prompt_tokens_details.cached_tokens
        - DashScope 兼容: usage.cached_tokens (顶层) / prompt_tokens_details.cached_tokens
        - DeepSeek / 国产网关: prompt_cache_hit_tokens / cached_input_tokens /
          cache_read_tokens 等命名
        - usage 可能是 SDK 对象，也可能是 dict（兼容网关/代理常见）

        prompt_tokens_details 本身既可能是对象，也可能是 dict。
        """
        if usage_obj is None:
            return 0

        def _get(container, key):
            """同时兼容对象属性与 dict 取键."""
            try:
                if isinstance(container, dict):
                    return container.get(key)
                return getattr(container, key, None)
            except Exception:
                return None

        try:
            # 1) 嵌套: usage.prompt_tokens_details.cached_tokens
            details = _get(usage_obj, "prompt_tokens_details")
            if details is not None:
                for name in OpenAIProvider._CACHED_FIELD_NAMES:
                    v = _get(details, name)
                    if v:
                        try:
                            return int(v)
                        except (TypeError, ValueError):
                            pass

            # 2) 顶层字段（多命名逐一尝试）
            for name in OpenAIProvider._CACHED_FIELD_NAMES:
                v = _get(usage_obj, name)
                if v:
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        pass

            return 0
        except Exception:
            return 0

    @staticmethod
    def _usage_val(usage, key):
        """从 usage（可能是 SDK 对象或 dict）安全取字段值."""
        if usage is None:
            return None
        try:
            if isinstance(usage, dict):
                return usage.get(key)
            return getattr(usage, key, None)
        except Exception:
            return None

    def _estimate_usage(messages: list[dict], stream_chars: int, tool_acc: dict | None = None) -> dict:
        """估算 token 用量（DashScope 流式不返回 usage 时兜底）.

        中英混合文本平均 ~1 token / 1.5 字符；prompt 含 system+history+tools。
        """
        try:
            prompt_chars = 0
            for m in messages:
                c = m.get("content", "")
                if isinstance(c, str):
                    prompt_chars += len(c)
                elif isinstance(c, list):
                    prompt_chars += sum(len(str(x.get("text", ""))) for x in c if isinstance(x, dict))
            # tools 描述也计入 prompt
            tool_chars = 0
            if tool_acc:
                for slot in tool_acc.values():
                    tool_chars += len(slot.get("name", "")) + len(slot.get("arguments", ""))
            prompt_tokens = max(1, (prompt_chars + tool_chars) // 2)
            completion_tokens = max(1, stream_chars // 2)
            return {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cached_tokens": 0,  # 估算无真实缓存数据，保持结构一致
            }
        except Exception:
            return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cached_tokens": 0}

    def _record_usage(self, usage_data: dict, latency_start: float, session_id: str) -> None:
        """写入 token 用量到 usage.db（静默失败，不阻塞主流程）."""
        if not usage_data:
            return
        try:
            from scout.llm.tracker import token_tracker
            latency_ms = int((__import__("time").time() - latency_start) * 1000)
            _cached = usage_data.get("cached_tokens", 0) or 0
            token_tracker.record(
                self.model,
                provider=getattr(self, "_provider_name", "openai"),
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                cached_tokens=_cached,
                cache_hit=_cached > 0,
                latency_ms=latency_ms,
                session_id=session_id,
            )
        except Exception as _rec_err:
            import traceback
            __import__("logging").getLogger("scout.llm.providers.openai").warning(
                f"[usage-record] 写入失败: {_rec_err!r}\n{traceback.format_exc()}"
            )

    def _infer_cached_tokens(self, usage_data: dict, session_id: str) -> dict:
        """上游未上报真实 cached_tokens 时，用本地前缀稳定率推断并回填.

        背景：DashScope 等上游的**流式**响应通常不携带 usage（cached_tokens 缺失），
        导致模型监控页缓存命中率长期趋近 0（近 7 天仅 9.4%）。
        此处用 prompt_cache 的会话级相邻 LCP 稳定率（0~1）乘 prompt 得到推断值，
        仅用于监控展示，不改变实际请求行为。
        """
        try:
            if usage_data and not usage_data.get("cached_tokens") and session_id:
                ratio = get_prompt_cache_optimizer().get_session_hit_ratio(session_id)
                if ratio:
                    prompt = usage_data.get("prompt_tokens") or 0
                    usage_data["cached_tokens"] = int(prompt * ratio)
                    usage_data["cache_source"] = "local"  # 标记来源：本地推断
        except Exception:
            pass
        return usage_data

    @staticmethod
    def _assemble_tool_calls(tool_acc: dict[int, dict]) -> list[ToolCall]:
        """把流式累积的 tool_call 碎片组装成完整 ToolCall 列表."""
        out: list[ToolCall] = []
        for _idx in sorted(tool_acc.keys()):
            slot = tool_acc[_idx]
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {"_raw": args}
            out.append(ToolCall(name=slot["name"] or "", arguments=args))
        return out

    async def _throttle(self):
        """滑动窗口限流：允许短时间小突发，限制长期平均速率。

        兼容两种需求：
        - 防 DashScope 429 limit_burst_rate：长期速率受限（突发数量窗口内用尽）
        - 保 Multi-Agent 并行：窗口内允许 burst 个请求同时发出，不逐请求串行等待

        默认：1s 窗口内最多 3 个请求 → 平均 3 req/s，突发 3 个并发可同时过。
        """
        interval = self.min_request_interval      # 限流窗口（秒）
        burst = getattr(self, "_burst_size", 3)   # 窗口内允许的突发请求数
        if interval <= 0 or burst <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            # 维护窗口内时间戳队列
            if hasattr(self, "_req_times"):
                # 清理超过窗口的旧时间戳
                self._req_times = [t for t in self._req_times if now - t < interval]
            else:
                self._req_times = []
            if len(self._req_times) >= burst:
                # 窗口已满 → 等到最旧请求滑出窗口
                wait = interval - (now - self._req_times[0])
                if wait > 0:
                    await asyncio.sleep(wait)
            self._req_times.append(time.monotonic())

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """异步完成（带重试）."""
        await self._throttle()
        timeout = kwargs.pop("_timeout", self._request_timeout)
        session_id = kwargs.pop("session_id", "") or kwargs.pop("_session_id", "")
        start_time = __import__("time").time()
        # ── Prompt 前缀缓存优化：稳定 system+tools 前缀，提升 KV Cache 命中率 ──
        _pco = get_prompt_cache_optimizer()
        messages, tools = _pco.optimize(messages, tools, session_id=session_id)
        params = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.pop("temperature", self.temperature),
        }
        if self.max_tokens:
            params["max_tokens"] = self.max_tokens
        if tools:
            params["tools"] = tools
        # 2026-08-12 修复：extra_body (如 enable_thinking) 需通过 SDK 的 extra_body 参数
        # 合并到请求体（此前未传递导致思维链开关失效）。enable_thinking 作为顶层参数
        # 会被 SDK 拒绝，必须放 extra_body 里。
        extra_body = kwargs.pop("extra_body", None)
        if isinstance(extra_body, dict):
            params["extra_body"] = extra_body

        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await asyncio.wait_for(
                    self.client.chat.completions.create(**params),
                    timeout=timeout,
                )
                choice = resp.choices[0]
                usage_obj = getattr(resp, "usage", None)
                result = LLMResponse(
                    content=choice.message.content or "",
                    tool_calls=self._convert_message_tool_calls(choice.message.tool_calls),
                    usage={
                        "prompt_tokens": self._usage_val(usage_obj, "prompt_tokens") or 0,
                        "completion_tokens": self._usage_val(usage_obj, "completion_tokens") or 0,
                        "total_tokens": self._usage_val(usage_obj, "total_tokens") or 0,
                    } if usage_obj else {},
                )
                # 记录 token 用量
                try:
                    from scout.llm.tracker import token_tracker
                    latency_ms = int((time.time() - start_time) * 1000)
                    # 提取缓存命中信息 (OpenAI SDK: usage.prompt_tokens_details.cached_tokens)
                    _cached = self._extract_cached(resp.usage)
                    _prompt = self._usage_val(resp.usage, "prompt_tokens") or 0
                    if not _cached and session_id and _prompt:
                        # 上游未上报 cached → 本地前缀稳定率推断（仅监控展示）
                        _ratio = get_prompt_cache_optimizer().get_session_hit_ratio(session_id)
                        if _ratio:
                            _cached = int(_prompt * _ratio)
                    token_tracker.record(
                        self.model,
                        provider=getattr(self, "_provider_name", "openai"),
                        prompt_tokens=_prompt,
                        completion_tokens=self._usage_val(resp.usage, "completion_tokens") or 0,
                        cached_tokens=_cached,
                        cache_hit=_cached > 0,
                        latency_ms=latency_ms,
                        session_id=session_id,
                    )
                    # 同步补全返回给上层 usage 中的 cached_tokens（此前缺失导致
                    # cache_hit_rate 在全链路丢失）
                    if result.usage and _cached:
                        result.usage["cached_tokens"] = _cached
                except Exception as _rec_err:
                    import traceback
                    __import__("logging").getLogger("scout.llm.providers.openai").warning(
                        f"[usage-record-complete] 写入失败: {_rec_err!r}\n{traceback.format_exc()}"
                    )
                return result

            except Exception as e:
                last_error = e
                # 配额耗尽类 429（insufficient_quota）重试无意义 → 立即抛出让 fallback 接管
                _emsg = str(e).lower()
                if "insufficient_quota" in _emsg or "quota" in _emsg and "exceeded" in _emsg:
                    raise
                if not _is_retryable(e) or attempt >= self.max_retries:
                    raise
                delay = self._backoff_delay(attempt, e)
                logger.warning(
                    f"LLM complete 失败 (attempt {attempt + 1}/{self.max_retries + 1}): "
                    f"{e} — {delay:.0f}s 后重试"
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[Delta]:
        """流式完成（带重试）.

        重试策略：
        - 连接建立 / 首 chunk 前失败 → 可重试
        - 已经开始输出内容后失败 → 不重试（避免重复内容）
        """
        await self._throttle()
        stream_timeout = kwargs.pop("_stream_timeout", self._stream_timeout)
        session_id = kwargs.pop("session_id", "") or kwargs.pop("_session_id", "")
        start_time = __import__("time").time()
        # ── Prompt 前缀缓存优化 ──
        _pco = get_prompt_cache_optimizer()
        messages, tools = _pco.optimize(messages, tools, session_id=session_id)
        params = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": kwargs.pop("temperature", self.temperature),
        }
        if self.max_tokens:
            params["max_tokens"] = self.max_tokens
        if tools:
            params["tools"] = tools
        # 2026-08-15 修复：始终请求 usage 统计（此前仅在带 tools 时设置，导致
        # 无工具调用（简单问答）的 token 消耗完全无法追踪）
        params["stream_options"] = {"include_usage": True}
        # 2026-08-12 修复：extra_body (如 enable_thinking) 需通过 SDK 的 extra_body 参数
        # 合并到请求体（此前未传递导致思维链开关失效）。enable_thinking 作为顶层参数
        # 会被 SDK 拒绝，必须放 extra_body 里。
        extra_body = kwargs.pop("extra_body", None)
        if isinstance(extra_body, dict):
            params["extra_body"] = extra_body

        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            produced_any = False  # 是否已经向调用方输出过内容
            usage_data = None
            _stream_chars = 0  # 流式输出字符累积（用于 DashScope 无 usage 时估算）
            try:
                # 2026-08-28 修复：create() 建连/首响应若无保护可能永久挂起
                # （httpx read 超时只覆盖已有连接的读取，不覆盖连接建立阶段）。
                # 用 wait_for 包裹，超时走下方 TimeoutError 重试逻辑。
                _connect_timeout = min(60.0, float(stream_timeout or 180))
                stream = await asyncio.wait_for(
                    self.client.chat.completions.create(**params),
                    timeout=_connect_timeout,
                )
                # 流式 tool_calls 按 index 累积（SDK 把 name/arguments 拆成多个 chunk 推送）
                tool_acc: dict[int, dict] = {}
                async with asyncio.timeout(stream_timeout):
                    async for chunk in stream:
                        if not chunk.choices:
                            # 最后一个 chunk 包含 usage（OpenAI 兼容；部分网关以 dict 返回）
                            # 健壮提取：usage 既可能是对象也可能是 dict
                            chunk_usage = getattr(chunk, "usage", None)
                            if chunk_usage:
                                _pt = self._usage_val(chunk_usage, "prompt_tokens")
                                _ct = self._usage_val(chunk_usage, "completion_tokens")
                                _tt = self._usage_val(chunk_usage, "total_tokens")
                                if _pt is not None or _tt is not None:
                                    _cached = self._extract_cached(chunk_usage)
                                    usage_data = {
                                        "prompt_tokens": _pt or 0,
                                        "completion_tokens": _ct or 0,
                                        "total_tokens": _tt or (_pt or 0) + (_ct or 0),
                                        "cached_tokens": _cached,
                                    }
                            continue
                        delta = chunk.choices[0].delta
                        if delta.content:
                            produced_any = True
                            _stream_chars += len(delta.content)
                            yield Delta(text=delta.content)
                        # 推理模型的思考内容 (reasoning_content) 单独流式推送
                        if getattr(delta, "reasoning_content", None):
                            produced_any = True
                            _stream_chars += len(delta.reasoning_content)
                            yield Delta(text="", reasoning=delta.reasoning_content)
                        if delta.tool_calls:
                            produced_any = True
                            for tcc in delta.tool_calls:
                                slot = tool_acc.setdefault(
                                    tcc.index or 0, {"name": "", "arguments": ""}
                                )
                                if tcc.function:
                                    if tcc.function.name:
                                        slot["name"] += tcc.function.name
                                    if tcc.function.arguments:
                                        slot["arguments"] += tcc.function.arguments
                        if chunk.choices[0].finish_reason:
                            assembled = self._assemble_tool_calls(tool_acc)
                            # ── 用量记录（yield 之前写入，确保 done 事件能查到）──
                            usage_data = usage_data or OpenAIProvider._estimate_usage(messages, _stream_chars, tool_acc)
                            usage_data = self._infer_cached_tokens(usage_data, session_id)
                            self._record_usage(usage_data, latency_start=start_time, session_id=session_id)
                            # 与 done 一起发出 — agent 在 delta.done 时读取 tool_calls
                            yield Delta(done=True, usage=usage_data or {}, tool_calls=assembled)
                            return
                # 流正常结束但没有显式 finish_reason
                # ── 用量记录：真实 usage 优先，DashScope 流式无 usage 时估算 ──
                usage_data = usage_data or OpenAIProvider._estimate_usage(messages, _stream_chars, tool_acc)
                usage_data = self._infer_cached_tokens(usage_data, session_id)
                self._record_usage(usage_data, latency_start=start_time, session_id=session_id)
                yield Delta(done=True, usage=usage_data or {})
                return

            except TimeoutError:
                last_error = TimeoutError(
                    f"Stream timeout after {stream_timeout}s"
                )
                if produced_any or attempt >= self.max_retries:
                    # 已有输出或重试用尽 → 告知用户
                    yield Delta(text="\n\n[⏱ 响应超时，请重试]", done=True)
                    return
                delay = self._backoff_delay(attempt, last_error)
                logger.warning(
                    f"Stream 超时 (attempt {attempt + 1}/{self.max_retries + 1}) "
                    f"— {delay:.0f}s 后重试"
                )
                await asyncio.sleep(delay)

            except Exception as e:
                last_error = e
                # 配额耗尽类 429 直接失败（重试无意义，fallback 层已接管）
                _emsg = str(e).lower()
                if "insufficient_quota" in _emsg or "quota" in _emsg and "exceeded" in _emsg:
                    yield Delta(text=f"\n\n[❌ 调用失败: {e}]", done=True)
                    return
                if produced_any or not _is_retryable(e) or attempt >= self.max_retries:
                    yield Delta(text=f"\n\n[❌ 调用失败: {e}]", done=True)
                    return
                delay = self._backoff_delay(attempt, e)
                logger.warning(
                    f"Stream 失败 (attempt {attempt + 1}/{self.max_retries + 1}): "
                    f"{e} — {delay:.0f}s 后重试"
                )
                await asyncio.sleep(delay)

        # 理论上不会到这里
        yield Delta(text="\n\n[❌ 调用失败: 重试耗尽]", done=True)
