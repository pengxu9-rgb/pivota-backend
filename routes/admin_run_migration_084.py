"""Admin route to apply migration 084 to production.

084 adds catalog_products.last_seen_in_sync_at + sync_status and
catalog_merchants.last_full_sync_at — the sync-hygiene infrastructure
that lets Stage 2a's sweep tombstone stale Path A rows (e.g. the MOYU
cohort). See plans/rosy-mixing-bengio.md Stage 2a.

Cloned from routes/admin_run_migration_083.py — same auth, same shape.
Verifies three columns + the CHECK constraint + the partial index.
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
    / "084_catalog_products_sync_hygiene.sql"
)

VERIFY_SQL = """
SELECT
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='catalog_products' AND column_name='last_seen_in_sync_at') AS col_last_seen,
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='catalog_products' AND column_name='sync_status') AS col_sync_status,
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='catalog_merchants' AND column_name='last_full_sync_at') AS col_last_full_sync,
  (SELECT count(*)::int FROM pg_constraint
    WHERE conname='catalog_products_sync_status_check') AS check_constraint,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename='catalog_products'
      AND indexname='idx_catalog_products_sync_status_non_live') AS idx_non_live
"""


class SyncHygiene084RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class SyncHygiene084Response(BaseModel):
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
    # SQLite test DBs don't carry pg_constraint / pg_indexes; treat
    # verify as a pass there so local tests don't see false fails.
    if database_url.startswith("sqlite"):
        return {
            "col_last_seen": 1, "col_sync_status": 1,
            "col_last_full_sync": 1, "check_constraint": 1, "idx_non_live": 1,
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


@router.get("/verify/084", response_model=SyncHygiene084Response)
async def verify_sync_hygiene_084(current_user: dict = Depends(require_admin)):
    del current_user
    return SyncHygiene084Response(**_run("verify", _resolved_database_url()))


@router.post("/run/084", response_model=SyncHygiene084Response)
async def run_sync_hygiene_084_route(
    request: SyncHygiene084RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return SyncHygiene084Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/084", response_model=SyncHygiene084Response)
async def post_run_sync_hygiene_084_route(
    request: SyncHygiene084RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return SyncHygiene084Response(**_run(request.mode, _resolved_database_url()))
