"""
After-Sales Case API (Agent-facing)

Unifies:
- refund_without_return
- partial_refund
- refund_with_return (return label flow; currently placeholder label_url)

Design goals:
- Strong ownership enforcement (agent_id + agent_user_ref/buyer_ref where applicable)
- Idempotency for high-risk operations
- Minimal PII storage (no addresses/emails in after_sales_cases table)
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Header, Query, Body
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from decimal import Decimal
from datetime import datetime
import uuid
import json

from db.database import database
from db.orders import get_order
from routes.agent_auth import AgentContext, get_agent_context
from routes.agent_user_auth import AgentUserContext, get_agent_user_context
from routes.refund_api import RefundRequest, process_refund
from utils.logger import logger

# Reuse proven order access semantics from Agent Orders API.
from routes.agent_api import (
    _normalize_buyer_ref,
    _order_agent_user_ref,
    _agent_user_matches_order_ref,
    resolve_buyer_ref_sources,
)


router = APIRouter(prefix="/agent/v1/after-sales", tags=["agent-after-sales"])


CaseType = Literal["refund", "return_refund", "support"]
ResolutionType = Literal["refund_without_return", "refund_with_return"]


class MoneyBreakdown(BaseModel):
    subtotal_refund: Optional[str] = None
    shipping_refund: Optional[str] = None
    tax_refund: Optional[str] = None
    discount_refund: Optional[str] = None
    total_refund: Optional[str] = None


class AfterSalesLineItem(BaseModel):
    item_ref: Optional[str] = None
    quantity_requested: Optional[int] = Field(default=None, ge=1)
    refund_amount: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = None


class CreateAfterSalesCaseRequest(BaseModel):
    order_id: str
    case_type: CaseType = "refund"
    resolution: ResolutionType = "refund_without_return"

    reason_code: Optional[str] = None
    reason_text: Optional[str] = None

    # For partial refunds: set requested_refund_amount. If omitted -> full refund intent.
    requested_refund_amount: Optional[float] = Field(default=None, ge=0)
    currency: Optional[str] = None

    line_items: List[AfterSalesLineItem] = Field(default_factory=list)
    amount_breakdown: Optional[MoneyBreakdown] = None

    # Best-effort idempotency for create (agent-scoped).
    idempotency_key: Optional[str] = None


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

async def _ensure_after_sales_cases_table() -> None:
    """
    Best-effort defensive DDL. Production should normally rely on SQL migrations,
    but the migration runner is best-effort and may skip new files.
    """
    try:
        await database.execute(
            """
            CREATE TABLE IF NOT EXISTS after_sales_cases (
              id BIGSERIAL PRIMARY KEY,
              case_id TEXT NOT NULL UNIQUE,
              order_id TEXT NOT NULL,
              merchant_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              agent_user_ref TEXT,
              buyer_ref TEXT,
              case_type TEXT NOT NULL,
              resolution TEXT NOT NULL,
              reason_code TEXT,
              reason_text TEXT,
              requested_refund_amount NUMERIC(12, 2),
              currency_order TEXT,
              currency_charge TEXT,
              line_items_json JSONB NOT NULL DEFAULT '[]'::jsonb,
              amount_breakdown_json JSONB NOT NULL DEFAULT '{}'::jsonb,
              status TEXT NOT NULL DEFAULT 'requested',
              label_url TEXT,
              audit_log JSONB NOT NULL DEFAULT '[]'::jsonb,
              idempotency_key TEXT,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_after_sales_cases_order ON after_sales_cases (order_id, created_at DESC)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_after_sales_cases_agent ON after_sales_cases (agent_id, created_at DESC)"
        )
        await database.execute(
            "CREATE INDEX IF NOT EXISTS idx_after_sales_cases_status ON after_sales_cases (status, updated_at DESC)"
        )
        await database.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_after_sales_cases_agent_idempotency
              ON after_sales_cases (agent_id, idempotency_key)
              WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''
            """
        )
    except Exception:
        # Best-effort only; routes will surface meaningful errors if DB is broken.
        return


async def _enforce_order_access(
    *,
    order: Dict[str, Any],
    context: AgentContext,
    agent_user: Optional[AgentUserContext],
    buyer_ref: Optional[str],
) -> None:
    # Always enforce agent ownership first.
    if str(order.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this order")

    stored_agent_user_ref = _order_agent_user_ref(order)
    if stored_agent_user_ref:
        if not agent_user or not _agent_user_matches_order_ref(stored_ref=stored_agent_user_ref, agent_user=agent_user):
            raise HTTPException(status_code=403, detail="Not authorized for this order")
        return

    # Legacy: buyer_ref match allowed only for non-agent-user-attributed orders.
    if buyer_ref:
        stored = (order.get("metadata") or {}).get("buyer_ref")
        allowed_refs = await resolve_buyer_ref_sources(context.agent_id, str(buyer_ref))
        if str(stored or "") not in allowed_refs:
            raise HTTPException(status_code=403, detail="Not authorized for this order")
        return

    # Fallback legacy: merchant allowlist.
    if not context.can_access_merchant(order.get("merchant_id")):
        raise HTTPException(status_code=403, detail="Not authorized for this order")


async def _get_case_by_id(case_id: str) -> Optional[Dict[str, Any]]:
    try:
        row = await database.fetch_one(
            "SELECT * FROM after_sales_cases WHERE case_id = :case_id",
            {"case_id": case_id},
        )
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"after_sales get_case error: {e}")
        raise HTTPException(status_code=500, detail="Failed to load after-sales case")


def _append_audit(audit: List[Dict[str, Any]], event: str, payload: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    audit = list(audit or [])
    audit.append(
        {
            "at": _now_iso(),
            "event": event,
            "payload": payload or {},
        }
    )
    return audit


@router.post("/cases")
async def create_after_sales_case(
    payload: Dict[str, Any] = Body(...),
    buyer_ref: Optional[str] = Query(default=None),
    x_buyer_ref: Optional[str] = Header(None, alias="X-Buyer-Ref"),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    """
    Create an after-sales case. This is the canonical entrypoint for:
    - refund_without_return (full or partial)
    - refund_with_return (RMA/label flow; label_url is a placeholder for now)
    """
    # Backward compatibility: earlier OpenAPI mistakenly embedded the request as {"req": {...}}
    # due to a dependency signature. Accept both shapes.
    raw = payload.get("req") if isinstance(payload, dict) and "req" in payload else payload
    try:
        req = CreateAfterSalesCaseRequest.model_validate(raw)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid request body")

    order_id = str(req.order_id or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id is required")

    canonical_buyer_ref = _normalize_buyer_ref(buyer_ref or x_buyer_ref)

    # Best-effort idempotency for create
    idem = (req.idempotency_key or "").strip() or None
    if idem:
        try:
            existing = await database.fetch_one(
                "SELECT case_id FROM after_sales_cases WHERE agent_id = :agent_id AND idempotency_key = :k",
                {"agent_id": context.agent_id, "k": idem},
            )
            if existing and existing.get("case_id"):
                case_id = str(existing["case_id"])
                loaded = await _get_case_by_id(case_id)
                return {"status": "success", "case": _serialize_case(loaded)}
        except Exception:
            pass

    order = await get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    await _enforce_order_access(order=order, context=context, agent_user=agent_user, buyer_ref=canonical_buyer_ref)

    requested_amount = req.requested_refund_amount
    order_total = Decimal(str(order.get("total") or "0"))
    if requested_amount is not None:
        amt = Decimal(str(requested_amount))
        if amt < 0:
            raise HTTPException(status_code=400, detail="requested_refund_amount must be >= 0")
        if amt > order_total:
            raise HTTPException(status_code=400, detail="requested_refund_amount exceeds order total")

    case_id = f"ASC_{uuid.uuid4().hex[:20].upper()}"
    merchant_id = str(order.get("merchant_id") or "")

    currency_order = str(req.currency or order.get("currency") or "").strip() or None
    currency_charge = None
    try:
        meta = order.get("metadata") or {}
        pcs = meta.get("pcs") if isinstance(meta, dict) else None
        if isinstance(pcs, dict):
            currency_charge = str(pcs.get("currency_charge") or pcs.get("currency") or "").strip() or None
    except Exception:
        currency_charge = None

    audit: List[Dict[str, Any]] = []
    audit = _append_audit(
        audit,
        "case_created",
        {
            "case_type": req.case_type,
            "resolution": req.resolution,
            "requested_refund_amount": requested_amount,
        },
    )

    try:
        async def _do_insert() -> None:
	            await database.execute(
	                """
	                INSERT INTO after_sales_cases (
	                  case_id, order_id, merchant_id, agent_id,
	                  agent_user_ref, buyer_ref,
	                  case_type, resolution,
	                  reason_code, reason_text,
	                  requested_refund_amount, currency_order, currency_charge,
	                  line_items_json, amount_breakdown_json,
	                  status, label_url, audit_log, idempotency_key,
	                  created_at, updated_at
	                )
	                VALUES (
	                  :case_id, :order_id, :merchant_id, :agent_id,
	                  :agent_user_ref, :buyer_ref,
	                  :case_type, :resolution,
	                  :reason_code, :reason_text,
	                  :requested_refund_amount, :currency_order, :currency_charge,
	                  CAST(:line_items_json AS JSONB), CAST(:amount_breakdown_json AS JSONB),
	                  :status, :label_url, CAST(:audit_log AS JSONB), :idempotency_key,
	                  NOW(), NOW()
	                )
	                """,
	                {
	                    "case_id": case_id,
	                    "order_id": order_id,
                    "merchant_id": merchant_id,
                    "agent_id": context.agent_id,
                    "agent_user_ref": agent_user.agent_user_ref if agent_user else None,
                    "buyer_ref": canonical_buyer_ref,
                    "case_type": req.case_type,
                    "resolution": req.resolution,
                    "reason_code": (req.reason_code or None),
                    "reason_text": (req.reason_text or None),
                    "requested_refund_amount": float(requested_amount) if requested_amount is not None else None,
                    "currency_order": currency_order,
	                    "currency_charge": currency_charge,
	                    "line_items_json": json.dumps([li.model_dump() for li in (req.line_items or [])], ensure_ascii=False),
	                    "amount_breakdown_json": json.dumps(req.amount_breakdown.model_dump() if req.amount_breakdown else {}, ensure_ascii=False),
	                    "status": "requested",
	                    "label_url": None,
	                    "audit_log": json.dumps(audit, ensure_ascii=False),
	                    "idempotency_key": idem,
	                },
	            )

        try:
            await _do_insert()
        except Exception:
            # One-time self-heal: create table/indexes, then retry insert.
            await _ensure_after_sales_cases_table()
            await _do_insert()
    except Exception as e:
        debug_id = f"as_create_{uuid.uuid4().hex[:10]}"
        logger.error(
            f"after_sales create_case error debug_id={debug_id}: {e}",
            exc_info=True,
        )
        # Return a sanitized error so callers can debug without server logs.
        raise HTTPException(
            status_code=500,
            detail={
                "error": "AFTER_SALES_CREATE_FAILED",
                "message": "Failed to create after-sales case",
                "debug_id": debug_id,
                "cause": type(e).__name__,
                "detail": str(e)[:240],
            },
        )

    loaded = await _get_case_by_id(case_id)
    return {
        "status": "success",
        "case": _serialize_case(loaded),
        "next_action": _next_action_for_case(_serialize_case(loaded)),
    }


def _serialize_case(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    def _json_field(value, fallback):
        if value is None:
            return fallback
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return fallback

    return {
        "case_id": row.get("case_id"),
        "order_id": row.get("order_id"),
        "merchant_id": row.get("merchant_id"),
        "agent_id": row.get("agent_id"),
        "agent_user_ref": row.get("agent_user_ref"),
        "buyer_ref": row.get("buyer_ref"),
        "case_type": row.get("case_type"),
        "resolution": row.get("resolution"),
        "status": row.get("status"),
        "reason_code": row.get("reason_code"),
        "reason_text": row.get("reason_text"),
        "requested_refund_amount": row.get("requested_refund_amount"),
        "currency_order": row.get("currency_order"),
        "currency_charge": row.get("currency_charge"),
        "line_items": _json_field(row.get("line_items_json"), []),
        "amount_breakdown": _json_field(row.get("amount_breakdown_json"), {}),
        "label_url": row.get("label_url"),
        "audit_log": _json_field(row.get("audit_log"), []),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def _next_action_for_case(case: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not case:
        return None
    resolution = str(case.get("resolution") or "")
    status = str(case.get("status") or "")
    if resolution == "refund_with_return":
        if not case.get("label_url") and status in ("requested", "approved"):
            return {
                "type": "issue_return_label",
                "endpoint": f"/agent/v1/after-sales/cases/{case.get('case_id')}/labels",
            }
        return {
            "type": "await_return_or_refund",
            "message": "Return label issued (placeholder). Refund can be processed after return is received.",
        }
    return {
        "type": "process_refund",
        "endpoint": f"/agent/v1/after-sales/cases/{case.get('case_id')}/refund",
    }


@router.get("/cases/{case_id}")
async def get_after_sales_case(
    case_id: str,
    buyer_ref: Optional[str] = Query(default=None),
    x_buyer_ref: Optional[str] = Header(None, alias="X-Buyer-Ref"),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    loaded = await _get_case_by_id(case_id)
    if not loaded:
        raise HTTPException(status_code=404, detail="Case not found")

    if str(loaded.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this case")

    # Enforce order access semantics by checking the underlying order.
    order = await get_order(str(loaded.get("order_id") or ""))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    canonical_buyer_ref = _normalize_buyer_ref(buyer_ref or x_buyer_ref)
    await _enforce_order_access(order=order, context=context, agent_user=agent_user, buyer_ref=canonical_buyer_ref)

    return {"status": "success", "case": _serialize_case(loaded), "next_action": _next_action_for_case(_serialize_case(loaded))}


@router.get("/orders/{order_id}/cases")
async def list_after_sales_cases_for_order(
    order_id: str,
    buyer_ref: Optional[str] = Query(default=None),
    x_buyer_ref: Optional[str] = Header(None, alias="X-Buyer-Ref"),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    oid = str(order_id or "").strip()
    if not oid:
        raise HTTPException(status_code=400, detail="order_id is required")

    order = await get_order(oid)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    canonical_buyer_ref = _normalize_buyer_ref(buyer_ref or x_buyer_ref)
    await _enforce_order_access(order=order, context=context, agent_user=agent_user, buyer_ref=canonical_buyer_ref)

    try:
        rows = await database.fetch_all(
            """
            SELECT * FROM after_sales_cases
            WHERE order_id = :order_id AND agent_id = :agent_id
            ORDER BY created_at DESC
            LIMIT 50
            """,
            {"order_id": oid, "agent_id": context.agent_id},
        )
        cases = [_serialize_case(dict(r)) for r in (rows or [])]
        return {"status": "success", "total": len(cases), "cases": cases}
    except Exception as e:
        logger.error(f"after_sales list_cases error: {e}")
        raise HTTPException(status_code=500, detail="Failed to list after-sales cases")


@router.post("/cases/{case_id}/labels")
async def issue_return_label_placeholder(
    case_id: str,
    buyer_ref: Optional[str] = Query(default=None),
    x_buyer_ref: Optional[str] = Header(None, alias="X-Buyer-Ref"),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    loaded = await _get_case_by_id(case_id)
    if not loaded:
        raise HTTPException(status_code=404, detail="Case not found")

    if str(loaded.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this case")

    order = await get_order(str(loaded.get("order_id") or ""))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    canonical_buyer_ref = _normalize_buyer_ref(buyer_ref or x_buyer_ref)
    await _enforce_order_access(order=order, context=context, agent_user=agent_user, buyer_ref=canonical_buyer_ref)

    if str(loaded.get("resolution") or "") != "refund_with_return":
        raise HTTPException(status_code=400, detail="Label issuance is only valid for refund_with_return cases")

    if loaded.get("label_url"):
        return {
            "status": "success",
            "case": _serialize_case(loaded),
            "label_url": loaded.get("label_url"),
            "next_action": {"type": "open_url", "url": loaded.get("label_url")},
        }

    # Placeholder label URL (no external provider integration yet).
    label_url = f"https://pivota.cc/return-labels/{case_id}"
    audit = loaded.get("audit_log") or []
    audit = audit if isinstance(audit, list) else []
    audit = _append_audit(audit, "label_issued_placeholder", {"label_url": label_url})

    try:
        await database.execute(
            """
            UPDATE after_sales_cases
            SET label_url = :label_url,
                status = 'label_issued',
                audit_log = CAST(:audit_log AS JSONB),
                updated_at = NOW()
            WHERE case_id = :case_id
            """,
            {"case_id": case_id, "label_url": label_url, "audit_log": json.dumps(audit, ensure_ascii=False)},
        )
    except Exception as e:
        logger.error(f"after_sales issue_label error: {e}")
        raise HTTPException(status_code=500, detail="Failed to issue return label")

    reloaded = await _get_case_by_id(case_id)
    return {
        "status": "success",
        "case": _serialize_case(reloaded),
        "label_url": label_url,
        "next_action": {"type": "open_url", "url": label_url},
    }


@router.post("/cases/{case_id}/refund")
async def process_case_refund(
    case_id: str,
    background_tasks: BackgroundTasks,
    buyer_ref: Optional[str] = Query(default=None),
    x_buyer_ref: Optional[str] = Header(None, alias="X-Buyer-Ref"),
    context: AgentContext = Depends(get_agent_context),
    agent_user: Optional[AgentUserContext] = Depends(get_agent_user_context),
):
    loaded = await _get_case_by_id(case_id)
    if not loaded:
        raise HTTPException(status_code=404, detail="Case not found")

    if str(loaded.get("agent_id") or "") != str(context.agent_id):
        raise HTTPException(status_code=403, detail="Not authorized for this case")

    order = await get_order(str(loaded.get("order_id") or ""))
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    canonical_buyer_ref = _normalize_buyer_ref(buyer_ref or x_buyer_ref)
    await _enforce_order_access(order=order, context=context, agent_user=agent_user, buyer_ref=canonical_buyer_ref)

    resolution = str(loaded.get("resolution") or "")
    status = str(loaded.get("status") or "")
    if resolution == "refund_with_return" and status not in ("label_issued", "return_received", "approved", "requested"):
        raise HTTPException(status_code=409, detail="Refund is not ready for this case status")

    requested_amount = loaded.get("requested_refund_amount")
    try:
        amount = float(requested_amount) if requested_amount is not None else None
    except Exception:
        amount = None

    audit = loaded.get("audit_log") or []
    audit = audit if isinstance(audit, list) else []
    audit = _append_audit(audit, "refund_requested", {"amount": amount})

    try:
        await database.execute(
            """
            UPDATE after_sales_cases
            SET status = 'refund_pending',
                audit_log = CAST(:audit_log AS JSONB),
                updated_at = NOW()
            WHERE case_id = :case_id
            """,
            {"case_id": case_id, "audit_log": json.dumps(audit, ensure_ascii=False)},
        )
    except Exception:
        pass

    # Execute refund via existing refund API (supports full + partial).
    refund_req = RefundRequest(
        order_id=str(order.get("order_id")),
        amount=amount,
        reason=str(loaded.get("reason_text") or loaded.get("reason_code") or "After-sales refund"),
        restore_inventory=True,
        idempotency_key=f"after_sales_case:{case_id}",
    )

    refund_result = await process_refund(
        order_id=str(order.get("order_id")),
        refund_request=refund_req,
        background_tasks=background_tasks,
        current_user={"role": "admin"},
    )

    # Reload order after refund attempt (PII-safe summary only)
    try:
        order = await get_order(str(order.get("order_id")))
    except Exception:
        pass

    # Update case status best-effort
    audit = _append_audit(audit, "refund_processed", {"result_status": refund_result.get("status") if isinstance(refund_result, dict) else None})
    next_status = "refund_processed"
    try:
        if isinstance(refund_result, dict) and refund_result.get("refund_type") == "partial":
            next_status = "partially_refunded"
        if isinstance(refund_result, dict) and refund_result.get("status") == "already_refunded":
            # Make the case converge to the order's current terminal status.
            next_status = str((order or {}).get("status") or "refunded")
        if isinstance(refund_result, dict) and refund_result.get("status") in ("partially_refunded", "refunded"):
            next_status = str(refund_result.get("status"))
    except Exception:
        pass

    try:
        await database.execute(
            """
            UPDATE after_sales_cases
            SET status = :status,
                audit_log = CAST(:audit_log AS JSONB),
                updated_at = NOW()
            WHERE case_id = :case_id
            """,
            {"case_id": case_id, "status": next_status, "audit_log": json.dumps(audit, ensure_ascii=False)},
        )
    except Exception:
        pass

    reloaded = await _get_case_by_id(case_id)
    order_summary = None
    try:
        if isinstance(order, dict) and order.get("order_id"):
            order_summary = {
                "order_id": str(order.get("order_id")),
                "status": str(order.get("status") or ""),
                "payment_status": str(order.get("payment_status") or ""),
                "fulfillment_status": str(order.get("fulfillment_status") or ""),
                "total": str(order.get("total") or ""),
                "currency": str(order.get("currency") or ""),
                "updated_at": str(order.get("updated_at") or ""),
            }
    except Exception:
        order_summary = None
    return {
        "status": "success",
        "case": _serialize_case(reloaded),
        "refund": refund_result,
        "order": order_summary,
    }
