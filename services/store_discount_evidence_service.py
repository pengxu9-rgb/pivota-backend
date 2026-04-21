from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence

from services.savings_presentation_service import (
    attach_savings_presentation_to_product_cards,
    attach_savings_presentation_to_product_detail,
)
from services.promotions_service import PromotionOut, PromotionStatus, list_promotions


DISPLAY_ONLY_STORE_DISCOUNT_POLICY = {
    "final_authority": "store_platform_quote",
    "affects_checkout_total_before_quote": False,
    "requires_storefront_allocation_for_applied_amount": True,
}

SUPPORTED_STORE_DISCOUNT_METADATA_SOURCES = {
    "shopify_discount_node": "shopify",
}


@dataclass(frozen=True)
class StoreDiscountTarget:
    target_id: str
    merchant_id: str
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    quantity: Optional[int] = None
    subtotal: Optional[Decimal] = None
    currency: Optional[str] = None
    market: Optional[str] = None


def empty_store_discount_evidence(reason: str = "no_store_discounts") -> Dict[str, Any]:
    return {
        "pricing_confidence": "not_applicable",
        "offers": [],
        "resolver_scope": "store_discount_metadata",
        "supported_platforms": sorted(set(SUPPORTED_STORE_DISCOUNT_METADATA_SOURCES.values())),
        "decisions": [{"type": "store_discount_resolution", "reason": reason}],
    }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _first_nested_variant_id(product: Dict[str, Any]) -> str:
    for key in ("variants", "options"):
        values = product.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            variant_id = _text(
                item.get("variant_id")
                or item.get("variantId")
                or item.get("id")
                or item.get("sku_id")
                or item.get("skuId")
            )
            if variant_id:
                return variant_id
    offers = product.get("offers")
    if isinstance(offers, list):
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            variant_id = _text(
                offer.get("variant_id")
                or offer.get("variantId")
                or offer.get("sku_id")
                or offer.get("skuId")
            )
            if variant_id:
                return variant_id
    return ""


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _list_text(value: Any) -> List[str]:
    return [_text(item) for item in _as_list(value) if _text(item)]


