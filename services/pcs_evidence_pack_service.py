import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.database import database
from db.orders import get_order
from services.merchant_store_service import get_primary_store
from services.pcs_hash import sha256_json
from services.shopify_policy_service import fetch_and_store_shop_policies, get_latest_policy_hashes

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
           :manifest_json, :manifest_sha256, NULL, '[]'::jsonb)
        ON CONFLICT (merchant_id, pack_type, order_id, dispute_ref, pack_version) DO NOTHING
        """,
        {
            "merchant_id": merchant_id,
            "order_id": order_id,
            "pack_version": pack_version,
            "manifest_json": manifest,
            "manifest_sha256": manifest_sha,
        },
    )

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
            }
        }
        await database.execute(
            """
            UPDATE orders
            SET metadata = COALESCE(metadata::jsonb, '{}'::jsonb) || :patch::jsonb
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
