from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from models.standard_product import StandardProduct

AGENT_PUSH_STATUS_ELIGIBLE = "eligible_for_agent_push"
AGENT_PUSH_STATUS_EXCLUDED = "excluded_from_agent_push"

AUTO_EXCLUSION_REASON_CODES = (
    "out_of_stock",
    "missing_price",
    "missing_currency",
)


def _format_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    return text or None


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
          parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _coerce_product_payload(product: Any) -> Dict[str, Any]:
    if isinstance(product, StandardProduct):
        return product.model_dump()
    if hasattr(product, "model_dump"):
        try:
            dumped = product.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if isinstance(product, dict):
        payload = dict(product)
    else:
        try:
            payload = dict(product)
        except Exception:
            payload = {}

    try:
        parsed = StandardProduct.model_validate(payload)
        return parsed.model_dump()
    except Exception:
        return payload


def _coerce_price_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        amount = float(value)
    except Exception:
        return None
    return amount if amount > 0 else None


def _coerce_currency(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    return text or None


def _availability_to_in_stock(availability: Any) -> bool:
    if availability is None:
        return True
    if isinstance(availability, bool):
        return availability
    raw = str(availability).strip().lower()
    if not raw:
        return True
    return raw not in {"out_of_stock", "outofstock", "sold_out", "soldout", "unavailable"}


def _build_standard_variant_fallback(product: Dict[str, Any]) -> list[Dict[str, Any]]:
    variants = product.get("variants")
    if isinstance(variants, list) and variants:
        result: list[Dict[str, Any]] = []
        for variant in variants:
            if isinstance(variant, dict):
                result.append(dict(variant))
            elif hasattr(variant, "model_dump"):
                try:
                    result.append(dict(variant.model_dump()))
                except Exception:
                    continue
        if result:
            return result

    return [
        {
            "id": product.get("product_id") or product.get("id"),
            "price": product.get("price"),
            "currency": product.get("currency"),
            "inventory_quantity": product.get("inventory_quantity"),
            "available": product.get("in_stock"),
            "availability": product.get("availability"),
        }
    ]


def _standard_variant_reason_codes(variant: Dict[str, Any], *, product_currency: Any) -> list[str]:
    reasons: list[str] = []
    price_amount = _coerce_price_amount(variant.get("price"))
    currency = _coerce_currency(variant.get("currency") or product_currency)

    available = variant.get("available")
    if available is None:
        inventory_quantity = variant.get("inventory_quantity")
        if inventory_quantity is not None:
            try:
                available = int(inventory_quantity) > 0
            except Exception:
                available = None
    if available is None:
        available = _availability_to_in_stock(variant.get("availability"))

    if not bool(available):
        reasons.append("out_of_stock")
    if price_amount is None:
        reasons.append("missing_price")
    if currency is None:
        reasons.append("missing_currency")
    return reasons


def build_agent_push_projection_from_standard_variant(
    variant: Dict[str, Any],
    *,
    product_currency: Any,
    checked_at: Any = None,
) -> Dict[str, Any]:
    reason_codes = _standard_variant_reason_codes(variant, product_currency=product_currency)
    status = AGENT_PUSH_STATUS_EXCLUDED if reason_codes else AGENT_PUSH_STATUS_ELIGIBLE
    return {
        "agent_push_status": status,
        "agent_push_reason_codes": list(reason_codes),
        "eligible_variant_count": 0 if reason_codes else 1,
        "excluded_variant_count": 1 if reason_codes else 0,
        "store_data_last_checked_at": _format_timestamp(checked_at),
    }
def build_agent_push_projection_from_standard_product(
    product: Any,
    *,
    checked_at: Any = None,
) -> Dict[str, Any]:
    payload = _coerce_product_payload(product)
    product_currency = payload.get("currency")
    reason_counter: Counter[str] = Counter()
    eligible_variant_count = 0
    excluded_variant_count = 0

    for variant in _build_standard_variant_fallback(payload):
        reason_codes = _standard_variant_reason_codes(variant, product_currency=product_currency)
        if reason_codes:
            excluded_variant_count += 1
            reason_counter.update(set(reason_codes))
        else:
            eligible_variant_count += 1

    if eligible_variant_count > 0:
        status = AGENT_PUSH_STATUS_ELIGIBLE
    else:
        status = AGENT_PUSH_STATUS_EXCLUDED

    resolved_checked_at = (
        checked_at
        or payload.get("updated_at")
        or payload.get("published_at")
        or payload.get("created_at")
    )

    return {
        "agent_push_status": status,
        "agent_push_reason_codes": [
            code for code, _ in reason_counter.most_common()
        ],
        "eligible_variant_count": eligible_variant_count,
        "excluded_variant_count": excluded_variant_count,
        "store_data_last_checked_at": _format_timestamp(resolved_checked_at),
    }


def pick_first_eligible_variant_from_standard_product(product: Any) -> Optional[Dict[str, Any]]:
    payload = _coerce_product_payload(product)
    product_currency = payload.get("currency")
    for variant in _build_standard_variant_fallback(payload):
        projection = build_agent_push_projection_from_standard_variant(
            variant,
            product_currency=product_currency,
            checked_at=payload.get("updated_at") or payload.get("published_at") or payload.get("created_at"),
        )
        if projection.get("agent_push_status") == AGENT_PUSH_STATUS_ELIGIBLE:
            return {
                "variant": dict(variant),
                "projection": projection,
            }
    return None
def build_agent_push_projection_from_cache_row(cache_row: Dict[str, Any]) -> Dict[str, Any]:
    payload = cache_row.get("product_data") or {}
    return build_agent_push_projection_from_standard_product(
        payload,
        checked_at=cache_row.get("cached_at") or cache_row.get("updated_at"),
    )


def _ready_variant_reason_codes(variant: Any) -> list[str]:
    reasons: list[str] = []
    blocker_codes = set(variant.blockers.get("discovery", []) + variant.blockers.get("checkout", []))
    price_data = variant.price or {}
    inventory_data = variant.inventory or {}

    price_amount = _coerce_price_amount(price_data.get("amount"))
    currency = _coerce_currency(price_data.get("currency"))

    availability = inventory_data.get("availability")
    quantity = inventory_data.get("quantity")
    in_stock = _availability_to_in_stock(availability)
    if quantity is not None:
        try:
            in_stock = in_stock and int(quantity) > 0
        except Exception:
            pass

    if "out_of_stock" in blocker_codes or not in_stock:
        reasons.append("out_of_stock")
    if "missing_price" in blocker_codes or price_amount is None:
        reasons.append("missing_price")
    if "missing_currency" in blocker_codes or currency is None:
        reasons.append("missing_currency")
    return reasons


def build_agent_push_projection_from_ready_product(
    product: Any,
    *,
    checked_at: Any = None,
) -> Dict[str, Any]:
    reason_counter: Counter[str] = Counter()
    eligible_variant_count = 0
    excluded_variant_count = 0

    for variant in getattr(product, "variants", []) or []:
        reason_codes = _ready_variant_reason_codes(variant)
        if reason_codes:
            excluded_variant_count += 1
            reason_counter.update(set(reason_codes))
        else:
            eligible_variant_count += 1

    status = (
        AGENT_PUSH_STATUS_ELIGIBLE
        if eligible_variant_count > 0
        else AGENT_PUSH_STATUS_EXCLUDED
    )

    resolved_checked_at = checked_at
    for variant in getattr(product, "variants", []) or []:
        freshness = getattr(variant, "freshness", None) or {}
        for field_status in freshness.values():
            observed_at = getattr(field_status, "observed_at", None)
            if observed_at:
                resolved_checked_at = resolved_checked_at or observed_at
                break
        if resolved_checked_at:
            break

    return {
        "agent_push_status": status,
        "agent_push_reason_codes": [
            code for code, _ in reason_counter.most_common()
        ],
        "eligible_variant_count": eligible_variant_count,
        "excluded_variant_count": excluded_variant_count,
        "store_data_last_checked_at": _format_timestamp(resolved_checked_at),
    }


def build_agent_push_projection_from_ready_variant(variant: Any) -> Dict[str, Any]:
    reason_codes = _ready_variant_reason_codes(variant)
    return {
        "agent_push_status": (
            AGENT_PUSH_STATUS_EXCLUDED if reason_codes else AGENT_PUSH_STATUS_ELIGIBLE
        ),
        "agent_push_reason_codes": reason_codes,
    }
def summarize_agent_push_projections(
    projections: Iterable[Dict[str, Any]],
    *,
    active_blocked_variants: int = 0,
) -> Dict[str, Any]:
    projection_list = [dict(item or {}) for item in projections]
    reason_counter: Counter[str] = Counter()
    last_checked_at: Optional[datetime] = None

    eligible_products = 0
    excluded_products = 0
    eligible_variants = 0
    excluded_variants = 0

    for projection in projection_list:
        status = projection.get("agent_push_status")
        if status == AGENT_PUSH_STATUS_EXCLUDED:
            excluded_products += 1
        else:
            eligible_products += 1

        eligible_variants += int(projection.get("eligible_variant_count") or 0)
        excluded_variants += int(projection.get("excluded_variant_count") or 0)
        reason_counter.update(set(projection.get("agent_push_reason_codes") or []))

        parsed = _parse_timestamp(projection.get("store_data_last_checked_at"))
        if parsed and (last_checked_at is None or parsed > last_checked_at):
            last_checked_at = parsed

    return {
        "total_products": len(projection_list),
        "eligible_products": eligible_products,
        "excluded_products": excluded_products,
        "eligible_variants": eligible_variants,
        "excluded_variants": excluded_variants,
        "active_blocked_variants": max(0, int(active_blocked_variants or 0)),
        "top_reason_codes": [
            {"code": code, "count": count}
            for code, count in reason_counter.most_common()
        ],
        "last_checked_at": _format_timestamp(last_checked_at),
    }
