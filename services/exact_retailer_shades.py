"""Read reviewed exact retailer shades for allowlisted US listings."""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlsplit

from services.catalog_identity import validated_source_gtin
from services.handover_variant_identity import candidate_from_row, canonical_variant_id
from services.seed_served_url import same_destination
from services.surratt_retailer_pilot import (
    KIOKII_RETAILER_COHORT, PROOF_KEY, RETAILERS, SURRATT_RETAILER_COHORT, ExactRetailerCohort,
)


RETAILER_HOSTS = frozenset(listing.host for listing in RETAILERS.values())
_KIOKII_CANONICAL_VARIANTS = frozenset(
    canonical_id for listing in KIOKII_RETAILER_COHORT.listings.values()
    for canonical_id, _ in listing.variants.values()
)
_ROW_LIMIT = 121

_SELECT = """
    SELECT p.product_key, p.merchant_id, p.content_key, p.canonical_url, m.merchant_name,
           m.metadata_json->'seller_identity'->>'kind' AS seller_kind,
           m.metadata_json->'seller_identity'->>'etld1' AS seller_domain,
           s.sku_key, s.source_product_id, s.source_variant_id, s.sku_payload, s.barcode,
           o.offer_id, o.offer_type, o.is_first_party, o.readiness_tier, o.source_domain,
           o.source_ref, o.offer_payload->>'destination_url' AS family_destination,
           o.offer_payload->CAST(:proof_key AS text) AS shade_proof,
           o.availability, o.currency, o.price_confidence, o.updated_at, o.price_checked_at,
           coalesce(o.merchant_effective_price, o.estimated_best_price, o.list_price) AS price_amount
    FROM catalog_products p
    JOIN product_group_members g ON g.merchant_id = p.merchant_id AND g.platform = p.platform
                               AND g.platform_product_id = p.source_product_id
    JOIN catalog_skus s ON s.product_key = p.product_key AND s.merchant_id = p.merchant_id
                       AND s.source_product_id = p.source_product_id AND s.platform = p.platform
    JOIN catalog_merchants m ON m.merchant_id = p.merchant_id AND m.status = 'observed'
    LEFT JOIN catalog_offers o ON o.product_key = p.product_key AND o.sku_key = s.sku_key
                              AND o.merchant_id = p.merchant_id AND o.market = 'US' AND o.currency = 'USD'
                              AND o.offer_type = 'retailer' AND o.offer_mode = 'redirect'
                              AND o.is_first_party IS FALSE AND o.readiness_tier = 'referral_only'
                              AND o.suppressed_at IS NULL AND o.suppression_reason IS NULL
    WHERE p.content_key = :content_key AND g.product_group_id = :group_id AND p.platform = 'external_seed'
      AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
      AND s.suppressed_at IS NULL AND s.suppression_reason IS NULL AND s.currency = 'USD'
      AND ((p.product_key = :canonical_key AND p.merchant_id = :canonical_merchant
           AND p.pivota_signature_id = :canonical_signature AND p.sync_status = 'live' AND g.is_primary
           AND EXISTS (SELECT 1 FROM index_pipeline_state i WHERE i.content_key = p.content_key AND i.serving_eligible))
           OR (p.product_key = ANY(:retailer_keys) AND NOT g.is_primary))
    LIMIT :limit
"""


@lru_cache(maxsize=2)
def _canonical_signature(merchant_id, product_key):
    from services.catalog_sync_service import make_pivota_signature_id

    return make_pivota_signature_id(
        merchant_id, "external_seed", product_key.rsplit("::", 1)[-1],
    )


def canonical_signature(cohort: ExactRetailerCohort = SURRATT_RETAILER_COHORT):
    return _canonical_signature(cohort.canonical_merchant_id, cohort.canonical_product_key)


def _named_us_request(sku_id, observed_market, merchant_scope):
    return bool(sku_id) and not merchant_scope and str(observed_market or "").upper() == "US"


def _matches_product(product_id, cohort):
    return product_id in {cohort.canonical_product_key, canonical_signature(cohort)}


def applies(product_id, sku_id, observed_market, merchant_scope):
    return (SURRATT_RETAILER_COHORT.enabled() and _named_us_request(sku_id, observed_market, merchant_scope)
            and _matches_product(product_id, SURRATT_RETAILER_COHORT))


def matching_cohort(product_id, sku_id, observed_market, merchant_scope):
    if applies(product_id, sku_id, observed_market, merchant_scope):
        return SURRATT_RETAILER_COHORT
    if (KIOKII_RETAILER_COHORT.enabled() and _named_us_request(sku_id, observed_market, merchant_scope)
            and _native_id(sku_id) in _KIOKII_CANONICAL_VARIANTS
            and _matches_product(product_id, KIOKII_RETAILER_COHORT)):
        return KIOKII_RETAILER_COHORT
    return None


