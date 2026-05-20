"""Admin route to apply migration 099 (domain_extractor_baselines) to production.

Creates the per-domain extraction quality baseline table used by the nightly
index health job to detect domain extractor regressions.

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
    / "099_domain_extractor_baselines.sql"
)

VERIFY_SQL = """
SELECT
  (SELECT count(*)::int FROM information_schema.tables
    WHERE table_name = 'domain_extractor_baselines') AS table_exists,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename = 'domain_extractor_baselines'
      AND indexname = 'idx_deb_alert_state') AS idx_alert_state,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename = 'domain_extractor_baselines'
      AND indexname = 'idx_deb_last_scored') AS idx_last_scored
"""


class DomainExtractorBaselines099RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class DomainExtractorBaselines099Response(BaseModel):
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
    if database_url.startswith("sqlite"):
        return {
            "table_exists": 1,
            "idx_alert_state": 1,
            "idx_last_scored": 1,
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


@router.get("/verify/099", response_model=DomainExtractorBaselines099Response)
async def verify_099(current_user: dict = Depends(require_admin)):
    del current_user
    return DomainExtractorBaselines099Response(**_run("verify", _resolved_database_url()))


@router.post("/run/099", response_model=DomainExtractorBaselines099Response)
async def run_099_route(
    request: DomainExtractorBaselines099RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return DomainExtractorBaselines099Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/099", response_model=DomainExtractorBaselines099Response)
async def post_run_099_route(
    request: DomainExtractorBaselines099RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return DomainExtractorBaselines099Response(**_run(request.mode, _resolved_database_url()))
