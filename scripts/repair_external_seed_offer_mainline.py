#!/usr/bin/env python3
"""Repair external_seed price mainline through catalog_skus/catalog_offers.

Price must flow through the canonical offer chain before agent_pdp_view
can expose it. This script does two narrow things:

1. For mirrored external_seed catalog rows with a positive
   external_product_seeds.price_amount but no positive catalog_offers.list_price,
   upsert the canonical SKU + redirect offer.
2. Refresh only agent_pdp_view price/offer fields for rows previously
   touched by the rejected APV fallback repair, plus rows repaired in
   this run. If a row still has no positive catalog offer, its APV price
   fields are cleared rather than inferred from payloads.

It never updates title, description, image fields, seed_data, or
catalog_products content.

Dry-run:
  python3 scripts/repair_external_seed_offer_mainline.py --limit 500

Apply:
  python3 scripts/repair_external_seed_offer_mainline.py --apply --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.database import database  # noqa: E402
from scripts.mirror_external_seeds_to_catalog_products import (  # noqa: E402
    _ensure_external_seed_merchant,
    _upsert_canonical_offer_for_mirror_row,
    _upsert_canonical_sku_for_mirror_row,
)
from services.agent_pdp_view_assembler import (  # noqa: E402
    assemble_row,
    fetch_external_seed_for_keys,
    fetch_offers_for_keys,
    fetch_products_for_key,
    fetch_skus_for_keys,
    to_jsonb,
)


FALLBACK_REFRESH_SOURCE = "price_repair_source_fallback_20260521"
MAINLINE_REFRESH_SOURCE = "price_mainline_offer_refresh_20260521"
NO_OFFER_REFRESH_SOURCE = "price_mainline_no_offer_20260521"


OFFER_CHAIN_TARGETS_SQL = """
WITH ranked_seed AS (
  SELECT
    eps.*,
    ROW_NUMBER() OVER (
      PARTITION BY eps.external_product_id
      ORDER BY
        CASE WHEN eps.market = 'US' THEN 0 ELSE 1 END,
        eps.updated_at DESC NULLS LAST,
        eps.created_at DESC NULLS LAST,
        eps.id ASC
    ) AS rn
  FROM external_product_seeds eps
  WHERE lower(coalesce(eps.status, '')) = 'active'
    AND nullif(btrim(coalesce(eps.external_product_id, '')), '') IS NOT NULL
    AND eps.price_amount > 0
),
targets AS (
  SELECT
    cp.content_key,
    cp.product_key,
    rs.id,
    rs.external_product_id,
    rs.market,
    rs.tool,
    rs.domain,
    rs.title,
    rs.destination_url,
    rs.canonical_url,
    rs.price_amount,
    rs.price_currency,
    rs.availability,
    rs.image_url,
    rs.seed_data,
    rs.updated_at
  FROM catalog_products cp
  JOIN ranked_seed rs
    ON rs.external_product_id = cp.source_product_id
   AND rs.rn = 1
  WHERE cp.merchant_id = 'external_seed'
    AND cp.platform = 'external_seed'
    AND cp.product_key IS NOT NULL
    AND cp.content_key IS NOT NULL
    AND NOT EXISTS (
      SELECT 1
      FROM catalog_offers co
      WHERE co.product_key = cp.product_key
        AND co.list_price > 0
    )
)
SELECT *
FROM targets
ORDER BY updated_at DESC NULLS LAST, id ASC
{limit_clause}
"""


FALLBACK_REFRESH_TARGETS_SQL = """
SELECT DISTINCT content_key
FROM agent_pdp_view
WHERE refresh_source = :fallback_refresh_source
  AND content_key IS NOT NULL
ORDER BY content_key ASC
{limit_clause}
"""


APV_OFFER_FIELDS_UPDATE_SQL = """
UPDATE agent_pdp_view
SET
  currency = :currency,
  price_min = :price_min,
  price_max = :price_max,
  offer_count = :offer_count,
  offers = CAST(:offers AS jsonb),
  refreshed_at = NOW(),
  refresh_source = :refresh_source
WHERE content_key = :content_key
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _positive_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    return amount if amount > 0 else None


async def _fetch_offer_chain_targets(limit: int, *, db: Any = database) -> List[Dict[str, Any]]:
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    params: Dict[str, Any] = {}
    if limit > 0:
        params["limit"] = int(limit)
    rows = await db.fetch_all(
        OFFER_CHAIN_TARGETS_SQL.format(limit_clause=limit_clause),
        params,
    )
    return [dict(row) for row in rows or []]


async def _fetch_fallback_refresh_targets(limit: int, *, db: Any = database) -> List[str]:
    limit_clause = "LIMIT :limit" if limit > 0 else ""
    params: Dict[str, Any] = {"fallback_refresh_source": FALLBACK_REFRESH_SOURCE}
    if limit > 0:
        params["limit"] = int(limit)
    rows = await db.fetch_all(
        FALLBACK_REFRESH_TARGETS_SQL.format(limit_clause=limit_clause),
        params,
    )
    return [str(dict(row)["content_key"]) for row in rows or [] if dict(row).get("content_key")]


