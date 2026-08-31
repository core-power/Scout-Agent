"""Checkpoint 系统 — 长任务的断点恢复.

核心功能：
1. 在执行过程中定期保存状态快照
2. 服务重启或中断后能从断点恢复
3. 支持手动和自动 checkpoint

设计原则：
- 轻量级：只保存必要的状态数据
- 非侵入：通过装饰器/钩子集成，不改动核心逻辑
- 可恢复：能从任意 checkpoint 恢复执行
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from scout.config.paths import DATA_DIR as _SCOUT_DATA_DIR

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """执行状态快照."""
    id: str
    session_id: str
    step: int
    status: str  # "thinking" | "acting" | "paused" | "error"
    messages_snapshot: list[dict]  # 消息历史快照
    pending_tools: list[dict] = field(default_factory=list)  # 待执行的工具调用
    completed_tools: list[dict] = field(default_factory=list)  # 已完成的工具调用
    budget_used: int = 0
    budget_max: int = 30
    context_summary: str = ""  # 上下文摘要（用于恢复时快速理解）
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为可序列化的字典."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "step": self.step,
            "status": self.status,
            "messages_snapshot": self.messages_snapshot,
            "pending_tools": self.pending_tools,
            "completed_tools": self.completed_tools,
            "budget_used": self.budget_used,
            "budget_max": self.budget_max,
            "context_summary": self.context_summary,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Checkpoint:
        """从字典反序列化."""
        return cls(
            id=data["id"],
            session_id=data["session_id"],
            step=data["step"],
            status=data["status"],
            messages_snapshot=data["messages_snapshot"],
            pending_tools=data.get("pending_tools", []),
            completed_tools=data.get("completed_tools", []),
            budget_used=data.get("budget_used", 0),
            budget_max=data.get("budget_max", 60),
            context_summary=data.get("context_summary", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
            metadata=data.get("metadata", {}),
        )


class CheckpointManager:
    """Checkpoint 管理器.

    用法：
        manager = CheckpointManager()
        
        # 保存 checkpoint
        checkpoint = manager.save_checkpoint(
            session_id="xxx",
            step=5,
            status="acting",
            messages=[...],
            pending_tools=[...],
            completed_tools=[...]
        )
        
        # 恢复 checkpoint
        checkpoint = manager.load_checkpoint(session_id="xxx")
        if checkpoint:
            # 从 checkpoint 恢复执行
            ...
    """

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(
            storage_path
            if storage_path is not None
            else str(_SCOUT_DATA_DIR / "checkpoints")
        ).expanduser()
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def save_checkpoint(
        self,
        session_id: str,
        step: int,
        status: str,
        messages: list[dict],
        pending_tools: list[dict] | None = None,
        completed_tools: list[dict] | None = None,
        budget_used: int = 0,
        budget_max: int = 60,
        context_summary: str = "",
        metadata: dict | None = None,
    ) -> Checkpoint:
        """保存 checkpoint."""
        import uuid

        checkpoint = Checkpoint(
            id=str(uuid.uuid4()),
            session_id=session_id,
            step=step,
            status=status,
            messages_snapshot=messages,
            pending_tools=pending_tools or [],
            completed_tools=completed_tools or [],
            budget_used=budget_used,
            budget_max=budget_max,
            context_summary=context_summary,
            metadata=metadata or {},
        )

        # 保存到文件
        checkpoint_file = self.storage_path / f"{session_id}.json"
        with open(checkpoint_file, "w", encoding="utf-8") as f:
            json.dump(checkpoint.to_dict(), f, ensure_ascii=False, indent=2)

        logger.info(f"Saved checkpoint {checkpoint.id} for session {session_id} at step {step}")
        return checkpoint

    def load_checkpoint(self, session_id: str) -> Checkpoint | None:
        """加载最新的 checkpoint."""
        checkpoint_file = self.storage_path / f"{session_id}.json"
        
        if not checkpoint_file.exists():
            return None

        try:
            with open(checkpoint_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            checkpoint = Checkpoint.from_dict(data)
            logger.info(f"Loaded checkpoint {checkpoint.id} for session {session_id}")
            return checkpoint
        except Exception as e:
            logger.error(f"Failed to load checkpoint for session {session_id}: {e}")
            return None

    def delete_checkpoint(self, session_id: str) -> bool:
        """删除 checkpoint."""
        checkpoint_file = self.storage_path / f"{session_id}.json"
        
        if checkpoint_file.exists():
            checkpoint_file.unlink()
            logger.info(f"Deleted checkpoint for session {session_id}")
            return True
        return False

    def list_checkpoints(self) -> list[dict]:
        """列出所有 checkpoint."""
        checkpoints = []
        for checkpoint_file in self.storage_path.glob("*.json"):
            try:
                with open(checkpoint_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                checkpoints.append({
                    "session_id": data["session_id"],
                    "step": data["step"],
                    "status": data["status"],
                    "created_at": data["created_at"],
                    "budget_used": data.get("budget_used", 0),
                })
            except Exception as e:
                logger.warning(f"Failed to read checkpoint file {checkpoint_file}: {e}")
        
        # 按创建时间排序
        checkpoints.sort(key=lambda x: x["created_at"], reverse=True)
        return checkpoints
