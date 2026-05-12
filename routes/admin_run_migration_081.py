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
    / "081_seed_content_lock_and_proposals.sql"
)

VERIFY_SQL = """
SELECT
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='external_product_seeds' AND column_name='content_lock') AS col,
  (SELECT count(*)::int FROM information_schema.tables
    WHERE table_name='seed_data_proposals') AS tbl,
  (SELECT count(*)::int FROM information_schema.triggers
    WHERE event_object_table='external_product_seeds'
      AND trigger_name='trg_enforce_seed_data_lock') AS trg
"""


class SeedContentMigration081RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class SeedContentMigration081Response(BaseModel):
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
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(VERIFY_SQL)).mappings().one()
            return {
                "col": int(row["col"]),
                "tbl": int(row["tbl"]),
                "trg": int(row["trg"]),
            }
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
    success = verification == {"col": 1, "tbl": 1, "trg": 1}
    return {
        "mode": mode,
        "database_kind": _database_kind(database_url),
        "migration_path": str(MIGRATION_PATH),
        "success": success,
        "apply": apply_result,
        "verification": verification,
    }


@router.get("/verify/081", response_model=SeedContentMigration081Response)
async def verify_seed_content_migration_081(current_user: dict = Depends(require_admin)):
    del current_user
    return SeedContentMigration081Response(**_run("verify", _resolved_database_url()))


@router.post("/run/081", response_model=SeedContentMigration081Response)
async def run_seed_content_migration_081_route(
    request: SeedContentMigration081RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return SeedContentMigration081Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/081", response_model=SeedContentMigration081Response)
async def post_run_seed_content_migration_081_route(
    request: SeedContentMigration081RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return SeedContentMigration081Response(**_run(request.mode, _resolved_database_url()))
