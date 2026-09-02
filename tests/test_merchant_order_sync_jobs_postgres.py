"""Production-dialect gate for the merchant-order sync queue (migration 207).

The queue's whole value is in SQL that SQLite cannot vouch for: a
`FOR UPDATE SKIP LOCKED` claim, `ON CONFLICT ... DO NOTHING RETURNING` for
idempotent enqueue, and a partial index on the claim predicate. This module
EXECUTES the real DDL and the real accessors against Postgres, so a statement
Postgres would refuse turns the gate red instead of prod.

    createdb pivota_sync_queue_check
    DATABASE_URL=postgresql://localhost/pivota_sync_queue_check \
        pytest tests/test_merchant_order_sync_jobs_postgres.py

Never point this at prod.
"""

from __future__ import annotations

import os
import uuid

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.merchant_order_sync_jobs import ensure_merchant_order_sync_jobs_table

    # Connect/disconnect PER TEST: each test runs on a fresh event loop, and an
    # asyncpg pool that outlives its loop fails with "attached to a different loop".
    await database.connect()
    await ensure_merchant_order_sync_jobs_table()
    try:
        yield database
    finally:
        await database.execute("DELETE FROM merchant_order_sync_jobs")
        await database.disconnect()


def _order_id():
    return f"ORD_{uuid.uuid4().hex[:12].upper()}"


async def _enqueue(order_id, dedupe="rfnd_1", **payload):
    from db.merchant_order_sync_jobs import (
        OP_REFUND_SYNC,
        enqueue_merchant_order_sync_job,
    )

    return await enqueue_merchant_order_sync_job(
        order_id=order_id,
        merchant_id="merch_test",
        op=OP_REFUND_SYNC,
        dedupe_key=dedupe,
        payload={"order_id": order_id, **payload},
    )


async def test_enqueue_is_idempotent_on_the_same_refund():
    """A retried refund request reusing one refund_id must not queue twice."""
    from db.database import database

    order_id = _order_id()
    first = await _enqueue(order_id)
    second = await _enqueue(order_id)

    assert first is not None
    assert second == first

    row = await database.fetch_one(
        "SELECT COUNT(*) AS c FROM merchant_order_sync_jobs WHERE order_id = :o",
        {"o": order_id},
    )
    assert dict(row)["c"] == 1


async def test_two_partial_refunds_on_one_order_are_separate_jobs():
    """Positive counterpart: dedupe must key on the refund, not the order."""
    from db.database import database

    order_id = _order_id()
    a = await _enqueue(order_id, dedupe="rfnd_a")
    b = await _enqueue(order_id, dedupe="rfnd_b")

    assert a is not None and b is not None and a != b
    row = await database.fetch_one(
        "SELECT COUNT(*) AS c FROM merchant_order_sync_jobs WHERE order_id = :o",
        {"o": order_id},
    )
    assert dict(row)["c"] == 2


async def test_claim_is_exclusive_and_round_trips_the_payload():
    from db.merchant_order_sync_jobs import claim_next_merchant_order_sync_job

    order_id = _order_id()
    await _enqueue(order_id, shopify_order_id="6001", is_partial=False)

    first = await claim_next_merchant_order_sync_job(worker_id="worker-a")
    assert first is not None
    assert first["order_id"] == order_id
    assert first["attempts"] == 1
    # Payload survives the JSON round trip with its types intact.
    assert first["payload"]["shopify_order_id"] == "6001"
    assert first["payload"]["is_partial"] is False

    # A second worker must not get the same leased job.
    assert await claim_next_merchant_order_sync_job(worker_id="worker-b") is None


