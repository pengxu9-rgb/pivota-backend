"""ADR-012 Phase 0b — daily serving-surface invariant sweep.

Runs the checks in services/catalog_invariant_checks.py and ERROR-logs every
violated invariant (prod filters INFO). Most checks are a Postgres count of
rows; the taxonomy ones are a SHARE in tenths of a percent, so both branches
log `detail`, which carries the numerator, the denominator and the buckets. Registered with a CRON trigger, not
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
                # DETAIL IS LOGGED HERE TOO, not only on the warn_only branch below. Most checks
                # report a plain row count, for which the count is self-explanatory. The taxonomy
                # checks report a SHARE in tenths of a percent, and `count=402 (threshold=400)`
                # alone says neither how many rows that is, nor out of how many, nor whether the
                # share rose because the cohort grew or because the serving set shrank — which are
                # opposite problems with opposite fixes. Withholding the numbers on the branch that
                # actually pages, and printing them only on the branch that does not, is backwards.
                logger.error(
                    "catalog_invariant VIOLATED: %s — count=%s (threshold=%s) "
                    "samples=%s detail=%s :: %s",
                    check.get("name"),
                    check.get("count"),
                    check.get("threshold"),
                    check.get("sample_keys"),
                    check.get("detail"),
                    check.get("description"),
                )
            elif check.get("over_threshold"):
                # A `warn_only` check over its threshold. WARNING, not ERROR:
                # the number is real and must be seen, but the check is not a
                # verdict yet (services/catalog_invariant_checks explains what
                # `warn_only` does and does not suppress). The DETAIL is logged
                # because for a reporting check the buckets ARE the finding —
                # a bare count cannot distinguish "433 unexplained" from "433
                # already quarantined".
                logger.warning(
                    "catalog_invariant REPORTING (warn_only): %s — count=%s "
                    "(threshold=%s) samples=%s detail=%s :: %s",
                    check.get("name"),
                    check.get("count"),
                    check.get("threshold"),
                    check.get("sample_keys"),
                    check.get("detail"),
                    check.get("description"),
                )
            elif check.get("error"):
                logger.error(
                    "catalog_invariant check ERRORED: %s — %s",
                    check.get("name"), check.get("error"),
                )
        # ERRORED IS IN THE SUMMARY LINE. It was not, so "27 checks, 0 violated" was the whole
        # story even when a check had been unable to run for weeks — the per-check ERROR above
        # scrolls past, the summary is what anyone greps, and an unrunnable check read as a
        # passing one.
        errored = int(report.get("errored_count", 0))
        logger.info(
            "catalog_invariant_sweep: %d checks, %d violated, %d reporting, %d ERRORED%s",
            len(report.get("checks", [])),
            int(report.get("violated_count", 0)),
            int(report.get("warned_count", 0)),
            errored,
            (" [%s]" % ", ".join(report.get("errored", []))) if errored else "",
        )
    except Exception:
        logger.exception("catalog_invariant_sweep: tick failed")
