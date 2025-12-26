from __future__ import annotations

import json
from typing import Any, Dict, Optional

from services.pcs_hash import sha256_json
from utils.logger import logger


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


def _normalize_return_status(raw: Optional[str]) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "open"
    if s in {"open", "requested", "in_progress", "processing"}:
        return "open"
    if s in {"closed", "completed"}:
        return "closed"
    if s in {"cancelled", "canceled"}:
        return "cancelled"
    return "open"


def _extract_shopify_return_id(payload: Dict[str, Any]) -> str:
    rid = payload.get("id") or payload.get("return_id") or payload.get("returnId")
    if rid is None:
        return f"sha256:{sha256_json(payload)}"
    return str(rid)


def _extract_shopify_return_items(payload: Dict[str, Any]) -> Any:
    # Shopify return payload shapes vary; keep best-effort items list for ops.
    for key in ("return_line_items", "returnLineItems", "line_items", "lineItems", "items"):
        if key in payload:
            return payload.get(key)
    return []


async def upsert_shopify_return_record_best_effort(
    *,
    merchant_id: str,
    payload: Dict[str, Any],
    topic: Optional[str],
    db=None,
) -> None:
    if db is None:
        try:
            from db.database import database as db
        except Exception:
            return

    if not merchant_id:
        return
    if not isinstance(payload, dict):
        return

    source_return_id = _extract_shopify_return_id(payload)

    raw_status = str(payload.get("status") or payload.get("state") or "").strip() or None
    status = _normalize_return_status(raw_status)

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

    items_json = _extract_shopify_return_items(payload)
    refund_status_raw = payload.get("refund_status") or payload.get("refundStatus")

    try:
        payload_json_str = json.dumps(payload or {}, ensure_ascii=False)
    except Exception:
        payload_json_str = "{}"
    try:
        items_json_str = json.dumps(items_json or [], ensure_ascii=False)
    except Exception:
        items_json_str = "[]"

    try:
        await db.execute(
            """
            INSERT INTO return_records (
              merchant_id, source, source_return_id,
              order_id, platform_order_id,
              status_raw, status,
              refund_status_raw, items_json,
              raw_payload, created_at, updated_at
            ) VALUES (
              :merchant_id, 'shopify', :source_return_id,
              :order_id, :platform_order_id,
              :status_raw, :status,
              :refund_status_raw, CAST(:items_json AS jsonb),
              CAST(:raw_payload AS jsonb), NOW(), NOW()
            )
            ON CONFLICT (source, source_return_id) DO UPDATE SET
              order_id = COALESCE(EXCLUDED.order_id, return_records.order_id),
              platform_order_id = COALESCE(EXCLUDED.platform_order_id, return_records.platform_order_id),
              status_raw = EXCLUDED.status_raw,
              status = EXCLUDED.status,
              refund_status_raw = COALESCE(EXCLUDED.refund_status_raw, return_records.refund_status_raw),
              items_json = EXCLUDED.items_json,
              raw_payload = EXCLUDED.raw_payload,
              updated_at = NOW()
            """,
            {
                "merchant_id": merchant_id,
                "source_return_id": source_return_id,
                "order_id": order_id,
                "platform_order_id": platform_order_id,
                "status_raw": raw_status,
                "status": status,
                "refund_status_raw": str(refund_status_raw) if refund_status_raw is not None else None,
                "items_json": items_json_str,
                "raw_payload": payload_json_str,
            },
        )
    except Exception as e:
        logger.warning(
            "Failed to upsert shopify return record (merchant_id=%s return_id=%s topic=%s): %s",
            merchant_id,
            source_return_id,
            topic,
            str(e),
        )
