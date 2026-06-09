"""Admin-triggered, report-only monthly statement assembly.

Statements are a report mirror of the balance system (consumption / overage
revenue / GMV → monthly_brand_statements). Overage is already collected inline
by the debit path, so this surface NEVER invoices or charges — it assembles and
freezes only. Scheduling (e.g. a monthly job) is intentionally left to ops; this
endpoint lets an admin assemble on demand and is the supported manual trigger.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from services.billing import monthly_brand_statements_service
from utils.auth import require_admin

router = APIRouter(tags=["Admin - Billing"])


class AssembleStatementRequest(BaseModel):
    merchant_id: str = Field(..., min_length=1)
    calendar_month: date = Field(
        ...,
        description="First day of the calendar month to assemble (YYYY-MM-01).",
    )


@router.post("/admin/billing/statements/assemble")
async def assemble_statement_report_only(
    body: AssembleStatementRequest,
    current_admin: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """Assemble + freeze a merchant's monthly statement (report-only, no charge)."""
    if body.calendar_month.day != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="calendar_month must be the first day of a calendar month",
        )

    statement_id = await monthly_brand_statements_service.assemble_and_freeze_report_only(
        body.merchant_id,
        body.calendar_month,
    )
    return {
        "ok": True,
        "report_only": True,
        "statement_id": statement_id,
        "merchant_id": body.merchant_id,
        "calendar_month": body.calendar_month.isoformat(),
        "status": "frozen",
        "note": (
            "Report mirror only — overage is charged inline by the debit path; "
            "this does not invoice."
        ),
    }
