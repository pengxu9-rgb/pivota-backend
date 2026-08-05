#!/usr/bin/env python3
"""Phase 6 backfill — INITIAL RESOLUTION of catalog_products.pdp_scope for
REAL-MERCHANT rows still at the migration default ('unverified').

Decision rule lives in services.pdp_scope_classifier. This script:
  1. Fetches a batch of rows with pdp_scope='unverified' along with
     their seller_count (distinct merchants across catalog_offers +
     external_product_seeds that link to the row's product_key).
  2. Calls classify() per row.
  3. UPDATEs pdp_scope, pdp_scope_source='backfill_2026_05',
     pdp_scope_set_at=NOW().

BUCKET ROWS (merchant_id='external_seed') ARE EXCLUDED — PR #1680 review
finding F1/F2. classify() never returns 'unverified': a single-seller row gets
'merchant_owned', an AFFIRMED state ("resolved, NOT canonical" —
brand_verified_graduation) that exits the `WHERE pdp_scope='unverified'`
promotion gate every promotion writer checks. A bucket row is not a resolved
single-merchant PDP — its own merchant_id is not a seller at all — so
'merchant_owned' is a state it must NEVER receive. Bucket lifecycle belongs to
the demote/promote pair: P4 (scripts/demote_unqualified_canonical_scopes) and
the D3 promote-only cron (jobs/pdp_scope_backfill_cron), between which an
unqualified bucket row STAYS 'unverified' and stays re-checkable.

Idempotent for its cohort — only touches non-bucket 'unverified' rows.

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
    seed_attachment_keys_sql,
    LABEL_SOURCE_BACKFILL,
    NON_SELLER_MERCHANT_ID,
    ScopeSignals,
    classify,
)

logger = logging.getLogger("backfill_pdp_scope")


async def _fetch_batch(batch_size: int) -> List[Dict[str, Any]]:
    """Fetch non-bucket unverified rows along with their merchant fan-out count.

    seller_count = the own-merchant term (1 for a real merchant) + distinct
    OTHER merchants visible through catalog_offers (by merchant_id) + distinct
    external_product_seeds (by domain — external_product_seeds doesn't
    carry merchant_id, the merchant identity is the destination domain).
    A small over-count is acceptable since the classifier only checks
    >= 2; cross-checking against pdp_subject_index.seller_count would
    tighten this but isn't required for the canonical/merchant_owned
    cut.

    Bucket rows are excluded at the source (see module docstring): this
    writer's terminal states are canonical/merchant_owned, and merchant_owned
    is a state a bucket row must never receive. Note also that this sum
    cannot see the predicate's product-group-peer and ext-cluster arms
    (110 prod canonical rows qualify only via those, measured 2026-08-04) —
    another reason bucket resolution does not belong here."""
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
                     WHERE eps.attached_product_key IN {seed_attach_keys}
                       AND eps.status = 'active'
                       AND eps.domain IS NOT NULL
                   ), 0)
               ) AS seller_count
        FROM (
            SELECT product_key, merchant_id, platform, source_product_id, category_label_source
            FROM catalog_products
            WHERE pdp_scope = 'unverified'
              AND merchant_id IS DISTINCT FROM :non_seller_bucket
            LIMIT :limit
        ) u
        """.format(
            own_merchant_term=own_merchant_seller_term_sql("u.merchant_id"),
            seed_attach_keys=seed_attachment_keys_sql("u"),
        ),
        {"limit": batch_size, "non_seller_bucket": NON_SELLER_MERCHANT_ID},
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
    """The one-shot classification loop — CLI ONLY. The D3 cron does NOT call
    this (PR #1680 review, F1): classify() never returns 'unverified', so on a
    schedule this loop converts every not-yet-multi-seller row to
    'merchant_owned' and parks it outside the promotion gate. Continuous
    re-qualification is the cron's promote-only UPDATE in
    jobs/pdp_scope_backfill_cron.

    Terminates because classify() never returns 'unverified': every processed
    row leaves the fetched set (except in dry-run, which stops after one batch
    for exactly that reason).
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
