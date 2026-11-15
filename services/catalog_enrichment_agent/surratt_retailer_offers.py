"""Scope a verified retailer plan to exact, seller-owned Surratt referral offers."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.catalog_identity import validated_source_gtin
from services.surratt_retailer_pilot import CANONICAL_GROUP_ID, CANONICAL_PRODUCT_KEY, PROOF_KEY, RETAILERS
from services.variant_identity import MERCHANT_ISSUED, variant_id_provenance


def scope_reviewed_plan(plan: dict) -> dict:
    """Retain catalog lineage and the refresh anchor without creating a routed seed."""
    pdps = plan.get("pdps") or []
    if len(pdps) != 1 or pdps[0].get("product_key") not in RETAILERS:
        raise ValueError("expected one reviewed retailer listing")
    product = pdps[0]
    key = product["product_key"]
    retailer = RETAILERS[key]
    if (
        product["merchant_id"] != retailer.merchant_id
        or product["canonical_url"] != retailer.family_url
        or product["source_domain"] != retailer.host
    ):
        raise ValueError("reviewed retailer listing identity changed")

    skus = {}
    for sku in plan.get("skus") or []:
        if sku["product_key"] != key or sku["merchant_id"] != retailer.merchant_id:
            raise ValueError("retailer SKU owner changed")
        variant = sku["source_variant_id"]
        if sku["sku_key"] == f"{key}::canonical":
            continue
        if variant not in retailer.variants or variant in skus:
            raise ValueError("unexpected or repeated retailer shade")
        if (
            variant_id_provenance(variant, product_id=product["source_product_id"], product_key=key)
            != MERCHANT_ISSUED
            or validated_source_gtin(sku.get("barcode")) != retailer.variants[variant][1]
        ):
            raise ValueError("retailer shade UPC or issuer changed")
        skus[variant] = sku["sku_key"]
    if set(skus) != retailer.variants.keys():
        raise ValueError("reviewed retailer shade missing")

    offers = []
    seen = set()
    by_sku = {sku_key: variant_id for variant_id, sku_key in skus.items()}
    for offer in plan.get("offers") or []:
        sku_key = offer["sku_key"]
        if sku_key == f"{key}::canonical":
            continue
        if sku_key not in by_sku or sku_key in seen:
            raise ValueError("unexpected or duplicate native offer")
        seen.add(sku_key)
        if (
            (offer["product_key"], offer.get("source_domain"), offer["source_ref"])
            != (key, retailer.host, retailer.family_url)
            or (offer["market"], offer["currency"], offer["offer_mode"], offer["readiness_tier"])
            != ("US", "USD", "redirect", "referral_only")
            or offer.get("offer_type") != "retailer"
            or offer.get("is_first_party") is not False
        ):
            raise ValueError("reviewed US referral offer changed")
        payload = json.loads(offer["offer_payload"])
        if payload.get("destination_url") != retailer.family_url:
            raise ValueError("retailer refresh anchor changed")
        observed_at = datetime.fromisoformat(payload["validated_at"])
        if observed_at.tzinfo is None or not (
            -timedelta(minutes=2) <= datetime.now(timezone.utc) - observed_at <= timedelta(hours=4)
        ):
            raise ValueError("retailer capture is outside the review freshness window")
        price_minor = Decimal(str(offer["list_price"])) * 100
        if (
            offer["availability"] not in {"in_stock", "out_of_stock"}
            or not price_minor.is_finite() or price_minor <= 0 or price_minor != price_minor.to_integral_value()
        ):
            raise ValueError("retailer native price or stock was not observed")
        variant = by_sku[sku_key]
        canonical_variant, gtin = retailer.variants[variant]
        payload[PROOF_KEY] = {
            "version": 1,
            "canonical_product_key": CANONICAL_PRODUCT_KEY,
            "canonical_native_variant_id": canonical_variant,
            "retailer_native_variant_id": variant,
            "gtin14": gtin,
            "retailer_landing_url": f"{retailer.family_url}?variant={variant}",
            "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
            "availability": offer["availability"],
            "price_minor": int(price_minor),
        }
        offers.append({**offer, "merchant_id": retailer.merchant_id, "offer_payload": json.dumps(payload)})
    if seen != by_sku.keys():
        raise ValueError("exact retailer offer missing")
    # The existing observed row is the FK target. Do not upsert aliases or
    # introduce an active seed that the old purchase routes could select.
    return {
        **plan, "skus": [sku for sku in plan["skus"] if sku["sku_key"] in seen],
        "offers": offers, "merchants": [], "seeds": [], "incis": [], "reviewed_secondary_group": CANONICAL_GROUP_ID,
    }
