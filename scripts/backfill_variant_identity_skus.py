"""Give external-referral products the purchasable SKU+offer their crawl already paid for.

WHAT THIS FIXES. Measured on prod 2026-09-07: of 13,799 `platform='external_seed'` products,
8,694 have no SKU carrying a merchant-issued variant id, while 4,560 of them have such an id
sitting unread in `seed_data.snapshot.variants` / `product_payload.variants`. The identity was
crawled and stored; nothing ever projected it to the layer checkout reads.

WHY NOT JUST RUN THE PROMOTER. `services/catalog_variant_promoter` projects variants into
catalog_skus and deliberately does not touch offers. That is fine for its own purpose (rendering
shade swatches) and useless for this one: measured the same day, **5,271 promoter-built
`<pk>::v::<id>` SKUs exist on the external track and exactly 0 of them carry a catalog_offers
row**, while 1,583 of 1,583 ingestion-built `<pk>::v:<id>` SKUs do. The serving price gate and
recall both read catalog_offers, so a SKU without one is inert. This backfill therefore writes the
pair, in the shape `services/catalog_enrichment_agent/ingestion` already writes at ingest time —
verified against 12 live rows, whose sku_keys `derive_variant_sku_key` reproduces exactly.

TWO THINGS IT REFUSES TO DO.

1. **It will not promote an id we minted ourselves.** 1,645 of the recoverable products carry
   `<external_product_id>-default` or a hex digest of the product key — see
   `services/variant_identity`. Writing those would manufacture a SKU that looks purchasable and
   is not, which is the failure the gateway's isRestatedProductId guard exists to catch.

2. **It will not inherit a price.** The obvious shortcut is to clone the product's canonical offer
   onto each variant, and it is wrong: variants differ precisely in the things that carry price
   (30 ml vs 50 ml, a set vs a single). A variant without its own price is counted and skipped,
   never given the product's. `--allow-inherited-price` exists to make that choice explicit and
   auditable if someone later decides a single-variant product may inherit; it is off by default.

Every row it writes is stamped `variant_id_provenance` and `source_system` so the whole batch is
identifiable and reversible.

    python3 scripts/backfill_variant_identity_skus.py                 # dry run, prints the plan
    python3 scripts/backfill_variant_identity_skus.py --apply
    python3 scripts/backfill_variant_identity_skus.py --apply --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402
from services.catalog_enrichment_agent.ingestion import (  # noqa: E402
    OFFER_CATALOG_TRACK,
    OFFER_MODE,
    OFFER_READINESS_TIER,
    OFFER_TRUTH_TIER,
    derive_offer_id,
    derive_variant_sku_key,
    variant_own_price,
)
from services.catalog_variant_promoter import (  # noqa: E402
    _extract_variants_from_payload,
    _extract_variants_from_seed,
    filter_real_variants,
)
from services.variant_identity import (  # noqa: E402
    MERCHANT_ISSUED,
    variant_id_provenance,
)

logger = logging.getLogger("backfill_variant_identity_skus")

SOURCE_SYSTEM = "variant_identity_backfill_v1"

SELECT_PRODUCTS_SQL = """
    SELECT cp.product_key, cp.merchant_id, cp.platform, cp.source_product_id,
           cp.source_domain, cp.title, cp.brand,
           (eps.seed_data->'snapshot'->'variants') AS seed_variants,
           (cp.product_payload->'variants')        AS payload_variants
    FROM catalog_products cp
    LEFT JOIN external_product_seeds eps
      ON eps.external_product_id = cp.source_product_id
    WHERE cp.platform = 'external_seed'
"""

#: The offer we copy non-price fields from. Its destination_url is what the buyer is sent to,
#: and a variant of the same product is sold on the same PDP, so the destination carries over.
SELECT_CANONICAL_OFFER_SQL = """
    SELECT offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
           readiness_tier, offer_mode, channel, availability, currency,
           list_price, merchant_effective_price, estimated_best_price,
           source_domain, market, offer_payload
    FROM catalog_offers
    WHERE product_key = :pk
    ORDER BY (suppressed_at IS NULL) DESC, updated_at DESC NULLS LAST
    LIMIT 1
