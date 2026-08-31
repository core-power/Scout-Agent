"""Memories 治理 — 借鉴 Codex Memories 的配置化治理.

配置键（~/.scout/memories.json，对标 Codex config.toml 的 memories.* 段）：
- generate_memories: 任务结束后是否允许生成记忆（默认 true）
- use_memories: 是否将记忆注入未来会话（默认 true）
- background_only: 仅在空闲时后台生成，不打断进行中的任务（默认 true）
- min_idle_seconds: 判定"空闲"的秒数（默认 120 — 任务结束且无新消息）
- skip_short_sessions: 会话消息数低于此值时跳过记忆生成（默认 4）
- disable_on_external_context: 使用了外部上下文（web_search/MCP）的任务不沉淀（默认 false）
- min_rate_limit_remaining_percent: 剩余额度低于此百分比时跳过生成，不跟任务抢额度（默认 20）
- security_scan: 写入前安全扫描开关（默认 true，见 scout/memory/security_scan.py）

与 starlight（记忆蒸馏器）的关系：starlight 是生成引擎，本模块是它的"调度策略层"。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR
from typing import Any

logger = logging.getLogger(__name__)

_CONFIG_PATH = _SCOUT_DATA_DIR / "memories.json"

_DEFAULTS: dict[str, Any] = {
    "generate_memories": True,
    "use_memories": True,
    "background_only": True,
    "min_idle_seconds": 120,
    "skip_short_sessions": 4,
    "disable_on_external_context": False,
    "min_rate_limit_remaining_percent": 20,
    "security_scan": True,
}


@dataclass
class MemoriesConfig:
    generate_memories: bool = True
    use_memories: bool = True
    background_only: bool = True
    min_idle_seconds: int = 120
    skip_short_sessions: int = 4
    disable_on_external_context: bool = False
    min_rate_limit_remaining_percent: int = 20
    security_scan: bool = True

    @classmethod
    def load(cls) -> "MemoriesConfig":
        """从 ~/.scout/memories.json 加载，缺失项用默认值补齐."""
        data = dict(_DEFAULTS)
        try:
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH) as f:
                    stored = json.load(f)
                if isinstance(stored, dict):
                    for k in _DEFAULTS:
                        if k in stored:
                            data[k] = stored[k]
        except Exception as e:
            logger.warning(f"memories.json 读取失败，使用默认配置: {e}")
        return cls(**data)

    def save(self) -> None:
        try:
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_CONFIG_PATH, "w") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"memories.json 保存失败: {e}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GenerationGate:
    """记忆生成闸门 — 判断"此刻是否允许为某会话生成记忆".

    集中实现 Codex Memories 的四个保护行为：
    1. 活跃/短会话跳过（skip_short_sessions）
    2. 空闲判定（min_idle_seconds — 避免总结进行中的工作）
    3. 外部上下文排除（disable_on_external_context）
    4. 额度保护（min_rate_limit_remaining_percent）
    """

    def __init__(self, config: MemoriesConfig | None = None):
        self.config = config or MemoriesConfig.load()

    def should_generate(
        self,
        message_count: int,
        last_activity_ts: float,
        used_external_context: bool = False,
        rate_limit_remaining_percent: float | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """返回 (是否允许, 原因)."""
        cfg = self.config
        if not cfg.generate_memories:
            return False, "generate_memories 已关闭"

        if message_count < cfg.skip_short_sessions:
            return False, f"会话过短({message_count}<{cfg.skip_short_sessions}条)"

        now = now or time.time()
        idle = now - last_activity_ts
        if cfg.background_only and idle < cfg.min_idle_seconds:
            return False, f"会话仍活跃(空闲{idle:.0f}s<{cfg.min_idle_seconds}s)"

        if cfg.disable_on_external_context and used_external_context:
            return False, "任务使用了外部上下文(MCP/web_search)，已排除"

        if rate_limit_remaining_percent is not None:
            if rate_limit_remaining_percent < cfg.min_rate_limit_remaining_percent:
                return False, (
                    f"剩余额度{rate_limit_remaining_percent:.0f}%低于保护阈值"
                    f"{cfg.min_rate_limit_remaining_percent}%，不与任务抢额度"
                )

        return True, "ok"

    def should_inject(self) -> bool:
        """是否允许把记忆注入会话."""
        return self.config.use_memories
