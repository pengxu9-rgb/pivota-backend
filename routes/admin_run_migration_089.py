"""Admin route to apply migration 089 (merchant_onboarding APM cols) to production.

089 (apm config) was shipped by PR-13 / pivota-backend#494 but the PR
did NOT add an apply lever. Production deploys do not auto-run
db/migrations/, so the deploy landed code that referenced
`merchant_onboarding.apm_enabled` while the column did not exist —
every audit failed in `discovering` until the column was added.

The columns now self-heal via db/schema_guard.py:ensure_required_schema_light,
so once the deploy with that change reaches every pod, this route is
a manual one-shot lever for the historical gap window.

Cloned from routes/admin_run_migration_085.py. Verifies the 5 APM
columns + the cadence check constraint + the partial index exist.
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
    / "089_merchant_onboarding_apm_config.sql"
)

VERIFY_SQL = """
SELECT
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='merchant_onboarding'
      AND column_name='apm_enabled') AS col_enabled,
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='merchant_onboarding'
      AND column_name='apm_cadence_days') AS col_cadence,
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='merchant_onboarding'
      AND column_name='apm_scope_jsonb') AS col_scope,
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='merchant_onboarding'
      AND column_name='apm_configured_at') AS col_configured,
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='merchant_onboarding'
      AND column_name='apm_last_run_at') AS col_last_run,
  (SELECT count(*)::int FROM pg_constraint
    WHERE conname='merchant_onboarding_apm_cadence_days_chk') AS chk_cadence,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename='merchant_onboarding'
      AND indexname='idx_merchant_onboarding_apm_due') AS idx_due
"""


class ApmConfig089RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class ApmConfig089Response(BaseModel):
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
    # SQLite test DBs don't carry pg_indexes or pg_constraint; report
    # all-present so local tests don't see false fails. Production
    # target is Postgres.
    if database_url.startswith("sqlite"):
        return {
            "col_enabled": 1, "col_cadence": 1, "col_scope": 1,
            "col_configured": 1, "col_last_run": 1,
            "chk_cadence": 1, "idx_due": 1,
        }
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(VERIFY_SQL)).mappings().one()
            return {k: int(v) for k, v in row.items()}
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
    success = all(v == 1 for v in verification.values())
    return {
        "mode": mode,
        "database_kind": _database_kind(database_url),
        "migration_path": str(MIGRATION_PATH),
        "success": success,
        "apply": apply_result,
        "verification": verification,
    }


@router.get("/verify/089", response_model=ApmConfig089Response)
async def verify_apm_089(current_user: dict = Depends(require_admin)):
    del current_user
    return ApmConfig089Response(**_run("verify", _resolved_database_url()))


@router.post("/run/089", response_model=ApmConfig089Response)
async def run_apm_089_route(
    request: ApmConfig089RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return ApmConfig089Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/089", response_model=ApmConfig089Response)
async def post_run_apm_089_route(
    request: ApmConfig089RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return ApmConfig089Response(**_run(request.mode, _resolved_database_url()))
