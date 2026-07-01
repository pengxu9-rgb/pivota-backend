"""Attach a retailer offer (Amazon, Olive Young, ...) to an existing canonical
product, then re-materialize the served PDP.

WHY. A foreign-D2C brand's own site is the identity/content anchor but often is
not the US-buyable surface (KRW, non-US checkout). US-buyability comes from
retailers. The commerce index already separates identity (one content_key) from
offers (N catalog_offers), so the right move is to attach the retailer's offer to
the SAME content_key: cite the brand-direct content for authority, surface the
retailer offer for the buy.

A retailer offer IS a redirect/referral (we send the buyer to the retailer's
page), so it reuses the external-referral offer shape the mirror already writes
(catalog_track=external_referral, offer_mode=redirect, readiness_tier=
referral_only, truth_tier=observed), but with offer_type='retailer',
is_first_party=False, and the retailer's own market/currency/price.

PRICE. Retailer prices are NOT crawlable (Amazon bot-blocks; Olive Young renders
price client-side behind an access-restricted API). So --price comes from a feed
you trust (retailer API, paid provider, or manual curation) -- never fabricated.
The assembler drops null-price offers, so an offer attached without --price is a
recorded destination that will not surface until a price is supplied.

Usage:
  python3 scripts/attach_retailer_offer.py \
    --product-key prod::external_seed::external_seed::anuko_32 \
    --merchant-id oliveyoung_global --merchant-name "Olive Young Global" \
    --retailer-url "https://global.oliveyoung.com/product/detail?prdtNo=GA250732178" \
    --market US --currency USD --price 25.90 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
)

# A retailer offer is a referral: we redirect the buyer to the retailer.
CATALOG_TRACK = "external_referral"
TRUTH_TIER = "observed"
READINESS_TIER = "referral_only"
OFFER_MODE = "redirect"
CHANNEL = "external_referral"
SOURCE_SYSTEM = "retailer_offer_attach_v1"
RETAILER_REFRESH_SOURCE = "retailer_offer_attach"


def _offer_id(product_key: str, merchant_id: str) -> str:
    digest = hashlib.sha256(f"{product_key}|{merchant_id}".encode("utf-8")).hexdigest()[:16]
    return f"offer:retailer:{merchant_id}:{digest}"


def build_retailer_offer_row(
    *,
    product_key: str,
    merchant_id: str,
    merchant_name: Optional[str],
    retailer_url: str,
    market: str = "US",
    currency: str = "USD",
    price: Optional[float] = None,
    availability: str = "in_stock",
) -> Dict[str, Any]:
    """Pure: build the catalog_offers bind-params for a retailer referral offer.

    price is None => a destination-only offer (recorded, but the assembler will
    not surface it until a price exists). Never invent a price here.
    """
    list_price_value: Optional[float]
    try:
        list_price_value = float(price) if price is not None else None
    except (TypeError, ValueError):
        list_price_value = None
    payload = {
        "source": SOURCE_SYSTEM,
        "destination_url": retailer_url,
        "retailer": merchant_id,
        "market": market,
    }
    return {
        "offer_id": _offer_id(product_key, merchant_id),
        "sku_key": product_key + "::canonical",
        "product_key": product_key,
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "offer_type": "retailer",
        "is_first_party": False,
        "market": market,
        "catalog_track": CATALOG_TRACK,
        "truth_tier": TRUTH_TIER,
        "readiness_tier": READINESS_TIER,
        "offer_mode": OFFER_MODE,
        "channel": CHANNEL,
        "availability": availability,
        "inventory_quantity": None,
        "currency": currency,
        "list_price": list_price_value,
        "merchant_effective_price": list_price_value,
        "estimated_best_price": list_price_value,
        "price_confidence": "0.9" if list_price_value is not None else None,
        "source_system": SOURCE_SYSTEM,
        "source_ref": retailer_url,
        "offer_payload": json.dumps(payload, ensure_ascii=False),
    }


_INSERT_SQL = """
    INSERT INTO catalog_offers
      (offer_id, sku_key, product_key, merchant_id, offer_type, is_first_party, market,
       catalog_track, truth_tier, readiness_tier, offer_mode, channel,
       availability, inventory_quantity, currency,
       list_price, merchant_effective_price, estimated_best_price,
       price_confidence, source_system, source_ref, offer_payload)
    VALUES
      (:offer_id, :sku_key, :product_key, :merchant_id, :offer_type, :is_first_party, :market,
       :catalog_track, :truth_tier, :readiness_tier, :offer_mode, :channel,
       :availability, :inventory_quantity, :currency,
       :list_price, :merchant_effective_price, :estimated_best_price,
       :price_confidence, :source_system, :source_ref, CAST(:offer_payload AS jsonb))
    ON CONFLICT (offer_id) DO UPDATE SET
      availability = EXCLUDED.availability,
      currency = EXCLUDED.currency,
      list_price = EXCLUDED.list_price,
      merchant_effective_price = EXCLUDED.merchant_effective_price,
      estimated_best_price = EXCLUDED.estimated_best_price,
      price_confidence = EXCLUDED.price_confidence,
      offer_payload = EXCLUDED.offer_payload,
      updated_at = NOW()
