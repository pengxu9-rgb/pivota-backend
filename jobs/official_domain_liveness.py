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

async def run_official_domain_liveness_tick():
    global _cursor
    if os.getenv("OFFICIAL_DOMAIN_LIVENESS_ENABLED", "true").lower() != "true":
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
