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

BOTH HALVES ARE LOAD-BEARING, so this file is named to run in the sweep AND is
listed explicitly in the ride-along loop in `.github/workflows/postgres-dialect-
gate.yml`. If you remove it from that list, this file silently stops testing the
statements that actually ship: the sweep sets no DATABASE_URL, so it would only
ever exercise the SQLite twins. That was the state this file shipped in for one
commit, and a defect reintroduced in the POSTGRES constant alone stayed green
across every job. Renaming it to `*_postgres.py` is NOT the fix either — the
sweep `--ignore-glob`s that pattern, which trades the gap for the opposite gap.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from db.database import database
from db.product_quality_backfill_jobs import (
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


async def test_completing_a_job_with_no_counters_keeps_every_one_of_them():
    """Closes a mutant that survived: no test completed a job without counters.

    With every counter omitted, a wrong COALESCE fallback column — `failed =
    COALESCE(:failed, skipped)` — is invisible to every other test here, because
    they all pass `failed` explicitly and the bind wins.
    """
    job_id = await _new_job()
    await update_quality_backfill_job_progress(
        job_id, total_candidates=40, processed=7, skipped=2, failed=1
    )

    await complete_quality_backfill_job(job_id, status="completed")

    row = await get_quality_backfill_job(job_id)
    assert row["status"] == "completed"
    assert (
        row["total_candidates"],
        row["processed"],
        row["skipped"],
        row["failed"],
    ) == (40, 7, 2, 1), "completion with no counters must preserve all four"


async def test_an_empty_errors_sample_list_is_written_not_ignored():
    """`[]` is not `None`, and the whole rewrite hinges on that distinction.

    This is the real first-tick shape: services/product_quality_backfill_service
    passes the accumulator list, which is empty on the first call. `[]` must
    reach the column (clearing a previous sample); only None means "leave it".
    """
    job_id = await _new_job()
    await update_quality_backfill_job_progress(
        job_id, errors_sample=[{"error": "boom", "message": "first failure"}]
    )

    await update_quality_backfill_job_progress(job_id, errors_sample=[])

    row = await get_quality_backfill_job(job_id)
    assert row["errors_sample"] == [], (
        "an explicitly empty sample must overwrite the stored one — only None "
        "means 'leave this column alone'"
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


async def test_a_second_claim_of_the_same_job_is_refused():
    """The atomic-claim conjunct, pinned.

    `claim_quality_backfill_job`'s `AND status = 'queued'` is what makes the
    claim atomic — it is the difference between one worker taking a job and two
    workers taking the same one and scoring the merchant's catalog twice. Every
    other test here claims exactly once, so dropping that conjunct changed
    nothing observable and the mutation survived.
    """
    job_id = await _new_job()

    first = await claim_quality_backfill_job(job_id)
    second = await claim_quality_backfill_job(job_id)

    assert first is not None and first["status"] == "running"
    assert second is None, (
        "a job already running was claimed a second time — the `AND status = "
        "'queued'` conjunct is what stops two workers taking the same job"
    )


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

    # Asserted on the ROW, not on the return value. `database.execute` yields no
    # rowcount for an UPDATE without RETURNING on the asyncpg backend, so this
    # helper returns 0 on Postgres however many rows it moved — pre-existing, and
    # only ever read for a log line in jobs/product_quality_backfill_worker.py
    # ("Requeued %s stale ... job(s)" therefore never fires in production). An
    # `assert requeued >= 1` here would pass on SQLite and fail on Postgres while
    # the requeue itself worked perfectly.
    row = await get_quality_backfill_job(job_id)
    assert row["status"] == "queued"
    assert row["started_at"] is None and row["finished_at"] is None
    assert row["processed"] == 0 and row["total_candidates"] == 0
    assert row["errors_sample"], "the requeue reason must survive the JSON round trip"
    assert row["errors_sample"][0]["error"] == "stale_backfill_job_requeued", (
        "the JSON payload was mangled — on SQLite a CAST(... AS JSONB) silently "
        "stores 0, which is why the dialect split exists"
    )
