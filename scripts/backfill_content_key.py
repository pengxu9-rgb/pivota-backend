"""Backfill content_key on catalog_products rows predating migration 083.

Stage 1 deliverable per plans/rosy-mixing-bengio.md. Migration 083 added
the column nullable; new writes (Path A/B/C/D) populate it; this script
fills in the existing ~thousands of legacy rows.

What it does:
  1. SELECT product_key, brand, title, optional GTIN from catalog_skus
     for rows where catalog_products.content_key IS NULL.
  2. Compute content_key via services.catalog_identity.make_content_key.
  3. UPDATE catalog_products.content_key for each row.

What it does NOT do:
  - Touch seed_data, product_payload, or any content field. content_key
    is identity metadata, not content; no need to route through the
    seed_data writer service (PR #426).
  - Auto-merge duplicates. Stage 2 (services/product_group_autogrouper.py)
    will pick up the duplicate surface this script exposes.

Usage:
  Dry-run (default; reports per-batch counts + first N samples):
    python3 scripts/backfill_content_key.py --limit 200

  Apply:
    python3 scripts/backfill_content_key.py --apply --limit 200 --offset 0
    # Then walk offset N until coverage complete.

  Full backfill in one shot (only on small DBs / staging):
    python3 scripts/backfill_content_key.py --apply --limit 0

Note: --limit 0 means "no limit". --offset always honored when --limit>0.
Ordering is by id ASC (write-stable — UPDATE doesn't change id, so
chunks don't re-process the same rows even under --apply).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_identity import make_content_key  # noqa: E402

logger = logging.getLogger(__name__)


def _select_sql(*, limit: int, offset: int) -> str:
    """Pull rows that don't have content_key yet. id ASC ordering is
    stable under concurrent UPDATEs of unrelated columns — id is
    immutable, so paginating with --offset is reliable across chunks."""
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    offset_clause = "OFFSET :offset" if offset > 0 else ""
    # LEFT JOIN catalog_skus to fish a GTIN-like value for rows whose
    # SKU table has barcode. Many legacy rows won't have any catalog_skus
    # row (Path B mirror used to skip the SKU chain pre-Phase-7d); they
    # just get content_key from brand+title with gtin=None.
    return f"""
        SELECT
          cp.id,
          cp.product_key,
          cp.brand,
          cp.title,
          (SELECT s.barcode FROM catalog_skus s
             WHERE s.product_key = cp.product_key
               AND s.barcode IS NOT NULL
             LIMIT 1) AS gtin
        FROM catalog_products cp
        WHERE cp.content_key IS NULL
        ORDER BY cp.id ASC
        {limit_clause}
        {offset_clause}
    """


UPDATE_SQL = """
    UPDATE catalog_products
    SET content_key = :content_key,
        updated_at = NOW()
    WHERE id = :id
      AND content_key IS NULL
"""


async def _fetch_candidates(args: argparse.Namespace) -> List[Dict[str, Any]]:
    sql = _select_sql(limit=args.limit, offset=args.offset)
    params: Dict[str, Any] = {}
    if args.limit > 0:
        params["limit"] = int(args.limit)
    if args.offset > 0:
        params["offset"] = int(args.offset)
    rows = await database.fetch_all(sql, params)
    return [dict(r) for r in rows or []]


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    rows = await _fetch_candidates(args)
    logger.info("loaded %d candidate rows (limit=%d offset=%d)",
                len(rows), args.limit, args.offset)

    outcomes: Dict[str, int] = {
        "considered": len(rows),
        "key_computed": 0,
        "key_null_brand_or_title_missing": 0,
        "updated": 0,
        "skipped_no_op_in_dry_run": 0,
    }
    samples: List[Dict[str, Any]] = []

    for row in rows:
        key = make_content_key(row.get("brand"), row.get("title"), row.get("gtin"))
        if key is None:
            outcomes["key_null_brand_or_title_missing"] += 1
            continue
        outcomes["key_computed"] += 1
        if len(samples) < 5:
            samples.append({
                "id": row["id"],
                "product_key": row["product_key"],
                "brand": row.get("brand"),
                "title": row.get("title"),
                "gtin": row.get("gtin"),
                "content_key": key,
            })
        if not args.apply:
            outcomes["skipped_no_op_in_dry_run"] += 1
            continue
        rc = await database.execute(UPDATE_SQL, {"id": row["id"], "content_key": key})
        # `databases.execute` returns rowcount-equivalent for UPDATE
        if rc is None or rc:
            outcomes["updated"] += 1

    return {"outcome_counts": outcomes, "samples": samples}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually UPDATE catalog_products. Default: dry-run.",
    )
    p.add_argument(
        "--limit", type=int, default=200,
        help="Max candidate rows to process this run (0 = all). Default 200.",
    )
    p.add_argument(
        "--offset", type=int, default=0,
        help="Skip the first N candidate rows. Use to paginate across chunks.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
