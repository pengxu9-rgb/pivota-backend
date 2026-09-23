"""Admin surface for GMV invoice credits: review, approve (which issues), retry, cancel.

A thin layer over services/gmv_invoice_credits.py. Credits are computed automatically as `pending`
when a refund lands on an invoiced day; NOTHING reaches Stripe until an admin approves one here.
Approving issues the credit note at once (against the amount due on an open invoice, to the
customer's Stripe balance on a paid one; never a cash refund).

AUTH: the guard is on the ROUTER (`dependencies=[Depends(require_admin)]`), so a handler added to
this file later inherits it instead of shipping open. Handlers that record WHO acted also take
`require_admin` as a parameter to read the identity; FastAPI caches the dependency per request.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from db.database import database
from services import gmv_invoice_credits as credits
from utils.auth import require_admin

router = APIRouter(
    prefix="/admin/billing/invoice-credits",
    tags=["Admin - Billing"],
    dependencies=[Depends(require_admin)],
)

# The refunded edges behind a credit: the rollup group's edges (same day, merchant, agent, channel
# partner) that carry a refund. Uses the rollup's own UTC day bounds.
_GROUP_REFUNDED_EDGES_SQL = """
SELECT e.edge_id, e.order_id, e.gross_attributed_gmv_cents, e.refund_amount_cents, e.refund_ids,
       e.latest_refund_at
FROM gmv_attribution_daily g
JOIN commerce_attribution_edges e
  ON e.merchant_id = g.merchant_id
 AND e.created_at >= CAST(g.date AS TIMESTAMP) AT TIME ZONE 'UTC'
 AND e.created_at < CAST(g.date + 1 AS TIMESTAMP) AT TIME ZONE 'UTC'
 AND e.agent_id IS NOT DISTINCT FROM g.agent_id
 AND e.channel_partner_id IS NOT DISTINCT FROM g.channel_partner_id
WHERE g.id = :rollup_id AND COALESCE(e.refund_amount_cents, 0) > 0
ORDER BY e.edge_id
LIMIT 200
"""


def _actor(admin: Dict[str, Any]) -> str:
    return str(admin.get("email") or admin.get("sub") or "admin")


async def _credit_or_404(credit_id: int) -> Dict[str, Any]:
    credit = await credits.get_credit(credit_id)
    if not credit:
        raise HTTPException(status_code=404, detail=f"unknown invoice credit {credit_id}")
    return credit


def _not_in_state(credit: Dict[str, Any], action: str, allowed) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": f"cannot {action} a credit in status {credit.get('status')!r}; allowed from {list(allowed)}",
        "credit_id": credit.get("id"), "status": credit.get("status")})


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = Field(max_length=2000)

    @field_validator("reason")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a cancel needs a reason")
        return value.strip()


class ApproveBody(BaseModel):
    """The amount the approver reviewed. A pending credit grows when another refund lands; the
    approval is refused (409) rather than issuing an amount nobody looked at."""
    model_config = ConfigDict(extra="forbid")

    amount_cents: StrictInt = Field(gt=0)


class ComputeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: StrictStr = Field(min_length=1, max_length=100)
    day: date


@router.get("", response_model=None)
async def list_credits(status: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500)):
    try:
        rows = await credits.list_credits(status=status, limit=limit)
    except credits.CreditActionRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return jsonable_encoder({"credits": rows, "count": len(rows)})


@router.get("/summary", response_model=None)
async def summary():
    return jsonable_encoder({"by_status": await credits.status_summary()})


@router.get("/{credit_id}", response_model=None)
async def credit_detail(credit_id: int):
    credit = await _credit_or_404(credit_id)
    edges = [dict(r) for r in await database.fetch_all(
        _GROUP_REFUNDED_EDGES_SQL, {"rollup_id": int(credit["rollup_id"])})]
    return jsonable_encoder({"credit": credit, "refunded_edges": edges})


@router.post("/{credit_id}/approve", response_model=None)
async def approve_credit(credit_id: int, body: ApproveBody, admin: Dict[str, Any] = Depends(require_admin)):
    """Approve a pending credit at the reviewed amount and issue it to Stripe now."""
    credit = await _credit_or_404(credit_id)
    if credit.get("status") != "pending":
        return _not_in_state(credit, "approve", ("pending",))
    try:
        approved = await credits.approve(credit_id, by=_actor(admin), expected_amount_cents=body.amount_cents)
    except credits.CreditActionRefused as exc:
        if exc.code == "line_voided":
            # A dispute replaced the line after the credit was computed; cancel it now.
            await credits.compute_credit_for_line(int(credit["billing_run_item_id"]))
            return JSONResponse(status_code=409, content={
                "detail": "the invoiced line was voided by a dispute; the credit has been cancelled",
                "credit_id": credit_id})
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not approved:
        current = await _credit_or_404(credit_id)
        if current.get("status") == "pending":
            return JSONResponse(status_code=409, content={
                "detail": "the credit's amount changed since it was reviewed; review it again",
                "credit_id": credit_id, "reviewed_amount_cents": body.amount_cents,
                "amount_cents": current.get("amount_cents")})
        # moved out of pending (a recompute superseded it, or another admin) between the reads
        return _not_in_state(current, "approve", ("pending",))
    result = await credits.issue(credit_id, by=_actor(admin))
    return jsonable_encoder({"credit_id": credit_id, "approved_by": _actor(admin), "issue": result.__dict__})


@router.post("/{credit_id}/issue", response_model=None)
async def retry_issue(credit_id: int, admin: Dict[str, Any] = Depends(require_admin)):
    """Retry a credit Stripe refused (`failed`), or one left `issuing` by a crash. Records who."""
    credit = await _credit_or_404(credit_id)
    result = await credits.issue(credit_id, by=_actor(admin))
    if result.status == "not_issuable":
        return _not_in_state(await _credit_or_404(credit_id), "issue", ("approved", "failed", "issuing (stale)"))
    return jsonable_encoder({"credit_id": credit_id, "issue": result.__dict__, "was": credit.get("status")})


@router.post("/{credit_id}/cancel", response_model=None)
async def cancel_credit(credit_id: int, body: CancelBody, admin: Dict[str, Any] = Depends(require_admin)):
    credit = await _credit_or_404(credit_id)
    if credit.get("status") not in credits.OPEN_STATUSES:
        return _not_in_state(credit, "cancel", credits.OPEN_STATUSES)
    try:
        outcome = await credits.cancel(credit_id, by=_actor(admin), reason=body.reason)
    except credits.CreditActionRefused as exc:
        if exc.code == "stripe_unreachable":
            return JSONResponse(status_code=503, content={
                "detail": "Stripe could not be checked for a credit note this failed credit may have "
                          "created; not cancelled. Retry the cancel.", "credit_id": credit_id})
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if outcome == "issued":
        current = await _credit_or_404(credit_id)
        return JSONResponse(status_code=409, content={
            "detail": "not cancelled: Stripe had already issued a credit note for this credit (its "
                      "failure hid it); it is now recorded as issued",
            "credit_id": credit_id, "status": "issued",
            "stripe_credit_note_id": current.get("stripe_credit_note_id")})
    if outcome != "cancelled":
        return _not_in_state(await _credit_or_404(credit_id), "cancel", credits.OPEN_STATUSES)
    return {"credit_id": credit_id, "status": "cancelled"}


@router.post("/compute", response_model=None)
async def compute_for_day(body: ComputeBody):
    """Recompute what one merchant's invoiced day is owed (pending credits only; issues nothing)."""
    result = await credits.compute_credits_for_day(body.day, body.merchant_id)
    return jsonable_encoder(result)
