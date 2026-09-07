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
   never given the product's. `--allow-inherited-price` makes ONE narrow exception explicit and
   auditable: a product whose only variant is the product (exactly one merchant-issued variant,
   no price of its own) may take the canonical offer's price, stamped `price_from=canonical`.
   Off by default; multi-variant products never inherit under any flag.

HOW IT WRITES. Per product, one transaction, one try/except: a failure is counted and logged as
`products_failed`, never allowed to abort the run with an unknown number of rows applied.
catalog_skus' live unique identity is the 4-column `idx_catalog_skus_source_identity_v2`
(merchant_id, platform, product_key, source_variant_id) -- migration 123 dropped the 3-column
one -- and `services/catalog_variant_promoter` already writes `<pk>::v::<id>` rows with that
exact identity, so an INSERT keyed on our `<pk>::v:<id>` would violate the index. The SKU is
therefore looked up by identity first and, when a row exists under any sku_key, that row is
stamped in place and its sku_key carries the offer.

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
    SKU_SUFFIX as CANONICAL_SKU_SUFFIX,
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
from utils.availability_vocabulary import normalize_availability  # noqa: E402

logger = logging.getLogger("backfill_variant_identity_skus")

SOURCE_SYSTEM = "variant_identity_backfill_v1"

#: Keyset-paged on product_key so a 13,799-row table is never held in memory and `--limit`
#: bounds the READ, not just the write loop. One seed per product (the newest), so the LEFT
#: JOIN cannot fan a product out into duplicate rows.
SELECT_PRODUCTS_SQL = """
    SELECT cp.product_key, cp.merchant_id, cp.platform, cp.source_product_id,
           cp.source_domain, cp.title, cp.brand,
           (SELECT eps.seed_data->'snapshot'->'variants'
              FROM external_product_seeds eps
             WHERE eps.external_product_id = cp.source_product_id
             ORDER BY eps.updated_at DESC NULLS LAST
             LIMIT 1)                                AS seed_variants,
           (cp.product_payload->'variants')          AS payload_variants
    FROM catalog_products cp
    WHERE cp.platform = 'external_seed'
      AND cp.product_key > :after
    ORDER BY cp.product_key
    LIMIT :page
"""

PAGE_SIZE = 500

#: The offer we copy non-price fields from. Its destination_url is what the buyer is sent to,
#: and a variant of the same product is sold on the same PDP, so the destination carries over.
#: A suppressed offer is not a source: ORDER BY only PREFERRED unsuppressed rows, so a product
#: whose every offer had been suppressed still yielded a canonical and we wrote a live variant
#: offer beside a dead one.
SELECT_CANONICAL_OFFER_SQL = """
    SELECT offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,
           readiness_tier, offer_mode, channel, availability, currency,
           list_price, merchant_effective_price, estimated_best_price,
           source_domain, market, offer_payload
    FROM catalog_offers
    WHERE product_key = :pk
      AND suppressed_at IS NULL
      AND sku_key NOT LIKE :variant_key_pattern
    ORDER BY (sku_key = :canonical_sku_key) DESC, updated_at DESC NULLS LAST
    LIMIT 1
"""

#: `<pk>::v:<id>` (ingest) and `<pk>::v::<id>` (promoter) both match; the canonical read must
#: never pick a VARIANT offer as its source -- on the second run the newest unsuppressed offer of
#: a product is one this script wrote, and hashing its id re-keyed every offer (found by the
#: dialect gate, not by reading the code).
VARIANT_SKU_KEY_PATTERN = "%::v:%"

#: The one live offer a variant SKU already carries, whoever wrote it. Re-runs and a later
#: ingest of the same product must UPDATE it, never stand a second offer beside it.
SELECT_LIVE_OFFER_FOR_SKU_SQL = """
    SELECT offer_id
    FROM catalog_offers
    WHERE sku_key = :sku_key
      AND suppressed_at IS NULL
    ORDER BY updated_at DESC NULLS LAST
    LIMIT 1
"""

COUNT_OFFERS_SQL = """
    SELECT COUNT(*) FROM catalog_offers WHERE product_key = :pk
"""

#: The row that already owns this variant's IDENTITY, under whatever sku_key wrote it.
SELECT_SKU_BY_IDENTITY_SQL = """
    SELECT sku_key, sku_payload
    FROM catalog_skus
    WHERE merchant_id = :merchant_id
      AND platform = :platform
      AND product_key = :product_key
      AND source_variant_id = :source_variant_id
    LIMIT 1
"""

