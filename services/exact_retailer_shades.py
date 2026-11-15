"""Read reviewed exact retailer shades for the Surratt US pilot."""

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
    CANONICAL_CONTENT_KEY, CANONICAL_GROUP_ID, CANONICAL_MERCHANT_ID, CANONICAL_PRODUCT_KEY,
    PROOF_KEY, RETAILERS, pilot_enabled,
)


RETAILER_HOSTS = frozenset(listing.host for listing in RETAILERS.values())
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


@lru_cache(maxsize=1)
def canonical_signature():
    from services.catalog_sync_service import make_pivota_signature_id

    return make_pivota_signature_id(
        CANONICAL_MERCHANT_ID, "external_seed", CANONICAL_PRODUCT_KEY.rsplit("::", 1)[-1],
    )


def applies(product_id, sku_id, observed_market, merchant_scope):
    return (
        pilot_enabled()
        and not merchant_scope
        and str(observed_market or "").upper() == "US"
        and bool(sku_id)
        and product_id in {CANONICAL_PRODUCT_KEY, canonical_signature()}
    )


def is_pilot_retailer_seed(row):
    if row.get("attached_product_key") in RETAILERS:
        return True
    for field in ("domain", "destination_url", "canonical_url"):
        value = str(row.get(field) or "")
        try:
            host = urlsplit(value if "://" in value else f"https://{value}").hostname
        except ValueError:
            continue
        if (host or "").lower().removeprefix("www.") in RETAILER_HOSTS:
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


def _offer(row, *, listing, canonical_id, gtin, now):
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
        proof.get("canonical_product_key") != CANONICAL_PRODUCT_KEY
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


async def read_offers(db, sku_id, *, timeout):
    """Accept only a unique, currently live paired variant with persisted review evidence."""
    numeric = _native_id(sku_id)
    if not numeric:
        return []
    records = await asyncio.wait_for(db.fetch_all(_SELECT, {
        "content_key": CANONICAL_CONTENT_KEY, "group_id": CANONICAL_GROUP_ID, "canonical_key": CANONICAL_PRODUCT_KEY,
        "canonical_merchant": CANONICAL_MERCHANT_ID, "canonical_signature": canonical_signature(),
        "retailer_keys": sorted(RETAILERS), "limit": _ROW_LIMIT, "proof_key": PROOF_KEY,
    }), timeout=timeout)
    if len(records) >= _ROW_LIMIT:
        return []
    rows = [dict(row) for row in records]
    anchor_rows = [row for row in rows if row["product_key"] == CANONICAL_PRODUCT_KEY]
    anchor = [row for row in anchor_rows if _variant(row) == numeric]
    if len(anchor) != 1:
        return []
    gtin = validated_source_gtin(anchor[0].get("barcode"))
    if not gtin or _selected_skus(anchor_rows, gtin) != {anchor[0]["sku_key"]}:
        return []

    offers = []
    now = datetime.now(timezone.utc)
    for product_key, listing in RETAILERS.items():
        retailer_rows = [row for row in rows if row["product_key"] == product_key and row["merchant_id"] == listing.merchant_id]
        selected = _selected_skus(retailer_rows, gtin)
        if len(selected) != 1:
            continue
        candidates = [row for row in retailer_rows if row["sku_key"] in selected and row["offer_id"]]
        if len(candidates) != 1:
            continue
        offer = _offer(candidates[0], listing=listing, canonical_id=numeric, gtin=gtin, now=now)
        if offer:
            offers.append(offer)
    return offers
