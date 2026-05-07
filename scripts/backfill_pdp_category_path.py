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
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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


async def _fetch_batch(limit: int, after_key: Optional[str]) -> List[dict]:
    rows = await database.fetch_all(
        """
        SELECT
          product_key,
          pivota_signature_id,
          brand,
          category,
          product_type,
          title
        FROM catalog_products
        WHERE category_path IS NULL
          AND (CAST(:after_key AS text) IS NULL OR product_key > CAST(:after_key AS text))
        ORDER BY product_key ASC
        LIMIT :limit
        """,
        {"limit": limit, "after_key": after_key},
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


def _increment(counter: Dict[str, int], key: str) -> None:
    normalized = key.strip() if key else "unknown"
    counter[normalized] = int(counter.get(normalized) or 0) + 1


def _group_key(row: Dict[str, Any]) -> str:
    brand = str(row.get("brand") or "unknown").strip() or "unknown"
    product_type = str(row.get("product_type") or "unknown").strip() or "unknown"
    return f"{brand} :: {product_type}"


async def run_category_path_backfill(
    *,
    batch_size: int = 1000,
    limit: int = 0,
    dry_run: bool = False,
    sample_limit: int = 20,
) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    matched = 0
    unmatched = 0
    after_key: Optional[str] = None
    matched_by_label: Dict[str, int] = {}
    matched_by_path: Dict[str, int] = {}
    unmatched_by_brand_product_type: Dict[str, int] = {}
    matched_samples: List[Dict[str, Any]] = []
    unmatched_samples: List[Dict[str, Any]] = []

    while True:
        remaining = max(0, int(limit or 0) - total) if limit else 0
        if limit and remaining <= 0:
            break
        rows = await _fetch_batch(min(batch_size, remaining) if limit else batch_size, after_key)
        if not rows:
            break
        for row in rows:
            total += 1
            after_key = str(row.get("product_key") or "")
            hit = resolve_path_from_row(
                category=row.get("category"),
                product_type=row.get("product_type"),
                title=row.get("title"),
            )
            if hit is None:
                unmatched += 1
                _increment(unmatched_by_brand_product_type, _group_key(row))
                if len(unmatched_samples) < sample_limit:
                    unmatched_samples.append(
                        {
                            "product_key": row.get("product_key"),
                            "pivota_signature_id": row.get("pivota_signature_id"),
                            "brand": row.get("brand"),
                            "product_type": row.get("product_type"),
                            "category": row.get("category"),
                            "title": row.get("title"),
                        }
                    )
                continue
            label, path = hit
            if not dry_run:
                await _apply_update(row["product_key"], path)
            matched += 1
            _increment(matched_by_label, label)
            _increment(matched_by_path, path)
            if len(matched_samples) < sample_limit:
                matched_samples.append(
                    {
                        "product_key": row.get("product_key"),
                        "pivota_signature_id": row.get("pivota_signature_id"),
                        "brand": row.get("brand"),
                        "product_type": row.get("product_type"),
                        "category": row.get("category"),
                        "title": row.get("title"),
                        "category_label": label,
                        "category_path": path,
                    }
                )
            if matched % 100 == 0:
                logger.info("matched=%d unmatched=%d total=%d", matched, unmatched, total)

    unmatched_groups = [
        {"brand_product_type": key, "rows": rows}
        for key, rows in sorted(
            unmatched_by_brand_product_type.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]
    return {
        "matched": matched,
        "unmatched": unmatched,
        "total": total,
        "dry_run": dry_run,
        "batch_size": batch_size,
        "limit": limit,
        "category_confidence": CONFIDENCE_REGEX_BACKFILL,
        "category_label_source": LABEL_SOURCE,
        "matched_by_label": dict(sorted(matched_by_label.items())),
        "matched_by_path": dict(sorted(matched_by_path.items())),
        "unmatched_by_brand_product_type": unmatched_groups,
        "matched_samples": matched_samples,
        "unmatched_samples": unmatched_samples,
    }


async def _run(args: argparse.Namespace) -> int:
    report = await run_category_path_backfill(
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        sample_limit=args.sample_limit,
    )
    logger.info(
        "Backfill complete: matched=%d unmatched=%d total=%d dry_run=%s",
        report["matched"],
        report["unmatched"],
        report["total"],
        report["dry_run"],
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000, help="rows per batch")
    parser.add_argument("--limit", type=int, default=0, help="cap total rows; 0 = no cap")
    parser.add_argument("--sample-limit", type=int, default=20, help="sample rows in JSON report")
    parser.add_argument("--dry-run", action="store_true", help="don't UPDATE; just count matches")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
