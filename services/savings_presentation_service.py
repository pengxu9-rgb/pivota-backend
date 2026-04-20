from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence


SAVINGS_PRESENTATION_CONTRACT_VERSION = "savings.v1"


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _money(value: Any) -> Optional[str]:
    amount = _to_decimal(value)
    if amount is None:
        return None
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _positive_money(value: Any) -> Optional[str]:
    amount = _to_decimal(value)
    if amount is None:
        return None
    return _money(abs(amount))


def _display(offer: Dict[str, Any]) -> Dict[str, str]:
    display = _as_dict(offer.get("display"))
    label = _text(offer.get("label") or display.get("short_copy") or display.get("badge") or "Offer")
    return {
        "badge": _text(display.get("badge")) or label,
        "short_copy": _text(display.get("short_copy")) or label,
        "detail_copy": _text(display.get("detail_copy")) or label,
        "disclaimer": _text(display.get("disclaimer")),
    }


def _add_badge(
    badges: List[Dict[str, Any]],
    *,
    label: str,
    group: str,
    confidence: str,
) -> None:
    clean = _text(label)
    if not clean:
        return
    if any(item.get("label") == clean and item.get("group") == group for item in badges):
        return
    badges.append({"label": clean, "group": group, "confidence": confidence})


def _discount_source_of_truth(discount_evidence: Dict[str, Any]) -> str:
    source = _text(discount_evidence.get("source"))
    if "shopify" in source:
        return "shopify_storefront_quote"
    return source or "store_platform_quote"


