"""Admin route to apply migration 164 (merchant_onboarding operating_mode) to production.

Migration 164 adds the operating_mode discriminator column to merchant_onboarding
(storefront vs store_less brand) and relaxes the NOT NULL constraint on store_url.
The migration runner is skipped in prod; this endpoint is the manual apply lever.

The columns now also self-heal via db/schema_guard.py:ensure_required_schema_light,
so a restart after this fix ships will auto-apply if the column is still missing.

Cloned from routes/admin_run_migration_089.py.
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
    / "164_merchant_onboarding_operating_mode.sql"
)

VERIFY_SQL = """
SELECT
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='merchant_onboarding'
      AND column_name='operating_mode') AS col_operating_mode,
  (SELECT is_nullable FROM information_schema.columns
    WHERE table_name='merchant_onboarding'
      AND column_name='store_url') AS store_url_nullable
"""


class Migration164RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class Migration164Response(BaseModel):
    mode: str
    database_kind: str
    migration_path: str
    success: bool
    apply: Optional[Dict[str, Any]] = None
    verification: Dict[str, Any]


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


def _verify(database_url: str) -> Dict[str, Any]:
    if database_url.startswith("sqlite"):
        return {"col_operating_mode": 1, "store_url_nullable": "YES"}
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(VERIFY_SQL)).mappings().one()
            return {k: v for k, v in row.items()}
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


def _is_success(verification: Dict[str, Any]) -> bool:
    return (
        int(verification.get("col_operating_mode") or 0) == 1
        and str(verification.get("store_url_nullable") or "").upper() == "YES"
    )


def _run(mode: str, database_url: str) -> Dict[str, Any]:
    apply_result: Optional[Dict[str, Any]] = None
    if mode == "apply":
        apply_result = _apply(database_url)

    verification = _verify(database_url)
    success = _is_success(verification)
    return {
        "mode": mode,
        "database_kind": _database_kind(database_url),
        "migration_path": str(MIGRATION_PATH),
        "success": success,
        "apply": apply_result,
        "verification": verification,
    }


@router.get("/verify/164", response_model=Migration164Response)
async def verify_migration_164(current_user: dict = Depends(require_admin)):
    del current_user
    return Migration164Response(**_run("verify", _resolved_database_url()))


@router.post("/run/164", response_model=Migration164Response)
async def run_migration_164_route(
    request: Migration164RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return Migration164Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/164", response_model=Migration164Response)
async def post_run_migration_164_route(
    request: Migration164RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return Migration164Response(**_run(request.mode, _resolved_database_url()))