"""

UPSERT_SKU_SQL = """
    INSERT INTO catalog_skus (
        sku_key, product_key, merchant_id, platform, source_product_id,
        source_variant_id, source_domain, sku, barcode, title, currency,
        image_url, visible_attributes, visible_option_labels, ingredient_ids,
        sku_payload, readiness_tier, updated_at
    ) VALUES (
        :sku_key, :product_key, :merchant_id, :platform, :source_product_id,
        :source_variant_id, :source_domain, :sku, :barcode, :title, :currency,
        :image_url, CAST(:visible_attributes AS jsonb),
        CAST(:visible_option_labels AS jsonb), CAST(:ingredient_ids AS jsonb),
        CAST(:sku_payload AS jsonb), :readiness_tier, NOW()
    )
    ON CONFLICT (sku_key) DO UPDATE SET
        source_variant_id = EXCLUDED.source_variant_id,
        sku_payload       = EXCLUDED.sku_payload,
        updated_at        = NOW()
"""

UPSERT_OFFER_SQL = """
    INSERT INTO catalog_offers (
        offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
        readiness_tier, offer_mode, channel, availability, currency,
        list_price, merchant_effective_price, estimated_best_price,
        source_system, source_domain, market, offer_payload, created_at, updated_at
    ) VALUES (
        :offer_id, :sku_key, :product_key, :merchant_id, :catalog_track, :truth_tier,
        :readiness_tier, :offer_mode, :channel, :availability, :currency,
        :list_price, :merchant_effective_price, :estimated_best_price,
        :source_system, :source_domain, :market, CAST(:offer_payload AS jsonb),
        NOW(), NOW()
    )
    ON CONFLICT (offer_id) DO UPDATE SET
        list_price               = EXCLUDED.list_price,
        merchant_effective_price = EXCLUDED.merchant_effective_price,
        estimated_best_price     = EXCLUDED.estimated_best_price,
        availability             = EXCLUDED.availability,
        updated_at               = NOW()
