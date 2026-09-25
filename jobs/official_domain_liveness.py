"""Bounded worker-only domain seeding and liveness; declarations alone are not probed."""
import logging
import os
import time
from sqlalchemy import select
from db.catalog import catalog_products
from db.database import database
from services.official_domain_liveness import seed_inferred_domains, refresh_official_domain_liveness

logger = logging.getLogger(__name__)
_cursor = ""

# DORMANT BY DEFAULT — same posture as ENABLE_IDENTITY_RECONCILE_SWEEP and
# merchant_order_create_reconcile in services/audit_scheduler.py, and for the
# same reason. audit_scheduler registers this tick every 6h on any worker with
# worker_enabled, the prod worker deploys, and staging shares the prod
# Postgres. Its first run seeds `merchant_official_domains` rows for EVERY
# merchant in the catalog — and `official_domains` is a comparability field
# (db.audit_basis.COMPARABILITY_FIELDS), so seeding moves attribution and
# turns the next re-audit of every merchant into a non-comparable pair.
# Defaulting this to "true" armed all of that on merge. Arm it deliberately,
# after a dry run has sized the seed: set OFFICIAL_DOMAIN_LIVENESS_ENABLED=true
# on the worker service.
_ENABLED_ENV = "OFFICIAL_DOMAIN_LIVENESS_ENABLED"


def liveness_job_enabled() -> bool:
    """Read the arming flag at CALL time, never at import."""
    return os.getenv(_ENABLED_ENV, "false").strip().lower() == "true"


async def run_official_domain_liveness_tick():
    global _cursor
    if not liveness_job_enabled():
        return {"skipped": True}
    started = time.monotonic()
    summary = {"merchants_seeded": 0, "seed_failed": 0, "deadline_hit": False}
    # Keyset pages avoid an unbounded catalog fetch. Retain a cursor within the
    # worker if the seed budget expires; a normal small cohort completes in one tick.
    while time.monotonic() - started < 90:
        rows = await database.fetch_all(select(catalog_products.c.merchant_id).distinct()
            .where(catalog_products.c.merchant_id > _cursor)
            .order_by(catalog_products.c.merchant_id).limit(25))
        if not rows:
            _cursor = ""
            break
        for row in rows:
            if time.monotonic() - started >= 90:
                summary["deadline_hit"] = True
                break
            merchant = row["merchant_id"]
            try:
                await seed_inferred_domains(merchant, strict=True)
                summary["merchants_seeded"] += 1
            except Exception:
                summary["seed_failed"] += 1
                logger.warning("official domain seed failed", exc_info=True)
            _cursor = merchant
    summary["liveness"] = await refresh_official_domain_liveness(
        limit=50, run_deadline_seconds=60,
    )
    logger.info("official domain liveness tick: %s", summary)
    return summary
