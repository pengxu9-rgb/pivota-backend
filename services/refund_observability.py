from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _as_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            parsed = value.to_dict()
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    if hasattr(value, "__dict__"):
        try:
            parsed = dict(value.__dict__)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {}


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _iso_timestamp(value: Any = None) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    text = _clean_str(value)
    if text:
        return text
    return datetime.now(timezone.utc).isoformat()


def _tracking_reference_kind(reference_type: Optional[str]) -> Optional[str]:
    normalized = str(reference_type or "").strip().lower()
    mapping = {
        "acquirer_reference_number": "ARN",
        "system_trace_audit_number": "STAN",
        "retrieval_reference_number": "RRN",
    }
    return mapping.get(normalized)


def _append_unique(target: List[str], value: Any) -> None:
    text = _clean_str(value)
    if not text:
        return
    if text not in target:
        target.append(text)


def _public_refund_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}

    keys = (
        "provider",
        "refund_id",
        "status",
        "pending_reason",
        "failure_reason",
        "amount_minor",
        "currency",
        "payment_intent_id",
        "reason",
        "destination_type",
        "destination_entry_type",
        "is_reversal",
        "reference",
        "reference_status",
        "reference_type",
        "tracking_reference_kind",
        "network_decline_code",
        "source_event",
        "observed_at",
    )
    return {key: snapshot.get(key) for key in keys if snapshot.get(key) is not None}


def extract_stripe_refund_snapshot(
    refund_obj: Any,
    *,
    source_event: Optional[str] = None,
    observed_at: Any = None,
) -> Dict[str, Any]:
    refund_dict = _as_dict(refund_obj)
    refund_id = _clean_str(_get(refund_obj, "id") or refund_dict.get("id"))
    status = _clean_str(_get(refund_obj, "status") or refund_dict.get("status"))
    currency = _clean_str(_get(refund_obj, "currency") or refund_dict.get("currency"))
    payment_intent_id = _clean_str(
        _get(refund_obj, "payment_intent") or refund_dict.get("payment_intent")
    )
    reason = _clean_str(_get(refund_obj, "reason") or refund_dict.get("reason"))
    pending_reason = _clean_str(
        _get(refund_obj, "pending_reason") or refund_dict.get("pending_reason")
    )
    failure_reason = _clean_str(
        _get(refund_obj, "failure_reason") or refund_dict.get("failure_reason")
    )
    amount_minor = _get(refund_obj, "amount", refund_dict.get("amount"))

    destination_details = _as_dict(
        _get(refund_obj, "destination_details") or refund_dict.get("destination_details")
    )
    destination_type = _clean_str(destination_details.get("type"))
    destination_entry = (
        _as_dict(destination_details.get(destination_type))
        if destination_type
        else {}
    )

    reference = _clean_str(destination_entry.get("reference"))
    reference_status = _clean_str(destination_entry.get("reference_status"))
    reference_type = _clean_str(destination_entry.get("reference_type"))
    destination_entry_type = _clean_str(destination_entry.get("type"))
    network_decline_code = _clean_str(destination_entry.get("network_decline_code"))

    return {
        "provider": "stripe",
        "refund_id": refund_id,
        "status": status.lower() if status else None,
        "pending_reason": pending_reason,
        "failure_reason": failure_reason,
        "amount_minor": int(amount_minor) if isinstance(amount_minor, int) else amount_minor,
        "currency": currency.upper() if currency else None,
        "payment_intent_id": payment_intent_id,
        "reason": reason,
        "destination_type": destination_type,
        "destination_entry_type": destination_entry_type,
        "is_reversal": destination_entry_type == "reversal",
        "reference": reference,
        "reference_status": reference_status,
        "reference_type": reference_type,
        "tracking_reference_kind": _tracking_reference_kind(reference_type),
        "network_decline_code": network_decline_code,
        "source_event": _clean_str(source_event),
        "observed_at": _iso_timestamp(observed_at),
    }


def stripe_refund_metadata_patch(
    snapshot: Dict[str, Any],
    *,
    existing_metadata: Any = None,
) -> Dict[str, Any]:
    public_snapshot = _public_refund_snapshot(snapshot)
    refund_id = _clean_str(public_snapshot.get("refund_id"))
    existing_statuses = _as_dict(_as_dict(existing_metadata).get("stripe_refund_statuses"))
    patch: Dict[str, Any] = {
        "stripe_refund_status": public_snapshot,
    }

    statuses = dict(existing_statuses)
    if refund_id:
        statuses[refund_id] = public_snapshot
    if statuses:
        patch["stripe_refund_statuses"] = statuses

    if public_snapshot:
        patch["stripe_last_refund"] = public_snapshot

    return patch


