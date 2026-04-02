from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from config.settings import settings
from scripts.catalog_migration_059 import _run as run_catalog_migration_059
from utils.auth import require_admin


router = APIRouter(prefix="/admin/migrations", tags=["Admin Migrations"])


class CatalogMigration059RunRequest(BaseModel):
    mode: Literal["apply", "apply-verify"] = "apply-verify"


class CatalogMigration059Response(BaseModel):
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


@router.get("/verify/059", response_model=CatalogMigration059Response)
async def verify_catalog_migration_059(current_user: dict = Depends(require_admin)):
    del current_user
    return CatalogMigration059Response(**run_catalog_migration_059("verify", _resolved_database_url()))


@router.post("/run/059", response_model=CatalogMigration059Response)
async def run_catalog_migration_059_route(
    request: CatalogMigration059RunRequest,
    current_user: dict = Depends(require_admin),
):
    del current_user
    return CatalogMigration059Response(**run_catalog_migration_059(request.mode, _resolved_database_url()))
