"""One-shot backfill — stamp the FDA/DSHEA disclaimer onto existing supplement
agent_pdp_view rows.

The assembler now floor-merges the category-mandatory disclaimer into
agent_pdp_view.required_disclaimers (see services.claim_safety
.ensure_category_disclaimers), so every supplement re-materialized after that
change carries the FDA/DSHEA statement even when the merchant authored none.
Rows assembled *before* that change keep their old (often empty) disclaimers
until something re-materializes them.

Catalog sync's writer hook and the scheduled sweep re-materialize on their own
cadence, so supplements that re-sync populate naturally. This script forces
immediate coverage for the existing supplement window without waiting.

It re-materializes through the canonical, evidence-aware, identity-gated path
(refresh_agent_pdp_view_for_content_key) — NOT the evidence-less assemble_row
loop — so it never wipes authored evidence/claims while adding the disclaimer.

Usage
-----
Dry-run (default — lists the supplement content_keys it would refresh):
  python3 scripts/backfill_supplement_disclaimers.py

Apply:
  python3 scripts/backfill_supplement_disclaimers.py --apply
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
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
)

logger = logging.getLogger("backfill_supplement_disclaimers")

REFRESH_SOURCE = "supplement_disclaimer_backfill"


async def _fetch_supplement_content_keys(*, limit: int) -> List[str]:
    """Distinct content_keys whose catalog product is classified supplement."""
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    sql = f"""
        SELECT DISTINCT content_key
        FROM catalog_products
        WHERE content_key IS NOT NULL
          AND category_kind = 'supplement'
        ORDER BY content_key ASC
        {limit_clause}
    """
    params: Dict[str, Any] = {}
    if limit > 0:
        params["limit"] = int(limit)
    rows = await database.fetch_all(sql, params)
    return [r["content_key"] for r in rows or []]


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    content_keys = await _fetch_supplement_content_keys(limit=args.limit)
    logger.info("found %d supplement content_keys (limit=%d)", len(content_keys), args.limit)

    outcomes: Dict[str, int] = {
        "supplement_content_keys": len(content_keys),
        "rows_refreshed": 0,
        "rows_skipped_too_thin": 0,
        "rows_skipped_no_op_in_dry_run": 0,
    }

    for ck in content_keys:
        if not args.apply:
            outcomes["rows_skipped_no_op_in_dry_run"] += 1
            continue
        refreshed = await refresh_agent_pdp_view_for_content_key(
            ck, refresh_source=REFRESH_SOURCE
        )
        if refreshed:
            outcomes["rows_refreshed"] += 1
        else:
            outcomes["rows_skipped_too_thin"] += 1

    return {"outcome_counts": outcomes, "sample_content_keys": content_keys[:10]}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--apply", action="store_true",
        help="Actually re-materialize the supplement rows. Default: dry-run.",
    )
    p.add_argument(
        "--limit", type=int, default=0,
        help="Max supplement content_keys to process (0 = all). Default 0.",
    )
    return p.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
