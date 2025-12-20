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


@router.post("/tasks/{task_id}/retry")
async def retry_import_task(
    task_id: int,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """
    Retry a failed import task.
    
    Sets status='retry_scheduled', increments attempt, and sets next_run_at=NOW()
    for immediate retry by the worker.
    """
    from db.platform_import_tasks import get_import_task, update_import_task_status
    from datetime import datetime
    
    try:
        task = await get_import_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Only allow retry for failed tasks
        if task.get("status") not in ["failed", "retry_scheduled"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot retry task with status '{task.get('status')}'. Only 'failed' tasks can be retried."
            )
        
        # Update task for retry
        new_attempt = (task.get("attempt") or 1) + 1
        await update_import_task_status(
            task_id=task_id,
            status="retry_scheduled",
            attempt=new_attempt,
            next_run_at=datetime.utcnow()
        )
        
        return {
            "status": "success",
            "task_id": task_id,
            "new_status": "retry_scheduled",
            "attempt": new_attempt,
            "message": "Task scheduled for immediate retry"
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retry task: {str(exc)}"
        )


@router.post("/tasks/{task_id}/skip")
async def skip_import_task(
    task_id: int,
    current_user: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """
    Skip/cancel an import task.
    
    Sets status='cancelled' to remove it from the processing queue.
    """
    from db.platform_import_tasks import get_import_task, update_import_task_status
    
    try:
        task = await get_import_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Only allow skip for pending/failed/retry_scheduled tasks
        if task.get("status") in ["succeeded", "cancelled"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot skip task with status '{task.get('status')}'"
            )
        
        # Update task to cancelled
        await update_import_task_status(
            task_id=task_id,
            status="cancelled"
        )
        
        return {
            "status": "success",
            "task_id": task_id,
            "new_status": "cancelled",
            "message": "Task cancelled successfully"
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to skip task: {str(exc)}"
        )