"""


async def _resolve_content_key(product_key: str) -> Optional[str]:
    row = await database.fetch_one(
        "SELECT content_key FROM catalog_products WHERE product_key = :pk",
        {"pk": product_key},
    )
    return (dict(row).get("content_key") if row else None)


async def attach_retailer_offer(row: Dict[str, Any]) -> Optional[str]:
    """Insert the offer + re-materialize the served PDP. Returns the content_key."""
    # merchant_name is carried in offer_payload/catalog_merchants elsewhere; the
    # catalog_offers row itself has no name column, so drop it before binding.
    params = {k: v for k, v in row.items() if k != "merchant_name"}
    await database.execute(_INSERT_SQL, params)
    content_key = await _resolve_content_key(row["product_key"])
    if content_key:
        await refresh_agent_pdp_view_for_content_key(
            content_key, refresh_source=RETAILER_REFRESH_SOURCE
        )
    return content_key


async def _drive(args: argparse.Namespace) -> None:
    price = None
    if args.price is not None:
        try:
            price = float(Decimal(str(args.price)))
        except (InvalidOperation, ValueError):
            raise SystemExit(f"invalid --price {args.price!r}")
    row = build_retailer_offer_row(
        product_key=args.product_key,
        merchant_id=args.merchant_id,
        merchant_name=args.merchant_name,
        retailer_url=args.retailer_url,
        market=args.market,
        currency=args.currency,
        price=price,
        availability=args.availability,
    )
    print(f"{'APPLY' if args.apply else 'DRY'} :: retailer offer {row['offer_id']}")
    print(f"  {row['merchant_id']} {row['currency']} {row['list_price']} market={row['market']} -> {args.product_key}")
    if not args.apply:
        if price is None:
            print("  NOTE: no --price => destination-only; will not surface until priced.")
        return
    await database.connect()
    try:
        content_key = await attach_retailer_offer(row)
        print(f"  attached + re-materialized content_key={content_key}")
    finally:
        await database.disconnect()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--product-key", required=True)
    p.add_argument("--merchant-id", required=True, help="e.g. oliveyoung_global, amazon_us")
    p.add_argument("--merchant-name", default=None)
    p.add_argument("--retailer-url", required=True)
    p.add_argument("--market", default="US")
    p.add_argument("--currency", default="USD")
    p.add_argument("--price", default=None, help="USD price from a trusted feed; omit for destination-only")
    p.add_argument("--availability", default="in_stock")
    p.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    return p.parse_args()


def main() -> int:
    asyncio.run(_drive(_parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
