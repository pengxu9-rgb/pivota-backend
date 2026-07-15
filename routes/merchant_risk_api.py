"""
Internal risk APIs for Ops/admin usage.

Scope:
- Disputes/chargebacks: Stripe disputes + Shopify disputes (signals).
- Returns: Shopify returns (signals).

Auth:
- Admin-key protected (X-ADMIN-KEY) using ADMIN_API_KEY (or PROMOTIONS_ADMIN_KEY for compatibility).
"""

import json
import os
import secrets
from typing import Any, Dict, Optional

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel

from config.settings import settings
from db.database import database
from services.merchant_store_service import get_primary_store
from utils.logger import logger

router = APIRouter(prefix="/agent/internal", tags=["risk"])


class DisputeCollectionTaskActionRequest(BaseModel):
    task_id: str
    action: str
    actor: Optional[str] = None
    assignee: Optional[str] = None


class DisputeCollectionTaskBatchActionItem(BaseModel):
    dispute_id: str
    task_id: str
    action: str
    actor: Optional[str] = None
    assignee: Optional[str] = None
    merchant_id: Optional[str] = None
    source: Optional[str] = None


class DisputeCollectionTaskBatchActionRequest(BaseModel):
    items: list[DisputeCollectionTaskBatchActionItem]
    stop_on_error: bool = False
    idempotency_key: Optional[str] = None


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            return {}
    try:
        return dict(value or {})
    except Exception:
        return {}


async def _get_dispute_collection_batch_action_replay(*, idempotency_key: str) -> Optional[Dict[str, Any]]:
    idempotency_norm = str(idempotency_key or "").strip()
    if not idempotency_norm:
        return None
    try:
        from services.pcs_fact_ingest import build_internal_fact_dedupe_key

        dedupe_key = build_internal_fact_dedupe_key(
            fact_type="internal.dispute_collection_batch_action",
            order_id=None,
            idempotency_key=idempotency_norm,
        )
        row = await database.fetch_one(
            """
            SELECT payload_json
            FROM pcs_order_facts
            WHERE merchant_id = :merchant_id
              AND dedupe_key = :dedupe_key
            ORDER BY received_at DESC
            LIMIT 1
            """,
            {"merchant_id": "batch", "dedupe_key": dedupe_key},
        )
        if not row:
            return None
        payload = _json_object(row.get("payload_json"))
        response = _json_object(payload.get("response"))
        if not response:
            return None
        response["replayed"] = True
        response["idempotency_key"] = response.get("idempotency_key") or idempotency_norm
        return response
    except Exception:
        return None

def _count_from_row(row: Any) -> int:
    if row is None:
        return 0
    try:
        return int(row["n"])
    except Exception:
        pass
    try:
        return int(dict(row).get("n") or 0)
    except Exception:
        return 0


def _db_error_details(err: Exception) -> Dict[str, Any]:
    msg = str(err or "")
    sqlstate = None
    for obj in (err, getattr(err, "orig", None), getattr(err, "__cause__", None)):
        if not obj:
            continue
        s = getattr(obj, "sqlstate", None)
        if isinstance(s, str) and s:
            sqlstate = s
            break
    return {
        "error_type": err.__class__.__name__,
        "sqlstate": sqlstate,
        "db_error": msg[:800],
    }


def _looks_like_missing_relation(err: Exception, relation: str) -> bool:
    details = _db_error_details(err)
    msg = (details.get("db_error") or "").lower()
    rel = relation.lower()
    if details.get("sqlstate") in {"42P01", "42703"}:
        return True
    if "undefinedtable" in err.__class__.__name__.lower():
        return True
    if "relation" in msg and rel in msg and "does not exist" in msg:
        return True
    if "no such table" in msg and rel in msg:
        return True
    return False


