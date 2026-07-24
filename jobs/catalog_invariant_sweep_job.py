"""ADR-012 Phase 0b — daily serving-surface invariant sweep.

Runs the checks in services/catalog_invariant_checks.py and ERROR-logs every
violated invariant (prod filters INFO). Registered with a CRON trigger, not
`interval` — an interval job's first fire resets on every redeploy and this
service recycles faster than daily, so it would never fire (the exact
restart-starvation that killed the trust backfill for weeks).

Env: CATALOG_INVARIANT_SWEEP_ENABLED (default true).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _is_enabled() -> bool:
    raw = os.getenv("CATALOG_INVARIANT_SWEEP_ENABLED", "true")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


async def run_catalog_invariant_sweep_tick() -> None:
    if not _is_enabled():
        logger.debug("catalog_invariant_sweep: disabled")
        return
    try:
        from db.database import database
        from services.catalog_invariant_checks import run_catalog_invariant_checks

        report = await run_catalog_invariant_checks(database)
        for check in report.get("checks", []):
            if check.get("violated"):
                logger.error(
                    "catalog_invariant VIOLATED: %s — count=%s (threshold=%s) "
                    "samples=%s :: %s",
                    check.get("name"),
                    check.get("count"),
                    check.get("threshold"),
                    check.get("sample_keys"),
                    check.get("description"),
                )
            elif check.get("error"):
                logger.error(
                    "catalog_invariant check ERRORED: %s — %s",
                    check.get("name"), check.get("error"),
                )
        logger.info(
            "catalog_invariant_sweep: %d checks, %d violated",
            len(report.get("checks", [])),
            int(report.get("violated_count", 0)),
        )
    except Exception:
        logger.exception("catalog_invariant_sweep: tick failed")
