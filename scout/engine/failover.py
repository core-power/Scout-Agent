"""P0.5: 单模型默认 + Failover 收紧 + 棘轮策略.

核心设计:
- 默认全走 Flash（主模型），消除路由切换废缓存
- Failover 仅硬异常触发（超时/输出异常/用户显式要求/模型自述无法处理）
- 棘轮：升级后不自动降回，仅超时重置（30 分钟无交互）
- 无路由残留：删除 FastText/LLM 裁判/规则匹配

Failover 触发条件（收紧版）:
1. OUTPUT_MALFORMED: 解析失败/空回复
2. TIMEOUT: API 超时
3. USER_EXPLICIT: 用户显式要求深度分析
4. SELF_DECLARED_UNCERTAIN: thinking 中明确表达无法处理（仅兜底）

不包含：任务复杂度预判、语义不确定性等软信号
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("scout.engine.failover")


class FailoverReason(Enum):
    """Failover 触发原因."""
    OUTPUT_MALFORMED = "output_malformed"      # 解析失败/空回复
    TIMEOUT = "timeout"                         # API 超时
    USER_EXPLICIT = "user_explicit"             # 用户显式要求深度分析
    SELF_DECLARED_UNCERTAIN = "self_uncertain"  # thinking 中明确表达无法处理


# 模型配置 — 默认值（可被 ~/.scout/config.json 中的 failover_primary_model / failover_fallback_model 覆盖）
PRIMARY_MODEL = "deepseek-v4-flash-0731"
FALLBACK_MODEL = "qwen3.7-max"


def _load_failover_models() -> tuple[str, str]:
    """从配置读取 failover 模型名，未配置则回退到默认常量.

    Returns:
        (primary_model, fallback_model)
    """
    primary = PRIMARY_MODEL
    fallback = FALLBACK_MODEL
    try:
        from scout.config.manager import ConfigManager
        cfg = ConfigManager().load()
        if getattr(cfg, "failover_primary_model", ""):
            primary = cfg.failover_primary_model
        if getattr(cfg, "failover_fallback_model", ""):
            fallback = cfg.failover_fallback_model
    except Exception:
        # 配置读取失败时使用默认值，不影响 Failover 主流程
        pass
    return primary, fallback

# 用户显式深度分析信号
EXPLICIT_DEEP_SIGNALS = [
    "深度分析", "详细推理", "全面评估", "仔细想想",
    "/think", "/deep", "/analyze",
]

# 模型自述无法处理的硬信号（仅作兜底，不可依赖）
SELF_UNCERTAIN_SIGNALS = [
    "无法处理", "超出能力范围", "无法确定",
    "cannot handle", "beyond my capability", "i'm not sure i can",
]


@dataclass
class SessionFailoverState:
    """会话级 Failover 状态（棘轮）."""
    model_locked: str = PRIMARY_MODEL
    primary_model: str = PRIMARY_MODEL
    fallback_model: str = FALLBACK_MODEL
    locked_at: float = 0.0
    locked_reason: str = ""  # 记录锁定原因，用于审计
    last_active: float = 0.0

    RATCHET_TTL_SECONDS: int = 1800  # 30 分钟无交互重置

    def get_active_model(self) -> str:
        """棘轮逻辑：升级后不自动降回，超时才重置."""
        if self.model_locked == self.fallback_model:
            if time.time() - self.last_active > self.RATCHET_TTL_SECONDS:
                logger.info(
                    f"[Failover] 棘轮超时重置: {self.fallback_model} → {self.primary_model} "
                    f"(inactive {time.time() - self.last_active:.0f}s)"
                )
                self.model_locked = self.primary_model
                self.locked_reason = ""
        return self.model_locked

    def upgrade_to_fallback(self, reason: FailoverReason) -> bool:
        """单向升级：primary→fallback 允许，fallback→primary 禁止（除非超时）.

        Returns:
            True if upgrade happened, False if already on fallback.
        """
        if self.model_locked == self.fallback_model:
            return False
        self.model_locked = self.fallback_model
        self.locked_at = time.time()
        self.locked_reason = reason.value
        logger.info(
            f"[Failover] 棘轮升级: {self.primary_model} → {self.fallback_model} "
            f"(reason: {reason.value})"
        )
        return True

    def touch(self) -> None:
        """更新最后活跃时间."""
        self.last_active = time.time()

    def to_dict(self) -> dict:
        return {
            "model_locked": self.model_locked,
            "primary_model": self.primary_model,
            "fallback_model": self.fallback_model,
            "locked_reason": self.locked_reason,
            "locked_at": self.locked_at,
            "last_active": self.last_active,
            "is_upgraded": self.model_locked == self.fallback_model,
        }


def should_failover(
    answer: str,
    thinking: str = "",
    user_msg: str = "",
    is_timeout: bool = False,
    is_malformed: bool = False,
) -> FailoverReason | None:
    """判断是否应该 Failover 到 Fallback 模型.

    仅在明确异常或用户显式要求时触发。

    Args:
        answer: 模型生成的回答文本
        thinking: 模型的 thinking/reasoning 内容
        user_msg: 用户原始消息
        is_timeout: 是否发生了 API 超时
        is_malformed: 是否输出格式异常

    Returns:
        FailoverReason if should failover, None otherwise.
    """
    # 条件 1: API 超时
    if is_timeout:
        return FailoverReason.TIMEOUT

    # 条件 2: 输出格式异常（空回复/过短）
    if is_malformed or not answer or len(answer.strip()) < 5:
        return FailoverReason.OUTPUT_MALFORMED

    # 条件 3: 用户显式要求深度分析
    user_lower = user_msg.lower()
    if any(sig in user_lower for sig in EXPLICIT_DEEP_SIGNALS):
        return FailoverReason.USER_EXPLICIT

    # 条件 4: 模型自述无法处理（仅 thinking 中的硬信号，仅作兜底）
    # ⚠️ 此条件最脆弱：模型不一定用这些硬词，宁可漏触发不可误触发
    if thinking:
        t_lower = thinking.lower()
        if any(s in t_lower for s in SELF_UNCERTAIN_SIGNALS):
            return FailoverReason.SELF_DECLARED_UNCERTAIN

    return None


class FailoverManager:
    """Failover 管理器 — 管理多个会话的 Failover 状态.

    用法:
        mgr = FailoverManager()
        state = mgr.get_state(session_id)
        model = state.get_active_model()
        # ... 调用 LLM ...
        reason = should_failover(answer, thinking, user_msg)
        if reason:
            state.upgrade_to_fallback(reason)
    """

    def __init__(self):
        self._states: dict[str, SessionFailoverState] = {}
        # 从配置读取模型名，未配置则回退到默认常量
        self._primary_model, self._fallback_model = _load_failover_models()

    def get_state(self, session_id: str) -> SessionFailoverState:
        """获取会话的 Failover 状态（不存在则创建）."""
        if session_id not in self._states:
            self._states[session_id] = SessionFailoverState(
                model_locked=self._primary_model,
                primary_model=self._primary_model,
                fallback_model=self._fallback_model,
            )
        return self._states[session_id]

    def get_active_model(self, session_id: str) -> str:
        """获取会话当前应使用的模型."""
        state = self.get_state(session_id)
        state.touch()
        return state.get_active_model()

    def try_failover(
        self,
        session_id: str,
        answer: str,
        thinking: str = "",
        user_msg: str = "",
        is_timeout: bool = False,
        is_malformed: bool = False,
    ) -> FailoverReason | None:
        """尝试 Failover，返回触发原因（None 表示不触发）."""
        reason = should_failover(answer, thinking, user_msg, is_timeout, is_malformed)
        if reason:
            state = self.get_state(session_id)
            state.upgrade_to_fallback(reason)
        return reason

    def cleanup_stale(self, max_age_seconds: int = 3600) -> int:
        """清理过期会话状态，返回清理数量."""
        now = time.time()
        stale = [
            sid for sid, s in self._states.items()
            if now - s.last_active > max_age_seconds
        ]
        for sid in stale:
            del self._states[sid]
        return len(stale)

    def get_stats(self) -> dict:
        """获取 Failover 统计."""
        total = len(self._states)
        upgraded = sum(1 for s in self._states.values() if s.model_locked == self._fallback_model)
        return {
            "total_sessions": total,
            "upgraded_sessions": upgraded,
            "upgrade_ratio": round(upgraded / total, 4) if total > 0 else 0,
        }


# ── 全局单例 ──
_manager: FailoverManager | None = None


def get_failover_manager() -> FailoverManager:
    """获取全局 FailoverManager 实例."""
    global _manager
    if _manager is None:
        _manager = FailoverManager()
    return _manager
