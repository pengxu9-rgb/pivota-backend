#!/usr/bin/env python3
"""Phase 2 backfill — populate catalog_products.category_path / brand_normalized
from regex patterns ported from
PIVOTA-Agent-mainline-verify/src/services/externalSeedProducts.js
(BEAUTY_CATEGORY_PATTERNS).

For each catalog_products row where category_path IS NULL:
  - Run regex against (category, product_type, title) in priority order.
  - On first match, populate category_path + category_label_source='regex_backfill'
    + category_confidence=0.85.

`--include-shallow` widens that to rows whose path is too SHALLOW to be routable (fewer than
MIN_ROUTABLE_DEPTH segments). Recall binds a hard prefix like `beauty/makeup/lip/`, and
`beauty/makeup` is an ANCESTOR of that prefix rather than a descendant, so a row filed there can
never match. Measured on prod 2026-09-09: 6,459 of 15,516 products (41.6%) sit at depth <= 2 or
NULL and 4,517 of those are serving_eligible — eligible and unreachable at the same time.

A write only lands when it strictly DEEPENS the row, so the widened mode cannot make anything
worse and stays idempotent across re-runs.

Runs in batches of 1000.

Usage:
  python scripts/backfill_pdp_category_path.py [--limit N] [--dry-run]
  python scripts/backfill_pdp_category_path.py --include-shallow --dry-run
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


# A path with fewer than this many segments cannot satisfy recall, which binds a hard prefix like
# `beauty/makeup/lip/`: `beauty/makeup` is an ANCESTOR of that prefix, not a descendant, so a row
# filed there can never match however good its content is.
MIN_ROUTABLE_DEPTH = 3

_DEPTH_SQL = "COALESCE(array_length(string_to_array(category_path, '/'), 1), 0)"


async def _fetch_batch(
    limit: int, after_key: Optional[str], *, include_shallow: bool = False
) -> List[dict]:
    """NULL category_path by default; optionally also rows too SHALLOW to be routable.

    The default is unchanged deliberately — this script has been run against NULLs before and
    widening its silent blast radius would be a nasty surprise. `--include-shallow` opts in.
    """
    predicate = "category_path IS NULL"
    if include_shallow:
        predicate = "(category_path IS NULL OR %s < :min_depth)" % _DEPTH_SQL
    rows = await database.fetch_all(
        """
        SELECT
          product_key,
          pivota_signature_id,
          brand,
          category,
          category_path,
          product_type,
          title
        FROM catalog_products
        WHERE """ + predicate + """
          AND (CAST(:after_key AS text) IS NULL OR product_key > CAST(:after_key AS text))
        ORDER BY product_key ASC
        LIMIT :limit
        """,
        {"limit": limit, "after_key": after_key, "min_depth": MIN_ROUTABLE_DEPTH},
    )
    return [dict(row) for row in rows or []]


def _depth(path: Optional[str]) -> int:
    text = str(path or "").strip()
    return len(text.split("/")) if text else 0


async def _apply_update(
    product_key: str, category_path: str, *, previous_path: Optional[str] = None
) -> None:
    """Write the new path, but NEVER make a row shallower than it already is.

    Two guards, both in SQL so a concurrent writer cannot slip between the read and the write:
      - the depth check refuses a write that does not strictly deepen the row, so re-running is
        idempotent and a regex that resolves something vaguer than what is already stored loses;
      - the previous-value check is optimistic concurrency: if anything changed the row since this
        batch read it, this update declines rather than clobbering that decision.
    """
    await database.execute(
        """
        UPDATE catalog_products
        SET category_path = :path,
            category_confidence = :confidence,
            category_label_source = :source
        WHERE product_key = :key
          AND category_path IS NOT DISTINCT FROM CAST(:previous AS varchar)
          AND :new_depth > """ + _DEPTH_SQL + """
        """,
        {
            "key": product_key,
            "path": category_path,
            "previous": previous_path,
            "new_depth": _depth(category_path),
            "confidence": CONFIDENCE_REGEX_BACKFILL,
            "source": LABEL_SOURCE,
        },
    )


def _classification_inputs(row: Dict[str, Any]):
    """Return (category, product_type) with any echo of the stored path's leaf removed.

    `category` and `product_type` are DERIVED FROM category_path's leaf by
    services/catalog_enrichment_agent/ingestion.py, so on a shallow row they are echoes of the very
    value being replaced ("makeup" from "beauty/makeup"). resolve_path_from_row tries `category`
    FIRST, so feeding an echo back lets the bad path re-derive itself and short-circuit before the
    title is ever read — the backfill would then confirm the defect instead of fixing it.

    Fields the merchant actually supplied are kept: only an exact, case-insensitive match against
    the leaf is dropped.
    """
    leaf = str(row.get("category_path") or "").rsplit("/", 1)[-1].strip().lower()
    category = row.get("category")
    product_type = row.get("product_type")
    if leaf:
        if str(category or "").strip().lower() == leaf:
            category = None
        if str(product_type or "").strip().lower() == leaf:
            product_type = None
    return category, product_type


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
    include_shallow: bool = False,
) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    matched = 0
    unmatched = 0
    not_deeper = 0
    not_deeper_by_path: Dict[str, int] = {}
    # A row moving beauty/makeup -> beauty/skincare/... is not merely deeper, it is RE-VERTICALISED.
    # category_kind is derived from this path and drives claim-safety, required disclaimers and the
    # serving gate, so a branch change is a different and larger decision than a deepening. Counted
    # separately so an operator can see how much of a run is which before applying.
    branch_changes = 0
    branch_change_pairs: Dict[str, int] = {}
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
        rows = await _fetch_batch(
            min(batch_size, remaining) if limit else batch_size,
            after_key,
            include_shallow=include_shallow,
        )
        if not rows:
            break
        for row in rows:
            total += 1
            after_key = str(row.get("product_key") or "")
            current_path = row.get("category_path")
            row_category, row_product_type = _classification_inputs(row)
            hit = resolve_path_from_row(
                category=row_category,
                product_type=row_product_type,
                title=row.get("title"),
            )
            if hit is not None and _depth(hit[1]) <= _depth(current_path):
                # Resolved, but no better than what is stored. Not a match and not a miss —
                # counting it as either would misreport what this run can achieve.
                not_deeper += 1
                _increment(not_deeper_by_path, str(current_path or "(null)"))
                continue
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
                await _apply_update(row["product_key"], path, previous_path=current_path)
            before_branch = str(current_path or "").split("/")[0].strip().lower()
            after_branch = str(path or "").split("/")[0].strip().lower()
            if before_branch and after_branch and before_branch != after_branch:
                branch_changes += 1
                _increment(branch_change_pairs, "%s -> %s" % (current_path, path))
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
                        "category_path_before": current_path,
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
        "not_deeper": not_deeper,
        "branch_changes": branch_changes,
        "branch_change_pairs": dict(
            sorted(branch_change_pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:25]
        ),
        "not_deeper_by_current_path": dict(
            sorted(not_deeper_by_path.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        ),
        "include_shallow": include_shallow,
        "min_routable_depth": MIN_ROUTABLE_DEPTH,
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
        include_shallow=args.include_shallow,
    )
    logger.info(
        "Backfill complete: matched=%d unmatched=%d not_deeper=%d branch_changes=%d "
        "total=%d include_shallow=%s dry_run=%s",
        report["matched"],
        report["unmatched"],
        report["not_deeper"],
        report["branch_changes"],
        report["total"],
        report["include_shallow"],
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
    parser.add_argument(
        "--include-shallow",
        action="store_true",
        help=(
            "also process rows whose category_path is too SHALLOW to be routable "
            "(fewer than %d segments), not just NULL ones. A write only lands when it "
            "strictly deepens the row." % MIN_ROUTABLE_DEPTH
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
