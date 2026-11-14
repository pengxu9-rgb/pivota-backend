"""Read Commerce Index witnesses for a version-bound external seed mirror."""

from collections import defaultdict
from urllib.parse import parse_qs, urlsplit

from services.catalog_identity import validated_source_gtin
from services.catalog_variant_offer_projection import (
    MIRROR,
    SOURCE,
    as_json,
    listing_identity,
    own_availability,
    variant_own_price,
)
from services.region_pricing import REGION_PRICING_CURRENCY
from services.variant_identity import is_merchant_issued_variant_id


_SHOPIFY_GID_PREFIX = "gid://shopify/ProductVariant/"
_MAX_VARIANTS = 50
_MAX_ROWS = 200

_READBACK_SQL = """
SELECT p.product_key, p.source_product_id, p.canonical_url,
       cm.merchant_id IS NOT NULL AS seller_observed,
       ips.serving_eligible, apv.pdp_lifecycle_stage, apv.variants AS pdp_variants,
       k.sku_key, k.source_variant_id, k.barcode,
       o.offer_id, o.list_price, o.availability,
       o.offer_payload->>'destination_url' AS offer_destination,
       o.offer_payload->>'variant_id' AS offer_variant_id
FROM catalog_products p
LEFT JOIN catalog_merchants cm
    ON cm.merchant_id = p.merchant_id AND cm.status IN ('observed', 'active')
LEFT JOIN index_pipeline_state ips ON ips.content_key = p.content_key
LEFT JOIN agent_pdp_view apv ON apv.content_key = p.content_key
LEFT JOIN catalog_skus k
    ON k.product_key = p.product_key AND k.merchant_id = p.merchant_id
    AND k.source_variant_id = ANY(CAST(:variant_aliases AS text[]))
    AND k.sku_key NOT LIKE '%::canonical'
    AND k.suppressed_at IS NULL AND k.suppression_reason IS NULL
LEFT JOIN catalog_offers o
    ON o.product_key = p.product_key AND o.sku_key = k.sku_key
    AND o.merchant_id = p.merchant_id AND o.market = :market AND o.currency = :currency
    AND o.offer_mode = 'redirect' AND o.source_system = :variant_offer_source
    AND o.offer_payload->>'external_seed_id' = :seed_id
    AND o.offer_payload->>'price_from' = 'variant'
    AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL
WHERE p.source_ref = :seed_id AND p.source_system = :mirror_source
  AND p.merchant_id = :seller_ref
  AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
ORDER BY p.product_key, k.sku_key, o.offer_id
LIMIT :row_limit
"""


def _native_variant_id(value):
    value = str(value or "").strip()
    if not is_merchant_issued_variant_id(value):
        return None
    return value.removeprefix(_SHOPIFY_GID_PREFIX)


def _expected_variants(variants):
    if not variants or len(variants) > _MAX_VARIANTS:
        return None
    expected = {}
    gtins = set()
    for variant in variants:
        if not isinstance(variant, dict):
            return None
        ids = {
            _native_variant_id(variant[key])
            for key in ("variant_id", "source_variant_id", "id", "shopify_variant_id")
            if variant.get(key)
        }
        barcodes = {validated_source_gtin(variant[key]) for key in ("gtin", "barcode") if variant.get(key)}
        if len(ids) != 1 or None in ids or len(barcodes) != 1 or None in barcodes:
            return None
        variant_id = ids.pop()
        gtin = barcodes.pop()
        if variant_id in expected or gtin in gtins:
            return None
        expected[variant_id] = (gtin, variant)
        gtins.add(gtin)
    return expected


def _offer_matches(row, listing, variant_id, price, availability):
    destination = row["offer_destination"]
    if (
        not row["offer_id"] or price is None or row["list_price"] is None
        or float(row["list_price"]) != price or listing_identity(destination) != listing
        or _native_variant_id(row["offer_variant_id"]) != variant_id
        or availability == "unknown" or row["availability"] != availability
    ):
        return False
    destination_variants = parse_qs(urlsplit(destination).query, keep_blank_values=True).get("variant")
    return not destination_variants or destination_variants == [variant_id]


