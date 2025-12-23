from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.pcs_hash import sha256_json


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_manifest_sha256(manifest: Dict[str, Any]) -> str:
    to_hash = dict(manifest)
    to_hash.pop("manifest_sha256", None)
    to_hash.pop("manifest_signature", None)
    return sha256_json(to_hash)


def build_dispute_pack_manifest(
    *,
    merchant_id: str,
    dispute_ref: str,
    order: Optional[Dict[str, Any]],
    dispute_payload: Dict[str, Any],
    status: str,
    policy_rows: List[Dict[str, Any]],
    audit_refs: List[Dict[str, Any]],
    triggered_by: str,
) -> Dict[str, Any]:
    """
    Pure builder for a PCS v0.1 dispute_pack manifest (no DB dependency).
    """
    order_ref = None
    if order:
        order_ref = {
            "order_id": order.get("order_id"),
            "shopify_order_id": order.get("shopify_order_id"),
            "created_at": order.get("created_at").isoformat() if order.get("created_at") else None,
            "paid_at": order.get("paid_at").isoformat() if order.get("paid_at") else None,
            "currency": order.get("currency"),
            "total": str(order.get("total")),
            "items_count": len(order.get("items") or []) if isinstance(order.get("items"), list) else None,
        }

    fulfillment_proof = {"tracking_number": None, "carrier": None, "status": "unknown"}
    if order:
        fulfillment_proof = {
            "tracking_number": order.get("tracking_number"),
            "carrier": (order.get("fulfillment_provider") or order.get("carrier") or None),
            "status": order.get("fulfillment_status") or "unknown",
        }

    now_iso = _utc_now_iso()
    manifest: Dict[str, Any] = {
        "schema_version": "0.1",
        "effective_from": "2025-01-01T00:00:00Z",
        "source": "pivota_derived",
        "last_updated_at": now_iso,
        "pack_type": "dispute_pack",
        "pack_version": 1,
        "status": "frozen" if status == "frozen" else "draft",
        "generated_at": now_iso,
        "frozen_at": now_iso if status == "frozen" else None,
        "merchant": {"merchant_id": merchant_id},
        "dispute": {
            "dispute_ref": dispute_ref,
            "raw_status": dispute_payload.get("status"),
            "reason": dispute_payload.get("reason"),
            "amount": dispute_payload.get("amount"),
            "currency": dispute_payload.get("currency"),
            "occurred_at": dispute_payload.get("created_at") or dispute_payload.get("occurred_at"),
        },
        "order_ref": order_ref,
        "policy_snapshot": {
            "captured_at": now_iso,
            "policies": [
                {
                    "policy_type": r.get("policy_type"),
                    "url": r.get("url"),
                    "hash_sha256": r.get("hash_sha256"),
                    "updated_at": (r.get("updated_at").isoformat() if r.get("updated_at") else None),
                }
                for r in (policy_rows or [])
            ],
        },
        "fulfillment_proof": fulfillment_proof,
        "audit_trail": {
            "source": "mvp_events",
            "events": audit_refs,
        },
        "support_timeline": {"source_system": "unknown", "notes": "TODO: integrate support timeline if present"},
        "assets": [],
        "manifest_sha256": "",
        "triggered_by": triggered_by,
    }
    manifest["manifest_sha256"] = _compute_manifest_sha256(manifest)
    return manifest

