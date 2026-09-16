"""Prompt 前缀缓存优化器 — 最大化 LLM API 的 KV Cache 复用率.

核心原理:
  OpenAI / DeepSeek / Anthropic 等 API 提供商均支持 Prefix Caching:
  如果两次请求的消息前缀完全一致（token 级别），服务端可复用已计算的 KV Cache，
  输入 token 费用降至 1/10（DeepSeek）或 1/2（OpenAI）。

  关键约束: **前缀必须逐 token 完全一致**，一个字符差异就全部 miss。

优化策略:
  1. 冻结前缀区: system_prompt + tools 定义 → 稳定不变
  2. 稳定中间区: few-shot 示例 + 历史消息 → 按固定顺序排列
  3. 动态尾部区: 最新用户消息 → 每次不同

  布局: [system_prompt][tools_schema][few_shot][history][latest_user_msg]
         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                     这部分每次请求完全一致 → KV Cache 命中

用法:
    optimizer = PromptCacheOptimizer()
    messages = optimizer.optimize(raw_messages, system_prompt, tools)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PrefixCacheStats:
    """前缀缓存统计."""
    total_calls: int = 0
    prefix_stable_calls: int = 0  # 前缀与上次一致的调用数
    last_prefix_hash: str = ""
    # ── 会话级统计（2026-08-16 新增，用于前端显示"缓存 XX%"）──
    session_calls: dict[str, int] = field(default_factory=dict)      # session_id -> 调用数
    session_stable: dict[str, int] = field(default_factory=dict)     # session_id -> 稳定(命中)次数
    session_last_hash: dict[str, str] = field(default_factory=dict)  # session_id -> 上次前缀hash
    session_lcp_sum: dict[str, float] = field(default_factory=dict)  # session_id -> LCP 累积和(滚动平均用)
    session_lcp_count: dict[str, int] = field(default_factory=dict)  # session_id -> 参与平均的次数
    session_last_fps: dict[str, list[str]] = field(default_factory=dict)  # session_id -> 上次消息指纹数组(相邻比较)

    @property
    def stability_rate(self) -> float:
        """前缀稳定率（越高说明 prompt 结构越稳定）."""
        if self.total_calls == 0:
            return 0.0
        return self.prefix_stable_calls / self.total_calls

    def to_dict(self) -> dict:
        return {
            "total_calls": self.total_calls,
            "prefix_stable_calls": self.prefix_stable_calls,
            "stability_rate": round(self.stability_rate, 4),
            "last_prefix_hash": self.last_prefix_hash[:8] if self.last_prefix_hash else "",
        }

    def session_hit_ratio(self, session_id: str) -> float | None:
        """某会话的缓存命中率（本地前缀稳定性推断）.

        四版（2026-08-16）：与**上一次调用**比较最长公共前缀比例，取**滚动平均**。
        反映"会话内相邻调用前缀重合的平均比例"——同一轮内高、跨轮边界低，
        数字有区分度且不会恒 100%。返回 None 表示暂无足够数据（<2 次调用）。
        """
        calls = self.session_calls.get(session_id, 0)
        lcp_sum = self.session_lcp_sum.get(session_id, 0.0)
        lcp_count = self.session_lcp_count.get(session_id, 0)
        if calls < 2 or lcp_count == 0:
            return None
        return lcp_sum / lcp_count


class PromptCacheOptimizer:
    """Prompt 前缀缓存优化器.

    职责:
    1. 确保 messages 布局对 prefix caching 友好
    2. 监控前缀稳定性（连续请求间前缀是否一致）
    3. 提供优化建议
    """

    def __init__(self):
        self.stats = PrefixCacheStats()

    def optimize(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        session_id: str = "",
    ) -> tuple[list[dict], list[dict] | None]:
        """优化消息列表，最大化前缀缓存命中率.

        策略:
        1. 确保 system 消息在最前面（已满足则跳过）
        2. 工具定义按 name 排序（确保稳定顺序）
        3. 历史消息按时间顺序排列（已满足则跳过）
        4. 记录前缀哈希用于稳定性监控（全局 + 会话级）

        Args:
            session_id: 会话 ID（可选）。传入时按会话维护前缀 hash，
                用于推断该会话的 KV Cache 命中率（API 不返回 cached 时兜底）。

        Returns:
            (optimized_messages, sorted_tools)
        """
        optimized = list(messages)  # shallow copy

        # ── 1. 工具定义排序（确保每次请求工具顺序一致）──
        sorted_tools = None
        if tools:
            sorted_tools = sorted(tools, key=lambda t: t.get("function", {}).get("name", ""))

        # ── 2. 确保 system 消息在最前 ──
        system_msgs = [m for m in optimized if m.get("role") == "system"]
        non_system = [m for m in optimized if m.get("role") != "system"]

        if len(system_msgs) > 1:
            # 多个 system 消息 → 合并为一个（减少前缀碎片）
            merged_content = "\n\n".join(
                m.get("content", "") for m in system_msgs if m.get("content")
            )
            optimized = [{"role": "system", "content": merged_content}] + non_system
        else:
            optimized = system_msgs + non_system

        # ── 3. 计算前缀哈希（用于稳定性监控）──
        prefix_hash = self._compute_prefix_hash(optimized, sorted_tools)
        self.stats.total_calls += 1
        if self.stats.last_prefix_hash and prefix_hash == self.stats.last_prefix_hash:
            self.stats.prefix_stable_calls += 1
        self.stats.last_prefix_hash = prefix_hash

        # ── 4. 会话级前缀稳定性（本地兜底缓存命中率，相邻 token 加权 LCP 滚动平均）──
        # v5 (2026-08-25)：由"消息条数比"改为"token 加权"。
        # 原因：agent 每步调用都会在尾部追加 assistant(tool_calls)+tool 两条消息，
        # 消息级 LCP 用 max(两次条数) 做分母，把"尾部新增的未破坏前缀的消息"也当
        # miss 计入，导致数字系统性低估（实测 ~81%，真实 token 命中率更高）。
        # v5 改为：逐条比对指纹，相同则累加该消息的估算 token 数，分母取本次总 token，
        # 与真实"前缀 KV Cache 命中占比"口径一致。
        if session_id:
            s = self.stats
            s.session_calls[session_id] = s.session_calls.get(session_id, 0) + 1
            fps = [self._msg_fingerprint(m) for m in optimized]
            lens = [self._msg_len_est(m) for m in optimized]
            prev_fps = s.session_last_fps.get(session_id)
            if prev_fps:
                hit_tokens = 0
                total_tokens = sum(lens)
                for a, b, ln in zip(prev_fps, fps, lens):
                    if a == b:
                        hit_tokens += ln
                    else:
                        break
                if total_tokens > 0:
                    s.session_lcp_sum[session_id] = (
                        s.session_lcp_sum.get(session_id, 0.0) + hit_tokens / total_tokens
                    )
                    s.session_lcp_count[session_id] = s.session_lcp_count.get(session_id, 0) + 1
            s.session_last_fps[session_id] = fps

        return optimized, sorted_tools

    def get_session_hit_ratio(self, session_id: str) -> float | None:
        """获取某会话的缓存命中率（本地前缀稳定率推断，API 无 cached 时兜底）. """
        return self.stats.session_hit_ratio(session_id)

    @staticmethod
    def _msg_fingerprint(m: dict) -> str:
        """单条消息的指纹（role + content + tool_calls），用于 LCP 比较."""
        try:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):  # 多模态 content
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)[:500]
            elif not isinstance(content, str):
                content = str(content)
            parts = [role, content[:300]]
            # 工具调用也计入（tool_calls 变化影响后续对齐）
            tc = m.get("tool_calls")
            if tc:
                parts.append(json.dumps(tc, ensure_ascii=False, sort_keys=True)[:300])
            raw = "|".join(parts)
            return hashlib.md5(raw.encode("utf-8")).hexdigest()
        except Exception:
            return "err"

    @staticmethod
    def _msg_len_est(m: dict) -> int:
        """估算单条消息的 token 数（用于 token 加权 LCP）.

        与 _msg_fingerprint 不同，这里用**完整**内容估算，不截断——
        指纹只需判断"是否相同"，而权重应反映真实 token 占比。
        中英混合文本按 ~3 字符/token 保守估算，tool_calls 按 JSON 长度估算。
        """
        try:
            content = m.get("content", "")
            if isinstance(content, list):  # 多模态 content
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            elif not isinstance(content, str):
                content = str(content)
            n = max(len(content) // 3, 1)
            tc = m.get("tool_calls")
            if tc:
                n += max(len(json.dumps(tc, ensure_ascii=False, sort_keys=True)) // 3, 1)
            return max(n, 1)
        except Exception:
            return 1

    @staticmethod
    def _compute_prefix_hash(messages: list[dict], tools: list[dict] | None) -> str:
        """计算消息前缀的哈希值.

        只哈希 system 消息 + 工具定义（这些应该每次一致）。
        历史消息和最新用户消息不参与哈希（它们自然会变）。
        """
        parts = []
        for m in messages:
            if m.get("role") == "system":
                parts.append(f"sys:{m.get('content', '')[:200]}")

        if tools:
            tool_names = [t.get("function", {}).get("name", "") for t in tools]
            parts.append(f"tools:{','.join(tool_names)}")

        raw = "|".join(parts)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def get_stability_report(self) -> dict:
        """获取前缀稳定性报告."""
        report = self.stats.to_dict()

        # 给出优化建议
        suggestions = []
        if self.stats.total_calls > 10 and self.stats.stability_rate < 0.8:
            suggestions.append(
                "前缀稳定率低于 80%，建议检查 system_prompt 是否每次请求都重新生成"
            )
        if self.stats.total_calls > 10 and self.stats.stability_rate >= 0.95:
            suggestions.append("前缀非常稳定，KV Cache 命中率应该很高 ✅")

        report["suggestions"] = suggestions
        return report


# ── 全局单例 ──
_optimizer: PromptCacheOptimizer | None = None


def get_prompt_cache_optimizer() -> PromptCacheOptimizer:
    """获取全局 PromptCacheOptimizer 实例."""
    global _optimizer
    if _optimizer is None:
        _optimizer = PromptCacheOptimizer()
    return _optimizer
