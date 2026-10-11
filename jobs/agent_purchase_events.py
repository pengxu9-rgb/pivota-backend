"""Scheduler job: detect rail-purchase events and hand them to the agent webhook sender.

Registered on the worker by services/audit_scheduler.py every AGENT_PURCHASE_EVENTS_INTERVAL_SECONDS
(default 30). Dormant unless AGENT_PURCHASE_WEBHOOKS_ENABLED and AGENT_PURCHASE_LEDGER_ENABLED are both
on; the check is inside the job, so registering it changes nothing until armed. The webhook sender
itself still only POSTs where services/webhook_delivery_gate allows (production by default).
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any, Dict

import db.agent_purchase_ledger as agent_purchase_ledger
from services.payment_orchestration import purchase_events

logger = logging.getLogger(__name__)

_INTERVAL_ENV = "AGENT_PURCHASE_EVENTS_INTERVAL_SECONDS"
_LOOKBACK_ENV = "AGENT_PURCHASE_EVENTS_LOOKBACK_HOURS"


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(str(os.getenv(name) or default).strip())
    except ValueError:
        value = default
    return max(low, min(high, value))


def job_interval_seconds() -> int:
    return _int_env(_INTERVAL_ENV, 30, 10, 600)


def _worker_id() -> str:
    return f"ape-{socket.gethostname()[:32]}-{os.getpid()}"


async def run_agent_purchase_events() -> Dict[str, Any]:
    if not (purchase_events.is_enabled() and agent_purchase_ledger.is_enabled()):
        return {"skipped": "disabled"}
    detected = await purchase_events.detect(lookback_hours=_int_env(_LOOKBACK_ENV, 24, 1, 168))
    handed = await purchase_events.hand_over(worker=_worker_id())
    if detected or handed:
        logger.info("agent_purchase_events: detected=%s handed=%s", detected, handed)
    return {"detected": detected, "handed": handed}
