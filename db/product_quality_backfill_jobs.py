from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, JSON, String, Table
from sqlalchemy.sql import func

from db.database import IS_POSTGRES, database, metadata


logger = logging.getLogger(__name__)


product_quality_backfill_jobs = Table(
    "product_quality_backfill_jobs",
    metadata,
    Column("job_id", String(64), primary_key=True),
    Column("merchant_id", String(100), nullable=False, index=True),
    Column("platform", String(50), nullable=True),
    Column("status", String(32), nullable=False, default="queued"),
    Column("requested_by", String(255), nullable=True),
    Column("requested_at", DateTime, server_default=func.now(), nullable=False),
    Column("started_at", DateTime, nullable=True),
    Column("finished_at", DateTime, nullable=True),
    Column("total_candidates", Integer, nullable=False, default=0),
    Column("processed", Integer, nullable=False, default=0),
    Column("skipped", Integer, nullable=False, default=0),
    Column("failed", Integer, nullable=False, default=0),
    Column("force_refresh", Boolean, nullable=False, default=False),
    Column("missing_only", Boolean, nullable=False, default=True),
    Column("errors_sample", JSON, nullable=True),
)

Index(
    "idx_quality_backfill_jobs_merchant_status_requested",
    product_quality_backfill_jobs.c.merchant_id,
    product_quality_backfill_jobs.c.status,
    product_quality_backfill_jobs.c.requested_at,
)

_DDL_READY = False
_DDL_LOCK = asyncio.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _json_type_sql() -> str:
    return "JSONB" if IS_POSTGRES else "JSON"


