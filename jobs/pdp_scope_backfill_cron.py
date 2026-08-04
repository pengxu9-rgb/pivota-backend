"""pdp_scope promotion cron — D3 of docs/PDP_SCOPE_REDESIGN.md.

Runs the classifier-backed backfill (scripts/backfill_pdp_scope.run_backfill —
the SAME loop as the CLI; a cron with its own copy would be the drift shape
this workstream removes) on a schedule, so `'unverified'` rows that gain a
second seller are promoted without an operator.

WHY THIS MUST BE SCHEDULED (D1, decided 2026-08-04): the pdp_matcher gates
cross-merchant seed attachment on the canonical label, and every promotion
writer is gated `WHERE pdp_scope='unverified'`. P4 demotes ~2,201 mislabelled
rows to 'unverified'; without this cron their only re-promotion path is a
manual script run — a one-way door with extra steps. With it, a row gains its
second seller through any non-matcher route (a foreign-merchant offer, a
product-group peer, an ext-cluster spanning >= 2 domains), the next tick
promotes it, and the matcher opens for it.

Measured safety at introduction (2026-08-04): 0 of the 284 unsuppressed
'unverified' bucket rows qualified for promotion, so the first ticks are no-ops
until the P4 demotion lands or ingestion adds qualifying rows.

Env vars:
  PDP_SCOPE_BACKFILL_ENABLED   default true — set false to pause without deploy
  PDP_SCOPE_BACKFILL_LIMIT     default 5000 — rows per tick (a tick that hits
      the cap simply leaves the tail for the next tick; the set only shrinks)
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 5_000
_ENV_ENABLED = "PDP_SCOPE_BACKFILL_ENABLED"
_ENV_LIMIT = "PDP_SCOPE_BACKFILL_LIMIT"


def _is_enabled() -> bool:
    return os.getenv(_ENV_ENABLED, "true").strip().lower() not in {"0", "false", "no", "off"}


def _limit() -> int:
    raw = os.getenv(_ENV_LIMIT)
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("pdp_scope_backfill: invalid %s=%r; using %d",
                       _ENV_LIMIT, raw, _DEFAULT_LIMIT)
        return _DEFAULT_LIMIT


async def run_pdp_scope_backfill_tick() -> None:
    """One scheduled pass. Fire-and-forget from the scheduler's point of view —
    an exception is logged, never raised into APScheduler."""
    if not _is_enabled():
        logger.info("pdp_scope_backfill: disabled via %s", _ENV_ENABLED)
        return
    try:
        from db.database import database
        from scripts.backfill_pdp_scope import run_backfill

        if not getattr(database, "is_connected", False):
            await database.connect()
        result = await run_backfill(limit=_limit())
        if result.get("total"):
            logger.info("pdp_scope_backfill: %s", result)
        else:
            logger.debug("pdp_scope_backfill: nothing unverified to classify")
    except Exception:
        logger.exception("pdp_scope_backfill tick failed")