async def read_external_seed_projection(db, *, seed_id, market, seller_ref, status, variants, destination_url=None):
    """Inspect the current mirror only; the caller verifies and binds the seed row version.

    These are database witnesses, not a public API or checkout probe. Missing
    witnesses stay pending/partial while the existing materializers catch up.
    """
    market = str(market or "").strip().upper()
    seed_status = str(status or "").strip().lower()
    result = {
        "status": "not_active",
        "seed_status": seed_status,
        "market": market,
        "seller_ref": seller_ref,
        "product_key": None,
        "seller_observed": False,
        "index_serving_eligible": False,
        "pdp_published": False,
        "variants": [],
    }
    expected = _expected_variants(variants)
    if expected:
        result["variants"] = [
            {"variant_id": variant_id, "gtin": gtin, "sku_key": None, "offer_id": None, "pdp_variant_present": False}
            for variant_id, (gtin, _) in expected.items()
        ]
    if seed_status != "active":
        return result

    currency = REGION_PRICING_CURRENCY.get(market)
    listing = listing_identity(destination_url)
    if not expected or not seller_ref or not currency or not listing:
        result["status"] = "scope_unverified"
        return result

    rows = await db.fetch_all(_READBACK_SQL, {
        "seed_id": seed_id,
        "seller_ref": seller_ref,
        "market": market,
        "currency": currency,
        "mirror_source": MIRROR,
        "variant_offer_source": SOURCE,
        "variant_aliases": [alias for vid in expected for alias in (vid, f"{_SHOPIFY_GID_PREFIX}{vid}")],
        "row_limit": _MAX_ROWS + 1,
    })
    if len(rows) > _MAX_ROWS or len({row["product_key"] for row in rows}) > 1:
        result["status"] = "scope_unverified"
        return result
    if not rows:
        result["status"] = "pending"
        return result

    product = rows[0]
    if listing_identity(product["canonical_url"]) != listing:
        result["status"] = "scope_unverified"
        return result
    pdp_variants = as_json(product["pdp_variants"])
    pdp_ids = set()
    if isinstance(pdp_variants, list):
        pdp_ids = {
            _native_variant_id(variant.get("variant_id"))
            for variant in pdp_variants
            if isinstance(variant, dict) and variant.get("merchant_id") == seller_ref
        }
    result.update(
        status="partial",
        product_key=product["product_key"],
        seller_observed=bool(product["seller_observed"]),
        index_serving_eligible=bool(product["serving_eligible"]),
        pdp_published=product["pdp_lifecycle_stage"] == "published",
    )

    rows_by_variant = defaultdict(list)
    for row in rows:
        if is_merchant_issued_variant_id(
            row["source_variant_id"], product_id=row["source_product_id"], product_key=row["product_key"]
        ):
            rows_by_variant[_native_variant_id(row["source_variant_id"])].append(row)
    for witness in result["variants"]:
        variant_id = witness["variant_id"]
        gtin, variant = expected[variant_id]
        matches = rows_by_variant[variant_id]
        sku_keys = {row["sku_key"] for row in matches}
        if len(sku_keys) != 1 or validated_source_gtin(matches[0]["barcode"]) != gtin:
            continue
        witness["sku_key"] = sku_keys.pop()
        witness["pdp_variant_present"] = result["pdp_published"] and variant_id in pdp_ids
        price = variant_own_price(variant)
        availability = own_availability(variant)
        expected_currency = str(variant.get("price_currency") or variant.get("currency") or "").strip().upper()
        if expected_currency != currency or len(matches) != 1:
            continue
        if _offer_matches(matches[0], listing, variant_id, price, availability):
            witness["offer_id"] = matches[0]["offer_id"]

    if (
        result["seller_observed"] and result["index_serving_eligible"] and result["pdp_published"]
        and all(row["offer_id"] and row["pdp_variant_present"] for row in result["variants"])
    ):
        result["status"] = "projected"
    return result
