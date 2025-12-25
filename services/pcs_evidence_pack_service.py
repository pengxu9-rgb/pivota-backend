import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.database import database
from db.orders import get_order
from services.merchant_store_service import get_primary_store
from services.pcs_hash import sha256_json
from services.shopify_policy_service import fetch_and_store_shop_policies, get_latest_policy_hashes
from mvp.dispute_evidence import build_dispute_pack_manifest

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_policy_disclosure_hash(order_id: str, placed_at_iso: str, policy_hashes: List[str]) -> str:
    # Stable: order_id + placed_at + policy hashes (sorted)
    base = {"order_id": order_id, "placed_at": placed_at_iso, "policy_hashes": sorted(policy_hashes)}
    return sha256_json(base)


def _compute_manifest_sha256(manifest: Dict[str, Any]) -> str:
    """
    Compute a stable manifest hash without including the hash/signature fields themselves.
    """
    to_hash = dict(manifest)
    to_hash.pop("manifest_sha256", None)
    to_hash.pop("manifest_signature", None)
    return sha256_json(to_hash)


def _extract_pricing_quote_evidence(order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Best-effort: extract a quote-first pricing lock summary from order.metadata.

    Security/PII:
    - The stored `pricing_quote` snapshot should not contain PII; it contains pricing, item refs and discount codes.
    - We still treat this as best-effort and return None when shape is unexpected.
    """
    meta = order.get("metadata") or {}
    if not isinstance(meta, dict):
        return None

    pricing_quote = meta.get("pricing_quote")
    if not isinstance(pricing_quote, dict):
        return None

    quote_id = pricing_quote.get("quote_id")
    if not quote_id:
        return None

    quote_hash_sha256 = pricing_quote.get("quote_hash_sha256") or sha256_json(pricing_quote)

    return {
        "quote_id": quote_id,
        "expires_at": pricing_quote.get("expires_at"),
        "engine": pricing_quote.get("engine"),
        "engine_ref": pricing_quote.get("engine_ref"),
        "request_fingerprint": pricing_quote.get("request_fingerprint"),
        "quote_hash_sha256": quote_hash_sha256,
        "pricing": pricing_quote.get("pricing"),
        "promotion_lines": pricing_quote.get("promotion_lines") or [],
        "line_items": pricing_quote.get("line_items") or [],
    }


async def create_order_snapshot_evidence_pack(order_id: str, *, triggered_by: str) -> Optional[Dict[str, Any]]:
    """
    Create and freeze an EvidencePack v0.1 'order_snapshot' for an order (best-effort).
    This does not store payment credentials; it stores references + policy hashes.
    """
    order = await get_order(order_id)
    if not order:
        return None

    merchant_id = order["merchant_id"]

    # If an order_snapshot already exists (frozen), do nothing.
    existing = await database.fetch_one(
        """
        SELECT id, pack_version, status
        FROM pcs_evidence_packs
        WHERE merchant_id = :merchant_id AND order_id = :order_id AND pack_type = 'order_snapshot'
        ORDER BY pack_version DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "order_id": order_id},
    )
    if existing and existing["status"] == "frozen":
        return dict(existing)

    store_info = await get_primary_store(merchant_id)

    # Prefer using already stored policy snapshots to avoid adding latency on payment/webhook paths.
    latest_policy_rows = await get_latest_policy_hashes(merchant_id)
    if (not latest_policy_rows) and store_info and (store_info.get("platform") or "").lower() == "shopify":
        try:
            await fetch_and_store_shop_policies(
                merchant_id=merchant_id,
                shop_domain=store_info.get("domain"),
                access_token=store_info.get("api_key"),
                api_version="2024-07",
            )
            latest_policy_rows = await get_latest_policy_hashes(merchant_id)
        except Exception as e:
            logger.warning("PCS policy fetch failed merchant=%s order=%s: %s", merchant_id, order_id, e)
    policy_hashes = [r.get("hash_sha256") for r in latest_policy_rows if r.get("hash_sha256")]

    placed_at = order.get("paid_at") or order.get("created_at") or datetime.utcnow()
    placed_at_iso = placed_at.isoformat() if hasattr(placed_at, "isoformat") else str(placed_at)

    policy_disclosure_hash = _compute_policy_disclosure_hash(order_id, placed_at_iso, policy_hashes)

    # Best-effort mandate evidence references: this repo doesn't implement AP2, so we store internal refs.
    meta = order.get("metadata") or {}
    pivota_agent_id = meta.get("pivota_agent_id") or order.get("agent_id") or "unknown"
    pivota_mandate_id = meta.get("pivota_mandate_id") or "unavailable"
    authorization_audit_ref = meta.get("authorization_audit_ref") or f"audit://pivota/orders/{order_id}"

    pricing_quote_evidence = _extract_pricing_quote_evidence(order)

    manifest: Dict[str, Any] = {
        "schema_version": "0.1",
        "effective_from": "2025-01-01T00:00:00Z",
        "source": "pivota_derived",
        "last_updated_at": _utc_now_iso(),
        "pack_type": "order_snapshot",
        "pack_version": 1,
        "status": "frozen",
        "generated_at": _utc_now_iso(),
        "frozen_at": _utc_now_iso(),
        "merchant": {"merchant_id": merchant_id, "platform": "shopify", "shop_domain": store_info.get("domain") if store_info else None},
        "order_ref": {
            "order_id": order_id,
            "placed_at": placed_at_iso,
            "currency": order.get("currency"),
            "order_total": str(order.get("total")),
        },
        "pricing_quote": pricing_quote_evidence,
        "mandate_evidence": {
            "pivota_mandate_id": pivota_mandate_id,
            "pivota_agent_id": pivota_agent_id,
            "authorization_audit_ref": authorization_audit_ref,
        },
        "policy_snapshot": {
            "captured_at": placed_at_iso,
            "policies": [
                {
                    "policy_type": r.get("policy_type"),
                    "url": r.get("url"),
                    "hash_sha256": r.get("hash_sha256"),
                    "updated_at": (r.get("updated_at").isoformat() if r.get("updated_at") else None),
                }
                for r in latest_policy_rows
            ],
            "policy_disclosure_hash": policy_disclosure_hash,
        },
        "fulfillment_proof": {"tracking": [], "delivered_evidence": {"status": "unknown", "source": "shopify"}, "pod_assets": []},
        "support_timeline": {"source_system": "none", "timeline_sha256": sha256_json({"order_id": order_id, "support": "none"})},
        "assets": [],
        "manifest_sha256": "",
    }

    manifest_sha = _compute_manifest_sha256(manifest)
    manifest["manifest_sha256"] = manifest_sha

    pack_version = 1
    if existing and existing.get("pack_version"):
        pack_version = int(existing["pack_version"]) + 1
        manifest["pack_version"] = pack_version

    await database.execute(
        """
        INSERT INTO pcs_evidence_packs
          (merchant_id, order_id, dispute_ref, pack_type, pack_version, status, generated_at, frozen_at,
           manifest_json, manifest_sha256, signature, assets_json)
        VALUES
          (:merchant_id, :order_id, NULL, 'order_snapshot', :pack_version, 'frozen', NOW(), NOW(),
           CAST(:manifest_json AS jsonb), :manifest_sha256, NULL, '[]'::jsonb)
        ON CONFLICT (merchant_id, pack_type, order_id, dispute_ref, pack_version) DO NOTHING
        """,
        {
            "merchant_id": merchant_id,
            "order_id": order_id,
            "pack_version": pack_version,
            "manifest_json": json.dumps(manifest, ensure_ascii=False),
            "manifest_sha256": manifest_sha,
        },
    )

    # PCS v0.2-b (best-effort): internal evidence pack fact for reducer replay (no manifest/PII).
    try:
        from services.pcs_fact_ingest import append_internal_fact_best_effort

        await append_internal_fact_best_effort(
            merchant_id=str(merchant_id),
            order_id=str(order_id),
            fact_type="internal.evidence_pack_frozen",
            payload={
                "merchant_id": str(merchant_id),
                "order_id": str(order_id),
                "pack_type": "order_snapshot",
                "pack_version": int(pack_version),
                "manifest_sha256": str(manifest_sha),
                "triggered_by": str(triggered_by),
            },
            idempotency_key=f"order_snapshot:{manifest_sha}",
        )
    except Exception:
        pass

    # Best-effort writeback into orders.metadata for downstream (quote-first/evidence/UI).
    try:
        patch = {
            "pcs": {
                "policy_disclosure_hash": policy_disclosure_hash,
                "pivota_mandate_id": pivota_mandate_id,
                "pivota_agent_id": pivota_agent_id,
                "authorization_audit_ref": authorization_audit_ref,
                "order_snapshot_manifest_sha256": manifest_sha,
                "triggered_by": triggered_by,
                "quote_id": (pricing_quote_evidence or {}).get("quote_id"),
                "quote_hash_sha256": (pricing_quote_evidence or {}).get("quote_hash_sha256"),
            }
        }
        await database.execute(
            """
            UPDATE orders
            SET metadata = COALESCE(metadata::jsonb, '{}'::jsonb) || CAST(:patch AS jsonb)
            WHERE order_id = :order_id
            """,
            {
                "order_id": order_id,
                "patch": json.dumps(patch),
            },
        )
    except Exception as e:
        logger.debug("PCS order metadata writeback failed order=%s: %s", order_id, e)

    return {"order_id": order_id, "merchant_id": merchant_id, "manifest_sha256": manifest_sha, "pack_version": pack_version}


