"""Generic admin route to apply a pending DB migration by number.

The team applies migrations through one-off admin endpoints (CI is billing-blocked,
so there is no automatic migrate-on-deploy). Dedicated runners exist for a few
(164/165); this generic runner covers the rest by number — it locates
``db/migrations/<NNN>_*.sql`` and executes it. Our migrations are additive and
idempotent (``ADD COLUMN/CREATE TABLE/CREATE INDEX ... IF NOT EXISTS`` inside an
explicit ``BEGIN; ... COMMIT;``), so re-running is safe.

Mirrors the file-execution mechanics of ``admin_run_migration_165.py`` (whole-file
``conn.execute`` inside ``engine.begin()`` — proven to handle files that carry their
own ``BEGIN/COMMIT``). ``require_admin`` gated; ``apply`` is opt-in (default dry-run).
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

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "db" / "migrations"


def _resolved_database_url() -> str:
    database_url = str(getattr(database, "url", "") or "").strip()
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url.startswith("sqlite+aiosqlite://"):
        database_url = database_url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured")
    return database_url


def _resolve_migration_file(number: str) -> Path:
    if not (number.isdigit() and 1 <= len(number) <= 4):
        raise HTTPException(
            status_code=422,
            detail="migration number must be 1-4 digits, e.g. 161",
        )
    matches = sorted(MIGRATIONS_DIR.glob(f"{number}_*.sql"))
    if not matches:
        raise HTTPException(
            status_code=404,
            detail=f"no migration file matching {number}_*.sql",
        )
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"ambiguous migration number {number}: {[m.name for m in matches]}",
        )
    return matches[0]


class RunRequest(BaseModel):
    mode: Literal["dry_run", "apply"] = "dry_run"


class RunResponse(BaseModel):
    number: str
    file: str
    database_kind: str
    mode: str
    applied: bool
    migration_bytes: int
    skipped: Optional[str] = None


def _database_kind(database_url: str) -> str:
    return "sqlite" if database_url.startswith("sqlite") else "postgres"


@router.get("/pending/{number}", response_model=RunResponse)
async def inspect_pending_migration(
    number: str,
    current_user: dict = Depends(require_admin),
):
    """Confirm a migration file resolves (read-only; never touches the DB)."""
    del current_user
    path = _resolve_migration_file(number)
    sql = path.read_text(encoding="utf-8")
    return RunResponse(
        number=number,
        file=path.name,
        database_kind=_database_kind(_resolved_database_url()),
        mode="inspect",
        applied=False,
        migration_bytes=len(sql.encode("utf-8")),
    )


@router.post("/pending/{number}/run", response_model=RunResponse)
async def run_pending_migration(
    number: str,
    request: RunRequest,
    current_user: dict = Depends(require_admin),
):
    """Apply ``db/migrations/<number>_*.sql`` (idempotent). Default dry-run."""
    del current_user
    path = _resolve_migration_file(number)
    sql = path.read_text(encoding="utf-8")
    database_url = _resolved_database_url()
    kind = _database_kind(database_url)

    if request.mode != "apply":
        return RunResponse(
            number=number, file=path.name, database_kind=kind,
            mode="dry_run", applied=False,
            migration_bytes=len(sql.encode("utf-8")),
        )

    if kind == "sqlite":
        # The migrations are Postgres DDL; nothing to do on the local sqlite path.
        return RunResponse(
            number=number, file=path.name, database_kind=kind,
            mode="apply", applied=False, skipped="sqlite",
            migration_bytes=len(sql.encode("utf-8")),
        )

    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()

    return RunResponse(
        number=number, file=path.name, database_kind=kind,
        mode="apply", applied=True,
        migration_bytes=len(sql.encode("utf-8")),
    )
