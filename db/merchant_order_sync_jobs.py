"""Durable queue for post-payment merchant-order sync work.

Why this exists: the merchant-side order write rides on FastAPI
`BackgroundTasks` at six call sites, all of which run AFTER the buyer has paid.
Those tasks run in the API process with no retry and no supervision, so a Cloud
Run revision swap or scale-down drops them and the merchant never learns about
an order the buyer was charged for.

Five of those sites leave a queryable trace — a paid order with no merchant
order — so a reconciler can find them after the fact. The refund site does not:
an order whose Shopify cancel never fired looks exactly like one whose cancel
succeeded. Both are refunded, both still carry `shopify_order_id`. Nothing can
reconcile it, so the intent is recorded here at the moment it is formed.

The claim/lease shape follows `db/merchant_audit_runs.py::claim_next_pending_run`
— `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING`
— which is what makes it safe to run more than one drainer.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from db._ddl_guard import apply_ddl_statements
from db.database import database
from utils.logger import logger

# Operations. Only OP_REFUND_SYNC is wired today; the five create sites can move
# onto this queue without a schema change.
OP_REFUND_SYNC = "refund_sync"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)

# A Shopify cancel + refund-transaction write is a handful of HTTP calls. 300s
# is generous enough that a slow upstream is not stolen mid-flight, short enough
# that a dead worker's job is retried within one reaper cadence.
DEFAULT_LEASE_SECONDS = 300

# Keeps a flapping worker from stealing a lease its holder is about to renew.
STALE_LEASE_GRACE_SECONDS = 30

# 10 attempts on the backoff below spans ~2h
# (30+60+120+240+480+960+1800*3 = 7290s), which covers a Shopify incident
# without retrying a genuinely broken job forever. Counted, not estimated: 8
# attempts is 3690s, barely an hour.
DEFAULT_MAX_ATTEMPTS = 10

_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 1800

_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS merchant_order_sync_jobs (
      job_id            UUID PRIMARY KEY,
      order_id          TEXT NOT NULL,
      merchant_id       TEXT NOT NULL,
      op                TEXT NOT NULL,
      dedupe_key        TEXT NOT NULL,
      payload           TEXT NOT NULL,
      status            TEXT NOT NULL DEFAULT 'pending',
      attempts          INTEGER NOT NULL DEFAULT 0,
      max_attempts      INTEGER NOT NULL DEFAULT 10,
      claimed_by_worker TEXT,
      claimed_until     TIMESTAMPTZ,
      next_attempt_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      last_error        TEXT,
      progress          TEXT,
      created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at      TIMESTAMPTZ
    );
    """,
    # CREATE TABLE IF NOT EXISTS cannot add a column to a table that already
    # exists, so every column added after the first cut needs its own idempotent
    # ALTER here — same pattern as db/merchant_audit_runs.py.
    "ALTER TABLE merchant_order_sync_jobs "
    "ADD COLUMN IF NOT EXISTS progress TEXT;",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_merchant_order_sync_jobs_dedupe "
    "ON merchant_order_sync_jobs (order_id, op, dedupe_key);",
    "CREATE INDEX IF NOT EXISTS idx_merchant_order_sync_jobs_claim "
    "ON merchant_order_sync_jobs (next_attempt_at, created_at) "
    "WHERE status IN ('pending', 'running');",
    "CREATE INDEX IF NOT EXISTS idx_merchant_order_sync_jobs_failed "
    "ON merchant_order_sync_jobs (updated_at DESC) "
    "WHERE status = 'failed';",
]