def is_pilot_retailer_seed(row, cohort: ExactRetailerCohort = SURRATT_RETAILER_COHORT):
    if row.get("attached_product_key") in cohort.listings:
        return True
    for field in ("domain", "destination_url", "canonical_url"):
        value = str(row.get(field) or "")
        try:
            address = urlsplit(value if "://" in value else f"https://{value}")
        except ValueError:
            continue
        host = (address.hostname or "").lower().removeprefix("www.")
        if cohort is SURRATT_RETAILER_COHORT and host in RETAILER_HOSTS:
            return True
        if any(host == listing.host and address.path.rstrip("/") == listing.product_path
               for listing in cohort.listings.values()):
            return True
    return False


def _native_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:gid://shopify/ProductVariant/)?[0-9]{6,20}", value):
        return None
    return canonical_variant_id(value)


def _variant(row):
    candidate = candidate_from_row(row)
    return _native_id(candidate.variant_id) if candidate and candidate.stamped else None


def _selected_skus(rows, gtin):
    return {row["sku_key"] for row in rows if validated_source_gtin(row.get("barcode")) == gtin}


def _fresh_observation(value, now):
    if not isinstance(value, str):
        return False
    try:
        observed_at = datetime.fromisoformat(value)
    except ValueError:
        return False
    return observed_at.utcoffset() is not None and timedelta(minutes=-2) <= now - observed_at <= timedelta(hours=4)


def _offer(row, *, listing, canonical_product_key, canonical_id, gtin, now):
    retailer_id = _variant(row)
    if (listing.variants.get(retailer_id) != (canonical_id, gtin)
            or row.get("seller_kind") != "retailer" or row.get("seller_domain") != listing.host
            or row.get("source_domain") != listing.host
            or row.get("availability") != "in_stock"):
        return None
    if not all(same_destination(url, listing.family_url) for url in (
        row.get("canonical_url"), row.get("source_ref"), row.get("family_destination"),
    )):
        return None
    try:
        proof = json.loads(row["shade_proof"]) if isinstance(row["shade_proof"], str) else row["shade_proof"]
    except ValueError:
        return None
    if not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1:
        return None
    landing = proof.get("retailer_landing_url")
    price = Decimal(str(row["price_amount"])) if row.get("price_amount") is not None else None
    price_minor = proof.get("price_minor")
    if (
        proof.get("canonical_product_key") != canonical_product_key
        or proof.get("canonical_native_variant_id") != canonical_id
        or proof.get("retailer_native_variant_id") != retailer_id
        or proof.get("gtin14") != gtin
        or proof.get("availability") != row["availability"]
        or not same_destination(landing, f"{listing.family_url}?variant={retailer_id}")
        or not _fresh_observation(proof.get("observed_at"), now)
        or type(price_minor) is not int or price_minor <= 0
        or price is None or not price.is_finite() or price * 100 != price_minor
    ):
        return None
    return {**row, "destination_url": f"{listing.family_url}?variant={retailer_id}",
            "variant_id": canonical_id, "retailer_variant_id": retailer_id}


async def read_offers(db, sku_id, *, timeout, cohort: ExactRetailerCohort = SURRATT_RETAILER_COHORT):
    """Accept only a unique, currently live paired variant with persisted review evidence."""
    numeric = _native_id(sku_id)
    if not numeric:
        return []
    records = await asyncio.wait_for(db.fetch_all(_SELECT, {
        "content_key": cohort.canonical_content_key, "group_id": cohort.canonical_group_id,
        "canonical_key": cohort.canonical_product_key, "canonical_merchant": cohort.canonical_merchant_id,
        "canonical_signature": canonical_signature(cohort), "retailer_keys": sorted(cohort.listings),
        "limit": _ROW_LIMIT, "proof_key": PROOF_KEY,
    }), timeout=timeout)
    if len(records) >= _ROW_LIMIT:
        return []
    rows = [dict(row) for row in records]
    anchor_rows = [row for row in rows if row["product_key"] == cohort.canonical_product_key]
    anchor = [row for row in anchor_rows if _variant(row) == numeric]
    if len(anchor) != 1:
        return []
    gtin = validated_source_gtin(anchor[0].get("barcode"))
    if not gtin or _selected_skus(anchor_rows, gtin) != {anchor[0]["sku_key"]}:
        return []

    offers = []
    now = datetime.now(timezone.utc)
    for product_key, listing in cohort.listings.items():
        retailer_rows = [row for row in rows if row["product_key"] == product_key and row["merchant_id"] == listing.merchant_id]
        selected = _selected_skus(retailer_rows, gtin)
        if len(selected) != 1:
            continue
        candidates = [row for row in retailer_rows if row["sku_key"] in selected and row["offer_id"]]
        if len(candidates) != 1:
            continue
        offer = _offer(candidates[0], listing=listing, canonical_product_key=cohort.canonical_product_key,
                       canonical_id=numeric, gtin=gtin, now=now)
        if offer:
            offers.append(offer)
    return offers
