"""Admin endpoint to apply migration 152 (agent_pdp_view evidence columns).

Production skips the startup migration runner (SKIP_HEAVY_STARTUP_INIT), so
additive migrations are applied via an authed admin route. 152 adds the two
JSONB columns the agent_pdp_view assembler (PR #875) now writes — without them
the assembler UPSERT errors, so this should be run right after the #875 deploy.

Idempotent: ADD COLUMN IF NOT EXISTS, so re-running is safe.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, List

from db.database import database
from utils.auth import ADMIN_ROLES, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])

_STATEMENTS = [
    "ALTER TABLE agent_pdp_view ADD COLUMN IF NOT EXISTS evidence_profile JSONB",
    "ALTER TABLE agent_pdp_view ADD COLUMN IF NOT EXISTS required_disclaimers JSONB",
    "COMMENT ON COLUMN agent_pdp_view.evidence_profile IS "
    "'Provenance-backed claims for the canonical product — highest-precedence "
    "evidence_profile across the content_key cluster (brand-official > supplier).'",
    "COMMENT ON COLUMN agent_pdp_view.required_disclaimers IS "
    "'Mandatory disclaimers (e.g. FDA/DSHEA supplement) derived per category_kind.'",
]


class MigrationResponse(BaseModel):
    status: str
    message: str
    steps: List[str]
    verification: Dict[str, Any]


@router.post("/run-152-agent-pdp-evidence", response_model=MigrationResponse)
async def run_migration_152(current_user: dict = Depends(get_current_user)) -> MigrationResponse:
    """Apply migration 152: agent_pdp_view.evidence_profile + required_disclaimers."""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin access required")

    steps: List[str] = []
    for stmt in _STATEMENTS:
        try:
            await database.execute(stmt)
            steps.append(f"✅ {stmt.split(' IS ')[0][:80]}")
        except Exception as exc:  # noqa: BLE001
            steps.append(f"❌ {stmt[:60]}: {exc}")
            logger.error("migration 152 statement failed: %s", exc)

    cols = await database.fetch_all(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'agent_pdp_view'
          AND column_name IN ('evidence_profile', 'required_disclaimers')
        """
    )
    present = [c["column_name"] for c in cols]
    ok = {"evidence_profile", "required_disclaimers"}.issubset(set(present))
    return MigrationResponse(
        status="success" if ok else "error",
        message="Migration 152 applied" if ok else "Migration 152 incomplete",
        steps=steps,
        verification={"agent_pdp_view_columns": present},
    )
