"""Concurrency-safe accessors for Commerce Index v2 publication jobs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db.database import database


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def claim_next_publication_job(
    *,
    target: str,
    worker_id: str,
    lease_seconds: int = 300,
    db: Any = None,
) -> Optional[Dict[str, Any]]:
    """Claim one pending or expired job using ``SKIP LOCKED`` for worker safety."""
    read_db = db or database
    now = _utcnow()
    lease_until = now + timedelta(seconds=max(30, int(lease_seconds or 300)))
    row = await read_db.fetch_one(
        """
        UPDATE commerce_index_publication_jobs
        SET status = 'processing',
            claimed_by = :worker_id,
            claimed_at = :now,
            lease_until = :lease_until,
            attempts = attempts + 1,
            updated_at = :now
        WHERE job_id = (
            SELECT job_id
            FROM commerce_index_publication_jobs
            WHERE target = :target
              AND (
                  status = 'pending'
                  OR (status = 'processing' AND lease_until < :now)
              )
            ORDER BY created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING job_id, change_id, merchant_id, target, scope_json, attempts
        """,
        {
            "target": str(target or "").strip(),
            "worker_id": str(worker_id or "").strip(),
            "now": now,
            "lease_until": lease_until,
        },
    )
    return dict(row) if row else None


async def complete_publication_job(
    *,
    job_id: str,
    worker_id: str,
    error_message: Optional[str] = None,
    db: Any = None,
) -> bool:
    """Complete a job only for its lease holder; failure returns it to pending."""
    write_db = db or database
    failed = bool(str(error_message or "").strip())
    result = await write_db.fetch_one(
        """
        UPDATE commerce_index_publication_jobs
        SET status = :status,
            error_message = :error_message,
            -- Keep this boolean separate from :status. PostgreSQL otherwise
            -- sees the same bind as varchar (the status column) and text (the
            -- string comparison), which makes the prepared statement invalid.
            published_at = CASE
                WHEN :completed THEN CAST(:now AS timestamp)
                ELSE NULL::timestamp
            END,
            claimed_by = NULL,
            claimed_at = NULL,
            lease_until = NULL,
            updated_at = :now
        WHERE job_id = :job_id
          AND status = 'processing'
          AND claimed_by = :worker_id
        RETURNING job_id
        """,
        {
            "job_id": str(job_id or "").strip(),
            "worker_id": str(worker_id or "").strip(),
            "status": "pending" if failed else "completed",
            "completed": not failed,
            "error_message": str(error_message or "").strip()[:1000] or None,
            "now": _utcnow(),
        },
    )
    return bool(result)