async def _build_apv_offer_field_update(
    content_key: str,
    *,
    db: Any = database,
) -> Optional[Dict[str, Any]]:
    products = await fetch_products_for_key(content_key, db=db)
    if not products:
        return None
    product_keys = [p["product_key"] for p in products if p.get("product_key")]
    skus = await fetch_skus_for_keys(product_keys, db=db)
    offers = await fetch_offers_for_keys(product_keys, db=db)
    external_seed = await fetch_external_seed_for_keys(product_keys, db=db)

    # `offers` IS an overlay-carrying column. This script's UPDATE replaces the
    # whole array (`offers = CAST(:offers AS jsonb)`) and the W8 seller-trust
    # envelope rides INSIDE each offer object — aggregate_offers sets
    # `n["seller_trust"]`. So omitting seller_trust_by_id here does not merely
    # skip an unrelated field: it strips seller trust from every offer of every
    # row the run touches.
    #
    # This file was reviewed and declared safe because it "never updates title,
    # description or image fields". True, and irrelevant — the column it DOES
    # update is one of the overlay carriers. A narrow UPDATE is not the same
    # thing as an overlay-free one.
    seller_trust_by_id: Dict[str, Dict[str, Any]] = {}
    try:
        from services.outcome_aggregation_service import seller_trust_bulk

        seller_trust_by_id = await seller_trust_bulk(
            [o.get("merchant_id") for o in offers if o.get("merchant_id")]
        )
    except Exception:  # noqa: BLE001 — best-effort, mirrors build_agent_pdp_view_row
        seller_trust_by_id = {}

    row = assemble_row(
        content_key=content_key,
        products=products,
        skus=skus,
        offers=offers,
        external_seed=external_seed,
        refresh_source=MAINLINE_REFRESH_SOURCE,
        seller_trust_by_id=seller_trust_by_id,
    )
    if row is None:
        return None

    has_positive_offer_price = (
        _positive_decimal(row.get("price_min")) is not None
        and _positive_decimal(row.get("price_max")) is not None
    )
    return {
        "content_key": content_key,
        "currency": row.get("currency") if has_positive_offer_price else None,
        "price_min": row.get("price_min") if has_positive_offer_price else None,
        "price_max": row.get("price_max") if has_positive_offer_price else None,
        "offer_count": row.get("offer_count") or 0,
        "offers": to_jsonb(row.get("offers")) if has_positive_offer_price else None,
        "refresh_source": (
            MAINLINE_REFRESH_SOURCE if has_positive_offer_price else NO_OFFER_REFRESH_SOURCE
        ),
        "has_positive_offer_price": has_positive_offer_price,
    }


async def _drive(args: argparse.Namespace, *, db: Any = database) -> Dict[str, Any]:
    if not getattr(db, "is_connected", False):
        await db.connect()

    chain_targets = await _fetch_offer_chain_targets(args.limit, db=db)
    touched_content_keys: Set[str] = set()
    outcomes: Dict[str, int] = {
        "offer_chain_targets": len(chain_targets),
        "offer_chains_upserted": 0,
        "apv_refresh_targets": 0,
        "apv_offer_price_refreshed": 0,
        "apv_price_cleared_no_offer": 0,
        "apv_refresh_skipped": 0,
        "dry_run_skipped_writes": 0,
    }

    samples: Dict[str, List[Dict[str, Any]]] = {
        "offer_chain_targets": [],
        "apv_updates": [],
    }

    if chain_targets and args.apply:
        await _ensure_external_seed_merchant()

    for row in chain_targets:
        content_key = str(row.get("content_key") or "")
        if content_key:
            touched_content_keys.add(content_key)
        if len(samples["offer_chain_targets"]) < 10:
            samples["offer_chain_targets"].append({
                "content_key": content_key,
                "product_key": row.get("product_key"),
                "seed_id": row.get("id"),
                "external_product_id": row.get("external_product_id"),
                "price_amount": str(row.get("price_amount")),
                "price_currency": row.get("price_currency"),
            })
        if not args.apply:
            outcomes["dry_run_skipped_writes"] += 1
            continue
        await _upsert_canonical_sku_for_mirror_row(str(row["product_key"]), row)
        await _upsert_canonical_offer_for_mirror_row(str(row["product_key"]), row)
        outcomes["offer_chains_upserted"] += 1

    refresh_keys: Set[str] = set(touched_content_keys)
    if args.refresh_fallback_tagged:
        refresh_keys.update(await _fetch_fallback_refresh_targets(args.refresh_limit, db=db))

    outcomes["apv_refresh_targets"] = len(refresh_keys)
    for content_key in sorted(refresh_keys):
        params = await _build_apv_offer_field_update(content_key, db=db)
        if params is None:
            outcomes["apv_refresh_skipped"] += 1
            continue
        if params["has_positive_offer_price"]:
            outcomes["apv_offer_price_refreshed"] += 1
        else:
            outcomes["apv_price_cleared_no_offer"] += 1

        if len(samples["apv_updates"]) < 10:
            samples["apv_updates"].append({
                "content_key": content_key,
                "price_min": str(params["price_min"]) if params["price_min"] is not None else None,
                "price_max": str(params["price_max"]) if params["price_max"] is not None else None,
                "currency": params["currency"],
                "refresh_source": params["refresh_source"],
            })

        if not args.apply:
            outcomes["dry_run_skipped_writes"] += 1
            continue

        update_params = dict(params)
        update_params.pop("has_positive_offer_price", None)
        await db.execute(APV_OFFER_FIELDS_UPDATE_SQL, update_params)

    return {
        "apply": bool(args.apply),
        "limit": args.limit,
        "refresh_fallback_tagged": bool(args.refresh_fallback_tagged),
        "refresh_limit": args.refresh_limit,
        "outcome_counts": outcomes,
        "samples": samples,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write catalog_skus/catalog_offers and APV offer fields. Default: dry-run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max external_seed offer-chain rows to repair (0 = all). Default 500.",
    )
    parser.add_argument(
        "--refresh-limit",
        type=int,
        default=1000,
        help="Max fallback-tagged APV rows to refresh/clear (0 = all). Default 1000.",
    )
    parser.add_argument(
        "--no-refresh-fallback-tagged",
        dest="refresh_fallback_tagged",
        action="store_false",
        help="Only refresh APV rows touched by offer-chain repairs.",
    )
    parser.set_defaults(refresh_fallback_tagged=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