async def test_failed_job_is_requeued_with_backoff_then_given_up_on():
    from db.database import database
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        fail_merchant_order_sync_job,
    )

    order_id = _order_id()
    await _enqueue(order_id)
    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")

    status = await fail_merchant_order_sync_job(
        job_id=job["job_id"], worker_id=job["worker_id"], attempts=1,
        max_attempts=3, error="shopify 503",
    )
    assert status == "pending"

    # Backoff must actually hold the job back — an immediately re-claimable job
    # would spin the worker against a failing upstream.
    assert await claim_next_merchant_order_sync_job(worker_id="worker-a") is None

    # Re-claim to hold the lease again: fail() is lease-fenced, and the call
    # above released it. Clear the backoff first — the assertion above proved it
    # is holding the job back.
    await database.execute(
        "UPDATE merchant_order_sync_jobs SET next_attempt_at = NOW() "
        "WHERE job_id = :j",
        {"j": job["job_id"]},
    )
    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")
    assert job is not None
    status = await fail_merchant_order_sync_job(
        job_id=job["job_id"], worker_id=job["worker_id"], attempts=3,
        max_attempts=3, error="shopify 503",
    )
    assert status == "failed"

    row = await database.fetch_one(
        "SELECT status, last_error FROM merchant_order_sync_jobs WHERE job_id = :j",
        {"j": job["job_id"]},
    )
    assert dict(row)["status"] == "failed"
    assert "503" in dict(row)["last_error"]


async def test_a_dead_workers_job_is_recovered():
    """The failure this queue exists for: the process holding the job dies."""
    from db.database import database
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        release_stale_merchant_order_sync_leases,
    )

    order_id = _order_id()
    await _enqueue(order_id)
    job = await claim_next_merchant_order_sync_job(worker_id="worker-that-dies")
    assert job is not None

    # Simulate the revision swap: the lease is held but the worker is gone.
    await database.execute(
        "UPDATE merchant_order_sync_jobs "
        "SET claimed_until = NOW() - INTERVAL '10 minutes' WHERE job_id = :j",
        {"j": job["job_id"]},
    )

    released = await release_stale_merchant_order_sync_leases()
    assert released >= 1

    recovered = await claim_next_merchant_order_sync_job(worker_id="worker-b")
    assert recovered is not None
    assert recovered["job_id"] == job["job_id"]
    assert recovered["attempts"] == 2


async def test_completed_job_is_not_reclaimed():
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        complete_merchant_order_sync_job,
    )

    order_id = _order_id()
    await _enqueue(order_id)
    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")
    await complete_merchant_order_sync_job(
        job_id=job["job_id"], worker_id=job["worker_id"]
    )

    assert await claim_next_merchant_order_sync_job(worker_id="worker-b") is None


async def test_a_late_write_from_an_expired_lease_cannot_disturb_the_new_holder():
    """Fencing. Worker A is slow, not dead; its lease expires, B legitimately
    re-claims, then A finishes and reports. A's write must not land — otherwise
    it clears B's lease and a third claim runs the job concurrently."""
    from db.database import database
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        complete_merchant_order_sync_job,
        fail_merchant_order_sync_job,
    )

    order_id = _order_id()
    await _enqueue(order_id)
    a = await claim_next_merchant_order_sync_job(worker_id="worker-a")

    await database.execute(
        "UPDATE merchant_order_sync_jobs "
        "SET claimed_until = NOW() - INTERVAL '1 minute' WHERE job_id = :j",
        {"j": a["job_id"]},
    )
    b = await claim_next_merchant_order_sync_job(worker_id="worker-b")
    assert b is not None and b["job_id"] == a["job_id"]

    # A's late completion, using A's now-stale worker id.
    wrote = await complete_merchant_order_sync_job(
        job_id=a["job_id"], worker_id="worker-a"
    )
    assert wrote == "lease_lost"

    status = await fail_merchant_order_sync_job(
        job_id=a["job_id"], worker_id="worker-a", attempts=1, max_attempts=5,
        error="late failure from a lease A no longer holds",
    )
    assert status == "lease_lost"

    row = dict(await database.fetch_one(
        "SELECT status, claimed_by_worker FROM merchant_order_sync_jobs "
        "WHERE job_id = :j",
        {"j": a["job_id"]},
    ))
    assert row["status"] == "running"
    assert row["claimed_by_worker"] == "worker-b"


