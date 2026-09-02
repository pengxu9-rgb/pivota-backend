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

import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from db.database import IS_POSTGRES, database
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


@asynccontextmanager
async def _columns_declared_timestamptz():
    """Run the body with the three `*_at` columns declared TIMESTAMPTZ, then put
    the declaration back exactly as it was found. POSTGRES ONLY — every caller is
    `skipif(not IS_POSTGRES)`, and SQLite has no such distinction to make.

    WHY THIS IS NEEDED AT ALL, AND WHY IT IS SCOPED. The deployed table does NOT
    have these columns as TIMESTAMPTZ. `main.py` runs its final
    `metadata.create_all` BEFORE `run_sql_migrations`, and this module is in
    metadata by then — routes/merchant_products.py imports it at module scope —
    so create_all builds the table from the bare `DateTime` Column as naive
    `timestamp`, and migration 054's `CREATE TABLE IF NOT EXISTS` is a dead
    no-op behind it. Both landed in the same commit (971f932b1), so no
    deployment ever got the migration's shape first, and no migration ALTERs the
    type afterwards. Reproduced by building a database in that exact order:

        errors_sample   json
        finished_at     timestamp without time zone
        requested_at    timestamp without time zone
        started_at      timestamp without time zone

    So naive is the shape production and this gate BOTH have, and the timezone
    defect cannot occur under it — asyncpg converts a naive datetime to the
    client process's local wall time only for a `timestamptz` parameter, and
    passes it through untouched for a naive one.

    What the cases below guard is therefore not a live production bug but the
    property that makes one impossible: this code must be correct under EITHER
    declaration, because the model/migration drift is live work — a session is
    aligning the model to the migrations, and the moment these columns become
    TIMESTAMPTZ every naive bind in this module turns into a real defect. That
    is the declaration this constructs, and it restores the original on the way
    out so no other case, in this file or a later one, is measured against a
    schema production does not have.
    """
    def _types():
        return database.fetch_all(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'product_quality_backfill_jobs' "
            "AND column_name IN ('requested_at', 'started_at', 'finished_at')"
        )

    before = {dict(r)["column_name"]: dict(r)["data_type"] for r in await _types()}
    # An empty read means the table is not there — `ensure_...` swallows every
    # exception — and an empty restore clause would be a syntax error masking
    # that. Fail on the real cause instead.
    assert set(before) == {"requested_at", "started_at", "finished_at"}, (
        f"expected three timestamp columns to switch, found {before}"
    )

    await database.execute(
        "ALTER TABLE product_quality_backfill_jobs "
        "ALTER COLUMN requested_at TYPE TIMESTAMPTZ, "
        "ALTER COLUMN started_at TYPE TIMESTAMPTZ, "
        "ALTER COLUMN finished_at TYPE TIMESTAMPTZ"
    )
    try:
        now = {dict(r)["column_name"]: dict(r)["data_type"] for r in await _types()}
        assert set(now.values()) == {"timestamp with time zone"}, now
        yield
    finally:
        # A failure to restore must not REPLACE the body's failure: an exception
        # raised in the finally of a generator context manager supersedes the one
        # in flight, so a real assertion error would surface as an ALTER error.
        # Re-raise only when nothing is already propagating.
        failing = sys.exc_info()[0] is not None
        restore = ", ".join(
            f"ALTER COLUMN {column} TYPE {kind.upper()}" for column, kind in sorted(before.items())
        )
        try:
            await database.execute(f"ALTER TABLE product_quality_backfill_jobs {restore}")
        except Exception:
            if not failing:
                raise


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


