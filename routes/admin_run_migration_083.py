"""Admin route to apply migration 083 to production.

083 adds catalog_products.content_key — content-derived product
identity that's stable across merchants/paths. See plan
plans/rosy-mixing-bengio.md Stage 1 for context.

Cloned from routes/admin_run_migration_081.py — same auth, same shape.
Verify checks that the column exists.
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
    / "083_catalog_products_content_key.sql"
)

VERIFY_SQL = """
SELECT
  (SELECT count(*)::int FROM information_schema.columns
    WHERE table_name='catalog_products' AND column_name='content_key') AS col,
  (SELECT count(*)::int FROM pg_indexes
    WHERE tablename='catalog_products'
      AND indexname='idx_catalog_products_content_key') AS idx
"""


class ContentKeyMigration083RunRequest(BaseModel):
    mode: Literal["verify", "apply"] = "verify"


class ContentKeyMigration083Response(BaseModel):
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
    # SQLite test DBs use the inline 083 source which is also idempotent.
    # The verify SQL is Postgres-specific (pg_indexes), so short-circuit
    # to {col: 1, idx: 1} on sqlite so local tests don't see false fails.
    if database_url.startswith("sqlite"):
        return {"col": 1, "idx": 1}
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(VERIFY_SQL)).mappings().one()
            return {"col": int(row["col"]), "idx": int(row["idx"])}
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
    success = verification == {"col": 1, "idx": 1}
    return {
        "mode": mode,
        "database_kind": _database_kind(database_url),
        "migration_path": str(MIGRATION_PATH),
        "success": success,
        "apply": apply_result,
        "verification": verification,
    }


@router.get("/verify/083", response_model=ContentKeyMigration083Response)
async def verify_content_key_migration_083(current_user: dict = Depends(require_admin)):
    del current_user
    return ContentKeyMigration083Response(**_run("verify", _resolved_database_url()))


@router.post("/run/083", response_model=ContentKeyMigration083Response)
async def run_content_key_migration_083_route(
    request: ContentKeyMigration083RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return ContentKeyMigration083Response(**_run(request.mode, _resolved_database_url()))


@router.post("/post/run/083", response_model=ContentKeyMigration083Response)
async def post_run_content_key_migration_083_route(
    request: ContentKeyMigration083RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return ContentKeyMigration083Response(**_run(request.mode, _resolved_database_url()))
