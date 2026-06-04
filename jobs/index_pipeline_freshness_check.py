"""Freshness watchdog for the serving-projection pipeline.

Two stages keep ``index_pipeline_state.serving_eligible`` current:

  1. ``agent_pdp_view`` materialization — the Stage 3a-iii inline writer
     plus the 03:30 UTC ``agent_pdp_view_sweep`` safety net.
  2. The 04:00 UTC ``nightly_index_health`` classifier, which reads
     ``agent_pdp_view`` and (re)derives ``serving_eligible``.

If either stalls, PDP coverage decays *silently*: between 2026-05-21 and
2026-06-04 the pipeline was frozen for ~3 weeks and ~1,800 products fell
out of serving before anyone noticed. This watchdog turns that into an
alert the next morning instead.

It checks symptoms, not just timestamps:
  * classifier recency — ``max(index_pipeline_state.last_consolidated_at)``
    must be within ``max_age_hours`` (a daily job should never be >25h old).
  * materialization health — the count of live, image-bearing external_seed
    products that have NO ``agent_pdp_view`` row must stay below
    ``max_stranded`` (steady state is the handful of sig-collision
    duplicates; a climb means the writer/sweep is broken).

NOTE: this runs on the same APScheduler as the jobs it watches, so it
cannot detect the scheduler itself being fully down — that is exactly the
failure mode of the 2026-05/06 incident. Pair it with an external uptime
/ log-absence alert (e.g. a dead-man's-switch on this job's heartbeat).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger("index_pipeline_freshness_check")

DEFAULT_MAX_AGE_HOURS = 25.0
DEFAULT_MAX_STRANDED = 50

_STRANDED_SQL = """
    SELECT count(*) AS n
    FROM catalog_products cp
    LEFT JOIN agent_pdp_view apv ON apv.content_key = cp.content_key
    WHERE cp.platform = 'external_seed'
      AND cp.sync_status = 'live'
      AND coalesce(cp.image_url, '') <> ''
      AND apv.content_key IS NULL
"""


async def run_index_pipeline_freshness_check(
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    max_stranded: int = DEFAULT_MAX_STRANDED,
) -> Dict[str, Any]:
    """Alert (Sentry + ``logger.error``) when the serving projection or the
    nightly classifier has fallen behind. Returns a summary dict and never
    raises, so it is safe as an APScheduler entry point.
    """
    from db.database import database

    summary: Dict[str, Any] = {
        "job": "index_pipeline_freshness_check",
        "max_age_hours": max_age_hours,
        "max_stranded": max_stranded,
        "classifier_age_hours": None,
        "stranded_with_image": None,
        "stale": [],
        "alerted": False,
        "error": None,
    }

    try:
        if not getattr(database, "is_connected", False):
            await database.connect()

        now = datetime.now(timezone.utc)

        # 1. classifier recency
        ips_row = await database.fetch_one(
            "SELECT max(last_consolidated_at) AS ts FROM index_pipeline_state"
        )
        ts = ips_row["ts"] if ips_row else None
        if ts is None:
            summary["stale"].append("classifier_never_ran")
        else:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (now - ts).total_seconds() / 3600.0
            summary["classifier_age_hours"] = round(age_h, 1)
            if age_h > max_age_hours:
                summary["stale"].append(f"classifier_stale_{round(age_h)}h")

        # 2. materialization health (stranded = live image-bearing products
        #    with no agent_pdp_view row)
        strand_row = await database.fetch_one(_STRANDED_SQL)
        stranded = int(strand_row["n"]) if strand_row else 0
        summary["stranded_with_image"] = stranded
        if stranded > max_stranded:
            summary["stale"].append(f"agent_pdp_view_stranded_{stranded}")

        if summary["stale"]:
            msg = (
                "serving-projection pipeline UNHEALTHY: "
                f"{summary['stale']} (classifier_age_h="
                f"{summary['classifier_age_hours']}, "
                f"stranded_with_image={stranded}, "
                f"thresholds: max_age_hours={max_age_hours}, "
                f"max_stranded={max_stranded})"
            )
            logger.error(msg)
            try:
                from config.sentry_config import capture_message

                capture_message(msg, "error", {"freshness": summary})
                summary["alerted"] = True
            except Exception as exc:  # noqa: BLE001 - alerting must not crash the check
                logger.warning(
                    "index_pipeline_freshness: sentry capture failed: %s", exc
                )
        else:
            logger.info(
                "index_pipeline_freshness OK (classifier_age_h=%s, stranded=%d)",
                summary["classifier_age_hours"],
                stranded,
            )
    except Exception as exc:  # noqa: BLE001 - watchdog must never raise
        logger.error(
            "index_pipeline_freshness check failed: %s", exc, exc_info=True
        )
        summary["error"] = repr(exc)

    return summary
