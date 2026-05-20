"""Admin route to apply migration 098 (index_pipeline_state) to production.

Creates the per-product pipeline stage machine table and its indexes.
This table is written exclusively by jobs/nightly_index_health_job.py and
read by the inspection API (GET /api/admin/index/inspect in PIVOTA-Agent).

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
    / "098_index_pipeline_state.sql"
)

VERIFY_SQL = """
SELECT
  (SELECT count(*)::int FROM information_schema.tables
    WHERE table_name = 'index_pipeline_state') AS table_exists,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename = 'index_pipeline_state'
      AND indexname = 'idx_ips_serving_eligible') AS idx_serving_eligible,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename = 'index_pipeline_state'
      AND indexname = 'idx_ips_pipeline_stage') AS idx_pipeline_stage,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename = 'index_pipeline_state'
      AND indexname = 'idx_ips_merchant_stage') AS idx_merchant_stage,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename = 'index_pipeline_state'
      AND indexname = 'idx_ips_last_consolidated') AS idx_last_consolidated,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename = 'index_pipeline_state'
      AND indexname = 'idx_ips_pivota_signature') AS idx_pivota_signature
"""


class IndexPipelineState098RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class IndexPipelineState098Response(BaseModel):
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
            "idx_serving_eligible": 1,
            "idx_pipeline_stage": 1,
            "idx_merchant_stage": 1,
            "idx_last_consolidated": 1,
            "idx_pivota_signature": 1,
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


@router.get("/verify/098", response_model=IndexPipelineState098Response)
async def verify_098(current_user: dict = Depends(require_admin)):
    del current_user
    return IndexPipelineState098Response(**_run("verify", _resolved_database_url()))


@router.post("/run/098", response_model=IndexPipelineState098Response)
async def run_098_route(
    request: IndexPipelineState098RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return IndexPipelineState098Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/098", response_model=IndexPipelineState098Response)
async def post_run_098_route(
    request: IndexPipelineState098RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return IndexPipelineState098Response(**_run(request.mode, _resolved_database_url()))
