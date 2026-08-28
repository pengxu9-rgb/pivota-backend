from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import stripe

from config.settings import settings
from services.pcs_hash import sha256_json
from utils.logger import logger


def _utc_from_unix_ts(value: Any) -> Optional[datetime]:
    try:
        if value is None:
            return None
        ts = float(value)
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        pass
    try:
        return dict(row).get(key)
    except Exception:
        return None


def _extract_str_meta(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in value.items():
        if k is None:
            continue
        ks = str(k).strip()
        if not ks:
            continue
        if v is None:
            continue
        out[ks] = str(v)
    return out


def _ensure_stripe_api_key() -> bool:
    """
    Ensure stripe.api_key is configured for best-effort lookups.
    Returns True when configured.
    """
    try:
        if stripe.api_key:
            return True
    except Exception:
        pass
    if settings.stripe_secret_key:
        try:
            stripe.api_key = settings.stripe_secret_key
            return True
        except Exception:
            return False
    return False


def _as_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            return {}
    try:
        return dict(obj)
    except Exception:
        return {}


def _extract_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    if isinstance(value, dict):
        v = str(value.get("id") or "").strip()
        return v or None
    v = str(getattr(value, "id", "") or "").strip()
    return v or None


def _stripe_charge_lookup_best_effort(charge_id: str) -> Dict[str, Any]:
    if not charge_id:
        return {}
    if not _ensure_stripe_api_key():
        return {}
    try:
        ch = stripe.Charge.retrieve(charge_id)
        return _as_dict(ch)
    except Exception:
        return {}


def _stripe_payment_intent_lookup_best_effort(payment_intent_id: str) -> Dict[str, Any]:
    if not payment_intent_id:
        return {}
    if not _ensure_stripe_api_key():
        return {}
    try:
        pi = stripe.PaymentIntent.retrieve(payment_intent_id)
        return _as_dict(pi)
    except Exception:
        return {}


def _normalize_dispute_status(*, source: str, raw: Optional[str], event_type: Optional[str] = None) -> str:
    """
    Normalize dispute status to a small set that ops can reason about.
    Never raises.
    """
    s = (raw or "").strip().lower()
    evt = (event_type or "").strip().lower()
    if not s:
        if source == "stripe" and evt == "charge.dispute.created":
            return "needs_response"
        if source == "stripe" and evt == "charge.dispute.funds_withdrawn":
            return "under_review"
        if source == "stripe" and evt == "charge.dispute.closed":
            return "closed"
        return "open"

    if source == "stripe":
        if s in {"needs_response", "warning_needs_response"}:
            return "needs_response"
        if s in {"funds_withdrawn"} or evt == "charge.dispute.funds_withdrawn":
            return "under_review"
        if s in {"under_review", "warning_under_review"}:
            return "under_review"
        if s in {"won"}:
            return "won"
        if s in {"lost"}:
            return "lost"
        if s in {"warning_closed", "closed", "resolved"} or evt == "charge.dispute.closed":
            return "closed"
        # Fallback: treat unknown statuses as open.
        return "open"

    if source == "shopify":
        # Shopify Payments disputes statuses vary by payload shape; we best-effort map.
        if s in {"open", "needs_response", "action_required"}:
            return "needs_response" if s != "open" else "open"
        if s in {"under_review", "review", "submitted"}:
            return "under_review"
        if s in {"won"}:
            return "won"
        if s in {"lost"}:
            return "lost"
        if s in {"closed", "resolved"}:
            return "closed"
        return "open"

    return "open"


def stripe_dispute_phase(*, raw: Optional[str], event_type: Optional[str]) -> str:
    evt = (event_type or "").strip().lower()
    status_raw = (raw or "").strip().lower()

    if status_raw in {"won", "lost", "closed", "resolved"} or evt == "charge.dispute.closed":
        return "closed"
    if status_raw == "warning_closed":
        return "warning_closed"
    if evt == "charge.dispute.funds_withdrawn" or status_raw == "funds_withdrawn":
        return "funds_withdrawn"
    if status_raw == "warning_under_review":
        return "warning_under_review"
    if status_raw == "under_review":
        return "under_review"
    if status_raw == "warning_needs_response":
        return "warning_needs_response"
    if status_raw == "needs_response" or evt == "charge.dispute.created":
        return "needs_response"
    return "open"


def stripe_dispute_outcome(*, raw: Optional[str], event_type: Optional[str]) -> Optional[str]:
    status_raw = (raw or "").strip().lower()
    evt = (event_type or "").strip().lower()
    if status_raw in {"won", "lost"}:
        return status_raw
    if evt == "charge.dispute.closed":
        return "closed"
    if status_raw in {"closed", "resolved"}:
        return "closed"
    return None


def _stripe_dispute_timeline_rank(
    *,
    raw: Optional[str],
    event_type: Optional[str],
    normalized: Optional[str] = None,
) -> int:
    status = normalized or _normalize_dispute_status(source="stripe", raw=raw, event_type=event_type)
    phase = stripe_dispute_phase(raw=raw, event_type=event_type)
    outcome = stripe_dispute_outcome(raw=raw, event_type=event_type)

    if outcome in {"won", "lost"} or status in {"won", "lost"}:
        return 40
    if phase == "closed":
        return 30
    if phase == "warning_closed":
        return 25
    if phase in {"funds_withdrawn", "warning_under_review", "under_review"}:
        return 20
    if phase == "warning_needs_response":
        return 15
    if phase in {"needs_response", "open"}:
        return 10
    return 0


def stripe_dispute_status_detail(*, raw: Optional[str], event_type: Optional[str]) -> Dict[str, Any]:
    normalized_status = _normalize_dispute_status(source="stripe", raw=raw, event_type=event_type)
    phase = stripe_dispute_phase(raw=raw, event_type=event_type)
    outcome = stripe_dispute_outcome(raw=raw, event_type=event_type)
    timeline_rank = _stripe_dispute_timeline_rank(
        raw=raw,
        event_type=event_type,
        normalized=normalized_status,
    )
    return {
        "normalized_status": normalized_status,
        "phase": phase,
        "outcome": outcome,
        "timeline_rank": timeline_rank,
        "pack_status": "frozen" if timeline_rank >= 20 else "draft",
    }


def stripe_dispute_pack_status(*, raw: Optional[str], event_type: Optional[str]) -> str:
    return str(stripe_dispute_status_detail(raw=raw, event_type=event_type)["pack_status"])


def _as_optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes"}:
            return True
        if v in {"false", "0", "no"}:
            return False
    return None


def _as_optional_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def stripe_dispute_evidence_summary(*, dispute: Dict[str, Any], event_type: Optional[str]) -> Dict[str, Any]:
    dispute = dispute if isinstance(dispute, dict) else {}
    evidence_details = dispute.get("evidence_details")
    evidence_details = evidence_details if isinstance(evidence_details, dict) else {}

    detail = stripe_dispute_status_detail(
        raw=str(dispute.get("status") or "").strip() or None,
        event_type=event_type,
    )
    has_evidence = _as_optional_bool(evidence_details.get("has_evidence"))
    submission_count = _as_optional_int(evidence_details.get("submission_count"))
    past_due = _as_optional_bool(evidence_details.get("past_due"))
    due_by_dt = _utc_from_unix_ts(evidence_details.get("due_by"))
    due_by = due_by_dt.isoformat() if due_by_dt else None
    submission_method = str(evidence_details.get("submission_method") or "").strip() or None

    if detail["outcome"] == "won":
        stage = "closed_won"
    elif detail["outcome"] == "lost":
        stage = "closed_lost"
    elif detail["phase"] in {"closed", "warning_closed"}:
        stage = detail["phase"]
    elif detail["phase"] in {"under_review", "warning_under_review", "funds_withdrawn"}:
        stage = "issuer_review"
    elif has_evidence or (submission_count or 0) > 0:
        stage = "evidence_submitted"
    elif detail["phase"] in {"needs_response", "warning_needs_response", "open"}:
        stage = "awaiting_submission"
    else:
        stage = detail["phase"]

    return {
        "stage": stage,
        "has_evidence": has_evidence,
        "submission_count": submission_count,
        "submission_method": submission_method,
        "past_due": past_due,
        "due_by": due_by,
    }


def _merge_stripe_dispute_state(
    *,
    existing_status_raw: Optional[str],
    existing_status: Optional[str],
    incoming_status_raw: Optional[str],
    incoming_status: str,
    event_type: Optional[str],
) -> Tuple[Optional[str], str]:
    existing_rank = _stripe_dispute_timeline_rank(
        raw=existing_status_raw,
        event_type=None,
        normalized=existing_status,
    )
    incoming_rank = _stripe_dispute_timeline_rank(
        raw=incoming_status_raw,
        event_type=event_type,
        normalized=incoming_status,
    )
    if incoming_rank < existing_rank:
        return existing_status_raw, existing_status or incoming_status
    return incoming_status_raw, incoming_status


async def _resolve_order_and_merchant_from_stripe_payload(
    payload: Dict[str, Any],
    *,
    payment_intent_id: Optional[str],
    order_id_hint: Optional[str],
    merchant_id_hint: Optional[str],
    db,
    merchant_scope: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (order_id, merchant_id) best-effort.

    `merchant_scope`, when set, is the merchant that owns the webhook endpoint the
    event arrived on. Every candidate identity here is otherwise derived from
    attacker-controllable event metadata or from an UNSCOPED `orders` lookup, so
    under a scope: the merchant is pinned to the scope, and an order is accepted
    only if the DB confirms it belongs to that merchant. Callers that have no
    endpoint owner (the bare platform-wide endpoint) pass None and keep the
    historical best-effort behaviour.
    """
    scope = (merchant_scope or "").strip() or None
    order_id = (order_id_hint or "").strip() or None
    merchant_id = (merchant_id_hint or "").strip() or None

    if scope:
        # The scope is authoritative; a metadata-supplied merchant never wins.
        merchant_id = scope
        if order_id:
            row = await db.fetch_one(
                "SELECT order_id FROM orders WHERE order_id = :order_id AND merchant_id = :merchant_id LIMIT 1",
                {"order_id": order_id, "merchant_id": scope},
            )
            if not row:
                order_id = None
        if not order_id and payment_intent_id:
            row = await db.fetch_one(
                "SELECT order_id FROM orders WHERE payment_intent_id = :pi AND merchant_id = :merchant_id LIMIT 1",
                {"pi": payment_intent_id, "merchant_id": scope},
            )
            if row:
                order_id = str(_row_get(row, "order_id") or "") or None
        return order_id, merchant_id

    if order_id and merchant_id:
        return order_id, merchant_id

    if order_id and not merchant_id:
        row = await db.fetch_one(
            "SELECT order_id, merchant_id FROM orders WHERE order_id = :order_id LIMIT 1",
            {"order_id": order_id},
        )
        if row:
            return str(_row_get(row, "order_id") or order_id), str(_row_get(row, "merchant_id") or "") or None

    if payment_intent_id:
        row = await db.fetch_one(
            "SELECT order_id, merchant_id FROM orders WHERE payment_intent_id = :pi LIMIT 1",
            {"pi": payment_intent_id},
        )
        if row:
            return str(_row_get(row, "order_id") or ""), str(_row_get(row, "merchant_id") or "")

    # Final fallback: try metadata on payload
    meta = payload.get("metadata") or {}
    if isinstance(meta, dict):
        order_id = order_id or (str(meta.get("order_id") or "") or None)
        merchant_id = merchant_id or (str(meta.get("merchant_id") or "") or None)
    return order_id, merchant_id


async def upsert_stripe_dispute_record_best_effort(
    dispute: Dict[str, Any],
    *,
    event_type: Optional[str],
    order_id_hint: Optional[str] = None,
    merchant_id_hint: Optional[str] = None,
    db=None,
    merchant_scope: Optional[str] = None,
) -> None:
    """
    Best-effort upsert of a Stripe dispute snapshot into dispute_records.
    Never raises.

    `merchant_scope` is the merchant owning the webhook endpoint the event came in
    on; when set it pins the resolved merchant and confines every `orders` lookup
    to that tenant. See `_resolve_order_and_merchant_from_stripe_payload`.
    """
    if db is None:
        try:
            from db.database import database as db
        except Exception:
            return

    if not isinstance(dispute, dict):
        return

    source_dispute_id = str(dispute.get("id") or "").strip()
    if not source_dispute_id:
        source_dispute_id = f"sha256:{sha256_json(dispute)}"

    raw_status = str(dispute.get("status") or "").strip() or None
    status = _normalize_dispute_status(source="stripe", raw=raw_status, event_type=event_type)

    payment_intent_id = str(dispute.get("payment_intent") or "").strip() or None
    charge_id = str(dispute.get("charge") or "").strip() or None

    meta = _extract_str_meta(dispute.get("metadata") or {})
    order_id_hint = order_id_hint or (str(meta.get("order_id") or "").strip() or None)
    merchant_id_hint = merchant_id_hint or (str(meta.get("merchant_id") or "").strip() or None)

    # Stripe disputes do not always include payment_intent/metadata; best-effort enrich from charge -> payment_intent.
    if (not payment_intent_id) and charge_id:
        charge_obj = _stripe_charge_lookup_best_effort(charge_id)
        payment_intent_id = payment_intent_id or _extract_id(charge_obj.get("payment_intent"))
        charge_meta = _extract_str_meta(charge_obj.get("metadata") or {})
        order_id_hint = order_id_hint or (charge_meta.get("order_id") or None)
        merchant_id_hint = merchant_id_hint or (charge_meta.get("merchant_id") or None)

    if payment_intent_id and (not order_id_hint or not merchant_id_hint):
        pi_obj = _stripe_payment_intent_lookup_best_effort(payment_intent_id)
        pi_meta = _extract_str_meta(pi_obj.get("metadata") or {})
        order_id_hint = order_id_hint or (pi_meta.get("order_id") or None)
        merchant_id_hint = merchant_id_hint or (pi_meta.get("merchant_id") or None)

    try:
        order_id, merchant_id = await _resolve_order_and_merchant_from_stripe_payload(
            dispute,
            payment_intent_id=payment_intent_id,
            order_id_hint=order_id_hint,
            merchant_id_hint=merchant_id_hint,
            db=db,
            merchant_scope=merchant_scope,
        )
    except Exception:
        order_id, merchant_id = None, None

    existing_row = None
    try:
        existing_row = await db.fetch_one(
            """
            SELECT status_raw, status, closed_at
            FROM dispute_records
            WHERE source = 'stripe' AND source_dispute_id = :source_dispute_id
            LIMIT 1
            """,
            {"source_dispute_id": source_dispute_id},
        )
    except Exception:
        existing_row = None

    if not merchant_id:
        logger.warning(
            "Stripe dispute received but merchant_id could not be resolved; skipping persist "
            "(dispute_id=%s payment_intent_id=%s charge_id=%s event_type=%s order_hint=%s)",
            source_dispute_id,
            payment_intent_id,
            charge_id,
            event_type,
            order_id_hint,
        )
        return

    amount = dispute.get("amount")
    currency = dispute.get("currency")
    reason = dispute.get("reason")

    merged_status_raw, merged_status = _merge_stripe_dispute_state(
        existing_status_raw=_row_get(existing_row, "status_raw"),
        existing_status=_row_get(existing_row, "status"),
        incoming_status_raw=raw_status,
        incoming_status=status,
        event_type=event_type,
    )

    evidence_due_by = None
    try:
        evidence_details = dispute.get("evidence_details") or {}
        if isinstance(evidence_details, dict):
            evidence_due_by = _utc_from_unix_ts(evidence_details.get("due_by"))
    except Exception:
        evidence_due_by = None

    opened_at = _utc_from_unix_ts(dispute.get("created"))
    closed_at = None
    if merged_status in {"won", "lost", "closed"}:
        closed_at = _utc_from_unix_ts(dispute.get("closed")) or _row_get(existing_row, "closed_at")

    payload_json = dispute
    try:
        payload_json_str = json.dumps(payload_json, ensure_ascii=False)
    except Exception:
        payload_json_str = "{}"

    try:
        await db.execute(
            """
            INSERT INTO dispute_records (
              merchant_id, source, source_dispute_id,
              order_id, platform_order_id,
              payment_intent_id, charge_id,
              currency, amount, reason,
              status_raw, status,
              evidence_due_by, opened_at, closed_at,
              raw_payload, created_at, updated_at
            ) VALUES (
              :merchant_id, 'stripe', :source_dispute_id,
              :order_id, :platform_order_id,
              :payment_intent_id, :charge_id,
              :currency, :amount, :reason,
              :status_raw, :status,
              :evidence_due_by, :opened_at, :closed_at,
              CAST(:raw_payload AS jsonb), NOW(), NOW()
            )
            ON CONFLICT (source, source_dispute_id) DO UPDATE SET
              merchant_id = EXCLUDED.merchant_id,
              order_id = COALESCE(EXCLUDED.order_id, dispute_records.order_id),
              payment_intent_id = COALESCE(EXCLUDED.payment_intent_id, dispute_records.payment_intent_id),
              charge_id = COALESCE(EXCLUDED.charge_id, dispute_records.charge_id),
              currency = COALESCE(EXCLUDED.currency, dispute_records.currency),
              amount = COALESCE(EXCLUDED.amount, dispute_records.amount),
              reason = COALESCE(EXCLUDED.reason, dispute_records.reason),
              status_raw = EXCLUDED.status_raw,
              status = EXCLUDED.status,
              evidence_due_by = COALESCE(EXCLUDED.evidence_due_by, dispute_records.evidence_due_by),
              opened_at = COALESCE(EXCLUDED.opened_at, dispute_records.opened_at),
              closed_at = COALESCE(EXCLUDED.closed_at, dispute_records.closed_at),
              raw_payload = EXCLUDED.raw_payload,
              updated_at = NOW()
            """,
            {
                "merchant_id": merchant_id,
                "source_dispute_id": source_dispute_id,
                "order_id": order_id,
                "platform_order_id": None,
                "payment_intent_id": payment_intent_id,
                "charge_id": charge_id,
                "currency": str(currency).upper() if currency else None,
                "amount": (float(amount) / 100.0) if isinstance(amount, (int, float)) else None,
                "reason": str(reason) if reason else None,
                "status_raw": merged_status_raw,
                "status": merged_status,
                "evidence_due_by": evidence_due_by,
                "opened_at": opened_at,
                "closed_at": closed_at,
                "raw_payload": payload_json_str,
            },
        )
    except Exception as e:
        logger.warning(
            "Failed to upsert stripe dispute record (merchant_id=%s dispute_id=%s): %s",
            merchant_id,
            source_dispute_id,
            str(e),
        )


async def upsert_shopify_dispute_record_best_effort(
    *,
    merchant_id: str,
    payload: Dict[str, Any],
    topic: Optional[str],
    db=None,
) -> None:
    """
    Best-effort upsert for Shopify disputes webhooks.
    """
    if db is None:
        try:
            from db.database import database as db
        except Exception:
            return

    if not merchant_id:
        return
    if not isinstance(payload, dict):
        return

    source_dispute_id = str(payload.get("id") or payload.get("dispute_id") or "").strip()
    if not source_dispute_id:
        source_dispute_id = f"sha256:{sha256_json(payload)}"

    raw_status = str(payload.get("status") or payload.get("state") or "").strip() or None
    status = _normalize_dispute_status(source="shopify", raw=raw_status)

    platform_order_id = str(payload.get("order_id") or payload.get("shopify_order_id") or "").strip() or None

    order_id = None
    if platform_order_id:
        try:
            row = await db.fetch_one(
                "SELECT order_id FROM orders WHERE merchant_id = :merchant_id AND shopify_order_id = :sid LIMIT 1",
                {"merchant_id": merchant_id, "sid": platform_order_id},
            )
            if row:
                order_id = str(_row_get(row, "order_id") or "") or None
        except Exception:
            order_id = None

    payload_json_str = json.dumps(payload or {}, ensure_ascii=False)

    try:
        await db.execute(
            """
            INSERT INTO dispute_records (
              merchant_id, source, source_dispute_id,
              order_id, platform_order_id,
              currency, amount, reason,
              status_raw, status,
              raw_payload, created_at, updated_at
            ) VALUES (
              :merchant_id, 'shopify', :source_dispute_id,
              :order_id, :platform_order_id,
              :currency, :amount, :reason,
              :status_raw, :status,
              CAST(:raw_payload AS jsonb), NOW(), NOW()
            )
            ON CONFLICT (source, source_dispute_id) DO UPDATE SET
              order_id = COALESCE(EXCLUDED.order_id, dispute_records.order_id),
              platform_order_id = COALESCE(EXCLUDED.platform_order_id, dispute_records.platform_order_id),
              currency = COALESCE(EXCLUDED.currency, dispute_records.currency),
              amount = COALESCE(EXCLUDED.amount, dispute_records.amount),
              reason = COALESCE(EXCLUDED.reason, dispute_records.reason),
              status_raw = EXCLUDED.status_raw,
              status = EXCLUDED.status,
              raw_payload = EXCLUDED.raw_payload,
              updated_at = NOW()
            """,
            {
                "merchant_id": merchant_id,
                "source_dispute_id": source_dispute_id,
                "order_id": order_id,
                "platform_order_id": platform_order_id,
                "currency": str(payload.get("currency") or "").upper() or None,
                "amount": payload.get("amount"),
                "reason": str(payload.get("reason") or "") or None,
                "status_raw": raw_status,
                "status": status,
                "raw_payload": payload_json_str,
            },
        )
    except Exception as e:
        logger.warning(
            "Failed to upsert shopify dispute record (merchant_id=%s dispute_id=%s topic=%s): %s",
            merchant_id,
            source_dispute_id,
            topic,
            str(e),
        )