def _applied_store_discounts(
    promotion_lines: Sequence[Any],
    *,
    source_of_truth: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in promotion_lines or []:
        if not isinstance(line, dict):
            if hasattr(line, "model_dump"):
                line = line.model_dump()
            else:
                continue
        amount = _to_decimal(line.get("amount"))
        if amount is None or amount == 0:
            continue
        rows.append(
            {
                "id": _text(line.get("id")),
                "source": _text(line.get("source") or "store"),
                "source_ref": line.get("source_ref"),
                "discount_class": _text(line.get("discount_class") or "order"),
                "method": _text(line.get("method") or "automatic"),
                "label": _text(line.get("label") or line.get("code") or "Store discount"),
                "code": line.get("code"),
                "amount": _money(amount),
                "savings_amount": _positive_money(amount),
                "allocations": _as_list(line.get("allocations")),
                "application_policy": {
                    "affects_checkout_total": True,
                    "source_of_truth": source_of_truth,
                },
            }
        )
    return rows


def _fallback_applied_from_discount_evidence(
    discount_evidence: Dict[str, Any],
    *,
    source_of_truth: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for app in _as_list(discount_evidence.get("applications")):
        if not isinstance(app, dict):
            continue
        amount = (
            app.get("amount")
            or app.get("discounted_amount")
            or _as_dict(app.get("discountedAmount")).get("amount")
        )
        money = _money(amount)
        if money is None or _to_decimal(money) == 0:
            continue
        rows.append(
            {
                "id": _text(app.get("id") or app.get("code") or app.get("title")),
                "source": _text(app.get("source") or "store"),
                "source_ref": app.get("source_ref"),
                "discount_class": _text(app.get("discount_class") or app.get("target_type") or "order"),
                "method": _text(app.get("method") or "automatic"),
                "label": _text(app.get("label") or app.get("title") or app.get("code") or "Store discount"),
                "code": app.get("code"),
                "amount": f"-{money}" if not str(money).startswith("-") else money,
                "savings_amount": money.replace("-", ""),
                "allocations": [],
                "application_policy": {
                    "affects_checkout_total": True,
                    "source_of_truth": source_of_truth,
                },
            }
        )
    return rows


def _store_offer_rows(store_discount_evidence: Dict[str, Any]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    available: List[Dict[str, Any]] = []
    unlocks: List[Dict[str, Any]] = []
    for offer in _as_list(store_discount_evidence.get("offers")):
        if not isinstance(offer, dict):
            continue
        display = _display(offer)
        status = _text(offer.get("status") or "unverified")
        minimum = _as_dict(offer.get("minimum_requirement"))
        discount_type = _text(offer.get("discount_type") or "unknown")
        row = {
            "id": _text(offer.get("store_discount_id")),
            "source": _text(offer.get("source") or "store_discount_metadata"),
            "platform": offer.get("platform"),
            "source_system": offer.get("source_system"),
            "shopify_discount_node_id": offer.get("shopify_discount_node_id"),
            "discount_method": offer.get("discount_method"),
            "discount_type": discount_type,
            "discount_classes": _as_list(offer.get("discount_classes")),
            "status": status,
            "codes": _as_list(offer.get("codes")),
            "combines_with": _as_dict(offer.get("combines_with")),
            "minimum_requirement": minimum,
            "display": display,
            "application_policy": {
                **_as_dict(offer.get("application_policy")),
                "affects_checkout_total_before_quote": False,
                "requires_storefront_allocation_for_applied_amount": True,
            },
        }
        has_remaining = bool(minimum.get("remaining_quantity") or minimum.get("remaining_subtotal"))
        if status == "unlockable" or has_remaining or discount_type in {"bxgy", "bundle"}:
            unlocks.append(row)
        else:
            available.append(row)
    return available, unlocks


def _payment_benefit_rows(payment_offer_evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for offer in _as_list(payment_offer_evidence.get("offers")):
        if not isinstance(offer, dict):
            continue
        display = _display(offer)
        eligibility = _as_dict(offer.get("eligibility"))
        rows.append(
            {
                "payment_offer_id": offer.get("payment_offer_id"),
                "label": _text(offer.get("label") or display["short_copy"]),
                "source_system": offer.get("source_system"),
                "funding_source": offer.get("funding_source"),
                "benefit_kind": offer.get("benefit_kind"),
                "benefit_value": offer.get("benefit_value"),
                "benefit_currency": offer.get("benefit_currency"),
                "requirements": _as_dict(offer.get("requirements")),
                "eligibility": {
                    "status": eligibility.get("status") or "unverified",
                    "confidence": eligibility.get("confidence"),
                    "reason_codes": _as_list(eligibility.get("reason_codes")),
                },
                "display": display,
                "estimated_savings": offer.get("estimated_savings"),
                "estimated_total_after_payment_offer": offer.get("estimated_total_after_payment_offer"),
                "application_policy": {
                    **_as_dict(offer.get("application_policy")),
                    "affects_shopify_discount": False,
                    "affects_psp_amount_v1": False,
                    "finalization_stage": "psp_evidence",
                },
            }
        )
    return rows


def _checkout_rows(
    *,
    pricing: Dict[str, Any],
    currency: Optional[str],
    payment_pricing: Dict[str, Any],
    store_discount_source_of_truth: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    subtotal = _money(pricing.get("subtotal"))
    discount_total = _to_decimal(pricing.get("discount_total"))
    shipping_fee = _money(pricing.get("shipping_fee"))
    tax = _money(pricing.get("tax"))
    total = _money(pricing.get("total"))
    if subtotal is not None:
        rows.append({"kind": "subtotal", "label": "Subtotal", "amount": subtotal, "currency": currency, "affects_total": True})
    if discount_total is not None and discount_total > 0:
        rows.append(
            {
                "kind": "store_discounts",
                "label": "Store discounts",
                "amount": f"-{_money(discount_total)}",
                "currency": currency,
                "affects_total": True,
                "source_of_truth": store_discount_source_of_truth,
            }
        )
    if shipping_fee is not None:
        rows.append({"kind": "shipping", "label": "Shipping", "amount": shipping_fee, "currency": currency, "affects_total": True})
    if tax is not None:
        rows.append({"kind": "tax", "label": "Tax", "amount": tax, "currency": currency, "affects_total": True})
    if total is not None:
        rows.append(
            {
                "kind": "total_charged_now",
                "label": "Total charged now",
                "amount": total,
                "currency": currency,
                "affects_total": True,
                "source_of_truth": "pivota_quote_psp_charge",
            }
        )
    estimated = _money(payment_pricing.get("estimated_payment_benefit"))
    if estimated is not None and _to_decimal(estimated) and _to_decimal(estimated) > 0:
        rows.append(
            {
                "kind": "estimated_payment_benefit",
                "label": "Estimated payment benefit",
                "amount": f"-{estimated}",
                "currency": payment_pricing.get("currency") or currency,
                "affects_total": False,
                "display_only": True,
                "disclaimer": "Final eligibility depends on selected payment method and is not deducted from today's charge.",
            }
        )
    return rows


def build_savings_presentation(
    *,
    pricing: Optional[Dict[str, Any]] = None,
    currency: Optional[str] = None,
    promotion_lines: Optional[Sequence[Any]] = None,
    discount_evidence: Optional[Dict[str, Any]] = None,
    store_discount_evidence: Optional[Dict[str, Any]] = None,
    payment_offer_evidence: Optional[Dict[str, Any]] = None,
    payment_pricing: Optional[Dict[str, Any]] = None,
    max_summary_badges: int = 6,
) -> Dict[str, Any]:
    pricing = _as_dict(pricing)
    discount_evidence = _as_dict(discount_evidence)
    store_discount_evidence = _as_dict(store_discount_evidence)
    payment_offer_evidence = _as_dict(payment_offer_evidence)
    payment_pricing = _as_dict(payment_pricing)

    store_discount_source_of_truth = _discount_source_of_truth(discount_evidence)
    applied = _applied_store_discounts(
        promotion_lines or [],
        source_of_truth=store_discount_source_of_truth,
    )
    if not applied:
        applied = _fallback_applied_from_discount_evidence(
            discount_evidence,
            source_of_truth=store_discount_source_of_truth,
        )
    available_store_offers, cart_unlocks = _store_offer_rows(store_discount_evidence)
    payment_benefits = _payment_benefit_rows(payment_offer_evidence)

    badges: List[Dict[str, Any]] = []
    for row in applied:
        _add_badge(badges, label=row.get("label") or "Store discount applied", group="applied_store_discounts", confidence="authoritative")
    for row in cart_unlocks:
        _add_badge(badges, label=_as_dict(row.get("display")).get("badge") or row.get("discount_type"), group="cart_unlocks", confidence=row.get("status") or "unverified")
    for row in available_store_offers:
        _add_badge(badges, label=_as_dict(row.get("display")).get("badge") or row.get("discount_type"), group="available_store_offers", confidence=row.get("status") or "unverified")
    for row in payment_benefits:
        _add_badge(badges, label=_as_dict(row.get("display")).get("badge") or row.get("label"), group="payment_benefits", confidence=_as_dict(row.get("eligibility")).get("status") or "unverified")

    return {
        "contract_version": SAVINGS_PRESENTATION_CONTRACT_VERSION,
        "pricing_confidence": {
            "store_discounts": discount_evidence.get("pricing_confidence")
            or store_discount_evidence.get("pricing_confidence")
            or "unverified",
            "store_discount_metadata": store_discount_evidence.get("pricing_confidence") or "not_applicable",
            "payment_benefits": payment_offer_evidence.get("pricing_confidence") or "not_applicable",
        },
        "appliedStoreDiscounts": applied,
        "availableStoreOffers": available_store_offers,
        "cartUnlocks": cart_unlocks,
        "paymentBenefits": payment_benefits,
        "summaryBadges": badges[: max(0, int(max_summary_badges))],
        "checkoutRows": _checkout_rows(
            pricing=pricing,
            currency=currency or pricing.get("currency") or payment_pricing.get("currency"),
            payment_pricing=payment_pricing,
            store_discount_source_of_truth=store_discount_source_of_truth,
        ),
        "agentFacing": {
            "externalAgentsCanRender": True,
            "priceAuthority": "pivota_quote_psp_charge",
            "payButtonUses": "pricing.total",
            "paymentBenefitsMutateCharge": False,
            "unverifiedStoreOffersMutatePrice": False,
        },
        "applicationPolicy": {
            "storePlatformQuoteIsStoreDiscountAuthority": True,
            "appliedStoreDiscountSourceOfTruth": store_discount_source_of_truth,
            "paymentOffersAreDisplayOnlyV1": True,
            "doNotEncodePaymentOffersAsStoreDiscounts": True,
            "doNotEncodePaymentOffersAsShopifyDiscounts": True,
        },
    }


def attach_savings_presentation_to_product_cards(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for product in products or []:
        if not isinstance(product, dict):
            continue
        price = product.get("price")
        currency = product.get("currency")
        product["savings_presentation"] = build_savings_presentation(
            pricing={"subtotal": price, "discount_total": "0", "shipping_fee": "0", "tax": "0", "total": price},
            currency=currency,
            store_discount_evidence=product.get("store_discount_evidence") or {},
            payment_offer_evidence=product.get("payment_offer_evidence") or {},
            payment_pricing=product.get("payment_pricing") or {},
            max_summary_badges=4,
        )
    return products


def attach_savings_presentation_to_product_detail(product: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(product, dict):
        return product
    variants = product.get("variants") if isinstance(product.get("variants"), list) else []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        price = variant.get("price") or product.get("price")
        currency = variant.get("currency") or product.get("currency")
        variant["savings_presentation"] = build_savings_presentation(
            pricing={"subtotal": price, "discount_total": "0", "shipping_fee": "0", "tax": "0", "total": price},
            currency=currency,
            store_discount_evidence=variant.get("store_discount_evidence") or {},
            payment_offer_evidence=variant.get("payment_offer_evidence") or {},
            payment_pricing=variant.get("payment_pricing") or {},
            max_summary_badges=4,
        )
    first_price = None
    if variants and isinstance(variants[0], dict):
        first_price = variants[0].get("price")
    first_price = first_price or product.get("price")
    product["savings_presentation"] = build_savings_presentation(
        pricing={"subtotal": first_price, "discount_total": "0", "shipping_fee": "0", "tax": "0", "total": first_price},
        currency=product.get("currency"),
        store_discount_evidence=product.get("store_discount_evidence") or {},
        payment_offer_evidence=product.get("payment_offer_evidence") or {},
        payment_pricing=product.get("payment_pricing") or {},
        max_summary_badges=6,
    )
    return product