def _normalize_row(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    payload = dict(row)
    for key in ("requested_at", "started_at", "finished_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            payload[key] = value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return payload


async def ensure_product_quality_backfill_jobs_table() -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        try:
            statements = [
                """
                CREATE TABLE IF NOT EXISTS product_quality_backfill_jobs (
                  job_id VARCHAR(64) PRIMARY KEY,
                  merchant_id VARCHAR(100) NOT NULL,
                  platform VARCHAR(50),
                  status VARCHAR(32) NOT NULL DEFAULT 'queued',
                  requested_by VARCHAR(255),
                  requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  started_at TIMESTAMPTZ,
                  finished_at TIMESTAMPTZ,
                  total_candidates INTEGER NOT NULL DEFAULT 0,
                  processed INTEGER NOT NULL DEFAULT 0,
                  skipped INTEGER NOT NULL DEFAULT 0,
                  failed INTEGER NOT NULL DEFAULT 0,
                  force_refresh BOOLEAN NOT NULL DEFAULT FALSE,
                  missing_only BOOLEAN NOT NULL DEFAULT TRUE,
                  errors_sample """ + _json_type_sql() + """
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_quality_backfill_jobs_merchant_status_requested ON product_quality_backfill_jobs(merchant_id, status, requested_at);",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS platform VARCHAR(50);",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS requested_by VARCHAR(255);",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS total_candidates INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS processed INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS skipped INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS failed INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS force_refresh BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS missing_only BOOLEAN NOT NULL DEFAULT TRUE;",
                f"ALTER TABLE product_quality_backfill_jobs ADD COLUMN IF NOT EXISTS errors_sample {_json_type_sql()};",
            ]
            for statement in statements:
                await database.execute(statement)
        except Exception as exc:
            logger.warning("ensure_product_quality_backfill_jobs_table failed: %s", str(exc)[:200])
            return
        _DDL_READY = True


async def create_quality_backfill_job(
    *,
    merchant_id: str,
    platform: Optional[str],
    requested_by: Optional[str],
    force_refresh: bool = False,
    missing_only: bool = True,
) -> Dict[str, Any]:
    await ensure_product_quality_backfill_jobs_table()
    job_id = f"qbf_{uuid.uuid4().hex[:20]}"
    requested_at = _utcnow()

    await database.execute(
        product_quality_backfill_jobs.insert().values(
            job_id=job_id,
            merchant_id=merchant_id,
            platform=platform,
            status="queued",
            requested_by=requested_by,
            requested_at=requested_at,
            force_refresh=force_refresh,
            missing_only=missing_only,
            total_candidates=0,
            processed=0,
            skipped=0,
            failed=0,
            errors_sample=[],
        )
    )
    row = await database.fetch_one(
        product_quality_backfill_jobs.select().where(product_quality_backfill_jobs.c.job_id == job_id)
    )
    return _normalize_row(row) or {}


async def get_quality_backfill_job(job_id: str) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    row = await database.fetch_one(
        product_quality_backfill_jobs.select().where(product_quality_backfill_jobs.c.job_id == job_id)
    )
    return _normalize_row(row)


async def get_active_quality_backfill_job(
    merchant_id: str,
    *,
    platform: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    query = product_quality_backfill_jobs.select().where(
        (product_quality_backfill_jobs.c.merchant_id == merchant_id)
        & (product_quality_backfill_jobs.c.status.in_(["queued", "running"]))
    )
    if platform:
        query = query.where(product_quality_backfill_jobs.c.platform == platform)
    query = query.order_by(product_quality_backfill_jobs.c.requested_at.desc()).limit(1)
    row = await database.fetch_one(query)
    return _normalize_row(row)


async def claim_quality_backfill_job(job_id: str) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    query = """
    UPDATE product_quality_backfill_jobs
    SET status = 'running',
        started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
    WHERE job_id = :job_id
      AND status = 'queued'
    RETURNING *
    """
    row = await database.fetch_one(query, {"job_id": job_id})
    return _normalize_row(row)


async def claim_next_quality_backfill_job() -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    queued = await database.fetch_one(
        """
        SELECT job_id
        FROM product_quality_backfill_jobs
        WHERE status = 'queued'
        ORDER BY requested_at ASC
        LIMIT 1
        """
    )
    if not queued:
        return None
    queued_payload = dict(queued)
    return await claim_quality_backfill_job(str(queued_payload.get("job_id") or ""))


async def update_quality_backfill_job_progress(
    job_id: str,
    *,
    total_candidates: Optional[int] = None,
    processed: Optional[int] = None,
    skipped: Optional[int] = None,
    failed: Optional[int] = None,
    errors_sample: Optional[list[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()

    assignments = []
    values: Dict[str, Any] = {"job_id": job_id}
    if total_candidates is not None:
        assignments.append("total_candidates = :total_candidates")
        values["total_candidates"] = total_candidates
    if processed is not None:
        assignments.append("processed = :processed")
        values["processed"] = processed
    if skipped is not None:
        assignments.append("skipped = :skipped")
        values["skipped"] = skipped
    if failed is not None:
        assignments.append("failed = :failed")
        values["failed"] = failed
    if errors_sample is not None:
        assignments.append("errors_sample = :errors_sample")
        values["errors_sample"] = errors_sample

    if not assignments:
        return await get_quality_backfill_job(job_id)

    query = f"""
    UPDATE product_quality_backfill_jobs
    SET {", ".join(assignments)}
    WHERE job_id = :job_id
    RETURNING *
    """
    row = await database.fetch_one(query, values)
    return _normalize_row(row)


async def complete_quality_backfill_job(
    job_id: str,
    *,
    status: str = "completed",
    total_candidates: Optional[int] = None,
    processed: Optional[int] = None,
    skipped: Optional[int] = None,
    failed: Optional[int] = None,
    errors_sample: Optional[list[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    await ensure_product_quality_backfill_jobs_table()
    values: Dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "finished_at": _utcnow(),
        "errors_sample": errors_sample or [],
    }
    assignments = [
        "status = :status",
        "finished_at = :finished_at",
        "errors_sample = :errors_sample",
    ]
    if total_candidates is not None:
        assignments.append("total_candidates = :total_candidates")
        values["total_candidates"] = total_candidates
    if processed is not None:
        assignments.append("processed = :processed")
        values["processed"] = processed
    if skipped is not None:
        assignments.append("skipped = :skipped")
        values["skipped"] = skipped
    if failed is not None:
        assignments.append("failed = :failed")
        values["failed"] = failed

    query = f"""
    UPDATE product_quality_backfill_jobs
    SET {", ".join(assignments)}
    WHERE job_id = :job_id
    RETURNING *
    """
    row = await database.fetch_one(query, values)
    return _normalize_row(row)