async def test_progress_survives_a_requeue_so_a_retry_can_resume():
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        fail_merchant_order_sync_job,
        record_merchant_order_sync_progress,
    )

    order_id = _order_id()
    await _enqueue(order_id)
    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")

    assert await record_merchant_order_sync_progress(
        job_id=job["job_id"], worker_id="worker-a",
        progress={"refund_transaction_synced": True},
    ) is True

    await fail_merchant_order_sync_job(
        job_id=job["job_id"], worker_id="worker-a", attempts=1, max_attempts=5,
        error="shopify 503 on cancel",
    )

    from db.database import database
    await database.execute(
        "UPDATE merchant_order_sync_jobs SET next_attempt_at = NOW() "
        "WHERE job_id = :j",
        {"j": job["job_id"]},
    )
    again = await claim_next_merchant_order_sync_job(worker_id="worker-b")

    assert again is not None
    # Without this the retry re-posts a refund transaction that already landed.
    assert again["progress"] == {"refund_transaction_synced": True}


async def test_progress_is_lease_fenced():
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        record_merchant_order_sync_progress,
    )

    order_id = _order_id()
    await _enqueue(order_id)
    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")

    assert await record_merchant_order_sync_progress(
        job_id=job["job_id"], worker_id="someone-else", progress={"x": True},
    ) is False


async def test_complete_is_lease_fenced_and_reports_why():
    """`fail` and `progress` fences were covered; `complete`'s was not, despite
    being the one that decides whether a job is recorded as done."""
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        complete_merchant_order_sync_job,
    )
    from db.database import database

    order_id = _order_id()
    await _enqueue(order_id)
    a = await claim_next_merchant_order_sync_job(worker_id="worker-a")

    await database.execute(
        "UPDATE merchant_order_sync_jobs "
        "SET claimed_until = NOW() - INTERVAL '1 minute' WHERE job_id = :j",
        {"j": a["job_id"]},
    )
    b = await claim_next_merchant_order_sync_job(worker_id="worker-b")
    assert b is not None

    assert await complete_merchant_order_sync_job(
        job_id=a["job_id"], worker_id="worker-a"
    ) == "lease_lost"

    assert await complete_merchant_order_sync_job(
        job_id=a["job_id"], worker_id="worker-b"
    ) == "written"

    row = dict(await database.fetch_one(
        "SELECT status FROM merchant_order_sync_jobs WHERE job_id = :j",
        {"j": a["job_id"]},
    ))
    assert row["status"] == "done"


async def test_five_call_sites_racing_on_one_order_enqueue_once():
    """The create op keys on a constant, so an agent confirm and a PSP webhook
    both landing for the same order produce ONE job, not five."""
    from db.database import database
    from db.merchant_order_sync_jobs import enqueue_merchant_order_create

    order_id = _order_id()
    ids = []
    for _ in range(5):
        ids.append(await enqueue_merchant_order_create(
            order_id=order_id, merchant_id="merch_test",
        ))

    assert all(i is not None for i in ids)
    assert len(set(ids)) == 1, f"five enqueues produced {len(set(ids))} distinct jobs"

    row = await database.fetch_one(
        "SELECT COUNT(*) AS c FROM merchant_order_sync_jobs WHERE order_id = :o",
        {"o": order_id},
    )
    assert dict(row)["c"] == 1


async def test_a_create_and_a_refund_job_coexist_for_one_order():
    """Different ops, so the create must not dedupe against a refund_sync job."""
    from db.database import database
    from db.merchant_order_sync_jobs import enqueue_merchant_order_create

    order_id = _order_id()
    await enqueue_merchant_order_create(order_id=order_id, merchant_id="merch_test")
    await _enqueue(order_id, dedupe="re_1")

    row = await database.fetch_one(
        "SELECT COUNT(*) AS c FROM merchant_order_sync_jobs WHERE order_id = :o",
        {"o": order_id},
    )
    assert dict(row)["c"] == 2


async def test_a_terminal_create_job_does_not_tombstone_the_order():
    """Two call sites exist ONLY to be retries — the agent and Checkout.com
    already-paid branches, which answer "Shopify sync initiated". Without
    revival a `done`/`failed` job is permanent: the unique index has no status
    column, the claim reads only pending/running, and nothing resets a terminal
    row. Those sites would return the tombstone's id and never run again."""
    from db.database import database
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        complete_merchant_order_sync_job,
        enqueue_merchant_order_create,
    )

    order_id = _order_id()
    first = await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")
    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")
    await complete_merchant_order_sync_job(job_id=job["job_id"], worker_id="worker-a")

    row = dict(await database.fetch_one(
        "SELECT status FROM merchant_order_sync_jobs WHERE job_id=:j", {"j": first}))
    assert row["status"] == "done"

    # The retry site enqueues again.
    again = await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")
    assert again == first, "still one row per order"

    revived = await claim_next_merchant_order_sync_job(worker_id="worker-b")
    assert revived is not None, "the re-enqueue was a silent no-op"
    assert str(revived["job_id"]) == first
    assert revived["attempts"] == 1, "attempts reset, so it gets its one attempt"
    assert revived["progress"] == {}


