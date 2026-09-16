"""Delegate-scoped communication hub (2026-09-07).

Solves two problems:
1. Mid-run findings/blockers of subagents were invisible to the main agent
   (fire-and-forget: only the final conclusion came back).
2. Parallel subagents were fully isolated - A's findings could not feed B.

DelegateBroker (in-process singleton via runtime.get_broker()):
- publish / drain: the subagent "report" tool writes; the delegating tool
  drains after subagents finish and appends the digest into its tool output
  (append-only, prefix-cache friendly).
- Shared namespace convention: subagents of one parallel_delegate batch share
  the "shared:delegation:{delegation_id}" namespace in SharedStateManager,
  usable through the shared_data tool for subagent-to-subagent exchange.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class SubReport:
    delegation_id: str
    sender: str
    kind: str  # progress | finding | blocker
    content: str


class DelegateBroker:
    """Message board for the delegation window: subagents publish, main agent drains."""

    MAX_PER_DELEGATION = 50

    def __init__(self) -> None:
        self._reports = defaultdict(list)

    def publish(self, r: SubReport) -> None:
        bucket = self._reports[r.delegation_id]
        bucket.append(r)
        if len(bucket) > self.MAX_PER_DELEGATION:
            del bucket[: len(bucket) - self.MAX_PER_DELEGATION]

    def drain(self, delegation_id: str):
        return list(self._reports.pop(delegation_id, []))

    def pending(self, delegation_id: str) -> int:
        # 2026-09-09：defaultdict.get(key, 0) 返回 int，len(int) 抛 TypeError
        return len(self._reports.get(delegation_id) or ())


def shared_namespace(delegation_id: str) -> str:
    """Namespace shared by subagents of one delegation batch."""
    return "shared:delegation:" + delegation_id


def digest_reports(reports) -> str:
    """Render reports into a readable digest block for the main agent."""
    if not reports:
        return ""
    kind_zh = {"progress": "进度", "finding": "发现", "blocker": "阻塞"}
    lines = []
    for r in reports:
        kind = kind_zh.get(r.kind, r.kind)
        lines.append(f"- [{r.sender}|{kind}] {r.content}")
    return "[子代理通讯摘要]\n" + "\n".join(lines)
