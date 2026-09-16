"""迭代预算追踪 — 借鉴 Hermes 的 IterationBudget."""

from __future__ import annotations


class IterationBudget:
    """迭代预算追踪."""

    def __init__(self, max_turns: int = 60):
        self.max_turns = max_turns
        self.current = 0

    def tick(self) -> None:
        self.current += 1

    @property
    def exhausted(self) -> bool:
        return self.current >= self.max_turns

    @property
    def remaining(self) -> int:
        return self.max_turns - self.current

    @property
    def percentage(self) -> float:
        return (self.current / self.max_turns) * 100 if self.max_turns > 0 else 0