async def require_admin_key(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


@router.get("/disputes", response_model=Dict[str, Any])
async def list_disputes(
    merchantId: Optional[str] = Query(None),
    orderId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    where = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if merchantId:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchantId
    if orderId:
        where.append("order_id = :order_id")
        params["order_id"] = orderId
    if status:
        where.append("status = :status")
        params["status"] = status
    if source:
        where.append("source = :source")
        params["source"] = source

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    count_params = {k: v for k, v in params.items() if k not in {"limit", "offset"}}

    try:
        total_row = await database.fetch_one(
            f"SELECT COUNT(*) AS n FROM dispute_records {where_sql}", count_params
        )
        rows = await database.fetch_all(
            f"""
            SELECT
              merchant_id,
              source,
              source_dispute_id,
              order_id,
              platform_order_id,
              payment_intent_id,
              charge_id,
              currency,
              amount,
              reason,
              status_raw,
              status,
              evidence_due_by,
              opened_at,
              closed_at,
              created_at,
              updated_at
            FROM dispute_records
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """,
            params,
        )
        return {"items": [dict(r) for r in (rows or [])], "total": _count_from_row(total_row)}
    except Exception as e:
        if _looks_like_missing_relation(e, "dispute_records"):
            return {"items": [], "total": 0, "not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("list_disputes failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to list disputes",
                "debug_id": debug_id,
                **details,
            },
        )


def _ensure_stripe_key() -> None:
    if stripe.api_key:
        return
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key


def _stripe_obj_to_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for method in ("to_dict_recursive", "to_dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                out = fn()
                return out if isinstance(out, dict) else {}
            except Exception:
                pass
    try:
        return dict(obj)
    except Exception:
        return {}


def _stripe_disputes_for_charge_best_effort(*, charge_id: str, limit: int) -> list[dict]:
    if not charge_id:
        return []
    try:
        resp = stripe.Dispute.list(charge=charge_id, limit=limit)
        data = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
        return [_stripe_obj_to_dict(d) for d in (data or []) if d]
    except Exception:
        # Fallback: list latest disputes and filter client-side (may miss older disputes).
        try:
            resp = stripe.Dispute.list(limit=limit)
            data = resp.get("data") if isinstance(resp, dict) else getattr(resp, "data", None)
            out = []
            for d in data or []:
                dd = _stripe_obj_to_dict(d)
                if str(dd.get("charge") or "") == charge_id:
                    out.append(dd)
            return out
        except Exception:
            return []


@router.post("/disputes/sync", response_model=Dict[str, Any])
async def sync_disputes(
    order_id: str = Query(..., alias="orderId"),
    limit: int = Query(20, ge=1, le=50),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """
    Admin-key helper: best-effort backfill Stripe disputes for a specific order.

    Motivation: Stripe dispute events may not carry enough metadata in webhook payloads;
    this allows ops to backfill disputes by order_id deterministically.
    """
    _ensure_stripe_key()
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe is not configured")

    try:
        row = await database.fetch_one(
            "SELECT order_id, merchant_id, payment_intent_id FROM orders WHERE order_id = :order_id LIMIT 1",
            {"order_id": order_id},
        )
    except Exception as e:
        debug_id = secrets.token_hex(8)
        logger.exception("sync_disputes order lookup failed debug_id=%s err=%s", debug_id, str(e))
        raise HTTPException(
            status_code=500,
            detail={"code": "ORDER_LOOKUP_FAILED", "message": "Failed to lookup order", "debug_id": debug_id},
        )

    if not row:
        raise HTTPException(status_code=404, detail={"code": "ORDER_NOT_FOUND", "message": "Order not found"})

    merchant_id = str(row["merchant_id"])
    payment_intent_id = str(row["payment_intent_id"] or "") or None

    charges = []
    try:
        if payment_intent_id:
            charge_list = stripe.Charge.list(payment_intent=payment_intent_id, limit=10)
            charges = charge_list.get("data") if isinstance(charge_list, dict) else getattr(charge_list, "data", None) or []
    except Exception:
        charges = []

    disputes: list[dict] = []
    for ch in charges:
        charge_id = (_stripe_obj_to_dict(ch).get("id") or getattr(ch, "id", None) or "") if ch else ""
        disputes.extend(_stripe_disputes_for_charge_best_effort(charge_id=charge_id, limit=limit))

    upserted = 0
    dispute_ids: list[str] = []
    try:
        from services.dispute_records_service import upsert_stripe_dispute_record_best_effort

        for d in disputes:
            did = str(d.get("id") or "").strip()
            if did:
                dispute_ids.append(did)
            await upsert_stripe_dispute_record_best_effort(
                d,
                event_type="sync",
                order_id_hint=order_id,
                merchant_id_hint=merchant_id,
                db=database,
            )
            upserted += 1
    except Exception as e:
        debug_id = secrets.token_hex(8)
        logger.exception("sync_disputes failed debug_id=%s err=%s", debug_id, str(e))
        raise HTTPException(
            status_code=500,
            detail={"code": "SYNC_FAILED", "message": "Failed to sync disputes", "debug_id": debug_id},
        )

    return {
        "ok": True,
        "order_id": order_id,
        "merchant_id": merchant_id,
        "payment_intent_id": payment_intent_id,
        "charges_count": len(charges),
        "disputes_found": len(disputes),
        "upserted": upserted,
        "dispute_ids": dispute_ids,
    }


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


async def _lookup_dispute_task_context(
    *,
    dispute_id: str,
    merchant_id: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    where = ["source_dispute_id = :dispute_id"]
    params: Dict[str, Any] = {"dispute_id": dispute_id}

    if merchant_id:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchant_id
    if source:
        where.append("source = :source")
        params["source"] = source

    row = await database.fetch_one(
        f"""
        SELECT merchant_id, source, source_dispute_id
        FROM dispute_records
        WHERE {" AND ".join(where)}
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        params,
    )
    return dict(row) if row else None


@router.get("/disputes/evidence-tasks", response_model=Dict[str, Any])
async def list_dispute_evidence_tasks(
    merchantId: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    taskStatus: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    blockingOnly: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    try:
        from services.pcs_evidence_pack_service import list_dispute_collection_tasks

        result = await list_dispute_collection_tasks(
            merchant_id=merchantId,
            source=source,
            task_status=taskStatus,
            assignee=assignee,
            blocking_only=blockingOnly,
            limit=limit,
            offset=offset,
            db=database,
        )
    except Exception as e:
        if _looks_like_missing_relation(e, "pcs_evidence_packs") or _looks_like_missing_relation(e, "dispute_records"):
            return {"items": [], "total": 0, "not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("list_dispute_evidence_tasks failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to list dispute evidence tasks",
                "debug_id": debug_id,
                **details,
            },
        )

    return {
        "ok": True,
        "filters": {
            "merchant_id": merchantId,
            "source": source,
            "task_status": taskStatus,
            "assignee": assignee,
            "blocking_only": blockingOnly,
        },
        **result,
    }


@router.get("/disputes/evidence-worklist", response_model=Dict[str, Any])
async def get_dispute_evidence_worklist(
    merchantId: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    blockingOnly: bool = Query(False),
    includeResolved: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    try:
        from services.pcs_evidence_pack_service import list_dispute_collection_worklist

        result = await list_dispute_collection_worklist(
            merchant_id=merchantId,
            source=source,
            assignee=assignee,
            blocking_only=blockingOnly,
            include_resolved=includeResolved,
            limit=limit,
            offset=offset,
            db=database,
        )
    except Exception as e:
        if _looks_like_missing_relation(e, "pcs_evidence_packs") or _looks_like_missing_relation(e, "dispute_records"):
            return {"items": [], "total": 0, "not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("get_dispute_evidence_worklist failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to build dispute evidence worklist",
                "debug_id": debug_id,
                **details,
            },
        )

    return {
        "ok": True,
        "filters": {
            "merchant_id": merchantId,
            "source": source,
            "assignee": assignee,
            "blocking_only": blockingOnly,
            "include_resolved": includeResolved,
        },
        **result,
    }


@router.get("/disputes/evidence-dashboard", response_model=Dict[str, Any])
async def get_dispute_evidence_dashboard(
    merchantId: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    viewerAssignee: Optional[str] = Query(None),
    blockingOnly: bool = Query(False),
    includeResolved: bool = Query(False),
    previewLimit: int = Query(5, ge=1, le=50),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    try:
        from services.pcs_evidence_pack_service import get_dispute_collection_dashboard

        result = await get_dispute_collection_dashboard(
            merchant_id=merchantId,
            source=source,
            assignee=assignee,
            viewer_assignee=viewerAssignee,
            blocking_only=blockingOnly,
            include_resolved=includeResolved,
            preview_limit=previewLimit,
            db=database,
        )
    except Exception as e:
        if _looks_like_missing_relation(e, "pcs_evidence_packs") or _looks_like_missing_relation(e, "dispute_records"):
            return {"items": [], "total": 0, "not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("get_dispute_evidence_dashboard failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to build dispute evidence dashboard",
                "debug_id": debug_id,
                **details,
            },
        )

    return {
        "ok": True,
        "filters": {
            "merchant_id": merchantId,
            "source": source,
            "assignee": assignee,
            "viewer_assignee": viewerAssignee,
            "blocking_only": blockingOnly,
            "include_resolved": includeResolved,
            "preview_limit": previewLimit,
        },
        **result,
    }


@router.get("/disputes/{dispute_id}/evidence-plan", response_model=Dict[str, Any])
async def get_dispute_evidence_plan(
    dispute_id: str,
    merchantId: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    refresh: bool = Query(False),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    where = ["source_dispute_id = :dispute_id"]
    params: Dict[str, Any] = {"dispute_id": dispute_id}

    if merchantId:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchantId
    if source:
        where.append("source = :source")
        params["source"] = source

    where_sql = " AND ".join(where)

    try:
        dispute_row = await database.fetch_one(
            f"""
            SELECT
              merchant_id,
              source,
              source_dispute_id,
              order_id,
              reason,
              status_raw,
              status,
              evidence_due_by,
              raw_payload,
              updated_at
            FROM dispute_records
            WHERE {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            params,
        )
    except Exception as e:
        if _looks_like_missing_relation(e, "dispute_records"):
            return {"not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("get_dispute_evidence_plan dispute lookup failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to load dispute",
                "debug_id": debug_id,
                **details,
            },
        )

    if not dispute_row:
        raise HTTPException(status_code=404, detail={"code": "DISPUTE_NOT_FOUND", "message": "Dispute not found"})

    dispute = dict(dispute_row)
    merchant_id = str(dispute.get("merchant_id") or "")
    dispute_source = str(dispute.get("source") or "")
    order_id = str(dispute.get("order_id") or "") or None

    latest_pack = None
    if not refresh:
        try:
            latest_pack = await database.fetch_one(
                """
                SELECT pack_version, status, generated_at, frozen_at, manifest_json, manifest_sha256
                FROM pcs_evidence_packs
                WHERE merchant_id = :merchant_id
                  AND dispute_ref = :dispute_ref
                  AND pack_type = 'dispute_pack'
                ORDER BY pack_version DESC
                LIMIT 1
                """,
                {"merchant_id": merchant_id, "dispute_ref": dispute_id},
            )
        except Exception as e:
            if not _looks_like_missing_relation(e, "pcs_evidence_packs"):
                logger.warning("get_dispute_evidence_plan pack lookup failed dispute=%s err=%s", dispute_id, str(e)[:200])

    if latest_pack:
        pack = dict(latest_pack)
        manifest = _coerce_json_object(pack.get("manifest_json"))
        evidence_plan = manifest.get("evidence_plan") if isinstance(manifest.get("evidence_plan"), dict) else None
        if evidence_plan is not None:
            return {
                "ok": True,
                "from_pack": True,
                "merchant_id": merchant_id,
                "source": dispute_source,
                "dispute_id": dispute_id,
                "order_id": order_id,
                "dispute": {
                    "status_raw": dispute.get("status_raw"),
                    "status": dispute.get("status"),
                    "reason": dispute.get("reason"),
                    "evidence_due_by": dispute.get("evidence_due_by"),
                    "updated_at": dispute.get("updated_at"),
                },
                "pack": {
                    "pack_version": pack.get("pack_version"),
                    "status": pack.get("status"),
                    "generated_at": pack.get("generated_at"),
                    "frozen_at": pack.get("frozen_at"),
                    "manifest_sha256": pack.get("manifest_sha256"),
                },
                "evidence_plan": evidence_plan,
                "collection_tasks": evidence_plan.get("collection_tasks") or [],
                "blocking_task_count": int(evidence_plan.get("blocking_task_count") or 0),
            }

    dispute_payload = _coerce_json_object(dispute.get("raw_payload"))
    if not dispute_payload:
        dispute_payload = {
            "id": dispute_id,
            "status": dispute.get("status_raw") or dispute.get("status"),
            "reason": dispute.get("reason"),
        }

    try:
        from services.pcs_evidence_pack_service import preview_dispute_evidence_pack

        preview = await preview_dispute_evidence_pack(
            merchant_id=merchant_id,
            dispute_ref=dispute_id,
            order_id=order_id,
            dispute_payload=dispute_payload,
            source=dispute_source,
            status=None,
            event_type="preview",
            triggered_by="merchant_risk_api:evidence_plan_preview",
        )
    except Exception as e:
        debug_id = secrets.token_hex(8)
        logger.exception("get_dispute_evidence_plan preview build failed debug_id=%s err=%s", debug_id, str(e))
        raise HTTPException(
            status_code=500,
            detail={"code": "PREVIEW_BUILD_FAILED", "message": "Failed to build evidence plan preview", "debug_id": debug_id},
        )

    preview_manifest = _coerce_json_object((preview or {}).get("manifest"))
    evidence_plan = preview_manifest.get("evidence_plan") if isinstance(preview_manifest.get("evidence_plan"), dict) else {}
    return {
        "ok": True,
        "from_pack": False,
        "merchant_id": merchant_id,
        "source": dispute_source,
        "dispute_id": dispute_id,
        "order_id": order_id,
        "dispute": {
            "status_raw": dispute.get("status_raw"),
            "status": dispute.get("status"),
            "reason": dispute.get("reason"),
            "evidence_due_by": dispute.get("evidence_due_by"),
            "updated_at": dispute.get("updated_at"),
        },
        "pack": {
            "status": (preview or {}).get("status"),
            "preview": True,
        },
        "evidence_plan": evidence_plan,
        "collection_tasks": evidence_plan.get("collection_tasks") or [],
        "blocking_task_count": int(evidence_plan.get("blocking_task_count") or 0),
    }


@router.post("/disputes/{dispute_id}/evidence-plan/tasks/action", response_model=Dict[str, Any])
async def apply_dispute_collection_task_action(
    dispute_id: str,
    req: DisputeCollectionTaskActionRequest,
    merchantId: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    try:
        dispute_row = await _lookup_dispute_task_context(
            dispute_id=dispute_id,
            merchant_id=merchantId,
            source=source,
        )
    except Exception as e:
        if _looks_like_missing_relation(e, "dispute_records"):
            return {"not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("apply_dispute_collection_task_action dispute lookup failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to load dispute",
                "debug_id": debug_id,
                **details,
            },
        )

    if not dispute_row:
        raise HTTPException(status_code=404, detail={"code": "DISPUTE_NOT_FOUND", "message": "Dispute not found"})

    dispute = dict(dispute_row)
    merchant_id = str(dispute.get("merchant_id") or "")
    actor = str(req.actor or "admin_api").strip() or "admin_api"

    try:
        from services.pcs_evidence_pack_service import update_dispute_collection_task_status

        result = await update_dispute_collection_task_status(
            merchant_id=merchant_id,
            dispute_ref=dispute_id,
            task_id=req.task_id,
            action=req.action,
            actor=actor,
            assignee=req.assignee,
            db=database,
        )
    except ValueError as e:
        code = str(e)
        if code == "unsupported_action":
            raise HTTPException(status_code=400, detail={"code": "UNSUPPORTED_ACTION", "message": "Unsupported collection task action"})
        if code == "missing_task_id":
            raise HTTPException(status_code=400, detail={"code": "MISSING_TASK_ID", "message": "Task id is required"})
        if code == "missing_assignee":
            raise HTTPException(status_code=400, detail={"code": "MISSING_ASSIGNEE", "message": "Assignee is required for assign action"})
        raise
    except LookupError as e:
        code = str(e)
        if code == "pack_not_found":
            raise HTTPException(status_code=409, detail={"code": "PACK_NOT_FOUND", "message": "No stored dispute pack available for task updates"})
        if code in {"evidence_plan_not_found", "collection_tasks_not_found"}:
            raise HTTPException(status_code=409, detail={"code": "TASKS_NOT_AVAILABLE", "message": "Dispute pack has no collection tasks"})
        if code == "task_not_found":
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Collection task not found"})
        raise
    except Exception as e:
        debug_id = secrets.token_hex(8)
        logger.exception("apply_dispute_collection_task_action failed debug_id=%s err=%s", debug_id, str(e))
        raise HTTPException(
            status_code=500,
            detail={"code": "TASK_UPDATE_FAILED", "message": "Failed to update collection task", "debug_id": debug_id},
        )

    return {
        "ok": True,
        "merchant_id": merchant_id,
        "source": str(dispute.get("source") or ""),
        "dispute_id": dispute_id,
        "task": result.get("task"),
        "pack": {
            "pack_version": result.get("pack_version"),
            "status": result.get("pack_status"),
            "manifest_sha256": result.get("manifest_sha256"),
        },
        "blocking_task_count": result.get("blocking_task_count"),
    }


@router.post("/disputes/evidence-tasks/batch-action", response_model=Dict[str, Any])
async def apply_dispute_collection_task_batch_action(
    req: DisputeCollectionTaskBatchActionRequest,
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    items = list(req.items or [])
    batch_idempotency = str(req.idempotency_key or "").strip() or None
    if not items:
        raise HTTPException(
            status_code=400,
            detail={"code": "MISSING_ITEMS", "message": "At least one batch action item is required"},
        )
    if len(items) > 50:
        raise HTTPException(
            status_code=400,
            detail={"code": "TOO_MANY_ITEMS", "message": "Batch action supports at most 50 items"},
        )

    if batch_idempotency:
        replay = await _get_dispute_collection_batch_action_replay(idempotency_key=batch_idempotency)
        if replay:
            return replay

    results = []
    succeeded = 0
    failed = 0
    merchant_ids_seen: set[str] = set()

    from services.pcs_evidence_pack_service import update_dispute_collection_task_status

    for index, item in enumerate(items):
        dispute_id = str(item.dispute_id or "").strip()
        task_id = str(item.task_id or "").strip()
        source = str(item.source or "").strip() or None
        merchant_id = str(item.merchant_id or "").strip() or None
        actor = str(item.actor or "admin_api").strip() or "admin_api"
        assignee = str(item.assignee or "").strip() or None

        try:
            dispute_row = await _lookup_dispute_task_context(
                dispute_id=dispute_id,
                merchant_id=merchant_id,
                source=source,
            )
            if not dispute_row:
                raise LookupError("dispute_not_found")

            dispute = dict(dispute_row)
            merchant_ids_seen.add(str(dispute.get("merchant_id") or ""))
            result = await update_dispute_collection_task_status(
                merchant_id=str(dispute.get("merchant_id") or ""),
                dispute_ref=dispute_id,
                task_id=task_id,
                action=item.action,
                actor=actor,
                assignee=assignee,
                db=database,
            )
            results.append(
                {
                    "index": index,
                    "ok": True,
                    "dispute_id": dispute_id,
                    "merchant_id": str(dispute.get("merchant_id") or ""),
                    "source": str(dispute.get("source") or ""),
                    "task": result.get("task"),
                    "pack": {
                        "pack_version": result.get("pack_version"),
                        "status": result.get("pack_status"),
                        "manifest_sha256": result.get("manifest_sha256"),
                    },
                    "blocking_task_count": result.get("blocking_task_count"),
                }
            )
            succeeded += 1
        except ValueError as e:
            code = str(e)
            if code == "unsupported_action":
                error = {"code": "UNSUPPORTED_ACTION", "message": "Unsupported collection task action"}
            elif code == "missing_task_id":
                error = {"code": "MISSING_TASK_ID", "message": "Task id is required"}
            elif code == "missing_assignee":
                error = {"code": "MISSING_ASSIGNEE", "message": "Assignee is required for assign action"}
            else:
                error = {"code": "INVALID_ITEM", "message": code}
            results.append({"index": index, "ok": False, "dispute_id": dispute_id, "task_id": task_id, "error": error})
            failed += 1
            if req.stop_on_error:
                break
        except LookupError as e:
            code = str(e)
            if code == "dispute_not_found":
                error = {"code": "DISPUTE_NOT_FOUND", "message": "Dispute not found"}
            elif code == "pack_not_found":
                error = {"code": "PACK_NOT_FOUND", "message": "No stored dispute pack available for task updates"}
            elif code in {"evidence_plan_not_found", "collection_tasks_not_found"}:
                error = {"code": "TASKS_NOT_AVAILABLE", "message": "Dispute pack has no collection tasks"}
            elif code == "task_not_found":
                error = {"code": "TASK_NOT_FOUND", "message": "Collection task not found"}
            else:
                error = {"code": "LOOKUP_FAILED", "message": code}
            results.append({"index": index, "ok": False, "dispute_id": dispute_id, "task_id": task_id, "error": error})
            failed += 1
            if req.stop_on_error:
                break
        except Exception as e:
            debug_id = secrets.token_hex(8)
            logger.exception("apply_dispute_collection_task_batch_action failed debug_id=%s err=%s", debug_id, str(e))
            results.append(
                {
                    "index": index,
                    "ok": False,
                    "dispute_id": dispute_id,
                    "task_id": task_id,
                    "error": {
                        "code": "TASK_UPDATE_FAILED",
                        "message": "Failed to update collection task",
                        "debug_id": debug_id,
                    },
                }
            )
            failed += 1
            if req.stop_on_error:
                break

    response = {
        "ok": failed == 0,
        "total": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "stop_on_error": bool(req.stop_on_error),
        "idempotency_key": req.idempotency_key,
        "results": results,
    }

    if not batch_idempotency:
        dispute_refs = ",".join(
            sorted({str(item.dispute_id or "").strip() for item in items if str(item.dispute_id or "").strip()})
        )
        batch_idempotency = f"risk_batch:{dispute_refs}:{len(items)}:{succeeded}:{failed}"

    fact_payload = {
        "total": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "stop_on_error": bool(req.stop_on_error),
        "merchant_ids": sorted(mid for mid in merchant_ids_seen if mid),
        "results": [
            {
                "index": result.get("index"),
                "ok": result.get("ok"),
                "dispute_id": result.get("dispute_id"),
                "merchant_id": result.get("merchant_id"),
                "task_id": ((result.get("task") or {}).get("task_id") if isinstance(result.get("task"), dict) else result.get("task_id")),
                "error_code": ((result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None),
            }
            for result in results
        ],
        "response": response,
    }

    try:
        from services.pcs_fact_ingest import append_internal_fact_best_effort

        await append_internal_fact_best_effort(
            merchant_id="batch",
            order_id=None,
            fact_type="internal.dispute_collection_batch_action",
            payload=fact_payload,
            idempotency_key=batch_idempotency,
            db=database,
        )
    except Exception:
        pass

    try:
        from mvp.events import emit_best_effort

        emit_best_effort(
            event_type="ops.dispute_collection_batch_action",
            payload=fact_payload,
            merchant_id="batch",
            geo=None,
            surface="backend",
            adapter="pcs_evidence_pack",
            idempotency_key=batch_idempotency,
        )
    except Exception:
        pass

    return response


@router.get("/returns", response_model=Dict[str, Any])
async def list_returns(
    merchantId: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    where = []
    params: Dict[str, Any] = {"limit": limit, "offset": offset}

    if merchantId:
        where.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchantId
    if status:
        where.append("status = :status")
        params["status"] = status

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    count_params = {k: v for k, v in params.items() if k not in {"limit", "offset"}}

    try:
        total_row = await database.fetch_one(
            f"SELECT COUNT(*) AS n FROM return_records {where_sql}", count_params
        )
        rows = await database.fetch_all(
            f"""
            SELECT
              merchant_id,
              source,
              source_return_id,
              order_id,
              platform_order_id,
              status_raw,
              status,
              refund_status_raw,
              items_json,
              created_at,
              updated_at
            FROM return_records
            {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """,
            params,
        )
        return {"items": [dict(r) for r in (rows or [])], "total": _count_from_row(total_row)}
    except Exception as e:
        if _looks_like_missing_relation(e, "return_records"):
            return {"items": [], "total": 0, "not_ready": True, "reason": "missing_migration_or_schema"}
        debug_id = secrets.token_hex(8)
        logger.exception("list_returns failed debug_id=%s err=%s", debug_id, str(e))
        details = _db_error_details(e)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INTERNAL_ERROR",
                "message": "Failed to list returns",
                "debug_id": debug_id,
                **details,
            },
        )


@router.post("/returns/sync", response_model=Dict[str, Any])
async def sync_returns(
    merchant_id: str = Query(..., alias="merchantId"),
    limit: int = Query(20, ge=1, le=100),
    api_version: str = Query("2025-10", alias="apiVersion"),
    _: None = Depends(require_admin_key),
) -> Dict[str, Any]:
    """
    Admin-key helper: best-effort sync latest Shopify returns into return_records.
    """
    store_info = await get_primary_store(merchant_id)
    if not store_info or (store_info.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=400, detail="Primary store is not Shopify")

    shop_domain = store_info.get("domain") or ""
    access_token = store_info.get("api_key") or ""
    if not shop_domain or not access_token:
        raise HTTPException(status_code=400, detail="Missing Shopify credentials")

    try:
        from services.shopify_returns_service import sync_shopify_returns_best_effort

        result = await sync_shopify_returns_best_effort(
            merchant_id=merchant_id,
            shop_domain=shop_domain,
            access_token=access_token,
            api_version=api_version,
            limit=limit,
        )
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": "SYNC_FAILED", "message": str(e)})
