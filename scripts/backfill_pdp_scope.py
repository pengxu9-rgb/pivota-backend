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
    LABEL_SOURCE_BACKFILL,
    ScopeSignals,
    classify,
)

logger = logging.getLogger("backfill_pdp_scope")


async def _fetch_batch(batch_size: int) -> List[Dict[str, Any]]:
    """Fetch unverified rows along with their merchant fan-out count.

    seller_count counts the row's own merchant_id once, plus distinct
    merchants from catalog_offers and external_product_seeds. The
    GREATEST() guard handles rows where neither offers nor seeds exist
    (the catalog_products row itself still represents one merchant)."""
    rows = await database.fetch_all(
        """
        WITH unverified AS (
          SELECT product_key, merchant_id, category_label_source
          FROM catalog_products
          WHERE pdp_scope = 'unverified'
          LIMIT :limit
        ),
        offer_merchants AS (
          SELECT cp.product_key,
                 COALESCE(co.merchant_id, eps_m.merchant_id) AS m
          FROM unverified cp
          LEFT JOIN catalog_offers co ON co.product_key = cp.product_key
          LEFT JOIN (
            SELECT attached_product_key AS pk,
                   merchant_id
            FROM external_product_seeds
            WHERE status = 'active' AND attached_product_key IS NOT NULL
          ) eps_m ON eps_m.pk = cp.product_key
        ),
        agg AS (
          SELECT u.product_key,
                 u.merchant_id        AS pdp_merchant_id,
                 u.category_label_source,
                 COUNT(DISTINCT om.m) FILTER (WHERE om.m IS NOT NULL) AS off_merchants
          FROM unverified u
          LEFT JOIN offer_merchants om ON om.product_key = u.product_key
          GROUP BY u.product_key, u.merchant_id, u.category_label_source
        )
        SELECT product_key,
               category_label_source,
               -- the PDP's own merchant counts once; offers/seeds add more.
               GREATEST(
                 1,
                 (
                   SELECT COUNT(DISTINCT m)
                   FROM (
                     SELECT pdp_merchant_id AS m
                     UNION
                     SELECT om.m
                     FROM offer_merchants om
                     WHERE om.product_key = agg.product_key
                       AND om.m IS NOT NULL
                   ) merged
                 )
               ) AS seller_count
        FROM agg
        """,
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


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    counts: Dict[str, int] = {}

    while True:
        rows = await _fetch_batch(args.batch_size)
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
            if not args.dry_run:
                await _apply_update(product_key=row["product_key"], scope=scope)
            if total % 200 == 0:
                logger.info("processed=%d running=%s", total, counts)
        if args.limit and total >= args.limit:
            break
        if args.dry_run:
            # Dry-run can't make progress; the WHERE pdp_scope='unverified'
            # filter would re-fetch the same rows. One batch is enough.
            break

    logger.info(
        "Backfill complete: total=%d distribution=%s dry_run=%s",
        total,
        counts,
        args.dry_run,
    )
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