async def test_requeue_stale_leaves_a_finished_job_alone():
    """The `WHERE status = 'running'` conjunct, pinned by a row it must NOT touch.

    The positive test below is not enough on its own and this is the reason: it
    seeds a `running` job and asserts it comes back `queued`, which is true with
    or without the conjunct — a `WHERE 1=1` sweep requeues that row too. Killing
    the mutant needs a row the sweep is required to skip. Verified: replacing
    `WHERE status = 'running'` with `WHERE 1=1` in BOTH dialect constants leaves
    the rest of this file green and fails only here.

    The conjunct is what stops a FINISHED backfill being silently re-run with its
    diagnostics destroyed — counters zeroed and the real errors_sample
    overwritten with `stale_backfill_job_requeued` — for the sole crime of having
    started before the cutoff.
    """
    done_id = await _new_job()
    await update_quality_backfill_job_progress(done_id, total_candidates=12, processed=12)
    # The sample goes in via completion, not via a progress tick: completing a
    # job ALWAYS rewrites errors_sample (pinned above), so a sample set earlier
    # would be cleared to [] here and this test would be asserting on nothing.
    await complete_quality_backfill_job(
        done_id,
        status="completed",
        errors_sample=[{"error": "real_failure", "message": "keep me"}],
    )
    # Same side of the cutoff as the stale running job in the test below, so the
    # ONLY thing keeping this row out of the sweep is its status.
    await database.execute(
        "UPDATE product_quality_backfill_jobs "
        "SET started_at = :old WHERE job_id = :j",
        {"old": datetime(2020, 1, 1), "j": done_id},
    )

    await requeue_stale_quality_backfill_jobs(stale_after_seconds=30, limit=5)

    row = await get_quality_backfill_job(done_id)
    assert row["status"] == "completed", (
        "a finished job was requeued — the sweep's `WHERE status = 'running'` "
        "conjunct is gone, so any job started before the cutoff gets re-run"
    )
    assert (row["total_candidates"], row["processed"]) == (12, 12), (
        "a finished job's counters were zeroed by the stale sweep"
    )
    assert row["errors_sample"] and row["errors_sample"][0]["error"] == "real_failure", (
        "a finished job's diagnostics were overwritten with the requeue reason"
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

    THE EXPECTED COUNT IS READ OUT OF THE TABLE, NOT ASSUMED TO BE 2, because
    the statement is table-wide — there is no merchant filter, so it counts and
    moves every eligible row, not just this file's. Today that is only this
    file's rows: every other suite touching this table monkeypatches the
    repository functions and writes none. The eligibility query below is the
    statement's own WHERE clause with the same cutoff, so the two agree by
    construction if that ever stops being true.

    Note what is NOT asserted: that a second call returns 0. It would, but it
    would also have returned 0 from the BROKEN helper on both engines, so it
    discriminates nothing — the kind of assertion this file exists to avoid.

    A FRESHLY CLAIMED JOB BEING SPARED is a different claim, pinned by
    test_a_non_utc_process_timezone_does_not_make_every_running_job_stale below
    — it used to be false on any non-UTC server and could not be asserted here.
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

    # The statement's OWN eligibility predicate, copied per dialect, evaluated
    # against the same server clock. The helper floors staleness at 30s.
    if IS_POSTGRES:
        eligible = await database.fetch_all(
            "SELECT job_id FROM product_quality_backfill_jobs "
            "WHERE status = 'running' AND started_at IS NOT NULL "
            "AND started_at < CURRENT_TIMESTAMP - (:stale_seconds * INTERVAL '1 second')",
            {"stale_seconds": 30},
        )
    else:
        eligible = await database.fetch_all(
            "SELECT job_id FROM product_quality_backfill_jobs "
            "WHERE status = 'running' AND started_at IS NOT NULL "
            "AND started_at < datetime('now', :stale_window)",
            {"stale_window": "-30 seconds"},
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


@pytest.mark.skipif(
    not IS_POSTGRES,
    reason="asyncpg encodes a naive datetime for timestamptz using the CLIENT timezone; SQLite binds no datetime here",
)
async def test_a_non_utc_process_timezone_does_not_make_every_running_job_stale():
    """`stale_after_seconds` used to mean nothing unless the PROCESS ran UTC.

    The cutoff was `_utcnow() - timedelta(...)`, a NAIVE datetime, and asyncpg
    encodes a naive datetime for a `timestamptz` parameter as LOCAL wall time —
    local to the CLIENT process, using its `TZ`. Measured on PG 15, against a
    database and session both pinned to UTC, from a process in
    America/Los_Angeles:

        naive utcnow                  2026-09-02 02:45:32
        true now                      2026-09-02 02:45:32+00
        bound as $1::timestamptz  ->  2026-09-02 09:45:32+00

    Seven hours in the FUTURE, so `started_at < :cutoff` held for every
    `running` row: a job claimed one second ago was requeued by a call asking
    for jobs stale for an hour.

    IT IS THE CLIENT'S TIMEZONE, NOT THE SERVER'S. An earlier revision of this
    test asserted the same behaviour by doing `SET TIME ZONE` on the connection,
    which looked right and proved nothing: running the probe above before and
    after `SET TIME ZONE 'America/Los_Angeles'` returns the SAME value both
    times. Both machines happened to be on Pacific time when the bug was first
    diagnosed, which made the two explanations indistinguishable. So the lever
    here is `TZ` + `time.tzset()`, which is what actually moves the encoding —
    and, unlike the session setting, makes this test fail on a UTC CI runner
    against the broken implementation instead of quietly passing.

    Production runs UTC (Cloud Run), which is why this never bit there.
    """
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        assert time.tzname[0] != "UTC", "the process timezone did not change"

        # TIMESTAMPTZ is the declaration under which a naive bind is silently
        # converted, so it is the one this case has to run against; see
        # _columns_declared_timestamptz for why that is NOT the deployed shape,
        # and why the guard still earns its place.
        async with _columns_declared_timestamptz():
            job_id = await _new_job()
            # started_at = CURRENT_TIMESTAMP, i.e. one second old, server time.
            await claim_quality_backfill_job(job_id)

            await requeue_stale_quality_backfill_jobs(stale_after_seconds=3600, limit=5)

            assert (await get_quality_backfill_job(job_id))["status"] == "running", (
                "a job claimed one second ago was requeued by a call asking for "
                "jobs stale for an hour — a naive cutoff was encoded as the "
                "client process's local wall time, not as UTC"
            )
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


async def test_the_staleness_window_is_measured_in_seconds_against_the_server_clock():
    """`stale_after_seconds` is now an INTERVAL built in SQL — pin its unit.

    Every other case in this file backdates `started_at` to 2020, which is stale
    under any unit and any direction: `INTERVAL '1 minute'` in place of
    `INTERVAL '1 second'`, or a `+` for the `-`, survives all of them. This one
    straddles the boundary, in both dialects, with both rows backdated by the
    SERVER so no client clock enters the comparison.
    """
    stale_id = await _new_job()      # 1h old,  asked for 300s -> must move
    recent_id = await _new_job()     # 45s old, asked for 300s -> must NOT move
    for job_id, age in ((stale_id, 3600), (recent_id, 45)):
        await claim_quality_backfill_job(job_id)
        if IS_POSTGRES:
            await database.execute(
                "UPDATE product_quality_backfill_jobs SET started_at = "
                "CURRENT_TIMESTAMP - (:age * INTERVAL '1 second') WHERE job_id = :j",
                {"age": age, "j": job_id},
            )
        else:
            await database.execute(
                "UPDATE product_quality_backfill_jobs "
                "SET started_at = datetime('now', :age) WHERE job_id = :j",
                {"age": f"-{age} seconds", "j": job_id},
            )

    # Both margins are minutes wide, so a slow run cannot flip either verdict:
    # 3600s against a 300s window, and 45s against the same 300s window. `limit`
    # only has to exceed the eligible rows so that nothing is spared by the
    # LIMIT instead of by the predicate — which would pass for the wrong reason.
    await requeue_stale_quality_backfill_jobs(stale_after_seconds=300, limit=5)
    assert (await get_quality_backfill_job(stale_id))["status"] == "queued", (
        "a job running for an hour was not stale at a 300s window — the interval "
        "is not being measured in seconds"
    )
    assert (await get_quality_backfill_job(recent_id))["status"] == "running", (
        "a job running for 45s was stale at a 300s window — the window is too "
        "short, or the comparison runs the wrong way"
    )


def _as_utc(value) -> datetime:
    """`_normalize_row` hands back an ISO string with a `Z`; raw rows a datetime.

    Always returns an AWARE datetime: a parsed string with no offset would
    otherwise be naive, and every caller subtracts an aware `now()` from it,
    which raises TypeError instead of failing the assertion it was written for.
    """
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def test_the_job_timestamps_are_written_and_are_close_to_now():
    """requested_at is no longer passed by the caller — the column default writes
    it. All three DDL paths define one — migration 054 `NOW()`, this module's DDL
    `CURRENT_TIMESTAMP`, the Column `server_default=func.now()` — but be exact
    about what this case can see: only ONE of them creates the table in any given
    run, and which one depends on the job. `create_all` wins in the dialect gate;
    in the SQLite sweep it is whichever caller reaches the freshly-deleted
    database first. Migration 054 is never the creator in either. So this fails
    loudly if the WINNING path loses its default, not if any of the three does.
    finished_at is the same story on the completion path. The timezone claim is
    the Postgres-only case below.
    """
    before = datetime.now(timezone.utc)
    job_id = await _new_job()

    row = await get_quality_backfill_job(job_id)
    requested = _as_utc(row["requested_at"])
    assert abs((requested - before).total_seconds()) < 120, (
        f"requested_at is {requested}, now is {before} — the column default did "
        "not write the instant this job was created"
    )
    assert row["finished_at"] is None

    await complete_quality_backfill_job(job_id, status="completed")
    finished = _as_utc((await get_quality_backfill_job(job_id))["finished_at"])
    assert abs((finished - datetime.now(timezone.utc)).total_seconds()) < 120, (
        f"finished_at is {finished} — completion did not stamp the current instant"
    )


@pytest.mark.skipif(
    not IS_POSTGRES,
    reason="asyncpg encodes a naive datetime for timestamptz using the CLIENT timezone; SQLite binds no datetime here",
)
async def test_a_non_utc_process_timezone_does_not_shift_the_stored_timestamps():
    """The write-path twin of the cutoff case.

    `requested_at` was `_utcnow()` through the SQLAlchemy insert and
    `finished_at` was `_utcnow()` bound into the completion UPDATE — both NAIVE
    datetimes into TIMESTAMPTZ columns, which asyncpg encodes as the CLIENT
    PROCESS's local wall time. From a process on Pacific time every job was
    recorded as requested SEVEN HOURS in the future, and `_normalize_row` then
    reported that shifted instant as UTC. Ordering stayed self-consistent (every
    row shifted equally) and nothing compares these to a cutoff, which is why
    the damage was confined to what the API reports.

    `TZ` + `time.tzset()` is the lever, NOT `SET TIME ZONE` on the connection —
    the session setting does not change how a bound parameter is encoded, and a
    test that used it passed against this very bug on a UTC runner.
    """
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        assert time.tzname[0] != "UTC", "the process timezone did not change"

        # TIMESTAMPTZ is the only declaration under which a naive bind is
        # silently converted, so it is the one this case has to run against.
        # It is NOT the deployed declaration — see
        # _columns_declared_timestamptz for what production actually has, why
        # this is therefore a latent defect rather than a live one, and why the
        # guard still earns its place.
        async with _columns_declared_timestamptz():
            now = datetime.now(timezone.utc)
            job_id = await _new_job()
            requested = _as_utc((await get_quality_backfill_job(job_id))["requested_at"])
            assert abs((requested - now).total_seconds()) < 120, (
                f"requested_at came back {requested} against a real now of {now} "
                "— a naive datetime was encoded as this process's local wall time"
            )

            await complete_quality_backfill_job(job_id, status="completed")
            finished = _as_utc((await get_quality_backfill_job(job_id))["finished_at"])
            assert abs((finished - datetime.now(timezone.utc)).total_seconds()) < 120, (
                f"finished_at came back {finished} — same defect on the "
                "completion path"
            )
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()
