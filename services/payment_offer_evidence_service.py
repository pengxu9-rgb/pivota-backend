from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from db.database import database
from models.catalog import PivotPaymentContext


DISPLAY_ONLY_POLICY = {
    "affects_shopify_discount": False,
    "affects_psp_amount_v1": False,
    "finalization_stage": "psp_evidence",
}


@dataclass(frozen=True)
class PaymentOfferTarget:
    target_id: str
    merchant_id: str
    product_id: Optional[str] = None
    variant_id: Optional[str] = None
    offer_id: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    market: Optional[str] = None


def payment_offers_display_enabled() -> bool:
    return str(os.getenv("PAYMENT_OFFERS_DISPLAY_ENABLED", "1")).strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def empty_payment_offer_evidence(reason: str = "no_payment_offers") -> Dict[str, Any]:
    return {
        "pricing_confidence": "not_applicable",
        "offers": [],
        "decisions": [{"type": "payment_offer_resolution", "reason": reason}],
    }


def payment_context_from_mapping(value: Any) -> Optional[PivotPaymentContext]:
    if isinstance(value, PivotPaymentContext):
        return value
    if not isinstance(value, dict):
        return None
    payload = {
        "psp": value.get("psp") or value.get("provider") or value.get("preferred_psp"),
        "payment_method_type": value.get("payment_method_type") or value.get("payment_method"),
        "card_network": value.get("card_network") or value.get("network"),
        "issuer_name": value.get("issuer_name") or value.get("issuer"),
        "wallet_type": value.get("wallet_type") or value.get("wallet"),
        "installment_provider": value.get("installment_provider") or value.get("bnpl_provider"),
    }
    payload = {k: str(v).strip() for k, v in payload.items() if str(v or "").strip()}
    return PivotPaymentContext(**payload) if payload else None


def payment_context_to_dict(payment_context: Optional[PivotPaymentContext]) -> Dict[str, str]:
    if payment_context is None:
        return {}
    raw = payment_context.model_dump(exclude_none=True)
    return {str(k): str(v).strip() for k, v in raw.items() if str(v or "").strip()}


