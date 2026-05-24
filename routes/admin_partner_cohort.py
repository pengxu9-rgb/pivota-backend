from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from services import cohort_target_evaluator
from utils.auth import require_admin

router = APIRouter(tags=["Admin - Partner Cohort"])


@router.get("/admin/partners/{channel_partner_id}/cohort")
async def get_partner_cohort_dashboard(
    channel_partner_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Cohort dashboard for a partner. Shows each target's progress."""

    try:
        targets = await cohort_target_evaluator.get_partner_target_progress(
            channel_partner_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "channel_partner_id": channel_partner_id,
        "targets": targets,
    }


@router.post("/admin/partners/{channel_partner_id}/cohort/evaluate")
async def trigger_cohort_evaluation(
    channel_partner_id: int,
    current_admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Manually trigger cohort target evaluation for this partner."""

    try:
        results = await cohort_target_evaluator.evaluate_partner_targets(
            channel_partner_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"channel_partner_id": channel_partner_id, "results": results}
