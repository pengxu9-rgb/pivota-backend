import json
import logging
from datetime import datetime, timedelta, timezone
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


def _coerce_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    try:
        parsed = dict(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _derive_task_ops_priority(task: Dict[str, Any], *, now: datetime) -> str:
    status = str(task.get("status") or "pending").strip().lower() or "pending"
    if status == "resolved":
        return "resolved"

    due_dt = _parse_iso_datetime(task.get("due_by"))
    if due_dt and due_dt <= now:
        return "urgent"
    if due_dt and due_dt <= now + timedelta(hours=24):
        return "urgent"

    explicit = str(task.get("priority") or "").strip().lower()
    if explicit in {"urgent", "high", "medium", "low"}:
        if explicit == "high" and not bool(task.get("blocking")) and str(task.get("collection_mode") or "") == "best_effort_auto":
            return "medium"
        return explicit

    if bool(task.get("blocking")) or str(task.get("collection_mode") or "") == "operator_required":
        return "high"
    if due_dt and due_dt <= now + timedelta(hours=72):
        return "medium"
    if str(task.get("collection_mode") or "") == "best_effort_auto":
        return "medium"
    return "low"


def _derive_task_due_bucket(task: Dict[str, Any], *, now: datetime) -> str:
    status = str(task.get("status") or "pending").strip().lower() or "pending"
    if status == "resolved":
        return "resolved"
    due_dt = _parse_iso_datetime(task.get("due_by"))
    if due_dt is None:
        return "unscheduled"
    if due_dt <= now:
        return "overdue"
    if due_dt <= now + timedelta(hours=24):
        return "due_24h"
    if due_dt <= now + timedelta(hours=72):
        return "due_72h"
    return "scheduled"


def _decorate_collection_task(task: Dict[str, Any], *, now: datetime) -> Dict[str, Any]:
    decorated = dict(task)
    due_dt = _parse_iso_datetime(decorated.get("due_by"))
    due_bucket = _derive_task_due_bucket(decorated, now=now)
    ops_priority = _derive_task_ops_priority(decorated, now=now)
    decorated["ops_priority"] = ops_priority
    decorated["due_bucket"] = due_bucket
    decorated["is_overdue"] = due_bucket == "overdue"
    decorated["has_assignee"] = bool(str(decorated.get("assigned_to") or "").strip())
    if due_dt and "due_by" in decorated:
        decorated["due_by"] = due_dt.isoformat()
    return decorated


def _task_sort_key(item: Dict[str, Any]) -> Any:
    task = item.get("task") if isinstance(item.get("task"), dict) else {}
    priority_order = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "resolved": 4}
    bucket_order = {"overdue": 0, "due_24h": 1, "due_72h": 2, "scheduled": 3, "unscheduled": 4, "resolved": 5}
    ops_priority = str(task.get("ops_priority") or "low")
    due_bucket = str(task.get("due_bucket") or "unscheduled")
    due_dt = _parse_iso_datetime(task.get("due_by"))
    unresolved = 0 if str(task.get("status") or "").strip().lower() != "resolved" else 1
    assigned = 1 if str(task.get("assigned_to") or "").strip() else 0
    return (
        unresolved,
        priority_order.get(ops_priority, 99),
        bucket_order.get(due_bucket, 99),
        assigned,
        due_dt or datetime.max.replace(tzinfo=timezone.utc),
        str(item.get("dispute_id") or ""),
        str(task.get("task_id") or ""),
    )


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
                api_version="2025-10",
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
    source: Optional[str] = None,
    status: str,
    event_type: Optional[str] = None,
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
    # Dispute packs are monotonic: once frozen, later out-of-order "created"/draft
    # signals must not downgrade the latest pack back to draft.
    if existing and existing.get("status") == "frozen":
        return dict(existing)

    order = await get_order(order_id) if order_id else None
    latest_policy_rows = await get_latest_policy_hashes(merchant_id)
    dispute_status_detail = None
    dispute_evidence_summary = None
    try:
        if (source or "").strip().lower() == "stripe":
            from services.dispute_records_service import (
                stripe_dispute_evidence_summary,
                stripe_dispute_status_detail,
            )

            dispute_status_detail = stripe_dispute_status_detail(
                raw=str(dispute_payload.get("status") or "").strip() or None,
                event_type=event_type,
            )
            dispute_evidence_summary = stripe_dispute_evidence_summary(
                dispute=dict(dispute_payload or {}),
                event_type=event_type,
            )
    except Exception:
        dispute_status_detail = None
        dispute_evidence_summary = None

    audit_refs = await _collect_audit_trail_refs(merchant_id=merchant_id, order_id=order_id)

    manifest = build_dispute_pack_manifest(
        source=source,
        merchant_id=merchant_id,
        dispute_ref=dispute_ref,
        order=order,
        dispute_payload=dispute_payload,
        status=normalized_status,
        dispute_status_detail=dispute_status_detail,
        dispute_evidence_summary=dispute_evidence_summary,
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


async def preview_dispute_evidence_pack(
    *,
    merchant_id: str,
    dispute_ref: str,
    order_id: Optional[str],
    dispute_payload: Dict[str, Any],
    source: Optional[str] = None,
    status: Optional[str] = None,
    event_type: Optional[str] = None,
    triggered_by: str,
) -> Optional[Dict[str, Any]]:
    """
    Build a dispute pack manifest without persisting it.

    This is used by ops/admin surfaces that need the latest evidence plan and
    collection tasks even when no stored dispute pack exists yet.
    """
    if not dispute_ref:
        return None

    normalized_status = "draft"
    source_norm = str(source or "").strip().lower()
    if status in {"draft", "frozen"}:
        normalized_status = str(status)
    elif source_norm == "stripe":
        try:
            from services.dispute_records_service import stripe_dispute_pack_status

            normalized_status = stripe_dispute_pack_status(
                raw=str(dispute_payload.get("status") or "").strip() or None,
                event_type=event_type,
            )
        except Exception:
            normalized_status = "draft"
    elif str(dispute_payload.get("status") or "").strip().lower() in {"won", "lost", "closed", "resolved"}:
        normalized_status = "frozen"

    order = await get_order(order_id) if order_id else None
    latest_policy_rows = await get_latest_policy_hashes(merchant_id)

    dispute_status_detail = None
    dispute_evidence_summary = None
    try:
        if source_norm == "stripe":
            from services.dispute_records_service import (
                stripe_dispute_evidence_summary,
                stripe_dispute_status_detail,
            )

            dispute_status_detail = stripe_dispute_status_detail(
                raw=str(dispute_payload.get("status") or "").strip() or None,
                event_type=event_type,
            )
            dispute_evidence_summary = stripe_dispute_evidence_summary(
                dispute=dict(dispute_payload or {}),
                event_type=event_type,
            )
    except Exception:
        dispute_status_detail = None
        dispute_evidence_summary = None

    audit_refs = await _collect_audit_trail_refs(merchant_id=merchant_id, order_id=order_id)
    manifest = build_dispute_pack_manifest(
        source=source,
        merchant_id=merchant_id,
        dispute_ref=dispute_ref,
        order=order,
        dispute_payload=dispute_payload,
        status=normalized_status,
        dispute_status_detail=dispute_status_detail,
        dispute_evidence_summary=dispute_evidence_summary,
        policy_rows=latest_policy_rows,
        audit_refs=audit_refs,
        triggered_by=triggered_by,
    )
    return {
        "merchant_id": merchant_id,
        "order_id": order_id,
        "dispute_ref": dispute_ref,
        "status": normalized_status,
        "manifest": manifest,
    }


def _collection_task_summary(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    by_ops_priority: Dict[str, int] = {}
    by_due_bucket: Dict[str, int] = {}
    blocking_count = 0
    assigned_count = 0
    overdue_count = 0
    unassigned_count = 0
    for item in items:
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        status = str(task.get("status") or "pending").strip().lower() or "pending"
        by_status[status] = int(by_status.get(status) or 0) + 1
        ops_priority = str(task.get("ops_priority") or "low").strip().lower() or "low"
        due_bucket = str(task.get("due_bucket") or "unscheduled").strip().lower() or "unscheduled"
        by_ops_priority[ops_priority] = int(by_ops_priority.get(ops_priority) or 0) + 1
        by_due_bucket[due_bucket] = int(by_due_bucket.get(due_bucket) or 0) + 1
        if bool(task.get("blocking")):
            blocking_count += 1
        if str(task.get("assigned_to") or "").strip():
            assigned_count += 1
        else:
            unassigned_count += 1
        if bool(task.get("is_overdue")):
            overdue_count += 1
    return {
        "blocking_count": blocking_count,
        "assigned_count": assigned_count,
        "unassigned_count": unassigned_count,
        "overdue_count": overdue_count,
        "by_status": by_status,
        "by_ops_priority": by_ops_priority,
        "by_due_bucket": by_due_bucket,
    }


async def _fetch_latest_dispute_pack_rows(
    *,
    merchant_id: Optional[str] = None,
    source: Optional[str] = None,
    db=None,
) -> List[Dict[str, Any]]:
    if db is None:
        db = database

    where: List[str] = []
    params: Dict[str, Any] = {}
    merchant_id_norm = str(merchant_id or "").strip() or None
    source_norm = str(source or "").strip().lower() or None

    if merchant_id_norm:
        where.append("pep.merchant_id = :merchant_id")
        params["merchant_id"] = merchant_id_norm
    if source_norm:
        where.append("LOWER(COALESCE(dr.source, '')) = :source")
        params["source"] = source_norm

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    rows = await db.fetch_all(
        f"""
        SELECT
          pep.merchant_id,
          pep.dispute_ref,
          pep.pack_version,
          pep.status AS pack_status,
          pep.generated_at,
          pep.frozen_at,
          pep.manifest_json,
          pep.manifest_sha256,
          dr.source,
          dr.order_id,
          dr.reason,
          dr.status AS dispute_status,
          dr.status_raw,
          dr.evidence_due_by,
          dr.updated_at AS dispute_updated_at
        FROM (
          SELECT DISTINCT ON (merchant_id, dispute_ref)
            merchant_id,
            dispute_ref,
            pack_version,
            status,
            generated_at,
            frozen_at,
            manifest_json,
            manifest_sha256
          FROM pcs_evidence_packs
          WHERE pack_type = 'dispute_pack'
          ORDER BY merchant_id, dispute_ref, pack_version DESC
        ) pep
        LEFT JOIN LATERAL (
          SELECT
            source,
            order_id,
            reason,
            status,
            status_raw,
            evidence_due_by,
            updated_at
          FROM dispute_records
          WHERE merchant_id = pep.merchant_id
            AND source_dispute_id = pep.dispute_ref
          ORDER BY updated_at DESC, id DESC
          LIMIT 1
        ) dr ON TRUE
        {where_sql}
        ORDER BY COALESCE(dr.updated_at, pep.generated_at) DESC NULLS LAST, pep.dispute_ref DESC
        """,
        params,
    )
    return [dict(row or {}) for row in (rows or [])]


def _flatten_dispute_collection_task_rows(
    rows: List[Dict[str, Any]],
    *,
    task_status: Optional[str] = None,
    assignee: Optional[str] = None,
    blocking_only: bool = False,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    task_status_norm = str(task_status or "").strip().lower() or None
    assignee_norm = str(assignee or "").strip() or None
    reference_now = now if now is not None else datetime.now(timezone.utc)
    items: List[Dict[str, Any]] = []
    for row in rows:
        manifest = _coerce_json_object(row.get("manifest_json"))
        evidence_plan = manifest.get("evidence_plan")
        if not isinstance(evidence_plan, dict):
            continue
        tasks = evidence_plan.get("collection_tasks")
        if not isinstance(tasks, list):
            continue
        for raw_task in tasks:
            task = _coerce_json_object(raw_task)
            if not task:
                continue
            decorated_task = _decorate_collection_task(task, now=reference_now)
            status_norm = str(decorated_task.get("status") or "pending").strip().lower() or "pending"
            assigned_to = str(decorated_task.get("assigned_to") or "").strip() or None
            if task_status_norm and status_norm != task_status_norm:
                continue
            if assignee_norm and assigned_to != assignee_norm:
                continue
            if blocking_only and not bool(decorated_task.get("blocking")):
                continue
            items.append(
                {
                    "merchant_id": str(row.get("merchant_id") or ""),
                    "source": str(row.get("source") or ""),
                    "dispute_id": str(row.get("dispute_ref") or ""),
                    "order_id": str(row.get("order_id") or "") or None,
                    "reason": row.get("reason"),
                    "dispute": {
                        "status_raw": row.get("status_raw"),
                        "status": row.get("dispute_status"),
                        "evidence_due_by": row.get("evidence_due_by"),
                        "updated_at": row.get("dispute_updated_at"),
                    },
                    "pack": {
                        "pack_version": row.get("pack_version"),
                        "status": row.get("pack_status"),
                        "generated_at": row.get("generated_at"),
                        "frozen_at": row.get("frozen_at"),
                        "manifest_sha256": row.get("manifest_sha256"),
                    },
                    "task": decorated_task,
                }
            )
    items.sort(key=_task_sort_key)
    return items


def _build_worklist_groups(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    assignee_index: Dict[str, Dict[str, Any]] = {}
    for item in items:
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        assignee = str(task.get("assigned_to") or "").strip() or "unassigned"
        bucket = assignee_index.setdefault(
            assignee,
            {
                "assignee": None if assignee == "unassigned" else assignee,
                "total": 0,
                "blocking_count": 0,
                "overdue_count": 0,
                "by_ops_priority": {},
            },
        )
        bucket["total"] += 1
        if bool(task.get("blocking")):
            bucket["blocking_count"] += 1
        if bool(task.get("is_overdue")):
            bucket["overdue_count"] += 1
        ops_priority = str(task.get("ops_priority") or "low").strip().lower() or "low"
        priority_counts = bucket["by_ops_priority"]
        priority_counts[ops_priority] = int(priority_counts.get(ops_priority) or 0) + 1

    assignees = list(assignee_index.values())
    assignees.sort(
        key=lambda item: (
            0 if item.get("assignee") is None else 1,
            -int(item.get("overdue_count") or 0),
            -int(item.get("blocking_count") or 0),
            -int(item.get("total") or 0),
            str(item.get("assignee") or ""),
        )
    )
    return {"assignees": assignees}


def _dashboard_card(*, items: List[Dict[str, Any]], preview_limit: int) -> Dict[str, Any]:
    ordered = sorted(items, key=_task_sort_key)
    return {
        "count": len(ordered),
        "items": ordered[:preview_limit],
    }


def _build_assignee_queue(
    items: List[Dict[str, Any]],
    *,
    assignee: Optional[str],
    preview_limit: int,
) -> Dict[str, Any]:
    assignee_norm = str(assignee or "").strip() or None
    if not assignee_norm:
        return {
            "assignee": None,
            "total": 0,
            "overdue_count": 0,
            "urgent_count": 0,
            "blocking_count": 0,
            "items": [],
        }
    mine = [
        item
        for item in items
        if str((((item.get("task") or {}).get("assigned_to")) or "")).strip() == assignee_norm
    ]
    return {
        "assignee": assignee_norm,
        "total": len(mine),
        "overdue_count": sum(1 for item in mine if bool(((item.get("task") or {}).get("is_overdue")))),
        "urgent_count": sum(
            1
            for item in mine
            if str((((item.get("task") or {}).get("ops_priority")) or "")).strip().lower() == "urgent"
        ),
        "blocking_count": sum(1 for item in mine if bool(((item.get("task") or {}).get("blocking")))),
        "items": sorted(mine, key=_task_sort_key)[:preview_limit],
    }


def _build_dispute_grouped_board(
    items: List[Dict[str, Any]],
    *,
    preview_limit: int,
    overdue_only: bool = False,
) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = {}
    for item in items:
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        if overdue_only and not bool(task.get("is_overdue")):
            continue
        dispute_id = str(item.get("dispute_id") or "").strip()
        if not dispute_id:
            continue
        group = groups.setdefault(
            dispute_id,
            {
                "merchant_id": item.get("merchant_id"),
                "source": item.get("source"),
                "dispute_id": dispute_id,
                "order_id": item.get("order_id"),
                "reason": item.get("reason"),
                "overdue_count": 0,
                "urgent_count": 0,
                "blocking_count": 0,
                "unassigned_count": 0,
                "earliest_due_by": None,
                "items": [],
            },
        )
        group["items"].append(item)
        if bool(task.get("is_overdue")):
            group["overdue_count"] += 1
        if str(task.get("ops_priority") or "").strip().lower() == "urgent":
            group["urgent_count"] += 1
        if bool(task.get("blocking")):
            group["blocking_count"] += 1
        if not bool(task.get("has_assignee")):
            group["unassigned_count"] += 1
        due_dt = _parse_iso_datetime(task.get("due_by"))
        if due_dt is not None:
            current_earliest = _parse_iso_datetime(group.get("earliest_due_by"))
            if current_earliest is None or due_dt < current_earliest:
                group["earliest_due_by"] = due_dt.isoformat()

    grouped_items = list(groups.values())
    grouped_items.sort(
        key=lambda group: (
            -int(group.get("overdue_count") or 0),
            -int(group.get("blocking_count") or 0),
            -int(group.get("urgent_count") or 0),
            _parse_iso_datetime(group.get("earliest_due_by")) or datetime.max.replace(tzinfo=timezone.utc),
            str(group.get("dispute_id") or ""),
        )
    )
    for group in grouped_items:
        group["items"] = sorted(group["items"], key=_task_sort_key)[:preview_limit]
    return {"count": len(grouped_items), "items": grouped_items[:preview_limit]}


def _build_sla_breach_risk(items: List[Dict[str, Any]], *, preview_limit: int) -> Dict[str, Any]:
    blocking_overdue = [
        item for item in items if bool(((item.get("task") or {}).get("blocking"))) and bool(((item.get("task") or {}).get("is_overdue")))
    ]
    blocking_due_24h = [
        item
        for item in items
        if bool(((item.get("task") or {}).get("blocking")))
        and str((((item.get("task") or {}).get("due_bucket")) or "")).strip().lower() == "due_24h"
    ]
    unassigned_overdue = [
        item
        for item in items
        if not bool(((item.get("task") or {}).get("has_assignee"))) and bool(((item.get("task") or {}).get("is_overdue")))
    ]
    unassigned_due_24h = [
        item
        for item in items
        if not bool(((item.get("task") or {}).get("has_assignee")))
        and str((((item.get("task") or {}).get("due_bucket")) or "")).strip().lower() == "due_24h"
    ]
    at_risk_disputes = _build_dispute_grouped_board(
        blocking_overdue + blocking_due_24h,
        preview_limit=preview_limit,
        overdue_only=False,
    )
    return {
        "blocking_overdue_count": len(blocking_overdue),
        "blocking_due_24h_count": len(blocking_due_24h),
        "unassigned_overdue_count": len(unassigned_overdue),
        "unassigned_due_24h_count": len(unassigned_due_24h),
        "high_risk_dispute_count": int(at_risk_disputes.get("count") or 0),
        "top_at_risk_disputes": at_risk_disputes.get("items") or [],
    }


def _build_aging_buckets(items: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "overdue": sum(
            1 for item in items if str((((item.get("task") or {}).get("due_bucket")) or "")).strip().lower() == "overdue"
        ),
        "due_24h": sum(
            1 for item in items if str((((item.get("task") or {}).get("due_bucket")) or "")).strip().lower() == "due_24h"
        ),
        "due_72h": sum(
            1 for item in items if str((((item.get("task") or {}).get("due_bucket")) or "")).strip().lower() == "due_72h"
        ),
        "scheduled": sum(
            1 for item in items if str((((item.get("task") or {}).get("due_bucket")) or "")).strip().lower() == "scheduled"
        ),
        "unscheduled": sum(
            1 for item in items if str((((item.get("task") or {}).get("due_bucket")) or "")).strip().lower() == "unscheduled"
        ),
    }


def _build_next_actions(
    *,
    viewer_assignee: Optional[str],
    my_queue: Dict[str, Any],
    my_overdue_items: List[Dict[str, Any]],
    team_unassigned_items: List[Dict[str, Any]],
    overdue_items: List[Dict[str, Any]],
    blocking_unassigned_items: List[Dict[str, Any]],
    preview_limit: int,
) -> List[Dict[str, Any]]:
    viewer_assignee_norm = str(viewer_assignee or "").strip() or None

    def _task_action_target(item: Dict[str, Any], *, action: str, assignee: Optional[str] = None) -> Dict[str, Any]:
        dispute_id = str(item.get("dispute_id") or "")
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        body: Dict[str, Any] = {
            "task_id": str(task.get("task_id") or ""),
            "action": action,
        }
        if viewer_assignee_norm:
            body["actor"] = viewer_assignee_norm
        if assignee:
            body["assignee"] = assignee
        return {
            "method": "POST",
            "path": f"/agent/internal/disputes/{dispute_id}/evidence-plan/tasks/action",
            "body": body,
        }

    def _task_brief(item: Dict[str, Any], *, action: str, assignee: Optional[str] = None) -> Dict[str, Any]:
        task = item.get("task") if isinstance(item.get("task"), dict) else {}
        return {
            "dispute_id": item.get("dispute_id"),
            "source": item.get("source"),
            "merchant_id": item.get("merchant_id"),
            "task_id": task.get("task_id"),
            "assigned_to": task.get("assigned_to"),
            "ops_priority": task.get("ops_priority"),
            "due_bucket": task.get("due_bucket"),
            "target": _task_action_target(item, action=action, assignee=assignee),
        }

    def _batch_target(task_targets: List[Dict[str, Any]]) -> Dict[str, Any]:
        batch_items = []
        idem_parts = []
        for task_target in task_targets:
            target = task_target.get("target") if isinstance(task_target.get("target"), dict) else {}
            body = target.get("body") if isinstance(target.get("body"), dict) else {}
            dispute_id = str(task_target.get("dispute_id") or "")
            task_id = str(task_target.get("task_id") or "")
            action = str(body.get("action") or "")
            idem_parts.append(f"{dispute_id}:{task_id}:{action}")
            batch_items.append(
                {
                    "dispute_id": task_target.get("dispute_id"),
                    "merchant_id": task_target.get("merchant_id"),
                    "source": task_target.get("source"),
                    "task_id": task_target.get("task_id"),
                    "action": body.get("action"),
                    "actor": body.get("actor"),
                    "assignee": body.get("assignee"),
                }
            )
        items = []
        items.extend(batch_items)
        return {
            "method": "POST",
            "path": "/agent/internal/disputes/evidence-tasks/batch-action",
            "body": {"items": items, "idempotency_key": "|".join(idem_parts)},
        }

    actions: List[Dict[str, Any]] = []
    my_overdue_count = int(my_queue.get("overdue_count") or 0)
    my_queue_total = int(my_queue.get("total") or 0)
    if my_overdue_count > 0:
        task_targets = [
            _task_brief(item, action="acknowledge")
            for item in sorted(my_overdue_items, key=_task_sort_key)[:preview_limit]
        ]
        actions.append(
            {
                "id": "clear_my_overdue",
                "label": "Clear my overdue tasks",
                "count": my_overdue_count,
                "priority": "urgent",
                "scope": "mine",
                "suggested_action": "acknowledge",
                "default_actor": viewer_assignee_norm,
                "task_targets": task_targets,
                "bulk_target": _batch_target(task_targets),
            }
        )
    if blocking_unassigned_items:
        suggested_assignee = viewer_assignee_norm
        task_targets = [
            _task_brief(item, action="assign", assignee=suggested_assignee)
            for item in sorted(blocking_unassigned_items, key=_task_sort_key)[:preview_limit]
        ]
        actions.append(
            {
                "id": "assign_blocking_unassigned",
                "label": "Assign blocking unassigned tasks",
                "count": len(blocking_unassigned_items),
                "priority": "urgent",
                "scope": "team",
                "suggested_action": "assign",
                "default_actor": viewer_assignee_norm,
                "default_assignee": suggested_assignee,
                "task_targets": task_targets,
                "bulk_target": _batch_target(task_targets),
            }
        )
    if team_unassigned_items:
        suggested_assignee = viewer_assignee_norm
        task_targets = [
            _task_brief(item, action="assign", assignee=suggested_assignee)
            for item in sorted(team_unassigned_items, key=_task_sort_key)[:preview_limit]
        ]
        actions.append(
            {
                "id": "triage_team_unassigned",
                "label": "Triage team unassigned queue",
                "count": len(team_unassigned_items),
                "priority": "high",
                "scope": "team",
                "suggested_action": "assign",
                "default_actor": viewer_assignee_norm,
                "default_assignee": suggested_assignee,
                "task_targets": task_targets,
                "bulk_target": _batch_target(task_targets),
            }
        )
    if my_queue_total > 0 and not my_overdue_count:
        task_targets = [
            _task_brief(item, action="acknowledge")
            for item in sorted((my_queue.get("items") or []), key=_task_sort_key)[:preview_limit]
        ]
        actions.append(
            {
                "id": "work_my_queue",
                "label": "Work my assigned queue",
                "count": my_queue_total,
                "priority": "high",
                "scope": "mine",
                "suggested_action": "acknowledge",
                "default_actor": viewer_assignee_norm,
                "task_targets": task_targets,
                "bulk_target": _batch_target(task_targets),
            }
        )
    if overdue_items and not actions:
        task_targets = [
            _task_brief(item, action="acknowledge")
            for item in sorted(overdue_items, key=_task_sort_key)[:preview_limit]
        ]
        actions.append(
            {
                "id": "review_overdue_disputes",
                "label": "Review overdue dispute evidence",
                "count": len(overdue_items),
                "priority": "high",
                "scope": "team",
                "suggested_action": "acknowledge",
                "default_actor": viewer_assignee_norm,
                "task_targets": task_targets,
                "bulk_target": _batch_target(task_targets),
            }
        )
    return actions[:4]


async def list_dispute_collection_tasks(
    *,
    merchant_id: Optional[str] = None,
    source: Optional[str] = None,
    task_status: Optional[str] = None,
    assignee: Optional[str] = None,
    blocking_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    now: Optional[datetime] = None,
    db=None,
) -> Dict[str, Any]:
    """
    Flatten collection tasks from the latest stored dispute_pack for each dispute.

    This is intended for low-volume internal ops/admin usage, so filtering on task
    fields is done in Python after loading the latest pack rows.
    """
    rows = await _fetch_latest_dispute_pack_rows(
        merchant_id=merchant_id,
        source=source,
        db=db,
    )
    items = _flatten_dispute_collection_task_rows(
        rows,
        task_status=task_status,
        assignee=assignee,
        blocking_only=blocking_only,
        now=now,
    )

    total = len(items)
    paged_items = items[offset : offset + limit]
    return {
        "items": paged_items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": _collection_task_summary(items),
    }


async def list_dispute_collection_worklist(
    *,
    merchant_id: Optional[str] = None,
    source: Optional[str] = None,
    assignee: Optional[str] = None,
    blocking_only: bool = False,
    include_resolved: bool = False,
    limit: int = 50,
    offset: int = 0,
    now: Optional[datetime] = None,
    db=None,
) -> Dict[str, Any]:
    task_status = None if include_resolved else None
    rows = await _fetch_latest_dispute_pack_rows(
        merchant_id=merchant_id,
        source=source,
        db=db,
    )
    items = _flatten_dispute_collection_task_rows(
        rows,
        task_status=task_status,
        assignee=assignee,
        blocking_only=blocking_only,
        now=now,
    )
    if not include_resolved:
        items = [
            item
            for item in items
            if str(((item.get("task") or {}).get("status") or "")).strip().lower() != "resolved"
        ]

    total = len(items)
    paged_items = items[offset : offset + limit]
    return {
        "items": paged_items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": _collection_task_summary(items),
        "worklist": _build_worklist_groups(items),
    }


async def get_dispute_collection_dashboard(
    *,
    merchant_id: Optional[str] = None,
    source: Optional[str] = None,
    assignee: Optional[str] = None,
    viewer_assignee: Optional[str] = None,
    blocking_only: bool = False,
    include_resolved: bool = False,
    preview_limit: int = 5,
    now: Optional[datetime] = None,
    db=None,
) -> Dict[str, Any]:
    rows = await _fetch_latest_dispute_pack_rows(
        merchant_id=merchant_id,
        source=source,
        db=db,
    )
    items = _flatten_dispute_collection_task_rows(
        rows,
        assignee=assignee,
        blocking_only=blocking_only,
        now=now,
    )
    if not include_resolved:
        items = [
            item
            for item in items
            if str(((item.get("task") or {}).get("status") or "")).strip().lower() != "resolved"
        ]

    overdue_items = [item for item in items if bool(((item.get("task") or {}).get("is_overdue")))]
    viewer_assignee_norm = str(viewer_assignee or "").strip() or None
    my_overdue_items = [
        item
        for item in overdue_items
        if viewer_assignee_norm
        and str((((item.get("task") or {}).get("assigned_to")) or "")).strip() == viewer_assignee_norm
    ]
    due_24h_items = [
        item
        for item in items
        if str(((item.get("task") or {}).get("due_bucket") or "")).strip().lower() == "due_24h"
    ]
    urgent_items = [
        item
        for item in items
        if str(((item.get("task") or {}).get("ops_priority") or "")).strip().lower() == "urgent"
    ]
    unassigned_items = [
        item
        for item in items
        if not bool(((item.get("task") or {}).get("has_assignee")))
    ]
    blocking_unassigned_items = [
        item
        for item in unassigned_items
        if bool(((item.get("task") or {}).get("blocking")))
    ]

    summary = _collection_task_summary(items)
    my_queue = _build_assignee_queue(items, assignee=viewer_assignee_norm, preview_limit=preview_limit)
    return {
        "total": len(items),
        "summary": summary,
        "sla": {
            "overdue_count": int(summary.get("overdue_count") or 0),
            "due_24h_count": int((summary.get("by_due_bucket") or {}).get("due_24h") or 0),
            "due_72h_count": int((summary.get("by_due_bucket") or {}).get("due_72h") or 0),
            "unscheduled_count": int((summary.get("by_due_bucket") or {}).get("unscheduled") or 0),
        },
        "cards": {
            "overdue": _dashboard_card(items=overdue_items, preview_limit=preview_limit),
            "due_24h": _dashboard_card(items=due_24h_items, preview_limit=preview_limit),
            "urgent": _dashboard_card(items=urgent_items, preview_limit=preview_limit),
            "unassigned": _dashboard_card(items=unassigned_items, preview_limit=preview_limit),
            "blocking_unassigned": _dashboard_card(items=blocking_unassigned_items, preview_limit=preview_limit),
        },
        "worklist": _build_worklist_groups(items),
        "board": {
            "viewer_assignee": viewer_assignee_norm,
            "my_queue": my_queue,
            "my_overdue": _dashboard_card(items=my_overdue_items, preview_limit=preview_limit),
            "team_unassigned": _dashboard_card(items=unassigned_items, preview_limit=preview_limit),
            "aging_buckets": _build_aging_buckets(items),
            "top_overdue_disputes": _build_dispute_grouped_board(
                items,
                preview_limit=preview_limit,
                overdue_only=True,
            ),
            "sla_breach_risk": _build_sla_breach_risk(items, preview_limit=preview_limit),
            "next_actions": _build_next_actions(
                viewer_assignee=viewer_assignee_norm,
                my_queue=my_queue,
                my_overdue_items=my_overdue_items,
                team_unassigned_items=unassigned_items,
                overdue_items=overdue_items,
                blocking_unassigned_items=blocking_unassigned_items,
                preview_limit=preview_limit,
            ),
        },
    }


def _normalize_collection_task_action(action: str) -> str:
    action_norm = str(action or "").strip().lower()
    if action_norm not in {"acknowledge", "resolve", "assign", "reopen"}:
        raise ValueError("unsupported_action")
    return action_norm


def _next_collection_task_status(current_status: Optional[str], action: str) -> str:
    current = str(current_status or "pending").strip().lower() or "pending"
    action_norm = _normalize_collection_task_action(action)
    if action_norm == "acknowledge":
        if current == "resolved":
            return "resolved"
        return "acknowledged"
    if action_norm == "resolve":
        return "resolved"
    if action_norm == "reopen":
        return "pending"
    if action_norm == "assign":
        return current
    return current


async def update_dispute_collection_task_status(
    *,
    merchant_id: str,
    dispute_ref: str,
    task_id: str,
    action: str,
    actor: str,
    assignee: Optional[str] = None,
    db=None,
) -> Dict[str, Any]:
    """
    Update a collection task status inside the latest stored dispute_pack manifest.
    """
    if db is None:
        db = database

    action_norm = _normalize_collection_task_action(action)
    task_id_norm = str(task_id or "").strip()
    if not task_id_norm:
        raise ValueError("missing_task_id")
    assignee_norm = str(assignee or "").strip() or None
    if action_norm == "assign" and not assignee_norm:
        raise ValueError("missing_assignee")

    row = await db.fetch_one(
        """
        SELECT id, pack_version, status, manifest_json, manifest_sha256
        FROM pcs_evidence_packs
        WHERE merchant_id = :merchant_id
          AND dispute_ref = :dispute_ref
          AND pack_type = 'dispute_pack'
        ORDER BY pack_version DESC
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "dispute_ref": dispute_ref},
    )
    if not row:
        raise LookupError("pack_not_found")

    pack = dict(row)
    manifest = pack.get("manifest_json")
    if not isinstance(manifest, dict):
        try:
            manifest = dict(manifest or {})
        except Exception:
            manifest = {}

    evidence_plan = manifest.get("evidence_plan")
    if not isinstance(evidence_plan, dict):
        raise LookupError("evidence_plan_not_found")

    tasks = evidence_plan.get("collection_tasks")
    if not isinstance(tasks, list):
        raise LookupError("collection_tasks_not_found")

    updated_tasks: List[Dict[str, Any]] = []
    matched_task = None
    now_iso = _utc_now_iso()
    previous_status = None
    for raw_task in tasks:
        task = dict(raw_task or {}) if isinstance(raw_task, dict) else {}
        if str(task.get("task_id") or "").strip() == task_id_norm:
            previous_status = str(task.get("status") or "pending")
            next_status = _next_collection_task_status(task.get("status"), action_norm)
            history = task.get("status_history")
            history_list = list(history) if isinstance(history, list) else []
            history_list.append(
                {
                    "at": now_iso,
                    "actor": actor,
                    "action": action_norm,
                    "from_status": previous_status,
                    "to_status": next_status,
                    "assignee": assignee_norm,
                }
            )
            task["status"] = next_status
            task["status_history"] = history_list
            task["updated_at"] = now_iso
            if action_norm == "acknowledge":
                task["acknowledged_at"] = now_iso
                task["acknowledged_by"] = actor
            if action_norm == "resolve":
                task["resolved_at"] = now_iso
                task["resolved_by"] = actor
            if action_norm == "assign":
                task["assigned_to"] = assignee_norm
                task["assigned_at"] = now_iso
                task["assigned_by"] = actor
            if action_norm == "reopen":
                task["reopened_at"] = now_iso
                task["reopened_by"] = actor
            matched_task = task
        updated_tasks.append(task)

    if matched_task is None:
        raise LookupError("task_not_found")

    evidence_plan["collection_tasks"] = updated_tasks
    evidence_plan["blocking_task_count"] = sum(
        1 for task in updated_tasks if str(task.get("status") or "pending").strip().lower() != "resolved"
    )
    manifest["evidence_plan"] = evidence_plan
    manifest["last_updated_at"] = now_iso
    manifest_sha = _compute_manifest_sha256(manifest)
    manifest["manifest_sha256"] = manifest_sha

    await db.execute(
        """
        UPDATE pcs_evidence_packs
        SET manifest_json = CAST(:manifest_json AS jsonb),
            manifest_sha256 = :manifest_sha256
        WHERE id = :id
        """,
        {
            "id": pack["id"],
            "manifest_json": json.dumps(manifest, ensure_ascii=False),
            "manifest_sha256": manifest_sha,
        },
    )

    order_ref = manifest.get("order_ref")
    order_id = None
    if isinstance(order_ref, dict):
        order_id = str(order_ref.get("order_id") or "").strip() or None

    event_payload = {
        "merchant_id": merchant_id,
        "dispute_ref": dispute_ref,
        "task_id": task_id_norm,
        "action": action_norm,
        "actor": actor,
        "assignee": assignee_norm,
        "from_status": previous_status,
        "to_status": matched_task.get("status"),
        "pack_version": pack.get("pack_version"),
        "manifest_sha256": manifest_sha,
    }

    try:
        from services.pcs_fact_ingest import append_internal_fact_best_effort

        await append_internal_fact_best_effort(
            merchant_id=str(merchant_id),
            order_id=order_id,
            fact_type="internal.dispute_collection_task_updated",
            payload=event_payload,
            idempotency_key=f"{dispute_ref}:{task_id_norm}:{action_norm}:{manifest_sha}",
            db=db,
        )
    except Exception:
        pass

    try:
        from mvp.events import emit_best_effort

        emit_best_effort(
            event_type="ops.dispute_collection_task_updated",
            payload=event_payload,
            merchant_id=str(merchant_id),
            geo=None,
            surface="backend",
            adapter="pcs_evidence_pack",
            idempotency_key=f"{dispute_ref}:{task_id_norm}:{manifest_sha}",
        )
    except Exception:
        pass

    return {
        "pack_version": pack.get("pack_version"),
        "pack_status": pack.get("status"),
        "task": matched_task,
        "blocking_task_count": evidence_plan["blocking_task_count"],
        "manifest_sha256": manifest_sha,
    }
