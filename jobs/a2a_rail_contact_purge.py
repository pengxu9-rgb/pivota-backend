"""Scheduler job: delete the buyer's email and shipping address from A2A-rail purchases, on its own clock.

The buyer terms promise that contact details are deleted when the purchase ends, and no later than 72
hours after it opens. The worker's tick (jobs/a2a_rail_purchase_poll) already purges them, but that
tick only runs while A2A_RAIL_WORKER_ENABLED is on AND the rail client is configured: with either dial
off (a rollback, a paused rail), nothing purges and the promise silently lapses. This job runs the same
purge (db/a2a_rail_ledger.purge_stale_contact) with NO dependency on the rail's dials or its client.

Its own threshold is one hour SHORTER than the worker's (71 h, `PURGE_AGE_SECONDS`), and it runs at four
fixed minutes of every hour (services/audit_scheduler.py, cron "3,18,33,48"), so a contact is gone by
71 h + 15 min plus one run, with room for two missed runs in a row before 72 h. Cron, not `interval`: an
interval job's first fire is start + N and every redeploy resets it.

The purge only NULLs `buyer_email` and `shipping_address` on rows opened more than 71 h ago, in any
state, without moving the state. A purged row can no longer enter `quoting` (the ledger's transition
guard), which every live purchase has long passed by then. Bounded: at most `BATCH` rows per statement
and `MAX_BATCHES` statements per run; a backlog finishes over several runs. Logs counts only, never an
id or a value.

DORMANT unless A2A_RAIL_CONTACT_PURGE_ENABLED is on ("1", "true", "on", "yes"). Turn it on before the
first A2A purchase can be created, and leave it on when rolling the rail back.

    POST /admin/scheduler/jobs/a2a_rail_contact_purge/run-now    # one forced run, same path
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import db.a2a_rail_ledger as a2a_ledger

logger = logging.getLogger(__name__)

ENABLED_ENV = "A2A_RAIL_CONTACT_PURGE_ENABLED"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: One hour inside the 72 h promise (the worker's own sweep keeps CONTACT_RETENTION_SECONDS_DEFAULT).
PURGE_AGE_SECONDS = a2a_ledger.CONTACT_RETENTION_SECONDS_DEFAULT - 3600
BATCH = 200
MAX_BATCHES = 25


def is_enabled() -> bool:
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUTHY


async def run_a2a_rail_contact_purge() -> Dict[str, Any]:
    if not is_enabled():
        return {"skipped": "disabled"}
    purged = 0
    batches = 0
    while batches < MAX_BATCHES:
        batches += 1
        n = await a2a_ledger.purge_stale_contact(max_age_seconds=PURGE_AGE_SECONDS, limit=BATCH)
        purged += n
        if n < BATCH:
            break
    result: Dict[str, Any] = {"contact_purged": purged, "batches": batches}
    if batches >= MAX_BATCHES and n >= BATCH:
        result["more"] = True
    if purged:
        # WARNING, not INFO: prod drops module-logger INFO, and a purge is worth one line. Counts only.
        logger.warning("a2a_rail_contact_purge: %s", result)
    return result
