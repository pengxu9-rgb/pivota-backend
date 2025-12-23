from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from mvp.schemas import (
    DeliveryOption,
    Geo,
    OfferConstraints,
    OfferObject,
    OfferPricing,
    Orderability,
    PreFlightCheck,
    PreFlightChecks,
    PreFlightResult,
    ProductRef,
    QuoteRef,
)
from mvp.playbooks import fallback_config_for_geo


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_offer_id(*, quote_id: str, variant_id: str) -> str:
    raw = f"{quote_id}:{variant_id}".encode("utf-8")
    return "offer_" + hashlib.sha256(raw).hexdigest()[:24]


def _as_geo(addr: Optional[Dict[str, Any]]) -> Geo:
    if not isinstance(addr, dict):
        return Geo(country="US")
    country = (addr.get("country") or "US").strip().upper()[:2] or "US"
    return Geo(
        country=country,
        postal_code=addr.get("postal_code") or addr.get("zip"),
        city=addr.get("city"),
        state=addr.get("state") or addr.get("province"),
    )


def build_offers_from_quote(
    *,
    merchant_id: str,
    quote_id: str,
    expires_at: datetime,
    engine: str,
    engine_ref: str,
    currency: str,
    pricing: Dict[str, Any],
    line_items: List[Dict[str, Any]],
    delivery_options: Optional[List[Dict[str, Any]]],
    shipping_address: Optional[Dict[str, Any]],
) -> List[OfferObject]:
    geo = _as_geo(shipping_address)

    offer_pricing = OfferPricing(
        currency=str(currency),
        subtotal=float(pricing.get("subtotal") or 0),
        discount_total=float(pricing.get("discount_total") or 0),
        shipping_fee=float(pricing.get("shipping_fee") or 0),
        tax=float(pricing.get("tax") or 0),
        total=float(pricing.get("total") or 0),
    )

    quote_ref = QuoteRef(
        quote_id=quote_id,
        expires_at=expires_at,
        engine=engine,
        engine_ref=engine_ref,
    )

    out: List[OfferObject] = []
    for li in line_items or []:
        variant_id = str(li.get("variant_id") or "")
        if not variant_id:
            continue
        offer_id = _stable_offer_id(quote_id=quote_id, variant_id=variant_id)
        product_ref = ProductRef(
            platform="shopify" if "shopify" in (engine or "") else "unknown",
            platform_product_id=str(li.get("product_id") or ""),
            variant_id=variant_id,
            sku_id=variant_id,
        )

        options: List[DeliveryOption] = []
        if isinstance(delivery_options, list):
            for opt in delivery_options[:20]:
                if not isinstance(opt, dict):
                    continue
                options.append(
                    DeliveryOption(
                        handle=str(opt.get("id") or opt.get("handle") or ""),
                        title=opt.get("title") or opt.get("name"),
                        price=float(opt.get("price") or opt.get("amount") or 0)
                        if (opt.get("price") or opt.get("amount") or None) is not None
                        else None,
                        currency=opt.get("currency") or currency,
                    )
                )

        out.append(
            OfferObject(
                offer_id=offer_id,
                merchant_id=merchant_id,
                product_ref=product_ref,
                geo=geo,
                pricing=offer_pricing,
                delivery_options=options,
                orderability=Orderability(status="unknown", reasons=[]),
                quote_ref=quote_ref,
                constraints=OfferConstraints(requires_hil=False),
            )
        )

    # Fallback: if quote line_items missing, still emit a single "unknown item" offer.
    if not out:
        offer_id = _stable_offer_id(quote_id=quote_id, variant_id="unknown")
        out.append(
            OfferObject(
                offer_id=offer_id,
                merchant_id=merchant_id,
                product_ref=ProductRef(
                    platform="unknown",
                    platform_product_id="",
                    variant_id="",
                    sku_id=None,
                ),
                geo=geo,
                pricing=offer_pricing,
                delivery_options=[],
                orderability=Orderability(status="unknown", reasons=["missing_line_items"]),
                quote_ref=quote_ref,
                constraints=OfferConstraints(requires_hil=False),
            )
        )

    return out


