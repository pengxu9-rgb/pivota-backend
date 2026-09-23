"""DB accessors for the retailer ingest pipeline (migration 234).

Unlike db/catalog_onboard_queue.py, nothing here swallows an error: a pipeline whose ledger write
failed silently would report a store as done that never was. Every accessor raises.

Leases, not a reaper: a claim sets `lease_until`, and an expired lease makes the job claimable
again, so a run killed mid-crawl (Cloud Run task timeout, OOM) is retried by the next tick instead
of sitting in a "processing" state nothing ever clears (the gap migration 158 left open).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db.database import database

OPEN_STATUSES = ("queued", "apply_due", "held")
DUE_STATUSES = ("queued", "apply_due")


def _dumps(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def scope_key(domain: str, brand: str, options: Dict[str, Any]) -> str:
    """One open job per (host, brand, scope). Operator bookkeeping (approvals, accepted flags,
    exclusions added at review) is NOT scope: approving a job must not let a duplicate enqueue."""
    scope = {k: v for k, v in (options or {}).items()
             if k not in {"accepted_flags", "exclude_handles", "notes"}}
    raw = json.dumps({"domain": domain.strip().lower(), "brand": brand.strip(), "scope": scope},
                     sort_keys=True, ensure_ascii=False, default=str)
    return f"rij:{domain.strip().lower()}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


async def enqueue_job(*, domain: str, brand: str, options: Dict[str, Any], priority: int = 0,
                      source: Optional[str] = None, max_attempts: int = 6, db: Any = None) -> Optional[str]:
    """Insert a queued job; returns its id, or None when the same cohort already has an open job."""
    if not domain or not brand:
        raise ValueError("a retailer ingest job needs a domain and a brand")
    write_db = db or database
    row = await write_db.fetch_one(
        """
        INSERT INTO retailer_ingest_jobs
          (id, domain, brand, options, scope_key, priority, source, max_attempts)
        VALUES
          (:id, :domain, :brand, CAST(:options AS jsonb), :scope_key, :priority, :source, :max_attempts)
        ON CONFLICT (scope_key) WHERE status IN ('queued', 'apply_due', 'held')
        DO NOTHING
        RETURNING id
        """,
        {"id": f"rij_{uuid.uuid4().hex}", "domain": domain.strip().lower(), "brand": brand.strip(),
         "options": _dumps(options or {}), "scope_key": scope_key(domain, brand, options),
         "priority": int(priority or 0), "source": source, "max_attempts": int(max_attempts or 6)},
    )
    return row["id"] if row else None


async def claim_due_job(*, lease_seconds: int, db: Any = None) -> Optional[Dict[str, Any]]:
    """Claim ONE due job (one crawl per tick keeps a shared crawl IP polite)."""
    write_db = db or database
    row = await write_db.fetch_one(
        """
        UPDATE retailer_ingest_jobs
        SET lease_until = NOW() + (CAST(:lease AS integer) * interval '1 second'), updated_at = NOW()
        WHERE id = (
            SELECT id FROM retailer_ingest_jobs
            WHERE status IN ('queued', 'apply_due')
              AND next_run_at <= NOW()
              AND (lease_until IS NULL OR lease_until < NOW())
              -- one crawl at a time: a slow stage still holding its lease blocks the next tick
              AND NOT EXISTS (SELECT 1 FROM retailer_ingest_jobs busy
                              WHERE busy.lease_until > NOW())
            ORDER BY priority DESC, next_run_at, created_at
            LIMIT 1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING *
        """,
        {"lease": int(lease_seconds)},
    )
    if not row:
        return None
    job = dict(row)
    if isinstance(job.get("options"), str):
        job["options"] = json.loads(job["options"])
    return job


async def start_run(*, job_id: str, stage: str, image_sha: Optional[str], execution: Optional[str],
                    db: Any = None) -> str:
    write_db = db or database
    run_id = f"rir_{uuid.uuid4().hex}"
    await write_db.execute(
        "INSERT INTO retailer_ingest_runs (id, job_id, stage, image_sha, execution) "
        "VALUES (:id, :job_id, :stage, :image_sha, :execution)",
        {"id": run_id, "job_id": job_id, "stage": stage, "image_sha": image_sha, "execution": execution},
    )
    return run_id


async def finish_run(run_id: str, *, outcome: str, crawl: Any = None, plan: Any = None,
                     checks: Any = None, flags: Any = None, applied: Any = None,
                     readback: Any = None, error: Optional[str] = None, db: Any = None) -> None:
    write_db = db or database
    await write_db.execute(
        """
        UPDATE retailer_ingest_runs
        SET outcome = :outcome, crawl = CAST(:crawl AS jsonb), plan = CAST(:plan AS jsonb),
            checks = CAST(:checks AS jsonb), flags = CAST(:flags AS jsonb),
            applied = CAST(:applied AS jsonb), readback = CAST(:readback AS jsonb),
            error = :error, finished_at = NOW()
        WHERE id = :id
        """,
        {"id": run_id, "outcome": outcome, "crawl": _dumps(crawl), "plan": _dumps(plan),
         "checks": _dumps(checks), "flags": _dumps(flags), "applied": _dumps(applied),
         "readback": _dumps(readback), "error": (error or None) and error[:8000]},
    )


async def transition(job_id: str, *, status: str, reason: Optional[str], run_id: Optional[str],
                     next_run_at: Optional[datetime] = None, count_attempt: bool = False,
                     expected_status: Optional[str] = None, db: Any = None) -> bool:
    """Move a job to its next state and release the lease; returns whether it moved.

    `expected_status` makes the move conditional on the status the stage CLAIMED: an operator who
    cancelled the job while its crawl ran must not be overwritten by the stage's verdict (review of
    #2263: cancelled -> apply_due -> applied). `count_attempt` spends retry budget (transient
    failures only: a clean dry run advancing to apply_due does not)."""
    write_db = db or database
    guard = "AND status = :expected" if expected_status else ""
    row = await write_db.fetch_one(
        f"""
        UPDATE retailer_ingest_jobs
        SET status = :status, status_reason = :reason, last_run_id = COALESCE(:run_id, last_run_id),
            next_run_at = COALESCE(CAST(:next_run_at AS timestamptz), next_run_at),
            attempts = attempts + CASE WHEN CAST(:count AS boolean) THEN 1 ELSE 0 END,
            lease_until = NULL, updated_at = NOW()
        WHERE id = :id {guard}
        RETURNING id
        """,
        {"id": job_id, "status": status, "reason": (reason or None) and reason[:2000], "run_id": run_id,
         "next_run_at": next_run_at, "count": bool(count_attempt),
         **({"expected": expected_status} if expected_status else {})},
    )
    return bool(row)


async def approve(job_id: str, *, approved_by: str, exclude_handles: List[str],
                  accepted_flags: List[str], db: Any = None) -> bool:
    """held -> apply_due, recording who approved and which rows/flags the approval covers.
    The apply re-runs every check; only the exclusions and accepted flag keys named here change
    its verdict, so an approval cannot wave through a flag that appears later."""
    write_db = db or database
    row = await write_db.fetch_one(
        """
        UPDATE retailer_ingest_jobs
        SET status = 'apply_due', next_run_at = NOW(), approved_by = :by, approved_at = NOW(),
            options = options
              || jsonb_build_object('exclude_handles',
                   COALESCE(options->'exclude_handles', '[]'::jsonb) || CAST(:excl AS jsonb))
              || jsonb_build_object('accepted_flags',
                   COALESCE(options->'accepted_flags', '[]'::jsonb) || CAST(:acc AS jsonb)),
            status_reason = 'approved', updated_at = NOW()
        WHERE id = :id AND status = 'held'
        RETURNING id
        """,
        {"id": job_id, "by": approved_by, "excl": _dumps(list(exclude_handles or [])),
         "acc": _dumps(list(accepted_flags or []))},
    )
    return bool(row)


async def cancel(job_id: str, *, by: str, reason: str, db: Any = None) -> bool:
    write_db = db or database
    row = await write_db.fetch_one(
        "UPDATE retailer_ingest_jobs SET status='cancelled', status_reason=:r, lease_until=NULL, "
        "updated_at=NOW() WHERE id=:id AND status IN ('queued','apply_due','held') "
        # A stage is running: its verdict would race the cancel. Refuse; the caller retries later.
        "AND (lease_until IS NULL OR lease_until < NOW()) RETURNING id",
        {"id": job_id, "r": f"cancelled by {by}: {reason}"[:2000]},
    )
    return bool(row)


async def list_jobs(*, status: Optional[str] = None, limit: int = 200, db: Any = None) -> List[Dict[str, Any]]:
    read_db = db or database
    where = "WHERE status = :status" if status else ""
    rows = await read_db.fetch_all(
        f"SELECT * FROM retailer_ingest_jobs {where} ORDER BY updated_at DESC LIMIT :limit",
        {"limit": int(limit), **({"status": status} if status else {})},
    )
    return [dict(r) for r in rows]


async def job_runs(job_id: str, *, db: Any = None) -> List[Dict[str, Any]]:
    read_db = db or database
    rows = await read_db.fetch_all(
        "SELECT * FROM retailer_ingest_runs WHERE job_id = :id ORDER BY started_at DESC", {"id": job_id})
    return [dict(r) for r in rows]


async def unfinished_run(job_id: str, *, db: Any = None) -> Optional[Dict[str, Any]]:
    """The job's latest run, when it never finished: its execution was killed (timeout, OOM)."""
    read_db = db or database
    row = await read_db.fetch_one(
        "SELECT id, stage, started_at, finished_at FROM retailer_ingest_runs WHERE job_id = :id "
        "ORDER BY started_at DESC LIMIT 1", {"id": job_id})
    return dict(row) if row and row["finished_at"] is None else None


async def status_counts(*, db: Any = None) -> Dict[str, int]:
    read_db = db or database
    rows = await read_db.fetch_all("SELECT status, count(*) AS n FROM retailer_ingest_jobs GROUP BY status")
    return {r["status"]: int(r["n"]) for r in rows}


def backoff_until(attempts: int, *, now: Optional[datetime] = None, base_minutes: int = 30,
                  cap_hours: int = 8) -> datetime:
    """30m, 1h, 2h, 4h, 8h, 8h... -- a throttled store is retried later, never back to back."""
    now = now or datetime.now(timezone.utc)
    minutes = min(base_minutes * (2 ** max(0, int(attempts))), cap_hours * 60)
    return now + timedelta(minutes=minutes)
