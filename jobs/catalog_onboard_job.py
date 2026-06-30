"""Scheduled drainer for the catalog-onboard queue (unattended catalog growth).

OFF BY DEFAULT: no-ops unless CATALOG_ONBOARD_ENABLED is truthy, so deploying the
scheduler never starts autonomous catalog writes — flip the flag to turn on
unattended growth. Registered as an APScheduler interval job in audit_scheduler
(already prod-worker-gated). Per-tick cap via CATALOG_ONBOARD_TICK_LIMIT (default 20).
"""

from __future__ import annotations

import logging
import os

from db.database import database
from services.catalog_onboard_worker import process_queue

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return (os.getenv("CATALOG_ONBOARD_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")


def _tick_limit() -> int:
    try:
        return max(1, int(os.getenv("CATALOG_ONBOARD_TICK_LIMIT") or "20"))
    except (TypeError, ValueError):
        return 20


async def run_catalog_onboard_queue() -> None:
    """One drain tick: claim + onboard up to the tick limit. Best-effort."""
    if not _enabled():
        return
    try:
        if not getattr(database, "is_connected", False):
            await database.connect()
        summary = await process_queue(limit=_tick_limit(), apply=True)
        if summary.get("claimed"):
            logger.info("catalog_onboard_queue tick: %s", summary)
    except Exception as exc:  # noqa: BLE001 — a tick failure must not crash the scheduler
        logger.warning("catalog_onboard_queue tick failed: %s", str(exc)[:200])
