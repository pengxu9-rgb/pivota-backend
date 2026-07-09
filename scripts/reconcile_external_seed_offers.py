#!/usr/bin/env python3
"""Reconcile external_product_seeds ↔ catalog_offers (convergence P1.6).

The seed→offer projection is dual-written (mirror job + on-demand
services.external_offer_dual_write), but employee price/availability edits reach
`external_product_seeds` through many write paths and can outrun the projection.
This is the nightly reconciliation query: it reports — and with --apply repairs —
three drift classes between the two tables, comparing on the mirror's offer
identity (`catalog_offers.source_ref = external_product_seeds.id`,
`source_system = 'external_product_seeds_mirror_v1'`):

  * missing  — an active seed WITH a mirror catalog_products row but NO offer
  * drift    — an offer whose price / availability / currency no longer matches
               its seed
  * orphan   — a mirror offer whose seed is gone or no longer active (reported
               only; never auto-deleted — a human decides suppression)

--apply re-projects the missing + drifted offers through the shared projection
(orphans are left for review). Idempotent. Dry-run is the default:

  python scripts/reconcile_external_seed_offers.py
Apply (staging first; production only with explicit user authorization):
  DATABASE_URL=... python scripts/reconcile_external_seed_offers.py --apply --limit 1000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

logger = logging.getLogger("reconcile_external_seed_offers")

_MIRROR_SOURCE = "external_product_seeds_mirror_v1"

# Active seeds that have a mirror PRODUCT but no mirror OFFER. The product is
# located by provenance (catalog_products.source_ref = seed id under the mirror
# source_system) — NOT by reconstructing a product_key, because ADR-009 D2 keys
# the mirror product under a per-brand observed seller (merch_obs_*), so the old
# 'prod::external_seed::external_seed::' || external_product_id join matched
# nothing and this reported a false 0-missing.
MISSING_SQL = """
    SELECT eps.id AS seed_id, eps.external_product_id, eps.price_amount
    FROM external_product_seeds eps
    JOIN catalog_products cp
      ON cp.source_ref = eps.id
     AND cp.source_system = :mirror_source
    LEFT JOIN catalog_offers co
      ON co.source_ref = eps.id
     AND co.source_system = :mirror_source
    WHERE eps.status = 'active'
      AND co.offer_id IS NULL
    ORDER BY eps.updated_at DESC
"""

# Offers whose price / availability / currency drifted from the live seed.
DRIFT_SQL = """
    SELECT eps.id AS seed_id,
           eps.price_amount AS seed_price, co.list_price AS offer_price,
           eps.availability AS seed_availability, co.availability AS offer_availability,
           coalesce(eps.price_currency, 'USD') AS seed_currency, co.currency AS offer_currency
    FROM external_product_seeds eps
    JOIN catalog_offers co
      ON co.source_ref = eps.id
     AND co.source_system = :mirror_source
    WHERE eps.status = 'active'
      AND (
            co.list_price IS DISTINCT FROM eps.price_amount::numeric(12,2)
         OR co.availability IS DISTINCT FROM eps.availability
         OR co.currency IS DISTINCT FROM coalesce(eps.price_currency, 'USD')
      )
    ORDER BY eps.updated_at DESC
"""

# Mirror offers whose seed vanished or is no longer active (report only).
ORPHAN_SQL = """
    SELECT co.offer_id, co.source_ref AS seed_id, co.list_price
    FROM catalog_offers co
    LEFT JOIN external_product_seeds eps ON eps.id = co.source_ref
    WHERE co.source_system = :mirror_source
      AND (eps.id IS NULL OR eps.status <> 'active')
    ORDER BY co.updated_at DESC
"""


async def run_reconcile(*, apply: bool, limit: int, sample_limit: int) -> Dict[str, Any]:
    params = {"mirror_source": _MIRROR_SOURCE}
    missing = [dict(r) for r in await database.fetch_all(MISSING_SQL, params)]
    drift = [dict(r) for r in await database.fetch_all(DRIFT_SQL, params)]
    orphan = [dict(r) for r in await database.fetch_all(ORPHAN_SQL, params)]

    report: Dict[str, Any] = {
        "apply": apply,
        "missing_offers": len(missing),
        "drifted_offers": len(drift),
        "orphan_offers": len(orphan),
        "missing_sample": missing[:sample_limit],
        "drift_sample": drift[:sample_limit],
        "orphan_sample": orphan[:sample_limit],
        "repaired": 0,
    }
    if not apply:
        return report

    # Explicit human-authorized repair opts the projection writer in for this run.
    os.environ["EXTERNAL_OFFER_DUAL_WRITE_ENABLED"] = "true"
    from services.external_offer_dual_write import sync_offer_for_seed

    to_repair: List[str] = [r["seed_id"] for r in missing] + [r["seed_id"] for r in drift]
    if limit and limit > 0:
        to_repair = to_repair[:limit]

    repaired = 0
    for seed_id in to_repair:
        result = await sync_offer_for_seed(seed_id)
        if result.get("status") == "synced":
            repaired += 1
    report["repaired"] = repaired
    report["remaining_missing"] = len(
        [dict(r) for r in await database.fetch_all(MISSING_SQL, params)]
    )
    report["remaining_drift"] = len(
        [dict(r) for r in await database.fetch_all(DRIFT_SQL, params)]
    )
    return report


async def _main(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await run_reconcile(
            apply=args.apply, limit=args.limit, sample_limit=args.sample_limit
        )
    finally:
        await database.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="repair missing/drifted offers (default: dry-run)")
    parser.add_argument("--limit", type=int, default=0, help="max seeds to repair (0 = all)")
    parser.add_argument("--sample-limit", type=int, default=20, help="rows per sample in the report")
    cli_args = parser.parse_args()

    result = asyncio.run(_main(cli_args))
    print(json.dumps(result, indent=2, default=str))
    if not cli_args.apply:
        print(
            "\nDRY-RUN — nothing written. Re-run with --apply to repair offers.",
            file=sys.stderr,
        )
