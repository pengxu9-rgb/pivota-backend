"""
Admin Platform Import Endpoints

Allows administrators to trigger catalog ImportTask processing manually.
EPIC‑2 skeleton: advances ImportTask status without performing real imports.
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Dict, Any

from utils.auth import require_admin
from jobs.catalog_import_worker import process_next_import_task

router = APIRouter(
    prefix="/admin/platform-import",
    tags=["Admin - Platform Import"]
)


@router.post("/run-once")
async def run_import_once(current_user: dict = Depends(require_admin)) -> Dict[str, Any]:
    """
    Process the next pending Platform ImportTask, if any.

    This is an admin-only endpoint and is safe to call repeatedly.
    """
    try:
        result = await process_next_import_task()
        return {
            "status": "success",
            "result": result,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process import task: {str(exc)}",
        )