async def ensure_merchant_order_sync_jobs_table() -> None:
    """Best-effort DDL backstop for hermetic environments. Postgres prod runs
    db/migrations/207_merchant_order_sync_jobs.sql directly. Memoizes only once
    every statement succeeded, so a transient failure retries later."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_merchant_order_sync_jobs_table",
            logger=logger,
            execute=database.execute,
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _backoff_seconds(attempts: int) -> int:
    """Exponential, capped. attempts is the count INCLUDING the one that failed."""
    exponent = max(0, int(attempts) - 1)
    return int(min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** exponent)))


async def enqueue_merchant_order_sync_job(
    *,
    order_id: str,
    merchant_id: str,
    op: str,
    dedupe_key: str,
    payload: Dict[str, Any],
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Optional[str]:
    """Record the intent durably. Returns the job_id, or the existing job's id if
    this (order_id, op, dedupe_key) was already queued.

    Returns None on persistence failure rather than raising. This is called on
    the money path AFTER the PSP has already moved funds, so raising here would
    convert a silently-lost background task into a 5xx on a refund that actually
    succeeded — strictly worse for the caller. A None return is logged at ERROR
    so the loss is visible rather than silent, which is the property the
    `add_task` version never had.
    """
    job_id = str(uuid.uuid4())
    now = _now_utc()

    async def _insert():
        return await database.fetch_one(
            """
            INSERT INTO merchant_order_sync_jobs (
                job_id, order_id, merchant_id, op, dedupe_key, payload,
                status, attempts, max_attempts, next_attempt_at,
                created_at, updated_at
            ) VALUES (
                :job_id, :order_id, :merchant_id, :op, :dedupe_key, :payload,
                'pending', 0, :max_attempts, :now,
                :now, :now
            )
            ON CONFLICT (order_id, op, dedupe_key) DO NOTHING
            RETURNING job_id
            """,
            {
                "job_id": job_id,
                "order_id": str(order_id),
                "merchant_id": str(merchant_id),
                "op": str(op),
                "dedupe_key": str(dedupe_key),
                "payload": json.dumps(payload, default=str),
                "max_attempts": int(max_attempts),
                "now": now,
            },
        )

    try:
        # DDL is NOT run on this path by default. `ensure_*` takes a table lock
        # before evaluating IF NOT EXISTS (db/_ddl_guard.py records a 665s lock
        # wait from that pattern), and this call sits inside the refund request
        # AFTER the PSP has moved funds. Postgres prod gets the table from
        # migration 207; only a hermetic environment that has never seen it pays
        # the ensure, and only once.
        try:
            row = await _insert()
        except Exception:  # noqa: BLE001
            await ensure_merchant_order_sync_jobs_table()
            row = await _insert()
        if row is not None:
            return str(dict(row).get("job_id") or job_id)
        # ON CONFLICT DO NOTHING returned no row: the job is already queued.
        existing = await database.fetch_one(
            """
            SELECT job_id FROM merchant_order_sync_jobs
             WHERE order_id = :order_id AND op = :op AND dedupe_key = :dedupe_key
            """,
            {"order_id": str(order_id), "op": str(op), "dedupe_key": str(dedupe_key)},
        )
        return str(dict(existing).get("job_id")) if existing else None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "merchant_order_sync: FAILED TO ENQUEUE op=%s order_id=%s dedupe=%s — "
            "this work is now lost and will not retry: %s",
            op,
            order_id,
            dedupe_key,
            str(exc)[:300],
        )
        return None


async def claim_next_merchant_order_sync_job(
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Atomically claim the oldest due job. Returns the claimed row, or None.

    `FOR UPDATE SKIP LOCKED` is what makes this safe with more than one drainer.
    Rows already `running` with an EXPIRED lease are claimable here, so a worker
    that died mid-job is recovered inline; the reaper is a backstop, not the
    primary path.
    """
    await ensure_merchant_order_sync_jobs_table()
    now = _now_utc()
    lease_until = now + timedelta(seconds=int(lease_seconds))
    try:
        row = await database.fetch_one(
            """
            UPDATE merchant_order_sync_jobs
               SET status            = 'running',
                   claimed_by_worker = :worker_id,
                   claimed_until     = :lease_until,
                   attempts          = attempts + 1,
                   updated_at        = :now
             WHERE job_id = (
                 SELECT job_id
                   FROM merchant_order_sync_jobs
                  WHERE status IN ('pending', 'running')
                    AND next_attempt_at <= :now
                    AND (claimed_until IS NULL OR claimed_until < :now)
                  ORDER BY created_at ASC
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
             )
            RETURNING job_id, order_id, merchant_id, op, dedupe_key, payload,
                      attempts, max_attempts, progress
            """,
            {"worker_id": str(worker_id), "lease_until": lease_until, "now": now},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_order_sync: claim failed: %s", str(exc)[:200])
        return None

    if row is None:
        return None
    claimed = dict(row)
    raw_payload = claimed.get("payload")
    try:
        claimed["payload"] = json.loads(raw_payload) if raw_payload else {}
    except Exception:  # noqa: BLE001
        # A row we cannot decode can never succeed; surface it rather than
        # retrying it eight times.
        logger.error(
            "merchant_order_sync: undecodable payload on job %s",
            claimed.get("job_id"),
        )
        claimed["payload"] = {}
    raw_progress = claimed.get("progress")
    try:
        claimed["progress"] = json.loads(raw_progress) if raw_progress else {}
    except Exception:  # noqa: BLE001
        claimed["progress"] = {}
    claimed["worker_id"] = str(worker_id)
    return claimed


async def record_merchant_order_sync_progress(
    *, job_id: str, worker_id: str, progress: Dict[str, Any]
) -> bool:
    """Persist which steps of a multi-step job have already succeeded.

    Lets a retry resume rather than re-running a side-effecting step that has
    already landed. Fenced on the lease for the same reason complete/fail are.
    """
    try:
        row = await database.fetch_one(
            """
            UPDATE merchant_order_sync_jobs
               SET progress = :progress, updated_at = :now
             WHERE job_id = :job_id AND claimed_by_worker = :worker_id
            RETURNING job_id
            """,
            {
                "job_id": str(job_id),
                "worker_id": str(worker_id),
                "progress": json.dumps(progress, default=str),
                "now": _now_utc(),
            },
        )
        return row is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "merchant_order_sync: progress write failed for %s: %s",
            job_id,
            str(exc)[:200],
        )
        return False


