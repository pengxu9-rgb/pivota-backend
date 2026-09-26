"""Validation contract for merchant-level after-sales facts.

The crawler may collect candidates, but only this normalized payload is allowed
into Commerce Index.  It intentionally rejects raw review bodies and buyer
identifiers; review evidence is aggregated and linked by source reference.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

FACT_KINDS = frozenset({"return_policy", "after_sales_review_summary"})


def normalize_after_sales_fact(raw: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    at = now or datetime.now(timezone.utc)
    kind = str(raw.get("fact_kind") or "")
    merchant_id = str(raw.get("merchant_id") or "")
    source_url = str(raw.get("source_url") or "")
    source_system = str(raw.get("source_system") or "")
    if not merchant_id or kind not in FACT_KINDS or not source_url.startswith(("https://", "http://")) or not source_system:
        raise ValueError("merchant_id, supported fact_kind, https source_url, and source_system are required")
    value = dict(raw.get("value") or {})
    evidence = dict(raw.get("evidence") or {})
    if kind == "return_policy":
        allowed = {"return_window_days", "exchange_available", "return_shipping_paid_by", "refund_method", "refund_timing_days", "exceptions"}
    else:
        allowed = {"sample_size", "return_process_sentiment", "customer_service_sentiment", "refund_speed_sentiment", "themes"}
        if int(value.get("sample_size") or 0) < 3:
            raise ValueError("after_sales_review_summary requires at least three attributable reviews")
    if set(value) - allowed:
        raise ValueError("unsupported after-sales value field")
    confidence = float(raw.get("confidence") or 0)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return {"merchant_id": merchant_id, "fact_kind": kind, "market_code": str(raw.get("market_code") or "unknown"),
            "policy_url": raw.get("policy_url"), "source_url": source_url, "source_system": source_system,
            "source_ref": raw.get("source_ref"), "value": value, "evidence": evidence, "confidence": confidence,
            "observed_at": raw.get("observed_at") or at, "fresh_until": raw.get("fresh_until") or at + timedelta(days=30),
            "review_required": bool(raw.get("review_required", True))}
