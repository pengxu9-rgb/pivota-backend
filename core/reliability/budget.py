from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RequestBudget:
    """
    Lightweight time budget helper based on monotonic clock.

    The budget is intentionally side-effect free: callers decide whether to
    enforce timeout, skip work, or fall back when remaining budget is small.
    """

    deadline_monotonic: float

    @classmethod
    def from_total_ms(cls, total_ms: int) -> "RequestBudget":
        safe_ms = max(1, int(total_ms))
        return cls(deadline_monotonic=time.monotonic() + (safe_ms / 1000.0))

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    @property
    def remaining_ms(self) -> int:
        return int(self.remaining_seconds * 1000)

    def expired(self) -> bool:
        return self.remaining_seconds <= 0.0

    def timeout_seconds(
        self,
        *,
        default_seconds: float,
        min_seconds: float = 0.05,
        max_seconds: Optional[float] = None,
    ) -> float:
        timeout = min(float(default_seconds), self.remaining_seconds)
        if max_seconds is not None:
            timeout = min(timeout, float(max_seconds))
        return max(float(min_seconds), float(timeout))

    def to_metadata(self) -> dict:
        return {
            "remaining_budget_ms": self.remaining_ms,
            "remaining_budget_seconds": round(self.remaining_seconds, 3),
        }

