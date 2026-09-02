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