async def test_repeat_enqueues_share_one_identical_payload():
    """The payload no longer carries a caller-specific flag, so a repeat enqueue
    cannot change the stored job's meaning. An earlier cut adopted the newer
    payload on every conflict — including onto a RUNNING row, where the worker
    had already read the old one, finished the old work, and the newer caller's
    request vanished with a success return."""
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        enqueue_merchant_order_create,
    )

    order_id = _order_id()
    await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")
    await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")

    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")
    # Every caller's payload is identical now — the store guard is applied at
    # the call site, so nothing about the stored job depends on which caller
    # won the race to enqueue.
    assert job["payload"] == {"order_id": order_id, "merchant_id": "m1"}


async def test_a_repeat_refund_enqueue_still_does_not_revive():
    """The refund op keys on the PSP refund id — one unrepeatable event — so a
    repeat enqueue must NOT re-run a completed sync."""
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        complete_merchant_order_sync_job,
    )

    order_id = _order_id()
    first = await _enqueue(order_id, dedupe="re_once")
    job = await claim_next_merchant_order_sync_job(worker_id="worker-a")
    await complete_merchant_order_sync_job(job_id=job["job_id"], worker_id="worker-a")

    again = await _enqueue(order_id, dedupe="re_once")
    assert again == first
    assert await claim_next_merchant_order_sync_job(worker_id="worker-b") is None


async def test_a_create_job_is_stored_with_a_single_attempt():
    """At-most-once is enforced by the ROW, not by the handler remembering to
    ask. A second attempt on Woo/Wix/BigCommerce is a second merchant order."""
    from db.database import database
    from db.merchant_order_sync_jobs import enqueue_merchant_order_create

    order_id = _order_id()
    job_id = await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")

    row = dict(await database.fetch_one(
        "SELECT max_attempts FROM merchant_order_sync_jobs WHERE job_id=:j",
        {"j": job_id}))
    assert row["max_attempts"] == 1


async def test_a_revive_does_not_disturb_a_running_job():
    """The revive is NOT lease-fenced, so it must not touch an in-flight row.
    An earlier cut swapped the payload under a worker that had already read it:
    the worker finished the old work, completed the job, and the newer caller's
    request disappeared having returned a job id."""
    from db.database import database
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        enqueue_merchant_order_create,
    )

    order_id = _order_id()
    first = await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")
    claimed = await claim_next_merchant_order_sync_job(worker_id="worker-a")
    assert claimed is not None

    again = await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")
    assert again == first

    row = dict(await database.fetch_one(
        "SELECT status, attempts, claimed_by_worker FROM merchant_order_sync_jobs "
        "WHERE job_id=:j", {"j": first}))
    assert row["status"] == "running", "a running job must not be revived"
    assert row["attempts"] == 1
    assert row["claimed_by_worker"] == "worker-a", "the lease must survive"