async def complete_merchant_order_sync_job(*, job_id: str, worker_id: str) -> bool:
    """Mark done, but ONLY while this worker still holds the lease.

    Without the `claimed_by_worker` fence a slow worker whose lease expired —
    and whose job a sibling has legitimately re-claimed — would clear the new
    holder's lease on its way out, letting a third claim run the job
    concurrently. Reachable in one process: scheduler_job_runner abandons a
    run that overshoots its deadline as a live zombie, so the zombie tick and
    the next tick race on the same rows.

    Returns True iff this call actually wrote.
    """
    now = _now_utc()
    try:
        row = await database.fetch_one(
            """
            UPDATE merchant_order_sync_jobs
               SET status = 'done', completed_at = :now, updated_at = :now,
                   claimed_by_worker = NULL, claimed_until = NULL, last_error = NULL
             WHERE job_id = :job_id AND claimed_by_worker = :worker_id
            RETURNING job_id
            """,
            {"job_id": str(job_id), "worker_id": str(worker_id), "now": now},
        )
        if row is None:
            logger.warning(
                "merchant_order_sync: lost the lease on %s before completing it; "
                "another worker owns it now",
                job_id,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "merchant_order_sync: complete failed for %s: %s", job_id, str(exc)[:200]
        )
        return False


async def fail_merchant_order_sync_job(
    *,
    job_id: str,
    worker_id: str,
    attempts: int,
    max_attempts: int,
    error: str,
) -> str:
    """Re-queue with backoff, or mark terminally failed once attempts are spent.

    Returns the status written, so the caller can log a retry differently from a
    give-up — a terminal failure on this queue is a money-path incident.
    """
    now = _now_utc()
    exhausted = int(attempts) >= int(max_attempts)
    status = STATUS_FAILED if exhausted else STATUS_PENDING
    next_attempt_at = now if exhausted else now + timedelta(
        seconds=_backoff_seconds(attempts)
    )
    try:
        # Lease-fenced for the same reason as complete(): a late write from a
        # worker whose lease expired must not disturb the current holder.
        row = await database.fetch_one(
            """
            UPDATE merchant_order_sync_jobs
               SET status = :status, last_error = :error, updated_at = :now,
                   next_attempt_at = :next_attempt_at, attempts = :attempts,
                   claimed_by_worker = NULL, claimed_until = NULL
             WHERE job_id = :job_id AND claimed_by_worker = :worker_id
            RETURNING job_id
            """,
            {
                "job_id": str(job_id),
                "worker_id": str(worker_id),
                "status": status,
                "error": str(error or "")[:1000],
                "now": now,
                "next_attempt_at": next_attempt_at,
                # Written so a terminally-spent row does not read
                # "gave up with 9 attempts left" to whoever triages it.
                "attempts": int(attempts),
            },
        )
        if row is None:
            logger.warning(
                "merchant_order_sync: lost the lease on %s before recording a "
                "failure; another worker owns it now",
                job_id,
            )
            return "lease_lost"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "merchant_order_sync: fail-update failed for %s: %s", job_id, str(exc)[:200]
        )
        # Do NOT report the status we computed: we did not write it. The row is
        # still `running` and the lease reaper will hand it back, so claiming
        # "failed" here would fire the GAVE UP money-path incident for a job
        # that is about to be retried.
        return "write_failed"
    return status