def preflight_offer(
    *,
    offer: OfferObject,
    now: Optional[datetime] = None,
    policy_hashes_available: bool = True,
    hil_required: bool = False,
    hil_reason: Optional[str] = None,
) -> PreFlightResult:
    ts = now or _utc_now()
    reasons: List[str] = []
    required_actions: List[Dict[str, Any]] = []
    fallback_options: List[Dict[str, Any]] = []
    warnings: List[str] = []

    near_expiry_seconds = 60
    try:
        cfg = fallback_config_for_geo(country=getattr(offer.geo, "country", None))
        if cfg.get("quote_near_expiry_seconds") is not None:
            near_expiry_seconds = int(cfg["quote_near_expiry_seconds"])
    except Exception:
        near_expiry_seconds = 60

    pricing_check = PreFlightCheck(status="pass")
    delivery_check = PreFlightCheck(status="pass")
    inventory_check = PreFlightCheck(status="warn", reason_codes=["inventory_unknown"])
    policy_check = PreFlightCheck(status="pass")

    # Geo completeness
    if not offer.geo.country:
        reasons.append("missing_geo_country")
        required_actions.append({"type": "ASK_USER_FOR_GEO", "fields": ["country"]})

    # Quote expiry gates "orderability certainty"
    if offer.quote_ref.expires_at <= ts:
        reasons.append("quote_expired")
        pricing_check = PreFlightCheck(status="fail", reason_codes=["quote_expired"])
        required_actions.append({"type": "REQUOTE", "quote_id": offer.quote_ref.quote_id})
        fallback_options.append({"type": "RETRY_QUOTE", "message": "Quote expired; request a new quote."})
    else:
        seconds_left = int((offer.quote_ref.expires_at - ts).total_seconds())
        if seconds_left < near_expiry_seconds:
            warnings.append("quote_near_expiry")
            pricing_check = PreFlightCheck(status="warn", reason_codes=["quote_near_expiry"])
            fallback_options.append({"type": "REQUOTE_SOON", "seconds_left": seconds_left})

    # Policy disclosure
    if not policy_hashes_available:
        warnings.append("missing_policy_snapshot")
        policy_check = PreFlightCheck(status="warn", reason_codes=["missing_policy_snapshot"])
        fallback_options.append(
            {
                "type": "FETCH_POLICIES",
                "message": "Policy hashes missing; fetch latest policy snapshots for evidence completeness.",
            }
        )

    # Optional HIL requirement (governance)
    if hil_required:
        required_actions.append(
            {"type": "HIL_REQUIRED", "message": "Step-up approval required", "reason": hil_reason}
        )

    checks = PreFlightChecks(
        pricing=pricing_check,
        inventory=inventory_check,
        delivery=delivery_check,
        policy_disclosure=policy_check,
    )

    # Aggregate status
    status: str
    if "fail" in {checks.pricing.status, checks.inventory.status, checks.delivery.status, checks.policy_disclosure.status}:
        status = "fail"
    elif "warn" in {checks.pricing.status, checks.inventory.status, checks.delivery.status, checks.policy_disclosure.status}:
        status = "warn"
    else:
        status = "pass"

    return PreFlightResult(
        merchant_id=offer.merchant_id,
        product_ref=offer.product_ref,
        geo=offer.geo,
        status=status,  # type: ignore[arg-type]
        reasons=reasons,
        checks=checks,
        required_actions=required_actions,
        fallback_options=fallback_options,
        warnings=warnings,
    )


def preflight_offers(
    *,
    offers: List[OfferObject],
    policy_hashes_available: bool,
    hil_required: bool = False,
    hil_reason: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[PreFlightResult]:
    return [
        preflight_offer(
            offer=o,
            now=now,
            policy_hashes_available=policy_hashes_available,
            hil_required=hil_required,
            hil_reason=hil_reason,
        )
        for o in (offers or [])
    ]
