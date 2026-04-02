from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config.settings import settings
from scripts.catalog_migration_058 import _run as run_catalog_migration_058
from utils.auth import require_admin


router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])


class CatalogMigration058RunRequest(BaseModel):
    mode: Literal["apply", "apply-verify"] = "apply-verify"


class CatalogMigration058Response(BaseModel):
    mode: str
    database_kind: str
    migration_path: str
    success: bool
    apply: Optional[Dict[str, Any]] = None
    verification: Dict[str, Any]


def _resolved_database_url() -> str:
    database_url = str(settings.database_url or "").strip()
    if not database_url:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured")
    return database_url


@router.get("/verify/058", response_model=CatalogMigration058Response)
async def verify_catalog_migration_058(current_user: dict = Depends(require_admin)):
    del current_user
    return CatalogMigration058Response(**run_catalog_migration_058("verify", _resolved_database_url()))


@router.post("/run/058", response_model=CatalogMigration058Response)
async def run_catalog_migration_058_route(
    request: CatalogMigration058RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return CatalogMigration058Response(**run_catalog_migration_058(request.mode, _resolved_database_url()))