def _connection_nodes(connection: Any) -> List[Dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes")
    if isinstance(nodes, list):
        return [node for node in nodes if isinstance(node, dict)]
    edges = connection.get("edges")
    out: List[Dict[str, Any]] = []
    if isinstance(edges, list):
        for edge in edges:
            node = (edge or {}).get("node") if isinstance(edge, dict) else None
            if isinstance(node, dict):
                out.append(node)
    return out


def _canonical_shopify_id(value: Any) -> str:
    text = _text(value)
    if text.startswith("gid://shopify/"):
        return text.rsplit("/", 1)[-1]
    return text


def _canonicalize_ids(values: Sequence[Any]) -> List[str]:
    out: List[str] = []
    for value in values or []:
        canonical = _canonical_shopify_id(value)
        if canonical and canonical not in out:
            out.append(canonical)
    return out


def _extract_scope_ids(items: Dict[str, Any]) -> tuple[List[str], List[str], List[str]]:
    if not isinstance(items, dict):
        return [], [], []
    product_ids = _list_text(
        items.get("productIds")
        or items.get("product_ids")
        or items.get("products")
        or items.get("productGids")
    )
    variant_ids = _list_text(
        items.get("variantIds")
        or items.get("variant_ids")
        or items.get("variants")
        or items.get("variantGids")
    )
    collection_ids = _list_text(
        items.get("collectionIds")
        or items.get("collection_ids")
        or items.get("collections")
        or items.get("collectionGids")
    )

    if isinstance(items.get("products"), dict):
        product_ids.extend(node.get("id") for node in _connection_nodes(items.get("products")))
    if isinstance(items.get("productVariants"), dict):
        variant_ids.extend(node.get("id") for node in _connection_nodes(items.get("productVariants")))
    if isinstance(items.get("collections"), dict):
        collection_ids.extend(node.get("id") for node in _connection_nodes(items.get("collections")))

    return (
        _canonicalize_ids(product_ids),
        _canonicalize_ids(variant_ids),
        _canonicalize_ids(collection_ids),
    )


def _shopify_scope_item_candidates(scope: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    scope_items = scope.get("shopifyItems") if isinstance(scope.get("shopifyItems"), dict) else None
    if isinstance(scope_items, dict):
        candidates.append(scope_items)

    customer_gets_items = cfg.get("customerGets", {}).get("items") if isinstance(cfg.get("customerGets"), dict) else None
    if isinstance(customer_gets_items, dict):
        candidates.append(customer_gets_items)

    if _lower(cfg.get("discountType")) == "bxgy":
        customer_buys_items = cfg.get("customerBuys", {}).get("items") if isinstance(cfg.get("customerBuys"), dict) else None
        if isinstance(customer_buys_items, dict):
            candidates.append(customer_buys_items)

    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        key = repr(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01")), "f")


def _get_nested(mapping: Dict[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _has_non_typename_payload(value: Any) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "__typename":
                continue
            if nested in (None, "", [], {}):
                continue
            if isinstance(nested, dict) and not _has_non_typename_payload(nested):
                continue
            return True
        return False
    if isinstance(value, list):
        return any(_has_non_typename_payload(item) for item in value)
    return value not in (None, "")


def _bxgy_metadata_is_actionable(cfg: Dict[str, Any]) -> bool:
    if not isinstance(cfg, dict):
        return False
    if _has_non_typename_payload(cfg.get("minimumRequirement")):
        return True
    if _has_non_typename_payload(cfg.get("customerBuys")):
        return True
    if _has_non_typename_payload(cfg.get("customerGets")):
        return True
    return False


def _scope_status(promo: PromotionOut, target: StoreDiscountTarget) -> tuple[bool, str, str]:
    scope = promo.scope if isinstance(promo.scope, dict) else {}
    cfg = promo.config if isinstance(promo.config, dict) else {}
    target_product_id = _canonical_shopify_id(target.product_id)
    target_variant_id = _canonical_shopify_id(target.variant_id)
    if scope.get("global") is True:
        if _lower(cfg.get("discountType")) == "bxgy" and not _bxgy_metadata_is_actionable(cfg):
            return True, "unverified", "bxgy_scope_unverified"
        return True, "available", "global_scope"

    candidates = _shopify_scope_item_candidates(scope, cfg)
    saw_typed_candidate = False
    saw_explicit_scope = False
    unverified_reason = ""
    for items in candidates:
        typename = _text(items.get("__typename"))
        if typename == "AllDiscountItems":
            return True, "available", "all_discount_items"
        explicit_product_ids, explicit_variant_ids, _ = _extract_scope_ids(items)
        if explicit_product_ids or explicit_variant_ids:
            saw_explicit_scope = True
            if target_product_id and target_product_id in explicit_product_ids:
                return True, "available", "product_scope_match"
            if target_variant_id and target_variant_id in explicit_variant_ids:
                return True, "available", "variant_scope_match"
            continue
        if typename:
            saw_typed_candidate = True
            if not unverified_reason:
                unverified_reason = f"shopify_scope_{typename}"
    if saw_explicit_scope:
        return False, "not_applicable", "target_out_of_scope"
    if saw_typed_candidate:
        return True, "unverified", unverified_reason

    # Older syncs may place scope metadata in config. Treat unknown Shopify scope as
    # displayable but unverified so UI and agents do not over-promise final pricing.
    return True, "unverified", "scope_missing"


def _minimum_requirement_status(promo: PromotionOut, target: StoreDiscountTarget) -> tuple[str, Dict[str, Any]]:
    cfg = promo.config if isinstance(promo.config, dict) else {}
    minimum = cfg.get("minimumRequirement") if isinstance(cfg.get("minimumRequirement"), dict) else None
    if not minimum:
        return "available", {}

    typename = _text(minimum.get("__typename"))
    details: Dict[str, Any] = {"type": typename or "minimum_requirement"}
    subtotal_amount = (
        _get_nested(minimum, "greaterThanOrEqualToSubtotal", "amount")
        or _get_nested(minimum, "amount", "amount")
        or minimum.get("subtotal")
        or minimum.get("minimumSubtotal")
    )
    quantity_value = (
        minimum.get("greaterThanOrEqualToQuantity")
        or minimum.get("quantity")
        or minimum.get("minimumQuantity")
    )

    subtotal_required = _to_decimal(subtotal_amount)
    quantity_required = None
    try:
        if quantity_value not in (None, ""):
            quantity_required = int(quantity_value)
    except Exception:
        quantity_required = None

    if subtotal_required is not None:
        current = target.subtotal or Decimal("0")
        remaining = max(Decimal("0"), subtotal_required - current)
        details.update(
            {
                "subtotal_required": _money(subtotal_required),
                "current_subtotal": _money(current),
                "remaining_subtotal": _money(remaining),
                "currency": target.currency,
            }
        )
        return ("available" if remaining == 0 else "unlockable"), details

    if quantity_required is not None:
        current_qty = max(0, int(target.quantity or 0))
        remaining_qty = max(0, quantity_required - current_qty)
        details.update(
            {
                "quantity_required": quantity_required,
                "current_quantity": current_qty,
                "remaining_quantity": remaining_qty,
            }
        )
        return ("available" if remaining_qty == 0 else "unlockable"), details

    return "unlockable", details


def _discount_badge(promo: PromotionOut, status: str) -> str:
    cfg = promo.config if isinstance(promo.config, dict) else {}
    discount_type = _lower(cfg.get("discountType"))
    method = _lower(cfg.get("discountMethod"))
    codes = _list_text(cfg.get("codes"))
    summary = _text(cfg.get("summary") or promo.humanReadableRule or promo.description)
    name = _text(promo.name)

    if discount_type == "free_shipping" or promo.type == "FREE_SHIPPING":
        if codes:
            return "Free shipping code"
        return "Free shipping"
    if discount_type == "bxgy":
        if status == "unverified":
            return "Bundle offer"
        return summary or name or "Buy more, save"
    if method == "code" and codes:
        return f"Code {codes[0]}"
    return summary or name or "Store offer"


def _offer_display(promo: PromotionOut, status: str, minimum: Dict[str, Any]) -> Dict[str, str]:
    cfg = promo.config if isinstance(promo.config, dict) else {}
    discount_type = _lower(cfg.get("discountType"))
    summary = _text(cfg.get("summary") or promo.humanReadableRule or promo.description)
    badge = _discount_badge(promo, status)
    if status == "unlockable":
        short_copy = summary or "Add more to unlock this store offer."
    elif status == "unverified":
        if discount_type == "bxgy":
            short_copy = "Bundle offer may be available at checkout."
            detail_copy = "Qualifying items and quantities are verified by the store platform quote and checkout."
            return {
                "badge": badge,
                "short_copy": short_copy,
                "detail_copy": detail_copy,
                "disclaimer": "Final eligibility and amounts are verified by the store platform quote and checkout.",
            }
        short_copy = summary or "Store offer may be available at checkout."
    else:
        short_copy = summary or "Store offer available at checkout."
    detail_copy = summary or promo.name or short_copy
    if minimum.get("remaining_quantity"):
        detail_copy = f"Add {minimum['remaining_quantity']} more to unlock this offer."
    elif minimum.get("remaining_subtotal"):
        detail_copy = f"Add {minimum['remaining_subtotal']} more to unlock this offer."
    return {
        "badge": badge,
        "short_copy": short_copy,
        "detail_copy": detail_copy,
        "disclaimer": "Final eligibility and amounts are verified by the store platform quote and checkout.",
    }


def _normalize_offer(
    promo: PromotionOut,
    *,
    target: StoreDiscountTarget,
    decisions: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    cfg = promo.config if isinstance(promo.config, dict) else {}
    metadata_source = _text(cfg.get("source"))
    platform = SUPPORTED_STORE_DISCOUNT_METADATA_SOURCES.get(metadata_source)
    if not platform:
        decisions.append(
            {
                "type": "store_discount_skipped",
                "store_discount_id": promo.id,
                "reason": "unsupported_store_discount_source",
                "source": metadata_source or None,
            }
        )
        return None
    if promo.status == PromotionStatus.UPCOMING:
        decisions.append(
            {
                "type": "store_discount_skipped",
                "store_discount_id": promo.id,
                "reason": "not_started",
            }
        )
        return None
    if promo.status == PromotionStatus.ENDED:
        decisions.append(
            {
                "type": "store_discount_skipped",
                "store_discount_id": promo.id,
                "reason": "expired",
            }
        )
        return None
    if _lower(cfg.get("status")) not in {"", "active", "scheduled"}:
        decisions.append(
            {
                "type": "store_discount_skipped",
                "store_discount_id": promo.id,
                "reason": "inactive_shopify_status",
                "status": cfg.get("status"),
            }
        )
        return None

    matches_scope, scope_state, scope_reason = _scope_status(promo, target)
    if not matches_scope:
        decisions.append(
            {
                "type": "store_discount_skipped",
                "store_discount_id": promo.id,
                "reason": scope_reason,
            }
        )
        return None

    requirement_state, minimum = _minimum_requirement_status(promo, target)
    state_priority = {"available": 0, "unlockable": 1, "unverified": 2}
    status = max([scope_state, requirement_state], key=lambda value: state_priority.get(value, 2))
    discount_type = _text(cfg.get("discountType") or "unknown")
    codes = _list_text(cfg.get("codes"))
    context = cfg.get("context") if isinstance(cfg.get("context"), dict) else {}

    return {
        "store_discount_id": promo.id,
        "label": _text(promo.name or cfg.get("summary") or "Store offer"),
        "source": "store_discount_metadata",
        "source_system": metadata_source,
        "platform": platform,
        "shopify_discount_node_id": cfg.get("shopifyDiscountNodeId"),
        "discount_method": cfg.get("discountMethod"),
        "discount_type": discount_type,
        "discount_classes": cfg.get("discountClasses") or [],
        "status": status,
        "scope_status": scope_state,
        "scope_reason": scope_reason,
        "codes": codes,
        "combines_with": cfg.get("combinesWith") or {},
        "context": context,
        "customer_gets": cfg.get("customerGets") or {},
        "customer_buys": cfg.get("customerBuys") or {},
        "minimum_requirement": minimum or cfg.get("minimumRequirement") or {},
        "usage_limit": cfg.get("usageLimit"),
        "applies_once_per_customer": cfg.get("appliesOncePerCustomer"),
        "async_usage_count": cfg.get("asyncUsageCount"),
        "starts_at": promo.startAt.isoformat() if isinstance(promo.startAt, datetime) else None,
        "ends_at": promo.endAt.isoformat() if isinstance(promo.endAt, datetime) else None,
        "display": _offer_display(promo, status, minimum),
        "application_policy": dict(DISPLAY_ONLY_STORE_DISCOUNT_POLICY),
    }


def _pricing_confidence(offers: Sequence[Dict[str, Any]]) -> str:
    if not offers:
        return "not_applicable"
    statuses = {_text(offer.get("status")) for offer in offers if isinstance(offer, dict)}
    if "available" in statuses:
        return "metadata_available"
    if "unlockable" in statuses:
        return "metadata_unlockable"
    return "unverified"


def summarize_store_discount_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    offers = [offer for offer in evidence.get("offers") or [] if isinstance(offer, dict)]
    badges: List[str] = []
    types: Dict[str, int] = {}
    for offer in offers:
        display = offer.get("display") if isinstance(offer.get("display"), dict) else {}
        badge = _text(display.get("badge"))
        if badge and badge not in badges:
            badges.append(badge)
        discount_type = _text(offer.get("discount_type") or "unknown")
        types[discount_type] = types.get(discount_type, 0) + 1
    return {
        "has_store_discounts": bool(offers),
        "pricing_confidence": evidence.get("pricing_confidence") or "not_applicable",
        "offers_count": len(offers),
        "discount_type_counts": types,
        "badges": badges,
    }


def store_discount_badges(evidence: Dict[str, Any]) -> List[str]:
    summary = summarize_store_discount_evidence(evidence)
    return [str(item) for item in summary.get("badges") or []]


async def resolve_store_discount_evidence_for_targets(
    *,
    merchant_id: str,
    targets: Sequence[StoreDiscountTarget],
) -> Dict[str, Dict[str, Any]]:
    normalized = [target for target in targets if target.target_id and target.merchant_id == merchant_id]
    if not normalized:
        return {}

    try:
        promos, _ = await list_promotions(merchant_id=merchant_id, status=None, limit=200)
    except Exception as exc:
        return {
            target.target_id: {
                **empty_store_discount_evidence("resolver_error"),
                "decisions": [
                    {
                        "type": "store_discount_resolution",
                        "reason": "resolver_error",
                        "message": str(exc)[:240],
                    }
                ],
            }
            for target in normalized
        }

    out: Dict[str, Dict[str, Any]] = {}
    for target in normalized:
        decisions: List[Dict[str, Any]] = []
        offers: List[Dict[str, Any]] = []
        for promo in promos:
            offer = _normalize_offer(promo, target=target, decisions=decisions)
            if offer is not None:
                offers.append(offer)
        out[target.target_id] = {
            "pricing_confidence": _pricing_confidence(offers),
            "offers": offers,
            "resolver_scope": "store_discount_metadata",
            "supported_platforms": sorted(set(SUPPORTED_STORE_DISCOUNT_METADATA_SOURCES.values())),
            "decisions": decisions,
            "presentation_contract_version": "savings.v1",
        }
    return out


def _target_from_product_card(product: Dict[str, Any], fallback_merchant_id: Optional[str] = None) -> Optional[StoreDiscountTarget]:
    merchant_id = _text(product.get("merchant_id") or fallback_merchant_id)
    product_id = _text(product.get("platform_product_id") or product.get("product_id") or product.get("id"))
    variant_id = _text(
        product.get("variant_id")
        or product.get("variantId")
        or product.get("sku_id")
        or product.get("skuId")
        or _first_nested_variant_id(product)
        or product.get("id")
    )
    if not merchant_id or not (product_id or variant_id):
        return None
    quantity = None
    try:
        if product.get("quantity") is not None:
            quantity = int(product.get("quantity"))
    except Exception:
        quantity = None
    return StoreDiscountTarget(
        target_id=f"{merchant_id}:{variant_id or product_id}",
        merchant_id=merchant_id,
        product_id=product_id or None,
        variant_id=variant_id or None,
        quantity=quantity,
        subtotal=_to_decimal(product.get("subtotal") or product.get("price")),
        currency=_text(product.get("currency")) or None,
        market=_text(product.get("market")) or None,
    )


async def enrich_product_cards_with_store_discounts(
    products: List[Dict[str, Any]],
    *,
    merchant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not products:
        return products
    targets_by_merchant: Dict[str, List[StoreDiscountTarget]] = {}
    target_by_index: Dict[int, StoreDiscountTarget] = {}
    for idx, product in enumerate(products):
        if not isinstance(product, dict):
            continue
        target = _target_from_product_card(product, merchant_id)
        if target is None:
            continue
        target_by_index[idx] = target
        targets_by_merchant.setdefault(target.merchant_id, []).append(target)

    evidence_by_target: Dict[str, Dict[str, Any]] = {}
    for merch, targets in targets_by_merchant.items():
        evidence_by_target.update(
            await resolve_store_discount_evidence_for_targets(
                merchant_id=merch,
                targets=targets,
            )
        )

    for idx, product in enumerate(products):
        target = target_by_index.get(idx)
        evidence = evidence_by_target.get(target.target_id) if target else None
        evidence = evidence or empty_store_discount_evidence()
        product["store_discount_evidence"] = evidence
        product["store_discount_summary"] = summarize_store_discount_evidence(evidence)
        product["store_discount_badges"] = store_discount_badges(evidence)
    attach_savings_presentation_to_product_cards(products)
    return products


async def enrich_product_detail_with_store_discounts(
    product: Dict[str, Any],
    *,
    merchant_id: str,
) -> Dict[str, Any]:
    variants = product.get("variants")
    if not isinstance(variants, list):
        variants = []
    product_id = _text(product.get("id") or product.get("product_id") or product.get("platform_product_id"))
    targets: List[StoreDiscountTarget] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        variant_id = _text(variant.get("variant_id") or variant.get("id"))
        target_id = variant_id or product_id
        if not target_id:
            continue
        targets.append(
            StoreDiscountTarget(
                target_id=target_id,
                merchant_id=merchant_id,
                product_id=product_id or None,
                variant_id=variant_id or None,
                quantity=1,
                subtotal=_to_decimal(variant.get("price") or product.get("price")),
                currency=_text(product.get("currency") or variant.get("currency")) or None,
                market=_text(product.get("market")) or None,
            )
        )

    per_target = await resolve_store_discount_evidence_for_targets(
        merchant_id=merchant_id,
        targets=targets,
    )
    aggregate_offers: List[Dict[str, Any]] = []
    aggregate_decisions: List[Dict[str, Any]] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        target_id = _text(variant.get("variant_id") or variant.get("id") or product_id)
        evidence = per_target.get(target_id) or empty_store_discount_evidence()
        variant["store_discount_evidence"] = evidence
        variant["store_discount_summary"] = summarize_store_discount_evidence(evidence)
        variant["store_discount_badges"] = store_discount_badges(evidence)
        aggregate_offers.extend([offer for offer in evidence.get("offers") or [] if isinstance(offer, dict)])
        aggregate_decisions.extend([item for item in evidence.get("decisions") or [] if isinstance(item, dict)])

    aggregate = {
        "pricing_confidence": _pricing_confidence(aggregate_offers),
        "offers": aggregate_offers,
        "resolver_scope": "store_discount_metadata",
        "supported_platforms": sorted(set(SUPPORTED_STORE_DISCOUNT_METADATA_SOURCES.values())),
        "decisions": aggregate_decisions,
        "presentation_contract_version": "savings.v1",
    }
    product["store_discount_evidence"] = aggregate
    product["store_discount_summary"] = summarize_store_discount_evidence(aggregate)
    product["store_discount_badges"] = store_discount_badges(aggregate)
    attach_savings_presentation_to_product_detail(product)
    return product
