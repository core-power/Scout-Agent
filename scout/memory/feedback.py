"""用户反馈存储 — 消息级 👍/👎 落盘 + 接入自进化链路.

设计（2026-09-24）：Web/渠道端对每条回复的**显式评价**是最强的学习信号，
此前完全缺失（项目主打「自进化」，却只有失败自愈 heal_loop，没有用户反馈源）。
本模块把反馈持久化到 ``<DATA_DIR>/feedback.jsonl``（JSON Lines，便于追加/审计），
并可选把「点踩 + 原因」写入记忆库，让跨会话上下文组装与技能蒸馏感知用户不满，
形成「用户反馈 → 记忆/技能」的闭环。

纯函数设计（不依赖 FastAPI/Agent），便于单测；HTTP 封装见
``scout/adapters/web/routes/feedback.py``。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_VALID_RATINGS = ("up", "down")


def _feedback_path() -> Path:
    """反馈落盘路径（复用全局数据目录，与 webhooks.json 等同级）."""
    from scout.config.paths import DATA_DIR

    return Path(DATA_DIR) / "feedback.jsonl"


def record_feedback(
    *,
    rating: str,
    session_id: str = "",
    message_id: str = "",
    reason: str = "",
    comment: str = "",
    question: str = "",
    answer: str = "",
) -> dict[str, Any]:
    """落盘一条反馈，返回带 id/ts 的完整记录.

    Args:
        rating: ``"up"`` 或 ``"down"``（其他值抛 ValueError）。
        reason: 点踩原因（枚举标签，如「答非所问」「未完成」「太慢」「工具用错」）。
        comment: 用户自由补充（截断到 2000 字）。
        question/answer: 触发反馈的用户问题与助手回复（截断，供后续分析/蒸馏）。

    Raises:
        ValueError: rating 非法。
    """
    rating = (rating or "").strip().lower()
    if rating not in _VALID_RATINGS:
        raise ValueError("rating 必须是 'up' 或 'down'")
    rec: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "rating": rating,
        "session_id": (session_id or "").strip(),
        "message_id": (message_id or "").strip(),
        "reason": (reason or "").strip(),
        "comment": (comment or "").strip()[:2000],
        "question": (question or "").strip()[:2000],
        "answer": (answer or "").strip()[:4000],
    }
    path = _feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _iter_all() -> Iterator[dict[str, Any]]:
    path = _feedback_path()
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue  # 跳过损坏行，不影响其余


def list_feedback(limit: int = 50, rating: str | None = None) -> list[dict[str, Any]]:
    """读取最近反馈（最新在前）. rating 非空时按评价过滤."""
    limit = max(1, int(limit or 50))
    out = [r for r in _iter_all() if (not rating or r.get("rating") == rating)]
    return out[-limit:][::-1]


def stats() -> dict[str, Any]:
    """反馈汇总：赞/踩计数、满意度、点踩原因分布."""
    ups = downs = 0
    reasons: dict[str, int] = {}
    for r in _iter_all():
        rt = r.get("rating")
        if rt == "up":
            ups += 1
        elif rt == "down":
            downs += 1
            key = (r.get("reason") or "其他").strip() or "其他"
            reasons[key] = reasons.get(key, 0) + 1
    total = ups + downs
    return {
        "up": ups,
        "down": downs,
        "total": total,
        "satisfaction": round(ups / total, 4) if total else None,
        "down_reasons": reasons,
    }


def learn_from_negative(rec: dict[str, Any], memory_store: Any) -> int:
    """把「点踩」信号写入记忆库（低重要性），供跨会话上下文/技能蒸馏感知.

    仅处理 down；memory_store 为空或写入失败均安全返回 -1，绝不抛异常
    （反馈学习是尽力而为的旁路，不能影响主链路/HTTP 回执）。

    Returns:
        新记忆 id（>=0）或 -1（未写入/被拒/失败）。
    """
    if memory_store is None or rec.get("rating") != "down":
        return -1
    reason = (rec.get("reason") or "").strip()
    comment = (rec.get("comment") or "").strip()
    question = (rec.get("question") or "").strip()
    parts = ["用户对该回复不满意"]
    if reason:
        parts.append(f"原因：{reason}")
    if comment:
        parts.append(f"补充：{comment[:200]}")
    if question:
        parts.append(f"（问题：{question[:120]}）")
    content = "；".join(parts)
    try:
        return int(
            memory_store.add(
                content,
                category="feedback",
                importance=0.35,
                source_session=rec.get("session_id", ""),
            )
        )
    except Exception:
        logger.debug("写入反馈记忆失败（忽略）", exc_info=True)
        return -1
