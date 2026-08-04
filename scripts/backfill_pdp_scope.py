#!/usr/bin/env python3
"""Phase 6 backfill — populate catalog_products.pdp_scope for rows still
at the migration default ('unverified').

Decision rule lives in services.pdp_scope_classifier. This script:
  1. Fetches a batch of rows with pdp_scope='unverified' along with
     their seller_count (distinct merchants across catalog_offers +
     external_product_seeds that link to the row's product_key).
  2. Calls classify() per row.
  3. UPDATEs pdp_scope, pdp_scope_source='backfill_2026_05',
     pdp_scope_set_at=NOW().

Idempotent — only touches 'unverified' rows. Re-running on top of a
fully-backfilled catalog is a no-op.

Usage:
  python scripts/backfill_pdp_scope.py [--batch-size N] [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from services.pdp_scope_classifier import (  # noqa: E402
    own_merchant_seller_term_sql,
    LABEL_SOURCE_BACKFILL,
    ScopeSignals,
    classify,
)

logger = logging.getLogger("backfill_pdp_scope")


async def _fetch_batch(batch_size: int) -> List[Dict[str, Any]]:
    """Fetch unverified rows along with their merchant fan-out count.

    seller_count = 1 (the PDP's own merchant) + distinct OTHER merchants
    visible through catalog_offers (by merchant_id) + distinct
    external_product_seeds (by domain — external_product_seeds doesn't
    carry merchant_id, the merchant identity is the destination domain).
    A small over-count is acceptable since the classifier only checks
    >= 2; cross-checking against pdp_subject_index.seller_count would
    tighten this but isn't required for the canonical/merchant_owned
    cut."""
    rows = await database.fetch_all(
        """
        SELECT u.product_key,
               u.category_label_source,
               (
                 {own_merchant_term}
                 + COALESCE((
                     SELECT COUNT(DISTINCT co.merchant_id)
                     FROM catalog_offers co
                     WHERE co.product_key = u.product_key
                       AND co.merchant_id IS NOT NULL
                       AND co.merchant_id <> u.merchant_id
                   ), 0)
                 + COALESCE((
                     SELECT COUNT(DISTINCT eps.domain)
                     FROM external_product_seeds eps
                     WHERE eps.attached_product_key IN (
                         u.product_key,
                         (u.merchant_id || '|' || u.platform || '|' || u.source_product_id)
                       )
                       AND eps.status = 'active'
                       AND eps.domain IS NOT NULL
                   ), 0)
               ) AS seller_count
        FROM (
            SELECT product_key, merchant_id, platform, source_product_id, category_label_source
            FROM catalog_products
            WHERE pdp_scope = 'unverified'
            LIMIT :limit
        ) u
        """.format(own_merchant_term=own_merchant_seller_term_sql("u.merchant_id")),
        {"limit": batch_size},
    )
    return [dict(row) for row in rows or []]


async def _apply_update(*, product_key: str, scope: str) -> None:
    await database.execute(
        """
        UPDATE catalog_products
        SET pdp_scope = :scope,
            pdp_scope_source = :src,
            pdp_scope_set_at = NOW()
        WHERE product_key = :key AND pdp_scope = 'unverified'
        """,
        {"scope": scope, "src": LABEL_SOURCE_BACKFILL, "key": product_key},
    )


async def run_backfill(*, batch_size: int = 500, limit: int = 0,
                       dry_run: bool = False) -> Dict[str, Any]:
    """The classification loop, callable by BOTH the CLI and the D3 cron
    (jobs/pdp_scope_backfill_cron). One implementation — a cron with its own
    copy of this loop is the drift shape this workstream exists to remove.

    Terminates because classify() never returns 'unverified': every processed
    row leaves the WHERE pdp_scope='unverified' set (except in dry-run, which
    stops after one batch for exactly that reason).
    """
    total = 0
    counts: Dict[str, int] = {}

    while True:
        rows = await _fetch_batch(batch_size)
        if not rows:
            break
        for row in rows:
            total += 1
            scope = classify(
                ScopeSignals(
                    category_label_source=row.get("category_label_source"),
                    seller_count=int(row.get("seller_count") or 1),
                )
            )
            counts[scope] = counts.get(scope, 0) + 1
            if not dry_run:
                await _apply_update(product_key=row["product_key"], scope=scope)
            if total % 200 == 0:
                logger.info("processed=%d running=%s", total, counts)
        if limit and total >= limit:
            break
        if dry_run:
            # Dry-run can't make progress; the WHERE pdp_scope='unverified'
            # filter would re-fetch the same rows. One batch is enough.
            break

    return {"total": total, "distribution": counts, "dry_run": dry_run}


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()

    result = await run_backfill(batch_size=args.batch_size, limit=args.limit,
                                dry_run=args.dry_run)
    logger.info("Backfill complete: %s", result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0, help="cap total rows; 0 = no cap")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
