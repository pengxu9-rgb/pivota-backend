"""`None` must mean "leave this column alone", not "write NULL".

WHY THIS FILE EXISTS. `update_quality_backfill_job_progress` and
`complete_quality_backfill_job` used to build their SET clause by joining a list
of `assignments` collected from whichever keyword arguments were not None. That
made the SQL an f-string, which
`tests/test_repo_sql_prepare_postgres.py::collect_statements` cannot resolve —
so three statements on the live backfill path were never PREPAREd against the
real schema. They are static `COALESCE(:param, column)` statements now, and the
gate sees them.

The rewrite is only safe if COALESCE reproduces the old rule exactly, and
NOTHING in the suite was checking that. Driving the four callers with a tripwire
raised at the top of each function, every one of the 26 tests that touch this
module stayed green: they all monkeypatch the repository functions out, so the
SQL was never executed by any test at all. A partial update writing NULL over a
live counter would have shipped green.

`routes/merchant_products.py` is the caller that makes this load-bearing — it
forwards `active_job.get("total_candidates")` and friends straight through, so
None reaches these functions whenever the stale job's row lacks the key.

Runs against whatever DATABASE_URL is configured; on the default SQLite that is
the `else` branch of each dialect split, on the Postgres dialect job the
`CAST(:errors_sample AS JSONB)` branch.

IT RUNS IN BOTH JOBS ON PURPOSE, and the Postgres half is not automatic: this
file is not named `test_*_postgres.py`, so `.github/workflows/
postgres-dialect-gate.yml` only executes it because it is named there as a
ride-along AND in that workflow's `paths:` filter. Both entries are load-bearing
for the last case in this file, which passes on SQLite against the broken
implementation it exists to catch. Renaming or unlisting it puts that case back
to proving nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db.database import database
from db.product_quality_backfill_jobs import (
    _utcnow,
    claim_quality_backfill_job,
    complete_quality_backfill_job,
    create_quality_backfill_job,
    ensure_product_quality_backfill_jobs_table,
    get_quality_backfill_job,
    requeue_stale_quality_backfill_jobs,
    update_quality_backfill_job_progress,
)

_MERCHANT = "qbfpartial_merchant"


@pytest.fixture(autouse=True)
async def _db():
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_product_quality_backfill_jobs_table()
    await _reset()
    try:
        yield
    finally:
        await _reset()
        if not was_connected:
            await database.disconnect()


async def _reset() -> None:
    await database.execute(
        "DELETE FROM product_quality_backfill_jobs WHERE merchant_id = :m",
        {"m": _MERCHANT},
    )


async def _new_job() -> str:
    job = await create_quality_backfill_job(
        merchant_id=_MERCHANT, platform="shopify", requested_by="test"
    )
    return str(job["job_id"])


async def test_a_none_counter_leaves_the_stored_value_alone():
    job_id = await _new_job()
    await update_quality_backfill_job_progress(
        job_id, total_candidates=40, processed=7, skipped=2, failed=1
    )

    # Exactly the shape routes/merchant_products.py produces when the stale
    # job's row is missing keys: one real number, the rest None.
    await update_quality_backfill_job_progress(job_id, processed=9)

    row = await get_quality_backfill_job(job_id)
    assert row["processed"] == 9, "the field that WAS passed must be written"
    assert row["total_candidates"] == 40, "a None counter overwrote a live value"
    assert row["skipped"] == 2, "a None counter overwrote a live value"
    assert row["failed"] == 1, "a None counter overwrote a live value"


async def test_a_none_errors_sample_does_not_clear_the_stored_sample():
    job_id = await _new_job()
    await update_quality_backfill_job_progress(
        job_id, errors_sample=[{"error": "boom", "message": "first failure"}]
    )

    await update_quality_backfill_job_progress(job_id, processed=3)

    row = await get_quality_backfill_job(job_id)
    assert row["processed"] == 3
    sample = row["errors_sample"]
    assert sample and sample[0]["error"] == "boom", (
        "errors_sample was cleared by an update that did not mention it — "
        "_json_param(None) yields [], so it must not be bound when None"
    )


async def test_an_all_none_update_issues_no_write_at_all(monkeypatch):
    """The early return is an OPTIMISATION now, and needs its own assertion.

    Before the COALESCE rewrite, falling through with an empty `assignments`
    list would have produced `SET  WHERE ...` — a syntax error, so the early
    return was load-bearing and any test would have caught its removal. It is
    not load-bearing any more: an all-None UPDATE is a well-formed no-op that
    returns the same row. Deleting the early return therefore changes nothing
    observable in the row, and a mutant that drops it survives every assertion
    above. What it does change is that a progress tick with nothing to report
    would issue a pointless write on every call. That is what is pinned here.
    """
    job_id = await _new_job()
    await update_quality_backfill_job_progress(job_id, total_candidates=11)

    sent: list[str] = []
    real_fetch_one = database.fetch_one

    async def _recording_fetch_one(query, values=None):
        sent.append(str(query))
        return await real_fetch_one(query, values) if values else await real_fetch_one(query)

    monkeypatch.setattr(database, "fetch_one", _recording_fetch_one)
    returned = await update_quality_backfill_job_progress(job_id)

    assert returned is not None, "the no-op update must still return the job"
    assert returned["total_candidates"] == 11
    assert returned["status"] == "queued"
    assert not any("UPDATE" in q.upper() for q in sent), (
        "an update with nothing to update issued a write anyway; the early "
        f"return was bypassed. statements sent: {sent}"
    )


async def test_completing_a_job_always_rewrites_errors_sample():
    """The one field that is deliberately NOT COALESCEd."""
    job_id = await _new_job()
    await update_quality_backfill_job_progress(
        job_id, errors_sample=[{"error": "transient", "message": "retried"}]
    )

    await complete_quality_backfill_job(job_id, status="completed", processed=5)

    row = await get_quality_backfill_job(job_id)
    assert row["status"] == "completed"
    assert row["processed"] == 5
    assert row["errors_sample"] == [], (
        "a clean completion must clear the sample — _json_param(None) -> []"
    )


async def test_completing_a_job_keeps_counters_it_was_not_given():
    job_id = await _new_job()
    await update_quality_backfill_job_progress(
        job_id, total_candidates=40, processed=7, skipped=2, failed=1
    )

    await complete_quality_backfill_job(job_id, status="failed", failed=3)

    row = await get_quality_backfill_job(job_id)
    assert row["status"] == "failed"
    assert row["failed"] == 3
    assert row["total_candidates"] == 40, "completion nulled a counter it was not given"
    assert row["processed"] == 7, "completion nulled a counter it was not given"
    assert row["skipped"] == 2, "completion nulled a counter it was not given"


async def test_requeue_stale_resets_a_running_job_and_stamps_the_reason():
    job_id = await _new_job()
    await claim_quality_backfill_job(job_id)
    await update_quality_backfill_job_progress(job_id, processed=4, total_candidates=9)

    # started_at is set to now by the claim, so a zero-second staleness window
    # is what makes the row eligible; the helper floors it at 30s internally.
    # A real datetime, not a string: asyncpg rejects a str bound to a timestamp
    # column outright, while SQLite accepts it — so a string here would pass
    # locally and fail the moment this ran against Postgres.
    await database.execute(
        "UPDATE product_quality_backfill_jobs "
        "SET started_at = :old WHERE job_id = :j",
        {"old": datetime(2020, 1, 1), "j": job_id},
    )

    await requeue_stale_quality_backfill_jobs(stale_after_seconds=30, limit=5)

    # This case is about the ROW: status, counters, timestamps, and the JSON
    # payload round trip. The RETURN VALUE — which used to be 0 on Postgres
    # however many rows moved, because `database.execute` yields no rowcount for
    # an UPDATE without RETURNING on the asyncpg backend — is pinned separately
    # by test_requeue_stale_returns_the_number_of_rows_it_actually_moved below.
    row = await get_quality_backfill_job(job_id)
    assert row["status"] == "queued"
    assert row["started_at"] is None and row["finished_at"] is None
    assert row["processed"] == 0 and row["total_candidates"] == 0
    assert row["errors_sample"], "the requeue reason must survive the JSON round trip"
    assert row["errors_sample"][0]["error"] == "stale_backfill_job_requeued", (
        "the JSON payload was mangled — on SQLite a CAST(... AS JSONB) silently "
        "stores 0, which is why the dialect split exists"
    )


async def test_requeue_stale_returns_the_number_of_rows_it_actually_moved():
    """The count, not the row — the half the case above deliberately skips.

    `requeue_stale_quality_backfill_jobs` returned `int(await
    database.execute(...) or 0)`, and the asyncpg backend yields no rowcount for
    an UPDATE without a RETURNING clause: on Postgres the helper answered 0 for
    every requeue it performed, while on SQLite it answered correctly. That is
    exactly the split this suite exists to catch, and it is why the assertion
    below has to run on a real Postgres to prove anything — on SQLite it passes
    against the BROKEN implementation.

    The only reader is jobs/product_quality_backfill_worker.py, twice:

        if requeued:
            logger.warning("Requeued %s stale quality backfill job(s)", requeued)

    so a falsy count does not merely misreport, it deletes the log line. The
    worker loop is dormant (nothing imports it), which is why this is an
    observability bug rather than an outage.

    THE EXPECTED COUNT IS READ OUT OF THE TABLE, NOT ASSUMED TO BE 2. The
    statement is table-wide — there is no merchant filter — and the shared
    Postgres database this job runs against can carry stale `running` rows from
    other files and from earlier runs. The eligibility query below is the
    statement's own WHERE clause, evaluated with the same cutoff, so the two
    agree by construction whatever else is in the table.

    NOTHING HERE ASSERTS THAT A FRESHLY CLAIMED JOB IS SPARED, which it should
    be and on a UTC server is: `:cutoff` is a NAIVE `datetime.utcnow()`, and
    asyncpg hands a naive datetime to a `timestamptz` column to be read in the
    SESSION timezone. On a Postgres running America/Los_Angeles the cutoff lands
    seven hours in the FUTURE and every `running` row is stale. Pre-existing, on
    the WHERE clause rather than the count, and inert in production (UTC) — but
    real enough that pinning it here would make this test fail on a developer's
    machine for a reason that has nothing to do with what it measures.
    """
    stale_ids = [await _new_job(), await _new_job()]
    for job_id in stale_ids:
        await claim_quality_backfill_job(job_id)
        # A real datetime, not a string: asyncpg rejects a str bound to a
        # timestamp column outright, while SQLite accepts it.
        await database.execute(
            "UPDATE product_quality_backfill_jobs "
            "SET started_at = :old WHERE job_id = :j",
            {"old": datetime(2020, 1, 1), "j": job_id},
        )

    # The helper floors staleness at 30s, so this is the cutoff it will use.
    cutoff = _utcnow() - timedelta(seconds=30)
    eligible = await database.fetch_all(
        "SELECT job_id FROM product_quality_backfill_jobs "
        "WHERE status = 'running' AND started_at IS NOT NULL AND started_at < :cutoff",
        {"cutoff": cutoff},
    )
    eligible_ids = {str(dict(row)["job_id"]) for row in eligible}
    assert set(stale_ids) <= eligible_ids, "the two backdated jobs must be eligible"

    # limit == the eligible count, so every eligible row moves and the expected
    # answer is exact rather than a floor.
    requeued = await requeue_stale_quality_backfill_jobs(
        stale_after_seconds=30, limit=len(eligible_ids)
    )

    assert requeued == len(eligible_ids), (
        f"requeue moved {len(eligible_ids)} row(s) and reported {requeued}. On the "
        "asyncpg backend a rowcount-less UPDATE reports None -> 0, which is why "
        "the statement carries RETURNING job_id and the helper counts the rows."
    )
    for job_id in stale_ids:
        assert (await get_quality_backfill_job(job_id))["status"] == "queued", (
            "the count must be the count of rows this call actually moved"
        )

    # Nothing is stale any more, so the answer must fall to 0 rather than stay
    # at whatever the previous call returned. Pins the count to the ROWS, not to
    # a constant that happens to match above.
    assert await requeue_stale_quality_backfill_jobs(stale_after_seconds=30, limit=5) == 0