async def test_the_reconciler_only_repairs_orders_the_queue_never_heard_of():
    """It used to call `create_shopify_order` directly on every candidate. On a
    schedule that re-POSTs, and the create is not remotely idempotent on
    Woo/Wix/BigCommerce — an order whose create partially landed matches the
    query forever, so every tick would make another merchant order."""
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql
    from datetime import datetime, timedelta, timezone

    from db.database import database
    from db.orders import orders as orders_table
    from db.merchant_order_sync_jobs import enqueue_merchant_order_create
    from jobs.agentic_commerce_reconciliation import (
        reconcile_paid_orders_missing_merchant_order,
    )

    try:
        await database.execute(
            str(CreateTable(orders_table).compile(dialect=postgresql.dialect())))
    except Exception:
        pass

    merchant = f"merch_recon_{uuid.uuid4().hex[:6]}"
    old = datetime.now(timezone.utc) - timedelta(hours=6)

    async def ins(oid, meta=None):
        await database.execute(orders_table.insert().values(
            order_id=oid, merchant_id=merchant, customer_email="r@x.test",
            shipping_address={}, items=[], subtotal=10, total=10, currency="USD",
            payment_status="paid", status="paid", shopify_order_id=None,
            metadata=meta or {}, is_deleted=False, created_at=old, paid_at=old))

    await ins("ORD_LOST_ENQUEUE")                       # must be repaired
    await ins("ORD_ALREADY_QUEUED")                     # queue owns it
    await ins("ORD_ON_WOO", {"merchant_order": {"platform_order_id": "woo-5"}})
    await enqueue_merchant_order_create(
        order_id="ORD_ALREADY_QUEUED", merchant_id=merchant)

    try:
        result = await reconcile_paid_orders_missing_merchant_order(
            merchant_id=merchant, limit=50, min_age_seconds=60, dry_run=True)
        assert result["candidates"] == ["ORD_LOST_ENQUEUE"], result

        done = await reconcile_paid_orders_missing_merchant_order(
            merchant_id=merchant, limit=50, min_age_seconds=60, dry_run=False)
        assert done["queued"] == 1

        # And it is now inert for that order: a second pass sees the job it made.
        again = await reconcile_paid_orders_missing_merchant_order(
            merchant_id=merchant, limit=50, min_age_seconds=60, dry_run=True)
        assert again["candidates"] == [], "the reconciler re-attempted its own work"
    finally:
        await database.execute(
            "DELETE FROM orders WHERE merchant_id = :m", {"m": merchant})


async def test_a_dead_worker_does_not_get_an_at_most_once_create_reattempted():
    """The premise the reconciler's whole safety argument rests on.

    `max_attempts` used to be consulted only by `fail_...`, i.e. only when a
    worker survived long enough to REPORT. A worker that died mid-POST — the
    Cloud Run revision swap this queue exists to survive — had its work
    re-claimed once the lease expired, handing the same non-idempotent
    Woo/Wix/BigCommerce create to a second worker.
    """
    from db.database import database
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        enqueue_merchant_order_create,
        release_stale_merchant_order_sync_leases,
    )

    order_id = _order_id()
    job_id = await enqueue_merchant_order_create(order_id=order_id, merchant_id="m1")
    claimed = await claim_next_merchant_order_sync_job(worker_id="worker-dies")
    assert claimed is not None and claimed["attempts"] == 1

    # The worker dies mid-attempt: the lease is held by nobody.
    await database.execute(
        "UPDATE merchant_order_sync_jobs SET claimed_until = NOW() - INTERVAL "
        "'10 minutes' WHERE job_id = :j", {"j": job_id})

    assert await claim_next_merchant_order_sync_job(worker_id="worker-b") is None, \
        "an at-most-once create was handed out a second time"

    # And the reaper retires it rather than parking it `pending` where nothing
    # would ever claim it again.
    await release_stale_merchant_order_sync_leases(grace_seconds=0)
    row = dict(await database.fetch_one(
        "SELECT status, last_error FROM merchant_order_sync_jobs WHERE job_id=:j",
        {"j": job_id}))
    assert row["status"] == "failed"
    assert "no attempts left" in (row["last_error"] or "")


async def test_a_refund_job_with_budget_left_is_still_recovered():
    """Positive counterpart: the budget guard must not strand a job that has
    attempts remaining — that is the dead-worker recovery the queue is for."""
    from db.database import database
    from db.merchant_order_sync_jobs import (
        claim_next_merchant_order_sync_job,
        release_stale_merchant_order_sync_leases,
    )

    order_id = _order_id()
    await _enqueue(order_id, dedupe="re_budget")   # refund op: max_attempts=10
    first = await claim_next_merchant_order_sync_job(worker_id="worker-dies")
    assert first["attempts"] == 1

    await database.execute(
        "UPDATE merchant_order_sync_jobs SET claimed_until = NOW() - INTERVAL "
        "'10 minutes' WHERE job_id = :j", {"j": first["job_id"]})
    released = await release_stale_merchant_order_sync_leases(grace_seconds=0)
    assert released >= 1

    again = await claim_next_merchant_order_sync_job(worker_id="worker-b")
    assert again is not None and again["attempts"] == 2


