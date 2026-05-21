#!/usr/bin/env python3
"""Repair missing agent_pdp_view price aggregates from source payloads.

This is intentionally narrower than scripts/backfill_agent_pdp_view.py:
it only updates `currency`, `price_min`, `price_max`, `refreshed_at`, and
`refresh_source` for rows whose current agent_pdp_view price is missing
or nonpositive. It never rewrites title, description, image, offers,
variants, seed_data, or catalog_products content.

Dry-run:
  python3 scripts/repair_agent_pdp_view_source_prices.py --limit 500

Apply:
  python3 scripts/repair_agent_pdp_view_source_prices.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    assemble_row,
    fetch_external_seed_for_keys,
    fetch_offers_for_keys,
    fetch_products_for_key,
    fetch_skus_for_keys,
)

logger = logging.getLogger("repair_agent_pdp_view_source_prices")

PRICE_REPAIR_REFRESH_SOURCE = "price_repair_source_fallback_20260521"

PRICE_REPAIR_TARGET_SQL = """
    SELECT content_key
    FROM agent_pdp_view
    WHERE content_key IS NOT NULL
      AND (
        price_min IS NULL
        OR price_min <= 0
        OR price_max IS NULL
        OR price_max <= 0
      )
    ORDER BY refreshed_at DESC NULLS LAST, content_key ASC
    {limit_clause}
"""

PRICE_REPAIR_UPDATE_SQL = """
    UPDATE agent_pdp_view
    SET
      currency = COALESCE(:currency, currency),
      price_min = :price_min,
      price_max = :price_max,
      refreshed_at = NOW(),
      refresh_source = :refresh_source
    WHERE content_key = :content_key
      AND (
        price_min IS NULL
        OR price_min <= 0
        OR price_max IS NULL
        OR price_max <= 0
      )
"""


def _positive_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    return amount if amount > 0 else None


def _build_update_params(content_key: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    price_min = _positive_decimal(row.get("price_min"))
    price_max = _positive_decimal(row.get("price_max"))
    if price_min is None or price_max is None:
        return None
    currency = row.get("currency")
    return {
        "content_key": content_key,
        "currency": str(currency).strip().upper() if isinstance(currency, str) and currency.strip() else None,
        "price_min": price_min,
        "price_max": price_max,
        "refresh_source": PRICE_REPAIR_REFRESH_SOURCE,
    }


async def _fetch_target_content_keys(
    *,
    limit: int,
    content_key: Optional[str],
    db: Any = database,
) -> List[str]:
    if content_key:
        return [content_key]
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    params: Dict[str, Any] = {}
    if limit > 0:
        params["limit"] = int(limit)
    rows = await db.fetch_all(
        PRICE_REPAIR_TARGET_SQL.format(limit_clause=limit_clause),
        params,
    )
    out: List[str] = []
    for row in rows or []:
        row_dict = dict(row)
        if row_dict.get("content_key"):
            out.append(str(row_dict["content_key"]))
    return out


async def _assemble_price_update(content_key: str, *, db: Any = database) -> Optional[Dict[str, Any]]:
    products = await fetch_products_for_key(content_key, db=db)
    if not products:
        return None
    product_keys = [p["product_key"] for p in products if p.get("product_key")]
    skus = await fetch_skus_for_keys(product_keys, db=db)
    offers = await fetch_offers_for_keys(product_keys, db=db)
    external_seed = await fetch_external_seed_for_keys(product_keys, db=db)

    row = assemble_row(
        content_key=content_key,
        products=products,
        skus=skus,
        offers=offers,
        external_seed=external_seed,
        refresh_source=PRICE_REPAIR_REFRESH_SOURCE,
    )
    if row is None:
        return None
    return _build_update_params(content_key, row)


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    if not getattr(db, "is_connected", False):
        await db.connect()

    target_keys = await _fetch_target_content_keys(
        limit=args.limit,
        content_key=args.content_key,
        db=db,
    )

    outcomes: Dict[str, int] = {
        "content_keys_considered": len(target_keys),
        "rows_with_source_price": 0,
        "rows_updated": 0,
        "rows_skipped_no_source_price": 0,
        "rows_skipped_dry_run": 0,
    }
    samples: List[Dict[str, Any]] = []

    for content_key in target_keys:
        params = await _assemble_price_update(content_key, db=db)
        if params is None:
            outcomes["rows_skipped_no_source_price"] += 1
            continue

        outcomes["rows_with_source_price"] += 1
        if len(samples) < 10:
            samples.append({
                "content_key": content_key,
                "currency": params["currency"],
                "price_min": str(params["price_min"]),
                "price_max": str(params["price_max"]),
            })

        if not args.apply:
            outcomes["rows_skipped_dry_run"] += 1
            continue

        await db.execute(PRICE_REPAIR_UPDATE_SQL, params)
        outcomes["rows_updated"] += 1

    return {
        "apply": bool(args.apply),
        "limit": args.limit,
        "content_key": args.content_key,
        "outcome_counts": outcomes,
        "samples": samples,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update price fields. Default: dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max agent_pdp_view rows to consider (0 = all). Default 500.",
    )
    parser.add_argument(
        "--content-key",
        default=None,
        help="Repair one content_key only.",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