async def release_stale_merchant_order_sync_leases(
    *, grace_seconds: int = STALE_LEASE_GRACE_SECONDS,
) -> int:
    """Backstop reaper: return jobs whose worker died to `pending`.

    `claim_next_merchant_order_sync_job` already tolerates an expired lease
    inline, so this exists to keep a stalled row's status honest for ops reads
    rather than to make recovery work.
    """
    await ensure_merchant_order_sync_jobs_table()
    cutoff = _now_utc() - timedelta(seconds=int(grace_seconds))
    try:
        # RETURNING + fetch_all, not execute(): under `databases`/asyncpg an
        # UPDATE without RETURNING yields None rather than a rowcount, so
        # counting execute()'s result reports 0 forever and the reaper's own
        # "released N leases" warning can never fire.
        rows = await database.fetch_all(
            """
            UPDATE merchant_order_sync_jobs
               SET status = 'pending', claimed_by_worker = NULL, claimed_until = NULL
             WHERE status = 'running'
               AND claimed_until IS NOT NULL
               AND claimed_until < :cutoff
            RETURNING job_id
            """,
            {"cutoff": cutoff},
        )
        return len(rows or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "merchant_order_sync: lease reaper failed: %s", str(exc)[:200]
        )
        return 0


async def count_merchant_order_sync_jobs_by_status() -> Dict[str, int]:
    """Ops read: queue depth by status. Best-effort, returns {} on failure."""
    await ensure_merchant_order_sync_jobs_table()
    try:
        rows = await database.fetch_all(
            "SELECT status, COUNT(*) AS count FROM merchant_order_sync_jobs GROUP BY status"
        )
        out: Dict[str, int] = {}
        for raw in rows or []:
            row = dict(raw)
            out[str(row.get("status"))] = int(row.get("count") or 0)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_order_sync: status count failed: %s", str(exc)[:200])
        return {}


async def list_failed_merchant_order_sync_jobs(*, limit: int = 50) -> List[Dict[str, Any]]:
    """Ops read: terminally-failed jobs, newest first."""
    await ensure_merchant_order_sync_jobs_table()
    try:
        rows = await database.fetch_all(
            """
            SELECT job_id, order_id, merchant_id, op, dedupe_key, attempts,
                   max_attempts, last_error, created_at, updated_at
              FROM merchant_order_sync_jobs
             WHERE status = 'failed'
             ORDER BY updated_at DESC
             LIMIT :limit
            """,
            {"limit": max(1, min(int(limit), 200))},
        )
        return [dict(r) for r in rows or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_order_sync: failed-list failed: %s", str(exc)[:200])
        return []
