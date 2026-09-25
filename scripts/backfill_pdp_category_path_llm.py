#!/usr/bin/env python3
"""LLM-backed category backfill — covers rows that the regex backfill
(backfill_pdp_category_path.py) couldn't classify: brand-opaque beauty
product names and electronics model numbers.

Requires:
  LLM_CATEGORY_CLASSIFIER_ENABLED=true
  DEEPSEEK_API_KEY=<key>
  DATABASE_URL=<url>

Calls fold_category_with_llm_fallback per row (regex first, LLM if miss).
Writes category_path, category_label, category_label_source, category_confidence.

Usage:
  LLM_CATEGORY_CLASSIFIER_ENABLED=true DEEPSEEK_API_KEY=sk-... \\
    python scripts/backfill_pdp_category_path_llm.py --dry-run
  ... (remove --dry-run to apply)
  ... --limit 100          # process at most N rows
  ... --concurrency 5      # parallel LLM calls (default 3)
  ... --min-confidence 0.5 # skip writes below this threshold (default 0.5)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database
from services.pdp_category_classifier import fold_category_with_llm_fallback

logger = logging.getLogger("backfill_pdp_category_path_llm")

LABEL_SOURCE = "llm_category_v1"
DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_CONCURRENCY = 3
DEFAULT_BATCH_SIZE = 200


async def _fetch_batch(limit: int, after_key: Optional[str]) -> List[dict]:
    rows = await database.fetch_all(
        """
        SELECT
          product_key,
          merchant_id,
          brand,
          category,
          product_type,
          title,
          description
        FROM catalog_products
        WHERE category_path IS NULL
          AND (CAST(:after_key AS text) IS NULL OR product_key > CAST(:after_key AS text))
        ORDER BY product_key ASC
        LIMIT :limit
        """,
        {"limit": limit, "after_key": after_key},
    )
    return [dict(row) for row in rows or []]


async def _apply_update(
    product_key: str,
    label: str,
    path: str,
    confidence: float,
) -> None:
    await database.execute(
        """
        UPDATE catalog_products
        SET category_path = :path,
            category_label = :label,
            category_confidence = :confidence,
            category_label_source = :source
        WHERE product_key = :key AND category_path IS NULL
        """,
        {
            "key": product_key,
            "path": path,
            "label": label,
            "confidence": confidence,
            "source": LABEL_SOURCE,
        },
    )


def _enriched_description(brand: Optional[str], description: Optional[str]) -> Optional[str]:
    """Prepend brand so the LLM has signal for model-number-only products."""
    brand = (brand or "").strip()
    description = (description or "").strip()
    if brand and description:
        return f"Brand: {brand}. {description}"
    if brand:
        return f"Brand: {brand}."
    return description or None


async def _classify_one(
    row: dict,
    semaphore: asyncio.Semaphore,
    min_confidence: float,
    dry_run: bool,
) -> Dict[str, Any]:
    product_key = row["product_key"]
    async with semaphore:
        enriched_desc = _enriched_description(row.get("brand"), row.get("description"))
        result = await fold_category_with_llm_fallback(
            merchant_id=row.get("merchant_id"),
            category=row.get("category"),
            product_type=row.get("product_type"),
            title=row.get("title"),
            description=enriched_desc,
        )

    if result is None:
        return {"product_key": product_key, "outcome": "no_signal"}

    (label, path), source, confidence = result

    if confidence < min_confidence:
        return {
            "product_key": product_key,
            "outcome": "below_threshold",
            "label": label,
            "path": path,
            "confidence": confidence,
            "source": source,
        }

    if not dry_run:
        await _apply_update(product_key, label, path, confidence)

    return {
        "product_key": product_key,
        "outcome": "matched",
        "label": label,
        "path": path,
        "confidence": round(confidence, 4),
        "source": source,
        "brand": row.get("brand"),
        "title": row.get("title"),
    }


async def run_llm_backfill(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int = 0,
    dry_run: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    sample_limit: int = 20,
) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    matched = 0
    below_threshold = 0
    no_signal = 0
    after_key: Optional[str] = None
    matched_by_path: Dict[str, int] = {}
    matched_samples: List[dict] = []
    skipped_samples: List[dict] = []

    semaphore = asyncio.Semaphore(concurrency)

    while True:
        remaining = max(0, int(limit or 0) - total) if limit else 0
        if limit and remaining <= 0:
            break
        fetch_size = min(batch_size, remaining) if limit else batch_size
        rows = await _fetch_batch(fetch_size, after_key)
        if not rows:
            break

        after_key = str(rows[-1].get("product_key") or "")
        total += len(rows)

        tasks = [
            _classify_one(row, semaphore, min_confidence, dry_run)
            for row in rows
        ]
        results = await asyncio.gather(*tasks)

        for res in results:
            outcome = res.get("outcome")
            if outcome == "matched":
                matched += 1
                path = res.get("path", "")
                matched_by_path[path] = matched_by_path.get(path, 0) + 1
                if len(matched_samples) < sample_limit:
                    matched_samples.append(res)
            elif outcome == "below_threshold":
                below_threshold += 1
                if len(skipped_samples) < sample_limit:
                    skipped_samples.append(res)
            else:
                no_signal += 1

        logger.info(
            "Batch done: total=%d matched=%d below_threshold=%d no_signal=%d",
            total, matched, below_threshold, no_signal,
        )

    return {
        "matched": matched,
        "below_threshold": below_threshold,
        "no_signal": no_signal,
        "total": total,
        "dry_run": dry_run,
        "min_confidence": min_confidence,
        "concurrency": concurrency,
        "category_label_source": LABEL_SOURCE,
        "matched_by_path": dict(sorted(matched_by_path.items(), key=lambda x: -x[1])),
        "matched_samples": matched_samples,
        "skipped_samples": skipped_samples,
    }


def _check_preflight() -> None:
    missing = []
    if not os.getenv("LLM_CATEGORY_CLASSIFIER_ENABLED", "").strip():
        missing.append("LLM_CATEGORY_CLASSIFIER_ENABLED=true")
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        missing.append("DEEPSEEK_API_KEY=<key>")
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    p.add_argument("--sample-limit", type=int, default=20)
    return p.parse_args()


async def _main() -> None:
    _check_preflight()
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    report = await run_llm_backfill(
        batch_size=args.batch_size,
        limit=args.limit,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        min_confidence=args.min_confidence,
        sample_limit=args.sample_limit,
    )
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(_main())