async def test_the_reconciler_is_inert_on_the_scheduled_unfiltered_path():
    """The tick always passes merchant_id=None, and the NOT EXISTS guard FAILS
    OPEN: an unbound named parameter renders as NULL under databases/asyncpg
    with no error, so `j.op = NULL` is never true and every candidate is
    re-enqueued. The filtered path alone could not catch that."""
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql
    from datetime import datetime, timedelta, timezone

    from db.database import database
    from db.orders import orders as orders_table
    from db.merchant_order_sync_jobs import enqueue_merchant_order_create
    from jobs.agentic_commerce_reconciliation import (
        reconcile_paid_orders_missing_merchant_order,
    )

    try:
        await database.execute(
            str(CreateTable(orders_table).compile(dialect=postgresql.dialect())))
    except Exception:
        pass

    merchant = f"merch_unfiltered_{uuid.uuid4().hex[:6]}"
    old = datetime.now(timezone.utc) - timedelta(hours=6)
    order_id = f"ORD_UNFILTERED_{uuid.uuid4().hex[:8]}"
    await database.execute(orders_table.insert().values(
        order_id=order_id, merchant_id=merchant, customer_email="u@x.test",
        shipping_address={}, items=[], subtotal=10, total=10, currency="USD",
        payment_status="paid", status="paid", shopify_order_id=None,
        metadata={}, is_deleted=False, created_at=old, paid_at=old))
    await enqueue_merchant_order_create(order_id=order_id, merchant_id=merchant)

    try:
        # merchant_id=None — exactly what run_merchant_order_create_reconcile_tick does.
        result = await reconcile_paid_orders_missing_merchant_order(
            merchant_id=None, limit=200, min_age_seconds=60, dry_run=True)
        assert order_id not in result["candidates"], \
            "the queue already owns this order; re-enqueuing would revive it every tick"
    finally:
        await database.execute(
            "DELETE FROM orders WHERE merchant_id = :m", {"m": merchant})


async def test_the_reconciler_repairs_an_order_with_a_null_is_deleted():
    """`orders.is_deleted` is nullable with only a CLIENT-side default, and two
    raw-SQL insert paths omit it entirely (scripts/shakeout/c_full_order_pipeline
    and routes/init_orders_table). A bare `is_deleted = false` is NULL for those
    rows, so the counter (which COALESCEs) paged on them forever while this
    repair lane could never touch them."""
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql
    from datetime import datetime, timedelta, timezone

    from db.database import database
    from db.orders import orders as orders_table
    from jobs.agentic_commerce_reconciliation import (
        reconcile_paid_orders_missing_merchant_order,
    )

    try:
        await database.execute(
            str(CreateTable(orders_table).compile(dialect=postgresql.dialect())))
    except Exception:
        pass

    merchant = f"merch_nulldel_{uuid.uuid4().hex[:6]}"
    order_id = f"ORD_NULLDEL_{uuid.uuid4().hex[:8]}"
    old = datetime.now(timezone.utc) - timedelta(hours=6)

    # Raw insert that OMITS is_deleted, as those two production paths do.
    await database.execute(
        """
        INSERT INTO orders (order_id, merchant_id, customer_email, shipping_address,
                            items, subtotal, total, currency, payment_status, status,
                            created_at, paid_at)
        VALUES (:oid, :mid, 'n@x.test', '{}', '[]', 10, 10, 'USD', 'paid', 'paid',
                :old, :old)
        """,
        {"oid": order_id, "mid": merchant, "old": old},
    )
    row = dict(await database.fetch_one(
        "SELECT is_deleted FROM orders WHERE order_id = :o", {"o": order_id}))
    assert row["is_deleted"] is None, "fixture must reproduce the NULL, not a false"

    try:
        result = await reconcile_paid_orders_missing_merchant_order(
            merchant_id=merchant, limit=50, min_age_seconds=60, dry_run=True)
        assert result["candidates"] == [order_id], \
            "an order the alert pages on must be repairable by this lane"
    finally:
        await database.execute(
            "DELETE FROM orders WHERE merchant_id = :m", {"m": merchant})
