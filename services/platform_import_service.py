"""
Platform Import Service - EPIC‑2/3

Provides a thin abstraction over platform_import_tasks.
EPIC‑3 extends the skeleton with basic retry semantics.
"""

from typing import Any, Dict, List, Optional
from datetime import datetime

from db.platform_import_tasks import (
    create_import_task,
    get_import_task,
    list_import_tasks_for_merchant,
    update_import_task_status,
    get_next_scheduled_task,
)


async def schedule_import_task(
    merchant_id: str,
    source_type: str,
    connector: Optional[str] = None,
    saga_id: Optional[str] = None,
) -> int:
    """
    Create a new import task in `pending` state for the given merchant.

    Future EPICs will extend this to enqueue background workers.
    """
    return await create_import_task(
        merchant_id=merchant_id,
        source_type=source_type,
        connector=connector,
        saga_id=saga_id,
    )


async def get_import_task_details(task_id: int) -> Optional[Dict[str, Any]]:
    """Return a single ImportTask record."""
    return await get_import_task(task_id)


async def list_import_tasks(merchant_id: str) -> List[Dict[str, Any]]:
    """List ImportTasks for a merchant, newest first."""
    return await list_import_tasks_for_merchant(merchant_id)


async def get_next_ready_task() -> Optional[Dict[str, Any]]:
    """
    Return the next ImportTask that is ready to run, or None.

    EPIC‑3 considers both `pending` and `retry_scheduled` tasks whose
    next_run_at is due (see db.platform_import_tasks.get_next_scheduled_task).
    """
    return await get_next_scheduled_task()


async def mark_import_task_running(task_id: int, attempt: int) -> bool:
    """Mark task as running."""
    return await update_import_task_status(
        task_id=task_id,
        status="running",
        attempt=attempt,
    )


async def mark_import_task_succeeded(
    task_id: int,
    counts: Optional[Dict[str, Any]] = None,
) -> bool:
    """Mark task as succeeded."""
    return await update_import_task_status(
        task_id=task_id,
        status="succeeded",
        counts=counts or {},
    )


async def mark_import_task_failed(
    task_id: int,
    error: str,
    counts: Optional[Dict[str, Any]] = None,
) -> bool:
    """Mark task as failed with error message."""
    return await update_import_task_status(
        task_id=task_id,
        status="failed",
        counts=counts or {},
        error=error,
    )


async def mark_import_task_retry_scheduled(
    task_id: int,
    error: str,
    counts: Optional[Dict[str, Any]] = None,
    next_run_at: Optional[datetime] = None,
) -> bool:
    """
    Mark task as retry_scheduled with optional backoff.

    The worker will pick it up again once next_run_at is due.
    """
    return await update_import_task_status(
        task_id=task_id,
        status="retry_scheduled",
        counts=counts or {},
        error=error,
        next_run_at=next_run_at,
    )