async def _collect_audit_trail_refs(*, merchant_id: str, order_id: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    """
    Best-effort: collect recent audit-event refs for a merchant and optionally filter by order_id.

    Source: `mvp_events` where `event_type = 'audit_event'`.
    """
    try:
        rows = await database.fetch_all(
            """
            SELECT event_id, occurred_at, chain_hash, payload_json
            FROM mvp_events
            WHERE merchant_id = :merchant_id AND event_type = 'audit_event'
            ORDER BY occurred_at DESC
            LIMIT :limit
            """,
            {"merchant_id": merchant_id, "limit": limit},
        )
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for r in rows or []:
        payload = dict(r.get("payload_json") or {})
        subj = payload.get("subject") or {}
        if order_id and str(subj.get("order_id") or "") != str(order_id):
            continue
        out.append(
            {
                "event_id": r.get("event_id"),
                "occurred_at": r.get("occurred_at").isoformat() if r.get("occurred_at") else None,
                "chain_hash": r.get("chain_hash"),
                "action": payload.get("action"),
            }
        )
    return out


async def create_dispute_evidence_pack(
    *,
    merchant_id: str,
    dispute_ref: str,
    order_id: Optional[str],
    dispute_payload: Dict[str, Any],
    status: str,
    triggered_by: str,
) -> Optional[Dict[str, Any]]:
    """
    Create a PCS v0.1 `dispute_pack` evidence pack (draft/frozen) when disputes arrive (best-effort).

    Composition (best-effort):
    - Order receipt summary
    - Latest policy snapshot hashes
    - Shipping/tracking refs if available
    - Audit trail refs from `mvp_events` (intent/approval/execution/receipt)
    """
    if not dispute_ref:
        return None

    normalized_status = "frozen" if status == "frozen" else "draft"

    existing = await database.fetch_one(
        """
        SELECT id, pack_version, status
        FROM pcs_evidence_packs
        WHERE merchant_id = :merchant_id AND dispute_ref = :dispute_ref AND pack_type = 'dispute_pack'
        ORDER BY pack_version DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "dispute_ref": dispute_ref},
    )
    if existing and existing.get("status") == "frozen" and normalized_status == "frozen":
        return dict(existing)

    order = await get_order(order_id) if order_id else None
    latest_policy_rows = await get_latest_policy_hashes(merchant_id)

    audit_refs = await _collect_audit_trail_refs(merchant_id=merchant_id, order_id=order_id)

    manifest = build_dispute_pack_manifest(
        merchant_id=merchant_id,
        dispute_ref=dispute_ref,
        order=order,
        dispute_payload=dispute_payload,
        status=normalized_status,
        policy_rows=latest_policy_rows,
        audit_refs=audit_refs,
        triggered_by=triggered_by,
    )
    manifest_sha = manifest.get("manifest_sha256") or _compute_manifest_sha256(manifest)

    pack_version = 1
    if existing and existing.get("pack_version"):
        pack_version = int(existing["pack_version"]) + 1
        manifest["pack_version"] = pack_version

    await database.execute(
        """
        INSERT INTO pcs_evidence_packs
          (merchant_id, order_id, dispute_ref, pack_type, pack_version, status, generated_at, frozen_at,
           manifest_json, manifest_sha256, signature, assets_json)
        VALUES
          (:merchant_id, :order_id, :dispute_ref, 'dispute_pack', :pack_version, :status, NOW(), :frozen_at,
           CAST(:manifest_json AS jsonb), :manifest_sha256, NULL, '[]'::jsonb)
        ON CONFLICT (merchant_id, pack_type, order_id, dispute_ref, pack_version) DO NOTHING
        """,
        {
            "merchant_id": merchant_id,
            "order_id": order_id,
            "dispute_ref": dispute_ref,
            "pack_version": pack_version,
            "status": normalized_status,
            "frozen_at": datetime.now(timezone.utc) if normalized_status == "frozen" else None,
            "manifest_json": json.dumps(manifest, ensure_ascii=False),
            "manifest_sha256": manifest_sha,
        },
    )

    # PCS v0.2-b (best-effort): internal evidence pack fact for reducer replay (no manifest/PII).
    if normalized_status == "frozen":
        try:
            from services.pcs_fact_ingest import append_internal_fact_best_effort

            await append_internal_fact_best_effort(
                merchant_id=str(merchant_id),
                order_id=str(order_id) if order_id else None,
                fact_type="internal.evidence_pack_frozen",
                payload={
                    "merchant_id": str(merchant_id),
                    "order_id": str(order_id) if order_id else None,
                    "dispute_ref": str(dispute_ref),
                    "pack_type": "dispute_pack",
                    "pack_version": int(pack_version),
                    "manifest_sha256": str(manifest_sha),
                    "triggered_by": str(triggered_by),
                },
                idempotency_key=f"dispute_pack:{manifest_sha}",
            )
        except Exception:
            pass

    return {
        "merchant_id": merchant_id,
        "order_id": order_id,
        "dispute_ref": dispute_ref,
        "status": normalized_status,
        "manifest_sha256": manifest_sha,
        "pack_version": pack_version,
    }
