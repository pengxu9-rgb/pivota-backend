"""
Platform Import Tasks - EPIC‑2 Skeleton

Tracks catalog import jobs for Platform merchants. This module is additive
and does not modify any existing v1 flows.
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, JSON, Text
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, List, Any, Optional
from datetime import datetime

platform_import_tasks = Table(
    "platform_import_tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("source_type", String(50), nullable=False),  # connector | report | unknown
    Column("connector", String(100), nullable=True),  # e.g. linnworks, channeladvisor
    Column("status", String(50), nullable=False, default="pending"),
    Column("counts", JSON, nullable=True),  # {"total": int, "succeeded": int, "failed": int}
    Column("error", Text, nullable=True),
    Column("saga_id", String(100), nullable=True),
    Column("attempt", Integer, nullable=False, default=0),
    Column("next_run_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)


async def create_import_task(
    merchant_id: str,
    source_type: str,
    connector: Optional[str] = None,
    saga_id: Optional[str] = None,
) -> int:
    """Create a new ImportTask in pending state and return its ID."""
    values = {
        "merchant_id": merchant_id,
        "source_type": source_type,
        "connector": connector,
        "status": "pending",
        "attempt": 0,
        "saga_id": saga_id,
    }
    query = platform_import_tasks.insert().values(**values)
    task_id = await database.execute(query)
    return int(task_id)


async def get_import_task(task_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single ImportTask by ID."""
    query = platform_import_tasks.select().where(platform_import_tasks.c.id == task_id)
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def list_import_tasks_for_merchant(merchant_id: str) -> List[Dict[str, Any]]:
    """List ImportTasks for a merchant, newest first."""
    query = (
        platform_import_tasks.select()
        .where(platform_import_tasks.c.merchant_id == merchant_id)
        .order_by(platform_import_tasks.c.created_at.desc())
    )
    rows = await database.fetch_all(query)
    return [dict(r) for r in rows]


async def get_next_scheduled_task() -> Optional[Dict[str, Any]]:
    """
    Fetch the next ImportTask that is ready to run.

    For EPIC‑3 we consider tasks that are:
    - in `pending` state (never run), or
    - in `retry_scheduled` state whose next_run_at is due or unset.
    """
    now = datetime.utcnow()
    query = (
        platform_import_tasks.select()
        .where(
            platform_import_tasks.c.status.in_(["pending", "retry_scheduled"])
            & (
                (platform_import_tasks.c.next_run_at.is_(None))
                | (platform_import_tasks.c.next_run_at <= now)
            )
        )
        .order_by(platform_import_tasks.c.created_at.asc())
        .limit(1)
    )
    row = await database.fetch_one(query)
    return dict(row) if row else None


async def update_import_task_status(
    task_id: int,
    status: str,
    counts: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    next_run_at: Optional[datetime] = None,
    attempt: Optional[int] = None,
) -> bool:
    """Update ImportTask status and optional metadata."""
    values: Dict[str, Any] = {
        "status": status,
        "updated_at": datetime.now(),
    }
    if counts is not None:
        values["counts"] = counts
    if error is not None:
        values["error"] = error
    if next_run_at is not None:
        values["next_run_at"] = next_run_at
    if attempt is not None:
        values["attempt"] = attempt

    query = platform_import_tasks.update().where(platform_import_tasks.c.id == task_id).values(
        **values
    )
    await database.execute(query)
    return True
