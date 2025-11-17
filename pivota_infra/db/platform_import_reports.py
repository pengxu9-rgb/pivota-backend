"""
Platform Import Reports - EPIC‑6

Stores raw platform reports (CSV/Excel) for later processing by ImportTasks.
"""

from sqlalchemy import Table, Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from db.database import metadata, database
from typing import Dict, Any, Optional
from datetime import datetime
import csv
import io

platform_import_reports = Table(
    "platform_import_reports",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("merchant_id", String(50), nullable=False, index=True),
    Column("report_type", String(50), nullable=False),
    Column("original_filename", String(255), nullable=True),
    Column("file_size_bytes", Integer, nullable=True),
    Column("rows_total", Integer, nullable=True),
    Column("raw_content", Text, nullable=True),
    Column("storage_type", String(20), nullable=False, server_default="inline"),
    Column("s3_key", String(500), nullable=True),
    Column("import_task_id", Integer, nullable=True, index=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("created_by", String(100), nullable=True),
)


async def save_raw_report(
    merchant_id: str,
    report_type: str,
    raw_content: str,
    original_filename: str,
    created_by: str,
) -> int:
    """Persist a raw platform report and return its ID."""
    file_size_bytes = len(raw_content.encode("utf-8"))

    # Best-effort row count (excluding header)
    rows_total: Optional[int] = None
    try:
        reader = csv.reader(io.StringIO(raw_content))
        rows_total = max(sum(1 for _ in reader) - 1, 0)
    except Exception:
        rows_total = None

    values: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "report_type": report_type,
        "original_filename": original_filename,
        "file_size_bytes": file_size_bytes,
        "rows_total": rows_total,
        "raw_content": raw_content,
        "storage_type": "inline",
        "s3_key": None,
        "created_at": datetime.utcnow(),
        "created_by": created_by,
    }
    query = platform_import_reports.insert().values(**values)
    report_id = await database.execute(query)
    return int(report_id)


async def attach_import_task(report_id: int, import_task_id: int) -> None:
    """Attach an ImportTask ID to a stored report."""
    query = (
        platform_import_reports.update()
        .where(platform_import_reports.c.id == report_id)
        .values(import_task_id=import_task_id)
    )
    await database.execute(query)


async def get_platform_report(report_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a stored platform report by ID."""
    query = platform_import_reports.select().where(platform_import_reports.c.id == report_id)
    row = await database.fetch_one(query)
    return dict(row) if row else None

