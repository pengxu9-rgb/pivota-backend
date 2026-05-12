"""Admin route to apply migration 082 to production.

082 fixes the seed_data_proposals.status CHECK constraint to include
'no_change' — required because the writer service from PR #426 can
legitimately return that status when a recovery proposal matches the
current row on every field. Codex hit the missing-value bug 2026-05-12
mid-recovery; admin-route pattern keeps the apply path auditable.

Cloned from routes/admin_run_migration_081.py — same auth, same shape,
narrower verification (just the constraint, not the column/table/trigger
from 081).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from db.database import database
from utils.auth import require_admin


router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "db"
    / "migrations"
    / "082_seed_data_proposals_allow_no_change.sql"
)

# Postgres exposes CHECK constraint definitions in pg_constraint.consrc /
# pg_get_constraintdef. We query whether 'no_change' is in the constraint
# expression — the simplest portable signal that 082 has been applied.
VERIFY_SQL = """
SELECT
  (SELECT count(*)::int
     FROM pg_constraint
     WHERE conname = 'seed_data_proposals_status_check'
       AND pg_get_constraintdef(oid) LIKE '%no_change%') AS constraint_includes_no_change
"""


class SeedProposalsStatus082RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class SeedProposalsStatus082Response(BaseModel):
    mode: str
    database_kind: str
    migration_path: str
    success: bool
    apply: Optional[Dict[str, Any]] = None
    verification: Dict[str, int]


def _resolved_database_url() -> str:
    database_url = str(getattr(database, "url", "") or "").strip()
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url.startswith("sqlite+aiosqlite://"):
        database_url = database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured")
    return database_url


def _database_kind(database_url: str) -> str:
    return "sqlite" if database_url.startswith("sqlite") else "postgres"


def _read_migration_sql() -> str:
    if not MIGRATION_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"migration file not found: {MIGRATION_PATH}",
        )
    return MIGRATION_PATH.read_text(encoding="utf-8")


def _verify(database_url: str) -> Dict[str, int]:
    # SQLite doesn't carry pg_constraint; treat verify as a no-op pass
    # there (the writer's local sqlite test DBs build their schema
    # from the inline 081 source which now already contains 'no_change').
    if database_url.startswith("sqlite"):
        return {"constraint_includes_no_change": 1}
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(VERIFY_SQL)).mappings().one()
            return {"constraint_includes_no_change": int(row["constraint_includes_no_change"])}
    finally:
        engine.dispose()


def _apply(database_url: str) -> Dict[str, Any]:
    sql = _read_migration_sql()
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()
    return {
        "applied": True,
        "migration_bytes": len(sql.encode("utf-8")),
    }


def _run(mode: str, database_url: str) -> Dict[str, Any]:
    apply_result: Optional[Dict[str, Any]] = None
    if mode == "apply":
        apply_result = _apply(database_url)

    verification = _verify(database_url)
    success = verification == {"constraint_includes_no_change": 1}
    return {
        "mode": mode,
        "database_kind": _database_kind(database_url),
        "migration_path": str(MIGRATION_PATH),
        "success": success,
        "apply": apply_result,
        "verification": verification,
    }


@router.get("/verify/082", response_model=SeedProposalsStatus082Response)
async def verify_seed_proposals_status_082(current_user: dict = Depends(require_admin)):
    del current_user
    return SeedProposalsStatus082Response(**_run("verify", _resolved_database_url()))


@router.post("/run/082", response_model=SeedProposalsStatus082Response)
async def run_seed_proposals_status_082_route(
    request: SeedProposalsStatus082RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return SeedProposalsStatus082Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/082", response_model=SeedProposalsStatus082Response)
async def post_run_seed_proposals_status_082_route(
    request: SeedProposalsStatus082RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return SeedProposalsStatus082Response(**_run(request.mode, _resolved_database_url()))
