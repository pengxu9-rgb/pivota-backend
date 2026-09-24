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
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

STATUSES = ("queued", "apply_due", "held", "done", "nothing", "failed", "cancelled")  # migration 234 CHECK
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


_CLAIM_LANE_LOCK_SQL = "SELECT pg_try_advisory_xact_lock(hashtext('retailer_ingest_drain')) AS mine"

#: A job's host is compared lowercased with "www." dropped: a store's single-brand re-crawl job and
#: its multi_brand store cohort are different scope_keys but ONE host. (A literal, not an f-string:
#: tests/test_repo_sql_prepare_postgres.py PREPAREs module-level literals.)
_CLAIM_SQL = """
    WITH busy AS (
        SELECT regexp_replace(lower(domain), '^www[.]', '') AS host, status FROM retailer_ingest_jobs WHERE lease_until > NOW()
    )
    UPDATE retailer_ingest_jobs
    SET lease_until = NOW() + (CAST(:lease AS integer) * interval '1 second'), updated_at = NOW()
    WHERE id = (
        SELECT id FROM retailer_ingest_jobs
        WHERE status IN ('queued', 'apply_due')
          AND next_run_at <= NOW()
          AND (lease_until IS NULL OR lease_until < NOW())
          -- at most :max_leases stages in flight (1 = one crawl at a time, the default)
          AND (SELECT count(*) FROM busy) < CAST(:max_leases AS integer)
          -- never two crawls at one host, whatever the cohort shape of the job holding it
          AND regexp_replace(lower(domain), '^www[.]', '') NOT IN (SELECT host FROM busy)
          -- catalog writes stay serial: an apply waits while any other apply is in flight
          AND NOT (status = 'apply_due' AND EXISTS (SELECT 1 FROM busy WHERE busy.status = 'apply_due'))
        ORDER BY priority DESC, next_run_at, created_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING *
"""


#: Every lane is its own execution with its own pool (DB_POOL_MAX_SIZE), and every crawl leaves from
#: the one crawl NAT: a typo must not become 30 crawls.
MAX_LEASES_CEILING = 4


def max_leases_from_env() -> int:
    """RETAILER_INGEST_MAX_LEASES: stages the lane may run at once (default 1). Overlapping */10
    executions are the lanes; this caps them. Bad values fall back to 1, never to unlimited."""
    raw = str(os.getenv("RETAILER_INGEST_MAX_LEASES", "1")).strip()
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if 1 <= value <= MAX_LEASES_CEILING:
        return value
    logger.warning("RETAILER_INGEST_MAX_LEASES=%r is not 1..%d; running one lane", raw, MAX_LEASES_CEILING)
    return 1


async def claim_due_job(*, lease_seconds: int, max_leases: int = 1, db: Any = None) -> Optional[Dict[str, Any]]:
    """Claim ONE due job, or None. Up to `max_leases` stages run at once (default 1), never two at one
    host (a shared crawl IP stays polite to each store) and never two applies (catalog writes stay
    serial).

    Two statements in one transaction, not one: the lane lock must be held BEFORE the claim takes
    its snapshot. A single statement snapshots first and locks second, so a claim that wins the
    lock just after another commits reads the lease table from before that claim, and can put a
    second crawl on the same host. An execution that loses the lock exits idle; the next tick retries.
    This relies on READ COMMITTED (every statement takes a fresh snapshot), which is what prod runs;
    under REPEATABLE READ the snapshot would be the lock statement's and the race would return.
    """
    if not 1 <= int(max_leases) <= MAX_LEASES_CEILING:
        raise ValueError(f"max_leases must be 1..{MAX_LEASES_CEILING}")
    write_db = db or database
    async with write_db.transaction():
        lane = await write_db.fetch_one(_CLAIM_LANE_LOCK_SQL)
        if not (lane and lane["mine"]):
            return None
        row = await write_db.fetch_one(_CLAIM_SQL, {"lease": int(lease_seconds), "max_leases": int(max_leases)})
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
    if not row and expected_status:
        # Superseded: the stage's verdict is dropped, but its lease must not block the lane.
        await write_db.execute("UPDATE retailer_ingest_jobs SET lease_until = NULL WHERE id = :id "
                               "AND status NOT IN ('queued', 'apply_due')", {"id": job_id})
    return bool(row)


async def approve(job_id: str, *, approved_by: str, exclude_handles: List[str],
                  accepted_flags: List[str], db: Any = None) -> bool:
    """held -> apply_due, recording who approved and which rows/flags the approval covers.
    The apply re-runs every check; only the exclusions and accepted flag keys named here change
    its verdict, so an approval cannot wave through a flag that appears later."""
    for name, values in (("exclude_handles", exclude_handles), ("accepted_flags", accepted_flags)):
        if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"{name} must be a list of non-empty strings")
    if not str(approved_by or "").strip():
        raise ValueError("approved_by is required")
    write_db = db or database
    row = await write_db.fetch_one(
        """
        UPDATE retailer_ingest_jobs
        SET status = 'apply_due', next_run_at = NOW(), approved_by = :by, approved_at = NOW(),
            options = options
              || jsonb_build_object('exclude_handles',
                   (CASE WHEN jsonb_typeof(options->'exclude_handles') = 'array'
                         THEN options->'exclude_handles' ELSE '[]'::jsonb END) || CAST(:excl AS jsonb))
              || jsonb_build_object('accepted_flags',
                   (CASE WHEN jsonb_typeof(options->'accepted_flags') = 'array'
                         THEN options->'accepted_flags' ELSE '[]'::jsonb END) || CAST(:acc AS jsonb)),
            status_reason = 'approved', updated_at = NOW()
        WHERE id = :id AND status = 'held'
        RETURNING id
        """,
        {"id": job_id, "by": approved_by, "excl": _dumps([v.strip() for v in exclude_handles]),
         "acc": _dumps([v.strip() for v in accepted_flags])},
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


async def get_job(job_id: str, *, db: Any = None) -> Optional[Dict[str, Any]]:
    read_db = db or database
    row = await read_db.fetch_one("SELECT * FROM retailer_ingest_jobs WHERE id = :id", {"id": job_id})
    return dict(row) if row else None


async def recent_runs(*, limit: int = 20, db: Any = None) -> List[Dict[str, Any]]:
    """The most recent runs across every job, with the job's domain/brand and the run's outcome."""
    read_db = db or database
    rows = await read_db.fetch_all(
        """
        SELECT r.id, r.job_id, j.domain, j.brand, r.stage, r.outcome, r.error, r.image_sha,
               r.started_at, r.finished_at
        FROM retailer_ingest_runs r JOIN retailer_ingest_jobs j ON j.id = r.job_id
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT :limit
        """,
        {"limit": int(limit)},
    )
    return [dict(r) for r in rows]


def backoff_until(attempts: int, *, now: Optional[datetime] = None, base_minutes: int = 30,
                  cap_hours: int = 8) -> datetime:
    """30m, 1h, 2h, 4h, 8h, 8h... -- a throttled store is retried later, never back to back."""
    now = now or datetime.now(timezone.utc)
    minutes = min(base_minutes * (2 ** max(0, int(attempts))), cap_hours * 60)
    return now + timedelta(minutes=minutes)