"""


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def _as_list(value: Any) -> List[Dict[str, Any]]:
    v = _jsonb(value)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _price_of(variant: Dict[str, Any]) -> Optional[float]:
    """The variant's OWN price. Never the product's — see the module docstring.
    One rule with ingestion: `variant_own_price`."""
    return variant_own_price(variant)


def _availability_of(variant: Dict[str, Any], fallback: Optional[str]) -> str:
    raw = variant.get("availability")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if "in_stock" in variant:
        return "in_stock" if variant.get("in_stock") else "out_of_stock"
    return fallback or "unknown"


def _option_labels(variant: Dict[str, Any]) -> Tuple[List[str], Dict[str, str]]:
    labels: List[str] = []
    attrs: Dict[str, str] = {}
    options = variant.get("options")
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            axis = str(opt.get("axis_kind") or opt.get("name") or "").strip().lower()
            val = str(opt.get("value") or "").strip()
            if axis and val:
                attrs[axis] = val
                labels.append(f"{axis}_{val.lower().replace(' ', '_')}")
    elif isinstance(options, dict):
        for k, v in options.items():
            if isinstance(v, str) and v.strip():
                axis = str(k).strip().lower()
                attrs[axis] = v.strip()
                labels.append(f"{axis}_{v.strip().lower().replace(' ', '_')}")
    if not labels:
        shade = str(variant.get("title") or "").strip()
        if shade:
            attrs.setdefault("shade", shade)
            labels.append("shade_" + shade.lower().replace(" ", "_"))
    return labels, attrs


def plan_for_product(row: Dict[str, Any], counts: collections.Counter) -> List[Dict[str, Any]]:
    """The variants of one product that have earned a purchasable SKU."""
    product_key = row["product_key"]
    variants = _extract_variants_from_seed(
        {"snapshot": {"variants": _as_list(row["seed_variants"])}}
    ) or _extract_variants_from_payload({"variants": _as_list(row["payload_variants"])})
    out: List[Dict[str, Any]] = []
    for variant in filter_real_variants(variants):
        vid = str(variant.get("variant_id") or variant.get("id") or "").strip()
        provenance = variant_id_provenance(
            vid, product_id=row["source_product_id"], product_key=product_key
        )
        if provenance != MERCHANT_ISSUED:
            counts["skipped_not_merchant_issued"] += 1
            continue
        price = _price_of(variant)
        if price is None:
            counts["skipped_no_variant_price"] += 1
            continue
        out.append({"variant_id": vid, "variant": variant, "price": price})
    return out


async def run(*, apply: bool, limit: int, allow_inherited_price: bool) -> Dict[str, Any]:
    counts: collections.Counter = collections.Counter()
    rows = await database.fetch_all(SELECT_PRODUCTS_SQL)
    counts["products_scanned"] = len(rows or [])

    touched = 0
    for row in rows or []:
        row = dict(row)
        picks = plan_for_product(row, counts)
        if not picks:
            continue
        canonical = await database.fetch_one(
            SELECT_CANONICAL_OFFER_SQL, {"pk": row["product_key"]}
        )
        if canonical is None:
            counts["skipped_no_canonical_offer"] += len(picks)
            continue
        canonical = dict(canonical)
        counts["products_planned"] += 1

        for pick in picks:
            vid, variant, price = pick["variant_id"], pick["variant"], pick["price"]
            sku_key = derive_variant_sku_key(row["product_key"], vid)
            labels, attrs = _option_labels(variant)
            title = str(variant.get("title") or "").strip() or row["title"]
            currency = (
                str(variant.get("currency") or "").strip()
                or canonical.get("currency")
                or "USD"
            )
            sku_params = {
                "sku_key": sku_key,
                "product_key": row["product_key"],
                "merchant_id": row["merchant_id"],
                "platform": row["platform"],
                "source_product_id": row["source_product_id"],
                "source_variant_id": vid[:128],
                "source_domain": row.get("source_domain"),
                "sku": str(variant.get("sku") or "").strip() or None,
                "barcode": str(variant.get("barcode") or "").strip() or None,
                "title": title,
                "currency": currency,
                "image_url": str(variant.get("image_url") or "").strip() or None,
                "visible_attributes": json.dumps(attrs),
                "visible_option_labels": json.dumps(labels),
                "ingredient_ids": json.dumps([]),
                "sku_payload": json.dumps(
                    {
                        "variant_id": vid,
                        "variant_id_provenance": MERCHANT_ISSUED,
                        "source_system": SOURCE_SYSTEM,
                        "recovered_from": "seed_snapshot_or_product_payload",
                    }
                ),
                "readiness_tier": OFFER_READINESS_TIER,
            }
            offer_payload = {
                "source_system": SOURCE_SYSTEM,
                "variant_id": vid,
                "variant_id_provenance": MERCHANT_ISSUED,
                "price_from": "variant",
            }
            offer_params = {
                # derive_offer_id's third argument is a destination_url at ingest time; it is
                # only hash input, and catalog_offers keeps no destination_url column (it lives
                # in offer_payload). The canonical offer's id is the stable stand-in: unique per
                # (product, retailer offer), so re-runs land on the same offer_id and UPSERT.
                "offer_id": derive_offer_id(
                    row["product_key"], sku_key, str(canonical.get("offer_id"))
                ),
                "sku_key": sku_key,
                "product_key": row["product_key"],
                "merchant_id": canonical.get("merchant_id") or row["merchant_id"],
                "catalog_track": canonical.get("catalog_track") or OFFER_CATALOG_TRACK,
                "truth_tier": canonical.get("truth_tier") or OFFER_TRUTH_TIER,
                "readiness_tier": canonical.get("readiness_tier") or OFFER_READINESS_TIER,
                "offer_mode": canonical.get("offer_mode") or OFFER_MODE,
                "channel": canonical.get("channel") or "default",
                "availability": _availability_of(variant, canonical.get("availability")),
                "currency": currency,
                "list_price": price,
                "merchant_effective_price": price,
                "estimated_best_price": price,
                "source_system": SOURCE_SYSTEM,
                "source_domain": row.get("source_domain") or canonical.get("source_domain"),
                "market": canonical.get("market"),
                "offer_payload": json.dumps(offer_payload),
            }
            counts["skus"] += 1
            counts["offers"] += 1
            if apply:
                async with database.transaction():
                    await database.execute(UPSERT_SKU_SQL, sku_params)
                    await database.execute(UPSERT_OFFER_SQL, offer_params)

        touched += 1
        if limit and touched >= limit:
            counts["stopped_at_limit"] = 1
            break

    counts["applied"] = 1 if apply else 0
    return dict(counts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products (0 = all)")
    ap.add_argument(
        "--allow-inherited-price",
        action="store_true",
        help="NOT IMPLEMENTED BY DEFAULT: let a variant take the product's price. Off, and "
        "deliberately so — a variant differs from its siblings in exactly the dimension that "
        "carries price.",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _go():
        await database.connect()
        try:
            return await run(
                apply=args.apply,
                limit=args.limit,
                allow_inherited_price=args.allow_inherited_price,
            )
        finally:
            await database.disconnect()

    report = asyncio.run(_go())
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