STAMP_EXISTING_SKU_SQL = """
    UPDATE catalog_skus
       SET sku_payload = COALESCE(sku_payload, '{}'::jsonb) || CAST(:stamp AS jsonb),
           updated_at  = NOW()
     WHERE sku_key = :sku_key
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
    """The one availability vocabulary (utils/availability_vocabulary). A crawled
    `"https://schema.org/InStock"` or `"Sold Out"` must land as `in_stock` /
    `out_of_stock`, never verbatim; anything unclassifiable is `unknown`, which the
    serving denylist treats as servable -- the same asymmetry every other lane keeps."""
    raw = variant.get("availability")
    if raw is not None and str(raw).strip():
        # The variant SAID something. If we cannot read it, it is unknown -- the product's
        # in_stock must not be written over a statement the variant made about itself.
        return normalize_availability(raw) or "unknown"
    if "in_stock" in variant and variant.get("in_stock") is not None:
        return "in_stock" if variant.get("in_stock") else "out_of_stock"
    # The variant said nothing: the product-level availability is the best evidence.
    return normalize_availability(fallback) or "unknown"


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


def plan_for_product(
    row: Dict[str, Any], counts: collections.Counter, *, allow_inherited_price: bool = False
) -> List[Dict[str, Any]]:
    """The variants of one product that have earned a purchasable SKU."""
    product_key = row["product_key"]
    variants = _extract_variants_from_seed(
        {"snapshot": {"variants": _as_list(row["seed_variants"])}}
    ) or _extract_variants_from_payload({"variants": _as_list(row["payload_variants"])})
    real = filter_real_variants(variants)
    out: List[Dict[str, Any]] = []
    for variant in real:
        vid = str(variant.get("variant_id") or variant.get("id") or "").strip()
        provenance = variant_id_provenance(
            vid, product_id=row["source_product_id"], product_key=product_key
        )
        if provenance != MERCHANT_ISSUED:
            counts["skipped_not_merchant_issued"] += 1
            continue
        price = _price_of(variant)
        if price is None:
            # The single-variant case is the only one `--allow-inherited-price` may
            # rescue: one merchant-issued variant IS the product, so the canonical
            # offer's price is its price. run() decides, with the flag in hand.
            if len(real) == 1 and allow_inherited_price:
                out.append({"variant_id": vid, "variant": variant, "price": None})
                continue
            counts["skipped_no_variant_price"] += 1
            continue
        out.append({"variant_id": vid, "variant": variant, "price": price})
    return out


def _positive(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


async def _write_product(
    *, row: Dict[str, Any], picks: List[Dict[str, Any]], canonical: Dict[str, Any],
    counts: collections.Counter, apply: bool,
) -> None:
    """Every SKU+offer pair of one product, in one transaction. Raises on failure; the
    caller counts it. Dry runs read (to report what a real run would reuse) and skip writes."""
    for pick in picks:
        vid, variant = pick["variant_id"], pick["variant"]
        price = pick["price"]
        price_from = "variant"
        if price is None:
            price = _positive(canonical.get("merchant_effective_price")) or _positive(
                canonical.get("list_price")
            )
            if price is None:
                counts["skipped_no_variant_price"] += 1
                continue
            price_from = "canonical"
            counts["offers_price_inherited"] += 1

        identity = {
            "merchant_id": row["merchant_id"],
            "platform": row["platform"],
            "product_key": row["product_key"],
            "source_variant_id": vid[:128],
        }
        existing = await database.fetch_one(SELECT_SKU_BY_IDENTITY_SQL, identity)
        stamp = {
            "variant_id": vid,
            "variant_id_provenance": MERCHANT_ISSUED,
            "source_system": SOURCE_SYSTEM,
            "recovered_from": "seed_snapshot_or_product_payload",
        }
        if existing is not None:
            # The promoter (or an earlier run) already owns this identity -- possibly under
            # `<pk>::v::<id>`. Stamp it in place and hang the offer off ITS key; a second
            # row would violate idx_catalog_skus_source_identity_v2.
            sku_key = str(existing["sku_key"])
            counts["skus_existing_reused"] += 1
        else:
            sku_key = derive_variant_sku_key(row["product_key"], vid)
            counts["skus_inserted"] += 1

        labels, attrs = _option_labels(variant)
        title = str(variant.get("title") or "").strip() or row["title"]
        currency = (
            str(variant.get("currency") or "").strip()
            or canonical.get("currency")
            or "USD"
        )
        sku_params = {
            "sku_key": sku_key,
            **identity,
            "source_product_id": row["source_product_id"],
            "source_domain": row.get("source_domain"),
            "sku": str(variant.get("sku") or "").strip() or None,
            "barcode": str(variant.get("barcode") or "").strip() or None,
            "title": title,
            "currency": currency,
            "image_url": str(variant.get("image_url") or "").strip() or None,
            "visible_attributes": json.dumps(attrs),
            "visible_option_labels": json.dumps(labels),
            "ingredient_ids": json.dumps([]),
            "sku_payload": json.dumps(stamp),
            "readiness_tier": OFFER_READINESS_TIER,
        }
        offer_payload = {
            "source_system": SOURCE_SYSTEM,
            "variant_id": vid,
            "variant_id_provenance": MERCHANT_ISSUED,
            "price_from": price_from,
        }
        live = await database.fetch_one(SELECT_LIVE_OFFER_FOR_SKU_SQL, {"sku_key": sku_key})
        if live is not None:
            offer_id = str(live["offer_id"])
            counts["offers_existing_updated"] += 1
        else:
            # derive_offer_id's third argument is a destination_url at ingest time; it is only
            # hash input. A CONSTANT salt keeps the id a pure function of (product, sku) so
            # nothing about the read order can re-key it.
            offer_id = derive_offer_id(row["product_key"], sku_key, SOURCE_SYSTEM)
            counts["offers_inserted"] += 1
        offer_params = {
            "offer_id": offer_id,
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
            if existing is not None:
                await database.execute(
                    STAMP_EXISTING_SKU_SQL, {"sku_key": sku_key, "stamp": json.dumps(stamp)}
                )
            else:
                await database.execute(UPSERT_SKU_SQL, sku_params)
            await database.execute(UPSERT_OFFER_SQL, offer_params)


async def run(*, apply: bool, limit: int, allow_inherited_price: bool) -> Dict[str, Any]:
    counts: collections.Counter = collections.Counter()
    touched = 0
    after = ""
    done = False
    while not done:
        page = await database.fetch_all(SELECT_PRODUCTS_SQL, {"after": after, "page": PAGE_SIZE})
        page = [dict(r) for r in (page or [])]
        if not page:
            break
        after = page[-1]["product_key"]
        for row in page:
            counts["products_scanned"] += 1
            picks = plan_for_product(row, counts, allow_inherited_price=allow_inherited_price)
            if not picks:
                continue
            canonical = await database.fetch_one(
                SELECT_CANONICAL_OFFER_SQL,
                {
                    "pk": row["product_key"],
                    "variant_key_pattern": VARIANT_SKU_KEY_PATTERN,
                    "canonical_sku_key": row["product_key"] + CANONICAL_SKU_SUFFIX,
                },
            )
            if canonical is None:
                had_any = await database.fetch_val(COUNT_OFFERS_SQL, {"pk": row["product_key"]})
                key = "skipped_all_offers_suppressed" if had_any else "skipped_no_canonical_offer"
                counts[key] += len(picks)
                continue
            counts["products_planned"] += 1
            try:
                if apply:
                    async with database.transaction():
                        await _write_product(
                            row=row, picks=picks, canonical=dict(canonical), counts=counts, apply=True
                        )
                else:
                    await _write_product(
                        row=row, picks=picks, canonical=dict(canonical), counts=counts, apply=False
                    )
            except Exception as exc:  # noqa: BLE001 -- one product must not abort 13k
                counts["products_failed"] += 1
                logger.warning(
                    "product %s failed (%s: %s); continuing", row["product_key"], type(exc).__name__, exc
                )
                continue
            touched += 1
            if limit and touched >= limit:
                counts["stopped_at_limit"] = 1
                done = True
                break
        if len(page) < PAGE_SIZE:
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
        help="let a product whose ONLY variant is merchant-issued and unpriced take the "
        "canonical offer's price (stamped price_from=canonical). Multi-variant products "
        "never inherit: siblings differ in exactly the dimension that carries price.",
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