def stable_payment_offer_hash(evidence: Any) -> Optional[str]:
    if not isinstance(evidence, dict) or not evidence:
        return None
    raw = json.dumps(evidence, sort_keys=True, default=str, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_lower(value: Any) -> str:
    return _normalize_text(value).lower()


def _first_nested_variant_id(product: Dict[str, Any]) -> str:
    for key in ("variants", "options"):
        values = product.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            variant_id = _normalize_text(
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
            variant_id = _normalize_text(
                offer.get("variant_id")
                or offer.get("variantId")
                or offer.get("sku_id")
                or offer.get("skuId")
            )
            if variant_id:
                return variant_id
    return ""


def _json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _money(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _list_strings(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        items = _json_list(value)
    return [str(item).strip() for item in items if str(item or "").strip()]


def _lookup_any(mapping: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def _scope_matches(row: Dict[str, Any], target: PaymentOfferTarget, decisions: List[Dict[str, Any]]) -> bool:
    scope = _json_dict(row.get("scope_json"))
    metadata = _json_dict(row.get("metadata_json"))
    metadata_scope = _json_dict(metadata.get("scope"))
    merged = {**metadata_scope, **scope}
    if not merged:
        return True

    if merged.get("global") is True or str(merged.get("scope") or "").lower() in {"all", "global"}:
        return True

    product_ids = _list_strings(_lookup_any(merged, ("productIds", "product_ids", "source_product_ids", "products")))
    variant_ids = _list_strings(_lookup_any(merged, ("variantIds", "variant_ids", "source_variant_ids", "variants")))
    offer_ids = _list_strings(_lookup_any(merged, ("offerIds", "offer_ids", "offers")))

    has_explicit_scope = bool(product_ids or variant_ids or offer_ids)
    if not has_explicit_scope:
        return True

    product_candidates = {
        _normalize_text(target.product_id),
        _normalize_text(row.get("source_product_id")),
    }
    variant_candidates = {
        _normalize_text(target.variant_id),
        _normalize_text(row.get("source_variant_id")),
    }
    offer_candidates = {
        _normalize_text(target.offer_id),
        _normalize_text(row.get("offer_id")),
    }

    if product_ids and product_candidates.intersection(product_ids):
        return True
    if variant_ids and variant_candidates.intersection(variant_ids):
        return True
    if offer_ids and offer_candidates.intersection(offer_ids):
        return True

    decisions.append(
        {
            "type": "payment_offer_rejected",
            "payment_offer_id": row.get("incentive_id"),
            "reason": "target_out_of_scope",
            "target_id": target.target_id,
        }
    )
    return False


def _schedule_matches(
    row: Dict[str, Any],
    *,
    now: datetime,
    decisions: List[Dict[str, Any]],
) -> bool:
    schedule = _json_dict(row.get("schedule_json"))
    starts_at = _parse_datetime(row.get("starts_at") or schedule.get("starts_at") or schedule.get("start_at"))
    ends_at = _parse_datetime(row.get("ends_at") or schedule.get("ends_at") or schedule.get("end_at"))
    if starts_at and now < starts_at:
        decisions.append(
            {
                "type": "payment_offer_rejected",
                "payment_offer_id": row.get("incentive_id"),
                "reason": "not_started",
            }
        )
        return False
    if ends_at and now > ends_at:
        decisions.append(
            {
                "type": "payment_offer_rejected",
                "payment_offer_id": row.get("incentive_id"),
                "reason": "expired",
            }
        )
        return False
    return True


def _market_matches(row: Dict[str, Any], market: Optional[str], decisions: List[Dict[str, Any]]) -> bool:
    configured = _normalize_lower(row.get("market"))
    requested = _normalize_lower(market)
    if configured and requested and configured != requested:
        decisions.append(
            {
                "type": "payment_offer_rejected",
                "payment_offer_id": row.get("incentive_id"),
                "reason": "market_mismatch",
                "market": market,
            }
        )
        return False
    return True


def _requirement_value(row: Dict[str, Any], key: str) -> Optional[str]:
    metadata = _json_dict(row.get("metadata_json"))
    conditions = _json_dict(row.get("conditions_json"))
    requirements = _json_dict(metadata.get("requirements"))
    payment = _json_dict(conditions.get("payment") or conditions.get("payment_context"))
    raw = (
        row.get(key)
        or requirements.get(key)
        or payment.get(key)
        or conditions.get(key)
    )
    value = _normalize_text(raw)
    return value or None


def _requirements(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {
        "psp": _requirement_value(row, "psp"),
        "payment_method_type": _requirement_value(row, "payment_method_type"),
        "card_network": _requirement_value(row, "card_network"),
        "issuer_name": _requirement_value(row, "issuer_name"),
        "wallet_type": _requirement_value(row, "wallet_type"),
        "installment_provider": _requirement_value(row, "installment_provider"),
    }


def _payment_context_status(
    row: Dict[str, Any],
    payment_context: Optional[PivotPaymentContext],
    decisions: List[Dict[str, Any]],
) -> Optional[Tuple[str, List[str]]]:
    required = {k: v for k, v in _requirements(row).items() if v}
    context = {k: v.lower() for k, v in payment_context_to_dict(payment_context).items()}
    if not context:
        return "potential", []

    reason_codes: List[str] = []
    matched_any = False
    for key, required_value in required.items():
        requested = context.get(key)
        if not requested:
            reason_codes.append(f"{key}_unverified")
            continue
        if requested != required_value.lower():
            decisions.append(
                {
                    "type": "payment_offer_rejected",
                    "payment_offer_id": row.get("incentive_id"),
                    "reason": f"{key}_mismatch",
                    "required": required_value,
                    "actual": requested,
                }
            )
            return None
        matched_any = True

    if required and not reason_codes and matched_any:
        return "context_matched", []
    if matched_any:
        return "context_matched", reason_codes
    return "potential", reason_codes


def _estimate_savings(amount: Optional[Decimal], row: Dict[str, Any]) -> Optional[Decimal]:
    if amount is None:
        return None
    benefit_kind = _normalize_lower(row.get("benefit_kind"))
    benefit_value = _to_decimal(row.get("benefit_value"))
    if benefit_value is None:
        return None
    if benefit_kind in {"percentage_off", "percent_off", "discount_percentage", "discount_rate"}:
        return max(Decimal("0"), (amount * benefit_value / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if benefit_kind in {"amount_off", "fixed_amount_off", "discount_amount", "statement_credit"}:
        return min(max(Decimal("0"), benefit_value), max(Decimal("0"), amount))
    if benefit_kind in {"fixed_price", "set_price"}:
        return max(Decimal("0"), amount - benefit_value)
    return None


def _benefit_badge(row: Dict[str, Any]) -> str:
    benefit_kind = _normalize_lower(row.get("benefit_kind"))
    value = _to_decimal(row.get("benefit_value"))
    currency = _normalize_text(row.get("benefit_currency") or "USD")
    if value is None:
        return _normalize_text(row.get("label") or "Payment benefit")
    if benefit_kind in {"percentage_off", "percent_off", "discount_percentage", "discount_rate"}:
        return f"{value.normalize()}% payment benefit"
    if benefit_kind in {"amount_off", "fixed_amount_off", "discount_amount"}:
        return f"{currency} {_money(value)} payment benefit"
    if benefit_kind == "statement_credit":
        return f"{currency} {_money(value)} statement credit"
    if benefit_kind in {"fixed_price", "set_price"}:
        return f"{currency} {_money(value)} payment price"
    if benefit_kind == "points":
        return f"{value.normalize()} points"
    if benefit_kind == "installment":
        return "Installment offer"
    return _normalize_text(row.get("label") or "Payment benefit")


def _offer_display(row: Dict[str, Any]) -> Dict[str, str]:
    metadata = _json_dict(row.get("metadata_json"))
    display = _json_dict(metadata.get("display"))
    label = _normalize_text(row.get("label") or "Payment benefit")
    badge = _normalize_text(display.get("badge")) or _benefit_badge(row)
    short_copy = _normalize_text(display.get("short_copy")) or label
    detail_copy = _normalize_text(display.get("detail_copy")) or f"{label} may apply with the selected payment method."
    disclaimer = _normalize_text(display.get("disclaimer")) or "Final eligibility depends on selected payment method."
    return {
        "badge": badge,
        "short_copy": short_copy,
        "detail_copy": detail_copy,
        "disclaimer": disclaimer,
    }


def _normalize_offer(
    row: Dict[str, Any],
    *,
    target: PaymentOfferTarget,
    payment_context: Optional[PivotPaymentContext],
    now: datetime,
    decisions: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    status = _normalize_lower(row.get("status") or "active")
    if status != "active":
        decisions.append(
            {
                "type": "payment_offer_rejected",
                "payment_offer_id": row.get("incentive_id"),
                "reason": "inactive",
                "status": row.get("status"),
            }
        )
        return None
    if not _scope_matches(row, target, decisions):
        return None
    if not _schedule_matches(row, now=now, decisions=decisions):
        return None
    if not _market_matches(row, target.market, decisions):
        return None

    context_result = _payment_context_status(row, payment_context, decisions)
    if context_result is None:
        return None
    eligibility_status, reason_codes = context_result
    requirements = _requirements(row)
    savings = _estimate_savings(target.amount, row)
    estimated_total = target.amount - savings if target.amount is not None and savings is not None else None

    return {
        "payment_offer_id": str(row.get("incentive_id") or ""),
        "label": _normalize_text(row.get("label") or "Payment benefit"),
        "source_system": _normalize_text(row.get("source_system") or "merchant_config"),
        "funding_source": _normalize_text(row.get("funding_source") or "unknown"),
        "benefit_kind": _normalize_text(row.get("benefit_kind") or "unknown"),
        "benefit_value": _money(_to_decimal(row.get("benefit_value"))),
        "benefit_currency": row.get("benefit_currency"),
        "requirements": requirements,
        "eligibility": {
            "status": eligibility_status,
            "confidence": _money(_to_decimal(row.get("eligibility_confidence"))),
            "reason_codes": reason_codes,
        },
        "display": _offer_display(row),
        "estimated_savings": _money(savings),
        "estimated_total_after_payment_offer": _money(estimated_total),
        "application_policy": dict(DISPLAY_ONLY_POLICY),
    }


def _pricing_confidence(offers: List[Dict[str, Any]]) -> str:
    if not offers:
        return "not_applicable"
    statuses = {
        str(((offer.get("eligibility") or {}).get("status")) or "")
        for offer in offers
    }
    if "psp_verified" in statuses:
        return "psp_verified"
    if "context_matched" in statuses:
        return "context_matched"
    if "potential" in statuses:
        return "display_estimate"
    return "unverified"


def summarize_payment_offer_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    offers = [offer for offer in evidence.get("offers") or [] if isinstance(offer, dict)]
    badges: List[str] = []
    max_savings: Optional[Decimal] = None
    for offer in offers:
        display = offer.get("display") if isinstance(offer.get("display"), dict) else {}
        badge = _normalize_text(display.get("badge"))
        if badge and badge not in badges:
            badges.append(badge)
        savings = _to_decimal(offer.get("estimated_savings"))
        if savings is not None and (max_savings is None or savings > max_savings):
            max_savings = savings
    return {
        "has_payment_offers": bool(offers),
        "pricing_confidence": evidence.get("pricing_confidence") or "not_applicable",
        "offers_count": len(offers),
        "badges": badges,
        "best_estimated_savings": _money(max_savings),
    }


def payment_offer_badges(evidence: Dict[str, Any]) -> List[str]:
    summary = summarize_payment_offer_evidence(evidence)
    return [str(item) for item in summary.get("badges") or []]


def payment_pricing_summary(
    *,
    evidence: Dict[str, Any],
    checkout_total: Any,
    currency: Optional[str],
) -> Dict[str, Any]:
    total = _to_decimal(checkout_total)
    best_savings: Optional[Decimal] = None
    best_after: Optional[Decimal] = None
    for offer in evidence.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        savings = _to_decimal(offer.get("estimated_savings"))
        after = _to_decimal(offer.get("estimated_total_after_payment_offer"))
        if savings is not None and (best_savings is None or savings > best_savings):
            best_savings = savings
            best_after = after
    return {
        "checkout_total": _money(total),
        "currency": currency,
        "estimated_payment_benefit": _money(best_savings),
        "estimated_total_after_payment_offer": _money(best_after),
        "display_only": True,
        "affects_psp_amount_v1": False,
    }


def _payment_offer_status_counts(evidence: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for offer in evidence.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        eligibility = offer.get("eligibility") if isinstance(offer.get("eligibility"), dict) else {}
        status = str(eligibility.get("status") or "unverified")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _payment_offer_decision_counts(evidence: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for decision in evidence.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        reason = str(decision.get("reason") or decision.get("type") or "unknown")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _payment_offer_refs(evidence: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for offer in evidence.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        eligibility = offer.get("eligibility") if isinstance(offer.get("eligibility"), dict) else {}
        refs.append(
            {
                "payment_offer_id": offer.get("payment_offer_id"),
                "benefit_kind": offer.get("benefit_kind"),
                "estimated_savings": offer.get("estimated_savings"),
                "eligibility_status": eligibility.get("status"),
                "reason_codes": eligibility.get("reason_codes") or [],
            }
        )
    return refs


def _redact_payment_method_evidence(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "psp",
        "payment_method_type",
        "card_network",
        "issuer_name",
        "wallet_type",
        "installment_provider",
        "verification_status",
        "eligible",
        "reason_codes",
        "available_payment_methods",
        "selected_payment_method_type",
        "express_checkout_wallet_type",
    }
    return {str(k): value.get(k) for k in allowed if k in value}


def redact_payment_method_evidence(value: Any) -> Dict[str, Any]:
    return _redact_payment_method_evidence(value)


def emit_payment_offer_analytics_event(
    *,
    event_type: str,
    merchant_id: Optional[str],
    surface: str,
    evidence: Optional[Dict[str, Any]] = None,
    payment_context: Optional[PivotPaymentContext] = None,
    selected_payment_offer_id: Optional[str] = None,
    payment_method_evidence: Optional[Dict[str, Any]] = None,
    quote_id: Optional[str] = None,
    order_id: Optional[str] = None,
    adapter: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    if str(os.getenv("PAYMENT_OFFERS_ANALYTICS_ENABLED", "1")).strip().lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        return

    evidence = evidence if isinstance(evidence, dict) else {}
    payload = {
        "pricing_confidence": evidence.get("pricing_confidence"),
        "summary": summarize_payment_offer_evidence(evidence or empty_payment_offer_evidence()),
        "status_counts": _payment_offer_status_counts(evidence),
        "decision_counts": _payment_offer_decision_counts(evidence),
        "payment_offer_refs": _payment_offer_refs(evidence),
        "payment_context": payment_context_to_dict(payment_context),
        "selected_payment_offer_id": selected_payment_offer_id,
        "payment_method_evidence": _redact_payment_method_evidence(payment_method_evidence),
        "payment_offer_evidence_hash": stable_payment_offer_hash(evidence),
        "quote_id": quote_id,
        "order_id": order_id,
        "application_policy": dict(DISPLAY_ONLY_POLICY),
    }
    try:
        from mvp.events import emit_best_effort

        emit_best_effort(
            event_type=event_type,
            payload=payload,
            merchant_id=merchant_id,
            geo=None,
            surface=surface,
            adapter=adapter,
            risk_tier="unknown",
            idempotency_key=idempotency_key,
        )
    except Exception:
        return


def _dedupe_offers(offers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for offer in offers:
        key = str(offer.get("payment_offer_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(offer)
    return out


async def _fetch_candidate_rows(
    merchant_id: str,
    *,
    targets: Sequence[PaymentOfferTarget],
) -> List[Dict[str, Any]]:
    offer_ids = sorted({target.offer_id for target in targets if target.offer_id})
    product_ids = sorted({target.product_id for target in targets if target.product_id})
    variant_ids = sorted({target.variant_id for target in targets if target.variant_id})

    where: List[str] = ["o.merchant_id = :merchant_id"]
    params: Dict[str, Any] = {"merchant_id": merchant_id}

    filters: List[str] = []
    for column, values, prefix in (
        ("o.offer_id", offer_ids, "offer_id"),
        ("p.source_product_id", product_ids, "product_id"),
        ("s.source_variant_id", variant_ids, "variant_id"),
    ):
        placeholders: List[str] = []
        for idx, value in enumerate(values):
            key = f"{prefix}_{idx}"
            params[key] = value
            placeholders.append(f":{key}")
        if placeholders:
            filters.append(f"{column} IN ({', '.join(placeholders)})")
    if filters:
        where.append(f"({' OR '.join(filters)})")

    rows = await database.fetch_all(
        f"""
        SELECT
            o.offer_id,
            o.product_key,
            o.sku_key,
            o.currency,
            o.merchant_effective_price,
            p.source_product_id,
            s.source_variant_id,
            l.priority,
            i.incentive_id,
            i.incentive_type,
            i.funding_source,
            i.payment_method_type,
            i.card_network,
            i.issuer_name,
            i.wallet_type,
            i.installment_provider,
            i.label,
            i.benefit_kind,
            i.benefit_value,
            i.benefit_currency,
            i.market,
            i.eligibility_confidence,
            i.source_system,
            i.source_ref,
            i.status,
            i.starts_at,
            i.ends_at,
            i.metadata_json,
            r.scope_json,
            r.conditions_json,
            r.schedule_json,
            r.human_rule
        FROM catalog_offers o
        JOIN catalog_products p ON p.product_key = o.product_key
        JOIN catalog_skus s ON s.sku_key = o.sku_key
        JOIN catalog_offer_incentive_links l ON l.offer_id = o.offer_id
        JOIN catalog_payment_incentives i ON i.incentive_id = l.incentive_id
        LEFT JOIN catalog_incentive_rules r ON r.incentive_id = i.incentive_id
        WHERE {' AND '.join(where)}
        ORDER BY o.offer_id, l.priority ASC, i.updated_at DESC
        """,
        params,
    )
    return [dict(row) for row in rows]


def _row_matches_target(row: Dict[str, Any], target: PaymentOfferTarget) -> bool:
    if target.offer_id and target.offer_id == str(row.get("offer_id") or ""):
        return True
    if target.variant_id and target.variant_id == str(row.get("source_variant_id") or ""):
        return True
    if target.product_id and target.product_id == str(row.get("source_product_id") or ""):
        return True
    return False


async def resolve_payment_offer_evidence_for_targets(
    *,
    merchant_id: str,
    targets: Sequence[PaymentOfferTarget],
    payment_context: Optional[PivotPaymentContext] = None,
    market: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    if not payment_offers_display_enabled():
        return {target.target_id: empty_payment_offer_evidence("feature_disabled") for target in targets}
    normalized_targets = [target for target in targets if target.target_id and target.merchant_id == merchant_id]
    if not normalized_targets:
        return {}

    try:
        rows = await _fetch_candidate_rows(merchant_id, targets=normalized_targets)
    except Exception as exc:
        return {
            target.target_id: {
                **empty_payment_offer_evidence("resolver_error"),
                "decisions": [
                    {
                        "type": "payment_offer_resolution",
                        "reason": "resolver_error",
                        "message": str(exc)[:240],
                    }
                ],
            }
            for target in normalized_targets
        }

    now = datetime.now(timezone.utc)
    out: Dict[str, Dict[str, Any]] = {}
    for target in normalized_targets:
        target = PaymentOfferTarget(
            target_id=target.target_id,
            merchant_id=target.merchant_id,
            product_id=target.product_id,
            variant_id=target.variant_id,
            offer_id=target.offer_id,
            amount=target.amount,
            currency=target.currency,
            market=target.market or market,
        )
        decisions: List[Dict[str, Any]] = []
        offers: List[Dict[str, Any]] = []
        for row in rows:
            if not _row_matches_target(row, target):
                continue
            offer = _normalize_offer(
                row,
                target=target,
                payment_context=payment_context,
                now=now,
                decisions=decisions,
            )
            if offer is not None:
                offers.append(offer)
        offers = _dedupe_offers(offers)
        out[target.target_id] = {
            "pricing_confidence": _pricing_confidence(offers),
            "offers": offers,
            "decisions": decisions,
        }
    return out


async def resolve_payment_offer_evidence(
    *,
    merchant_id: str,
    targets: Sequence[PaymentOfferTarget],
    payment_context: Optional[PivotPaymentContext] = None,
    market: Optional[str] = None,
    total_amount: Optional[Decimal] = None,
    currency: Optional[str] = None,
) -> Dict[str, Any]:
    per_target = await resolve_payment_offer_evidence_for_targets(
        merchant_id=merchant_id,
        targets=targets,
        payment_context=payment_context,
        market=market,
    )
    offers: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    for evidence in per_target.values():
        offers.extend([offer for offer in evidence.get("offers") or [] if isinstance(offer, dict)])
        decisions.extend([item for item in evidence.get("decisions") or [] if isinstance(item, dict)])
    offers = _dedupe_offers(offers)
    if total_amount is not None:
        for offer in offers:
            savings = _estimate_savings(total_amount, offer)
            if savings is not None:
                offer["estimated_savings"] = _money(savings)
                offer["estimated_total_after_payment_offer"] = _money(total_amount - savings)
            if currency and not offer.get("benefit_currency"):
                offer["benefit_currency"] = currency
    return {
        "pricing_confidence": _pricing_confidence(offers),
        "offers": offers,
        "decisions": decisions,
    }


def _target_from_product_card(product: Dict[str, Any], fallback_merchant_id: Optional[str] = None) -> Optional[PaymentOfferTarget]:
    merchant_id = _normalize_text(product.get("merchant_id") or fallback_merchant_id)
    product_id = _normalize_text(product.get("platform_product_id") or product.get("product_id") or product.get("id"))
    variant_id = _normalize_text(
        product.get("variant_id")
        or product.get("variantId")
        or product.get("sku_id")
        or product.get("skuId")
        or _first_nested_variant_id(product)
        or product.get("id")
    )
    if not merchant_id or not (product_id or variant_id):
        return None
    amount = _to_decimal(product.get("price"))
    target_id = f"{merchant_id}:{variant_id or product_id}"
    return PaymentOfferTarget(
        target_id=target_id,
        merchant_id=merchant_id,
        product_id=product_id or None,
        variant_id=variant_id or None,
        amount=amount,
        currency=_normalize_text(product.get("currency")) or None,
    )


async def enrich_product_cards_with_payment_offers(
    products: List[Dict[str, Any]],
    *,
    merchant_id: Optional[str] = None,
    payment_context: Optional[PivotPaymentContext] = None,
    market: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not products:
        return products
    targets_by_merchant: Dict[str, List[PaymentOfferTarget]] = {}
    target_by_index: Dict[int, PaymentOfferTarget] = {}
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
            await resolve_payment_offer_evidence_for_targets(
                merchant_id=merch,
                targets=targets,
                payment_context=payment_context,
                market=market,
            )
        )

    for idx, product in enumerate(products):
        target = target_by_index.get(idx)
        evidence = evidence_by_target.get(target.target_id) if target else None
        evidence = evidence or empty_payment_offer_evidence()
        product["payment_offer_evidence"] = evidence
        product["payment_offer_summary"] = summarize_payment_offer_evidence(evidence)
        product["payment_offer_badges"] = payment_offer_badges(evidence)
    return products


async def enrich_product_detail_with_payment_offers(
    product: Dict[str, Any],
    *,
    merchant_id: str,
    payment_context: Optional[PivotPaymentContext] = None,
    market: Optional[str] = None,
) -> Dict[str, Any]:
    variants = product.get("variants")
    if not isinstance(variants, list):
        variants = []
    product_id = _normalize_text(product.get("id") or product.get("product_id") or product.get("platform_product_id"))
    targets: List[PaymentOfferTarget] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        variant_id = _normalize_text(variant.get("variant_id") or variant.get("id"))
        target_id = variant_id or product_id
        if not target_id:
            continue
        targets.append(
            PaymentOfferTarget(
                target_id=target_id,
                merchant_id=merchant_id,
                product_id=product_id or None,
                variant_id=variant_id or None,
                amount=_to_decimal(variant.get("price") or product.get("price")),
                currency=_normalize_text(product.get("currency") or variant.get("currency")) or None,
                market=market,
            )
        )
    per_target = await resolve_payment_offer_evidence_for_targets(
        merchant_id=merchant_id,
        targets=targets,
        payment_context=payment_context,
        market=market,
    )
    aggregate_offers: List[Dict[str, Any]] = []
    aggregate_decisions: List[Dict[str, Any]] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        target_id = _normalize_text(variant.get("variant_id") or variant.get("id") or product_id)
        evidence = per_target.get(target_id) or empty_payment_offer_evidence()
        variant["payment_offer_evidence"] = evidence
        variant["payment_offer_summary"] = summarize_payment_offer_evidence(evidence)
        variant["payment_offer_badges"] = payment_offer_badges(evidence)
        aggregate_offers.extend([offer for offer in evidence.get("offers") or [] if isinstance(offer, dict)])
        aggregate_decisions.extend([item for item in evidence.get("decisions") or [] if isinstance(item, dict)])

    aggregate = {
        "pricing_confidence": _pricing_confidence(_dedupe_offers(aggregate_offers)),
        "offers": _dedupe_offers(aggregate_offers),
        "decisions": aggregate_decisions,
    }
    product["payment_offer_evidence"] = aggregate
    product["payment_offer_summary"] = summarize_payment_offer_evidence(aggregate)
    product["payment_offer_badges"] = payment_offer_badges(aggregate)
    return product
