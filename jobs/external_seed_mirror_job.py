"""Hourly external_product_seeds → catalog_products mirror job.

Background. The PDP serving gate enforces `index_pipeline_state.serving_eligible`.
The nightly index-health job (jobs/nightly_index_health_job.py) populates
index_pipeline_state by iterating catalog_products rows. External seeds reach
catalog_products via scripts/mirror_external_seeds_to_catalog_products.py.

That mirror script was historically run by hand. New seeds (notably the
creator_agents pipeline) accumulate in external_product_seeds without
`attached_product_key`, never get mirrored, never get an index_pipeline_state
row, and consequently fail PDP load with PRODUCT_NOT_SERVABLE. As of the
fix landing date, ~1,489 active seeds were stuck in this state.

This wrapper makes the mirror a scheduled job so the gap closes itself for
future seeds. Backfill of the existing orphans is a separate one-off
invocation (see the same script with `--apply`).

The underlying script's `_run` opens its own DB connection. We bypass that
and call `_build_report` + `_apply` directly so we share the app's pool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def run_external_seed_mirror() -> Dict[str, Any]:
    """Entry point for APScheduler. Apply mode is on by default — the
    mirror is idempotent (ON CONFLICT DO NOTHING on the unique identity
    tuple) and inserts only missing catalog_products rows.

    Returns a summary dict for logs. Never raises.
    """
    run_start = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {
        "run_start": run_start.isoformat(),
        "inserted_catalog_products": 0,
        "missing_before": None,
        "missing_after": None,
        "skipped": False,
        "errors": [],
    }

    try:
        from scripts.mirror_external_seeds_to_catalog_products import (
            _apply,
            _build_report,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("external_seed_mirror: import failed: %s", exc)
        summary["errors"].append(f"import: {exc!r}")
        return summary

    try:
        before = await _build_report(sample_limit=0, limit=0, apply=True)
        if not before.get("ok"):
            logger.warning(
                "external_seed_mirror: precheck failed (schema/index): %s",
                before.get("error"),
            )
            summary["skipped"] = True
            summary["errors"].append(f"precheck: {before.get('error')!r}")
            return summary
        summary["missing_before"] = int(
            (before.get("totals") or {}).get("missing_catalog_products") or 0
        )

        if summary["missing_before"] == 0:
            logger.info("external_seed_mirror: nothing to mirror")
            return summary

        inserted = await _apply(0)  # limit=0 means all missing
        summary["inserted_catalog_products"] = int(inserted)

        after = await _build_report(sample_limit=0, limit=0, apply=True)
        summary["missing_after"] = int(
            (after.get("totals") or {}).get("missing_catalog_products") or 0
        )
        logger.info(
            "external_seed_mirror: inserted=%s missing_before=%s missing_after=%s",
            inserted,
            summary["missing_before"],
            summary["missing_after"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("external_seed_mirror: run failed")
        summary["errors"].append(f"run: {exc!r}")

    return summary
