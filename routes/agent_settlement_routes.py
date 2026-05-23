"""[FIX-09] Legacy agent settlement routes - retired.

The Phase 5.5/6 settlement system was deprecated in PR #612/#613. v1.3
monetization runs through the new Stage 1+ pipeline. This file remains only to
serve 410-Gone for clients still calling legacy paths; do not re-add live
handlers here.

Rollback: set LEGACY_SETTLEMENT_LIVE=true to bypass the 410 stubs (emergency
only).
"""

import os

from fastapi import APIRouter, HTTPException, status

router = APIRouter(
    prefix="/agents/{agent_id}/settlements",
    tags=["[Phase 5.6] Agent Settlements (retired)"],
)


def _maybe_bypass_410():
    return os.getenv("LEGACY_SETTLEMENT_LIVE", "").strip().lower() == "true"


def _gone():
    if _maybe_bypass_410():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="legacy bypass requested but legacy handlers are removed",
        )
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "status": "gone",
            "message": "agent settlement legacy endpoints retired; use Stage-1 monetization endpoints",
        },
    )


@router.get("")
async def list_settlements_gone(agent_id: str):
    _gone()


@router.post("")
async def create_settlement_gone(agent_id: str):
    _gone()


@router.get("/pending")
async def get_pending_settlements_gone(agent_id: str):
    _gone()


@router.post("/calculate")
async def calculate_settlement_gone(agent_id: str):
    _gone()


@router.get("/payouts")
async def list_payouts_gone(agent_id: str):
    _gone()


@router.get("/{settlement_id}")
async def get_settlement_gone(agent_id: str, settlement_id: str):
    _gone()
