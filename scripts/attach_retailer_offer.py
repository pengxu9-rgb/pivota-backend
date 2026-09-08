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

THE SKU PRECONDITION (added 2026-09-08). This tool wrote 118 LIVE ORPHAN OFFERS
on prod: `sku_key` is built as `product_key + "::canonical"` and NOTHING checked
that such a `catalog_skus` row exists. It frequently does not — `--product-key`
is typed by an operator, and a product whose canonical chain was never
materialized (no mirror run, or a Path C row whose SKUs carry a different
spelling) has no `::canonical` SKU at all. The resulting offer is invisible to
every sku-joined read lane (`pivot_query_service` INNER JOINs `catalog_skus`)
while still counting as supply everywhere else.

THIS WRITER REFUSES; IT DOES NOT MINT. `scripts/capture_us_market_offers.py`
makes the opposite choice, and the difference is the point:

  * There, the SKU is derivable from a row we already validated, the identity IS
    the product, and refusing would drop the whole cohort that lane exists to
    recover.
  * Here, the product_key is operator-supplied (a typo produces a plausible
    key), and `merchant_id` is the RETAILER — Olive Young, Amazon — not the
    product's own seller. A SKU minted under the retailer would be a SECOND
    identity tuple for one product under the 4-column identity index, which is
    exactly the rival-identity state #2135 forbids; and a SKU minted under the
    product's seller would be this tool inventing brand identity as a side
    effect of recording a retailer link. Neither is this tool's business.

So an unresolvable `sku_key` is an INPUT ERROR and exits non-zero with the key
named, counted as `offers_refused_no_sku`. Build the canonical chain first (the
external-seed mirror, or the enrichment apply), then attach.

The orphan check reuses `fetch_existing_catalog_sku_keys` from
`services/catalog_offer_writer_guard` — the same read the shared
`guard_catalog_offer_rows` chokepoint makes. It deliberately does NOT call the
full guard: that also rejects a null price as `zero_or_missing_price`, which
would silently retire the destination-only offer this tool documents above.
Whether a destination-only offer should still be allowed is a separate policy
question and not one an orphan fix gets to decide by accident.

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
from services.catalog_offer_writer_guard import (  # noqa: E402
    ORPHAN_NO_SKU,
    WriterAuditAccumulator,
    fetch_existing_catalog_sku_keys,
    make_batch_id,
    write_writer_audit_log,
)

#: One line, fenced — `scripts/ops/run_oneoff_job.sh` reads job output from Cloud
#: Logging, which drops lines. See scripts/backfill_variant_identity_skus.py.
REPORT_BEGIN = "ATTACHREPORT>>>"
REPORT_END = "<<<ATTACHREPORT"

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


class OrphanOfferRefused(Exception):
    """The offer names a sku_key with no catalog_skus row behind it."""

    def __init__(self, sku_key: str):
        self.sku_key = sku_key
        super().__init__(
            f"{ORPHAN_NO_SKU}: no catalog_skus row for sku_key={sku_key!r}. "
            "This offer would be invisible to every sku-joined read lane. "
            "Materialize the product's canonical SKU chain first "
            "(the external-seed mirror, or the enrichment apply), then re-run."
        )


async def sku_exists(sku_key: str, *, db: Any = None) -> bool:
    """Does the offer's sku_key name a real row? The same read the shared
    `guard_catalog_offer_rows` chokepoint makes — see the module docstring for
    why the full guard is not used here."""
    found = await fetch_existing_catalog_sku_keys([sku_key], db=db)
    return sku_key in found


async def attach_retailer_offer(row: Dict[str, Any]) -> Optional[str]:
    """Insert the offer + re-materialize the served PDP. Returns the content_key.

    Raises OrphanOfferRefused BEFORE the INSERT when the sku_key does not exist.
    The check is here rather than in `_drive` on purpose: this is the function
    every caller (the CLI, and any future job) goes through, so the refusal
    cannot be skipped by not using the command line.
    """
    if not await sku_exists(row["sku_key"]):
        raise OrphanOfferRefused(row["sku_key"])
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


async def _drive(args: argparse.Namespace) -> int:
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
    audit = WriterAuditAccumulator(
        writer_name=SOURCE_SYSTEM, batch_id=make_batch_id(SOURCE_SYSTEM)
    )
    report: Dict[str, Any] = {
        "writer": SOURCE_SYSTEM,
        "batch_id": audit.batch_id,
        "applied": 1 if args.apply else 0,
        "offer_id": row["offer_id"],
        "product_key": row["product_key"],
        "sku_key": row["sku_key"],
        "merchant_id": row["merchant_id"],
        "market": row["market"],
        "currency": row["currency"],
        "list_price": row["list_price"],
        "written": 0,
        "offers_refused_no_sku": 0,
    }
    print(f"{'APPLY' if args.apply else 'DRY'} :: retailer offer {row['offer_id']}")
    print(f"  {row['merchant_id']} {row['currency']} {row['list_price']} market={row['market']} -> {args.product_key}")
    if price is None:
        print("  NOTE: no --price => destination-only; will not surface until priced.")

    exit_code = 0
    await database.connect()
    try:
        # THE DRY RUN MAKES THE SAME CHECK. A plan that reported "would attach"
        # for an offer --apply then refuses is the failure mode this whole change
        # is about: a writer whose report and whose writes disagree.
        if not await sku_exists(row["sku_key"]):
            report["offers_refused_no_sku"] = 1
            report["refusal"] = ORPHAN_NO_SKU
            print(f"  REFUSED ({ORPHAN_NO_SKU}): no catalog_skus row for "
                  f"sku_key={row['sku_key']}")
            exit_code = 2
        elif args.apply:
            content_key = await attach_retailer_offer(row)
            report["written"] = 1
            report["content_key"] = content_key
            print(f"  attached + re-materialized content_key={content_key}")
        if args.apply:
            audit.record_applied(report["written"])
            if report["offers_refused_no_sku"]:
                audit.record_skips({ORPHAN_NO_SKU: 1})
            await write_writer_audit_log(audit)
    finally:
        await database.disconnect()
    print(REPORT_BEGIN + json.dumps(report, sort_keys=True, default=str) + REPORT_END,
          flush=True)
    return exit_code


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
    # The refusal's exit code REACHES THE CALLER. Returning 0 unconditionally is
    # how an operator (or a wrapper script) reads "offer attached" off a run that
    # attached nothing.
    return asyncio.run(_drive(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
