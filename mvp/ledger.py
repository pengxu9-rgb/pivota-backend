from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


class InvalidTransition(Exception):
    pass


ORDER_STATES = (
    "draft",
    "checkout_attempted",
    "paid",
    "placed",
    "fulfilled",
    "cancelled",
    "refund_pending",
    "refunded",
    "disputed",
)


@dataclass(frozen=True)
class TransitionResult:
    prev_state: str
    new_state: str
    event_type: str


_TRANSITIONS: Dict[str, Dict[str, str]] = {
    "draft": {
        "checkout_attempted": "checkout_attempted",
        "cancelled": "cancelled",
    },
    "checkout_attempted": {
        "checkout_succeeded": "paid",
        "checkout_failed": "checkout_attempted",
        "cancelled": "cancelled",
    },
    "paid": {
        "order_placed": "placed",
        "refund_requested": "refund_pending",
        "dispute_opened": "disputed",
    },
    "placed": {
        "fulfilled": "fulfilled",
        "refund_requested": "refund_pending",
        "dispute_opened": "disputed",
    },
    "fulfilled": {
        "refund_requested": "refund_pending",
        "dispute_opened": "disputed",
    },
    "refund_pending": {
        "refund_completed": "refunded",
        "refund_failed": "refund_pending",
    },
    "disputed": {
        "dispute_resolved": "placed",
    },
    "cancelled": {},
    "refunded": {},
}


def apply_event(state: str, event_type: str) -> TransitionResult:
    if state not in _TRANSITIONS:
        raise InvalidTransition(f"Unknown state: {state}")
    next_map = _TRANSITIONS[state]
    if event_type not in next_map:
        raise InvalidTransition(f"Invalid transition: {state} ->({event_type})-> ?")
    return TransitionResult(prev_state=state, new_state=next_map[event_type], event_type=event_type)


class LedgerStateMachine:
    def __init__(self, initial_state: str = "draft"):
        if initial_state not in ORDER_STATES:
            raise ValueError("invalid initial_state")
        self.state = initial_state

    def step(self, event_type: str) -> TransitionResult:
        result = apply_event(self.state, event_type)
        self.state = result.new_state
        return result

    def try_step(self, event_type: str) -> Optional[TransitionResult]:
        try:
            return self.step(event_type)
        except InvalidTransition:
            return None

