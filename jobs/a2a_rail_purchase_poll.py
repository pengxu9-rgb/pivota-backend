"""Scheduler job: advance A2A-rail purchases one step at a time (services/payment_orchestration/a2a_rail_worker).

Registered on the worker by services/audit_scheduler.py every A2A_RAIL_POLL_INTERVAL_SECONDS (default
20). Dormant unless A2A_RAIL_WORKER_ENABLED is on AND the rail client is configured (A2A_RAIL_ENABLED,
A2A_RAIL_BASE_URL, A2A_RAIL_API_KEY); the checks are inside the job, so registering it changes nothing
until armed. One client per process: its per-member token cache is what keeps the token exchange off
every call.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any, Dict, Optional

from services.payment_orchestration import a2a_rail, a2a_rail_worker

logger = logging.getLogger(__name__)

A2A_RAIL_WORKER_ENABLED_ENV = "A2A_RAIL_WORKER_ENABLED"
_INTERVAL_ENV = "A2A_RAIL_POLL_INTERVAL_SECONDS"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

_client: Optional[Any] = None


def is_enabled() -> bool:
    return (os.getenv(A2A_RAIL_WORKER_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def job_interval_seconds() -> int:
    try:
        value = int(str(os.getenv(_INTERVAL_ENV) or 20).strip())
    except ValueError:
        value = 20
    return max(10, min(600, value))


def _worker_id() -> str:
    return f"a2a-{socket.gethostname()[:32]}-{os.getpid()}"


def _get_client() -> Optional[Any]:
    global _client
    if _client is None:
        _client = a2a_rail.from_env()
    return _client


async def run_a2a_rail_purchase_poll() -> Dict[str, Any]:
    if not is_enabled():
        return {"skipped": "disabled"}
    client = _get_client()
    if client is None:
        return {"skipped": "client_not_configured"}
    counts = await a2a_rail_worker.run_tick(client, _worker_id())
    if counts:
        logger.info("a2a_rail_purchase_poll: %s", counts)
    return counts
