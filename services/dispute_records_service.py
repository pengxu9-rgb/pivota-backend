from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

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


def _normalize_dispute_status(*, source: str, raw: Optional[str]) -> str:
    """
    Normalize dispute status to a small set that ops can reason about.
    Never raises.
    """
    s = (raw or "").strip().lower()
    if not s:
        return "open"

    if source == "stripe":
        if s in {"needs_response", "warning_needs_response"}:
            return "needs_response"
        if s in {"under_review", "warning_under_review"}:
            return "under_review"
        if s in {"won"}:
            return "won"
        if s in {"lost"}:
            return "lost"
        if s in {"warning_closed"}:
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


async def _resolve_order_and_merchant_from_stripe_payload(
    payload: Dict[str, Any],
    *,
    payment_intent_id: Optional[str],
    order_id_hint: Optional[str],
    merchant_id_hint: Optional[str],
    db,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (order_id, merchant_id) best-effort.
    """
    order_id = (order_id_hint or "").strip() or None
    merchant_id = (merchant_id_hint or "").strip() or None

    if order_id and merchant_id:
        return order_id, merchant_id

    if order_id and not merchant_id:
        row = await db.fetch_one(
            "SELECT order_id, merchant_id FROM orders WHERE order_id = :order_id LIMIT 1",
            {"order_id": order_id},
        )
        if row:
            return str(row.get("order_id") or order_id), str(row.get("merchant_id") or "") or None

    if payment_intent_id:
        row = await db.fetch_one(
            "SELECT order_id, merchant_id FROM orders WHERE payment_intent_id = :pi LIMIT 1",
            {"pi": payment_intent_id},
        )
        if row:
            return str(row.get("order_id") or ""), str(row.get("merchant_id") or "")

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
    db=None,
) -> None:
    """
    Best-effort upsert of a Stripe dispute snapshot into dispute_records.
    Never raises.
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
    status = _normalize_dispute_status(source="stripe", raw=raw_status)

    payment_intent_id = str(dispute.get("payment_intent") or "").strip() or None
    charge_id = str(dispute.get("charge") or "").strip() or None

    meta = dispute.get("metadata") or {}
    order_id_hint = str(meta.get("order_id") or "").strip() if isinstance(meta, dict) else None
    merchant_id_hint = str(meta.get("merchant_id") or "").strip() if isinstance(meta, dict) else None

    try:
        order_id, merchant_id = await _resolve_order_and_merchant_from_stripe_payload(
            dispute,
            payment_intent_id=payment_intent_id,
            order_id_hint=order_id_hint,
            merchant_id_hint=merchant_id_hint,
            db=db,
        )
    except Exception:
        order_id, merchant_id = None, None

    if not merchant_id:
        logger.warning(
            "Stripe dispute received but merchant_id could not be resolved; skipping persist "
            "(dispute_id=%s payment_intent_id=%s event_type=%s)",
            source_dispute_id,
            payment_intent_id,
            event_type,
        )
        return

    amount = dispute.get("amount")
    currency = dispute.get("currency")
    reason = dispute.get("reason")

    evidence_due_by = None
    try:
        evidence_details = dispute.get("evidence_details") or {}
        if isinstance(evidence_details, dict):
            evidence_due_by = _utc_from_unix_ts(evidence_details.get("due_by"))
    except Exception:
        evidence_due_by = None

    opened_at = _utc_from_unix_ts(dispute.get("created"))
    closed_at = None
    if status in {"won", "lost", "closed"}:
        closed_at = _utc_from_unix_ts(dispute.get("closed"))

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
                "status_raw": raw_status,
                "status": status,
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
                order_id = str(row.get("order_id") or "") or None
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