def merge_refund_metadata(
    existing_metadata: Any,
    patch: Dict[str, Any],
) -> Dict[str, Any]:
    merged = _as_dict(existing_metadata)
    if not patch:
        return merged

    statuses = _as_dict(merged.get("stripe_refund_statuses"))
    patch_statuses = _as_dict(patch.get("stripe_refund_statuses"))
    if patch_statuses:
        statuses.update(patch_statuses)

    for key, value in patch.items():
        if key == "stripe_refund_statuses":
            continue
        merged[key] = value

    if statuses:
        merged["stripe_refund_statuses"] = statuses
    return merged


def build_order_refund_tracking_payload(
    order_or_metadata: Any,
    *,
    psp_used: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if isinstance(order_or_metadata, dict) and "metadata" in order_or_metadata:
        metadata = _as_dict(order_or_metadata.get("metadata"))
    else:
        metadata = _as_dict(order_or_metadata)
    provider = _clean_str(psp_used)

    latest = _as_dict(metadata.get("stripe_refund_status"))
    if not latest:
        latest = _as_dict(metadata.get("stripe_last_refund"))
    if not latest:
        latest = _as_dict(metadata.get("stripe_last_refund_failure"))
    if not latest:
        generic_last = _as_dict(metadata.get("last_refund"))
        if generic_last:
            latest = {
                "provider": _clean_str(generic_last.get("psp")) or provider,
                "refund_id": _clean_str(
                    generic_last.get("refund_id") or generic_last.get("refund_reference")
                ),
                "status": _clean_str(
                    generic_last.get("status") or generic_last.get("refund_status")
                ),
                "amount_minor": generic_last.get("amount_minor"),
                "currency": _clean_str(generic_last.get("currency")),
                "observed_at": _iso_timestamp(generic_last.get("received_at")),
            }

    history_map = _as_dict(metadata.get("stripe_refund_statuses"))
    history = [
        _public_refund_snapshot(_as_dict(snapshot))
        for snapshot in history_map.values()
        if isinstance(snapshot, dict)
    ]
    history = [item for item in history if item]
    history.sort(key=lambda item: str(item.get("observed_at") or ""), reverse=True)

    public_latest = _public_refund_snapshot(latest)
    if public_latest:
        provider = _clean_str(public_latest.get("provider")) or provider

    if not public_latest and not history:
        return None

    return {
        "provider": provider,
        "latest": public_latest or None,
        "history": history,
    }


def collect_refund_ids(
    metadata_like: Any,
    *,
    provider: str = "stripe",
) -> List[str]:
    metadata = _as_dict(metadata_like)
    provider_norm = str(provider or "").strip().lower()
    ids: List[str] = []

    latest = _as_dict(metadata.get(f"{provider_norm}_refund_status"))
    _append_unique(ids, latest.get("refund_id"))

    latest = _as_dict(metadata.get(f"{provider_norm}_last_refund"))
    _append_unique(ids, latest.get("refund_id"))

    latest_failure = _as_dict(metadata.get(f"{provider_norm}_last_refund_failure"))
    _append_unique(ids, latest_failure.get("refund_id"))

    statuses = _as_dict(metadata.get(f"{provider_norm}_refund_statuses"))
    for refund_id in statuses.keys():
        _append_unique(ids, refund_id)
        snapshot = _as_dict(statuses.get(refund_id))
        _append_unique(ids, snapshot.get("refund_id"))

    psp_refund_records = _as_dict(metadata.get("psp_refund_records"))
    for record in psp_refund_records.values():
        refund_record = _as_dict(record)
        if str(refund_record.get("psp") or "").strip().lower() != provider_norm:
            continue
        _append_unique(ids, refund_record.get("refund_id"))
        _append_unique(ids, refund_record.get("refund_reference"))

    last_refund = _as_dict(metadata.get("last_refund"))
    if str(last_refund.get("psp") or "").strip().lower() == provider_norm:
        _append_unique(ids, last_refund.get("refund_id"))
        _append_unique(ids, last_refund.get("refund_reference"))

    if provider_norm == "stripe":
        ids = [refund_id for refund_id in ids if refund_id.startswith("re_")]

    return ids
