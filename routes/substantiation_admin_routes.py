"""P1 — substantiation grading admin endpoints (Pivota employees).

  GET  /api/admin/brands/evidence?status=submitted     -> the grading queue
  POST /api/admin/brands/evidence/{evidence_id}/grade  -> grade one evidence row

Employee-only (get_current_employee). Flag-gated (ENABLE_SUBSTANTIATION_GRADING,
default OFF -> 404 when disabled). On grade='substantiated' the evidence's product
advances attested -> substantiated and its served PDP refreshes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import db.brand_attestation_evidence as evidence_db
from services import substantiation_grading_service as grading
from utils.auth import get_current_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/brands", tags=["substantiation-grading"])


def _require_enabled() -> None:
    if not grading.grading_enabled():
        raise HTTPException(
            status_code=404, detail="substantiation grading is not enabled"
        )


def _employee_id(employee: Dict[str, Any]) -> str:
    return str(
        employee.get("employee_id")
        or employee.get("user_id")
        or employee.get("sub")
        or ""
    )


class GradeBody(BaseModel):
    grade: str = Field(..., description="substantiated | flagged | rejected")
    notes: Optional[str] = Field(None, description="Reviewer note (why this grade).")


@router.get("/evidence")
async def list_evidence(
    status: str = Query("submitted", description="grading_status to list (queue = submitted)"),
    limit: int = Query(100, ge=1, le=500),
    employee: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """The grading queue: brand-submitted evidence awaiting (or in) a grade."""
    _require_enabled()
    rows = await evidence_db.list_evidence_by_status(status, limit=limit)
    return {"status": status, "count": len(rows), "evidence": rows}


@router.post("/evidence/{evidence_id}/grade")
async def grade_evidence(
    evidence_id: int,
    body: GradeBody,
    employee: Dict[str, Any] = Depends(get_current_employee),
) -> Dict[str, Any]:
    """Grade one submitted evidence row. On 'substantiated' the product advances
    attested -> substantiated and its served PDP refreshes. Pivota-employee only;
    the grader is recorded on the evidence row."""
    _require_enabled()
    graded_by = _employee_id(employee)
    if not graded_by:
        raise HTTPException(status_code=403, detail="employee identity required")
    result = await grading.grade_evidence(
        evidence_id, graded_by=graded_by, grade=body.grade, notes=body.notes
    )
    status = result.get("status")
    if status == "not_found":
        raise HTTPException(status_code=404, detail="evidence not found")
    if status == "invalid_grade":
        raise HTTPException(status_code=422, detail=result)
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="employee identity required")
    return result
