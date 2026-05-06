#!/usr/bin/env python3
"""Phase 2 backfill — populate catalog_products.category_path / brand_normalized
from regex patterns ported from
PIVOTA-Agent-mainline-verify/src/services/externalSeedProducts.js
(BEAUTY_CATEGORY_PATTERNS).

For each catalog_products row where category_path IS NULL:
  - Run regex against (category, product_type, title) in priority order.
  - On first match, populate category_path + category_label_source='regex_backfill'
    + category_confidence=0.85.

Runs in batches of 1000. Idempotent: re-running only touches NULL rows.

Usage:
  python scripts/backfill_pdp_category_path.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database
from services.pdp_category_classifier import (  # noqa: E402
    CATEGORY_PATTERNS,  # re-exported for the test that pins drift
    classify,  # noqa: F401  (re-exported for tests)
    resolve_path_from_row,
)

logger = logging.getLogger("backfill_pdp_category_path")

CONFIDENCE_REGEX_BACKFILL = 0.85
LABEL_SOURCE = "regex_backfill"


async def _fetch_batch(limit: int) -> List[dict]:
    rows = await database.fetch_all(
        """
        SELECT product_key, category, product_type, title
        FROM catalog_products
        WHERE category_path IS NULL
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return [dict(row) for row in rows or []]


async def _apply_update(product_key: str, category_path: str) -> None:
    await database.execute(
        """
        UPDATE catalog_products
        SET category_path = :path,
            category_confidence = :confidence,
            category_label_source = :source
        WHERE product_key = :key AND category_path IS NULL
        """,
        {
            "key": product_key,
            "path": category_path,
            "confidence": CONFIDENCE_REGEX_BACKFILL,
            "source": LABEL_SOURCE,
        },
    )


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    matched = 0
    unmatched = 0

    while True:
        rows = await _fetch_batch(args.batch_size)
        if not rows:
            break
        for row in rows:
            total += 1
            hit = resolve_path_from_row(
                category=row.get("category"),
                product_type=row.get("product_type"),
                title=row.get("title"),
            )
            if hit is None:
                unmatched += 1
                continue
            label, path = hit
            if not args.dry_run:
                await _apply_update(row["product_key"], path)
            matched += 1
            if matched % 100 == 0:
                logger.info("matched=%d unmatched=%d total=%d", matched, unmatched, total)
        if args.limit and total >= args.limit:
            break
        # Page through; the WHERE filter shrinks the eligible set automatically.
        if args.dry_run:
            # Dry-run can't progress through the batch since rows still match.
            break

    logger.info(
        "Backfill complete: matched=%d unmatched=%d total=%d dry_run=%s",
        matched,
        unmatched,
        total,
        args.dry_run,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000, help="rows per batch")
    parser.add_argument("--limit", type=int, default=0, help="cap total rows; 0 = no cap")
    parser.add_argument("--dry-run", action="store_true", help="don't UPDATE; just count matches")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
