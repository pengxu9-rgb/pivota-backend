from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Mapping


_STATUS_LABELS = {
    "transactable": "Transactable",
    "servable_not_transactable": "Servable, checkout not ready",
    "servable_not_direct_purchase": "Servable, not Pivota direct purchase",
    "not_publishable": "Not publishable",
    "not_measured": "Not measured",
}

_STATUS_ORDER = [
    "transactable",
    "servable_not_transactable",
    "servable_not_direct_purchase",
    "not_publishable",
    "not_measured",
]


def deliverability_status_label(status: Any) -> str:
    key = str(status or "not_measured").strip() or "not_measured"
    return _STATUS_LABELS.get(key, key.replace("_", " ").capitalize())


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sku_display_name(report: Mapping[str, Any]) -> str:
    for key in ("sku_title", "title", "sku_key", "product_key"):
        value = str(report.get(key) or "").strip()
        if value:
            return value
    return "SKU"


def _row_from_sku_report(report: Mapping[str, Any]) -> Dict[str, Any]:
    prediction = _as_mapping(report.get("deliverability"))
    serving = _as_mapping(prediction.get("serving"))
    checkout = _as_mapping(prediction.get("checkout"))
    status = str(prediction.get("status") or "not_measured").strip() or "not_measured"
    return {
        "sku_key": report.get("sku_key"),
        "product_key": report.get("product_key"),
        "sku_title": _sku_display_name(report),
        "status": status,
        "status_label": deliverability_status_label(status),
        "summary": prediction.get("summary"),
        "serving_status": serving.get("status"),
        "checkout_status": checkout.get("status"),
        "checkout_reason": checkout.get("reason"),
        "commerce_path": checkout.get("commerce_path"),
    }


def _row_from_rollup(row: Mapping[str, Any]) -> Dict[str, Any]:
    status = str(row.get("status") or "not_measured").strip() or "not_measured"
    return {
        "sku_key": row.get("sku_key"),
        "product_key": row.get("product_key"),
        "sku_title": _sku_display_name(row),
        "status": status,
        "status_label": deliverability_status_label(status),
        "summary": row.get("summary"),
        "serving_status": row.get("serving_status"),
        "checkout_status": row.get("checkout_status"),
        "checkout_reason": row.get("checkout_reason"),
        "commerce_path": row.get("commerce_path"),
    }


def _count_rows(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter(str(row.get("status") or "not_measured") for row in rows)
    return {key: int(counts[key]) for key in counts}


def _safe_count(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _ordered_counts(counts: Mapping[str, Any]) -> List[Dict[str, Any]]:
    normalized: Dict[str, int] = {}
    for status, count in counts.items():
        value = _safe_count(count)
        if value > 0:
            normalized[str(status)] = value

    ordered = [status for status in _STATUS_ORDER if status in normalized]
    ordered.extend(sorted(status for status in normalized if status not in _STATUS_ORDER))
    return [
        {
            "status": status,
            "label": deliverability_status_label(status),
            "count": normalized[status],
        }
        for status in ordered
    ]


def build_deliverability_render_view(report: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a renderer-ready view over per-SKU deliverability facts.

    The source facts come from `build_sku_deliverability_prediction`; this
    helper only labels and summarizes them for merchant-facing renderers.
    """
    if not isinstance(report, Mapping):
        return {}

    per_sku_reports = [
        row for row in (report.get("per_sku_reports") or [])
        if isinstance(row, Mapping) and isinstance(row.get("deliverability"), Mapping)
    ]
    rows = [_row_from_sku_report(row) for row in per_sku_reports]
    if not rows and isinstance(report.get("deliverability"), Mapping):
        rows = [_row_from_sku_report(report)]

    rollup = _as_mapping(report.get("brand_rollup"))
    deliverability = _as_mapping(rollup.get("deliverability"))
    if not rows and deliverability:
        rollup_rows = []
        for key in ("transactable_skus", "attention_skus"):
            rollup_rows.extend(
                row for row in (deliverability.get(key) or [])
                if isinstance(row, Mapping)
            )
        rows = [_row_from_rollup(row) for row in rollup_rows]

    counts = _as_mapping(deliverability.get("status_counts"))
    if not counts and rows:
        counts = _count_rows(rows)
    if not counts and not rows:
        return {}

    total = sum(int(item["count"]) for item in _ordered_counts(counts))
    if total <= 0:
        total = len(rows)
    transactable_count = _safe_count(counts.get("transactable"))
    sku_word = "SKU" if total == 1 else "SKUs"
    verb = "is" if transactable_count == 1 else "are"
    if transactable_count:
        headline = f"{transactable_count} of {total} audited {sku_word} {verb} confirmed transactable."
    else:
        no_verb = "is" if total == 1 else "are"
        headline = f"No audited {sku_word} {no_verb} confirmed transactable yet."

    attention_rows = [row for row in rows if row.get("status") != "transactable"]
    transactable_rows = [row for row in rows if row.get("status") == "transactable"]

    return {
        "headline": headline,
        "definition": (
            "Transactable means serving eligibility, explicit available-stock signal, "
            "merchant-checkout offer, merchant execute readiness, and public agent "
            "purchase support are all confirmed."
        ),
        "counts": _ordered_counts(counts),
        "transactable_rows": transactable_rows[:5],
        "attention_rows": attention_rows[:8],
    }
