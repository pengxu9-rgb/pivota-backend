"""Unattended syncs must not enqueue a quality rescore of an unchanged catalog.

Measured on prod 2026-09-02: the gateway's catalog auto-sync hits
routes/platform_products_sync_api (force_refresh=True, by design) up to 16 times
a day for one 20-product Wix store, and every call enqueued a full rescore — 20
fresh product_quality_snapshot rows per sync, 20 to 320 a day, for products
that had not changed. Those rows were the entire recent window of the
dead_quality_component invariant.

`enqueue_quality_backfill_if_needed` folds an UNATTENDED request into a recent
job for the same (merchant, platform) inside a cooldown. Three things it must
never do, each pinned below: dedupe a request a person made, dedupe a request
STRONGER than the job it would collide with, or keep deduping once the cooldown
has elapsed.

Runs against whatever DATABASE_URL is configured: SQLite in the sweep, and the
Postgres twin of the lookup statement on the dialect gate — this file is on the
ride-along list in .github/workflows/postgres-dialect-gate.yml, because the two
statements are different text and only one of them ships.
"""

from __future__ import annotations

import pytest

from db.database import IS_POSTGRES, database
from db.product_quality_backfill_jobs import (
    DEFAULT_QUALITY_BACKFILL_ENQUEUE_COOLDOWN_SECONDS,
    QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV,
    enqueue_quality_backfill_if_needed,
    ensure_product_quality_backfill_jobs_table,
    quality_backfill_enqueue_cooldown_seconds,
)

_MERCHANT = "qbfdedupe_merchant"


@pytest.fixture(scope="module", autouse=True)
def _client_process_tz_utc():
    """Pin the CLIENT PROCESS zone to UTC for this module, restore after.

    Not part of what this file tests, and not something this PR changes: on a
    gate database whose `requested_at` is TIMESTAMPTZ (a fresh one — prod's is
    a naive `timestamp`), asyncpg encodes the naive `_utcnow()` the writer
    binds using the client process zone, and `SET TIME ZONE` cannot reach
    that. On a Los Angeles host a 1-second cooldown then never expires because
    the row lands 7 hours in the future. That hazard predates this file (see
    memory: a naive datetime binds with the client process tz) and is closed
    only by the writer binding server-side; until then the local recipe runs
    with TZ=UTC, and this makes that explicit instead of ambient."""
    import os
    import time

    before = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


async def _reset() -> None:
    await database.execute(
        "DELETE FROM product_quality_backfill_jobs WHERE merchant_id = :m", {"m": _MERCHANT}
    )


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


async def _count() -> int:
    row = await database.fetch_one(
        "SELECT count(*) AS c FROM product_quality_backfill_jobs WHERE merchant_id = :m",
        {"m": _MERCHANT},
    )
    return int(row["c"])


async def _requested_at_type() -> str:
    row = await database.fetch_one(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'product_quality_backfill_jobs' AND column_name = 'requested_at'"
    )
    return str(row["data_type"])


async def _backdate(job_id: str, hours: int) -> None:
    """Server-side, per dialect — no client clock is bound (see #1990).

    On Postgres the written value must mean "N hours ago in UTC" under EITHER
    column declaration: a naive column stores whatever wall clock it is handed,
    so `CURRENT_TIMESTAMP - interval` (session zone) would store Los Angeles
    wall clock — 7 hours further into the past than intended, which is the
    exact skew the lookup itself is being tested against. Hand a naive column
    UTC explicitly; hand a timestamptz column an instant."""
    if IS_POSTGRES:
        naive = (await _requested_at_type()).startswith("timestamp without")
        now_utc = "(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')" if naive else "CURRENT_TIMESTAMP"
        sql = (
            "UPDATE product_quality_backfill_jobs SET requested_at = "
            f"{now_utc} - INTERVAL '{int(hours)} hours' WHERE job_id = :j"
        )
    else:
        sql = (
            "UPDATE product_quality_backfill_jobs SET requested_at = "
            f"datetime('now', '-{int(hours)} hours') WHERE job_id = :j"
        )
    await database.execute(sql, {"j": job_id})


async def _age(job_id: str, minutes: int) -> None:
    """Move a job's requested_at INTO THE PAST relative to its stored value —
    zone-free on both dialects because it never consults now()."""
    if IS_POSTGRES:
        sql = (
            "UPDATE product_quality_backfill_jobs SET requested_at = "
            f"requested_at - INTERVAL '{int(minutes)} minutes' WHERE job_id = :j"
        )
    else:
        sql = (
            "UPDATE product_quality_backfill_jobs SET requested_at = "
            f"datetime(requested_at, '-{int(minutes)} minutes') WHERE job_id = :j"
        )
    await database.execute(sql, {"j": job_id})


def _auto(**overrides):
    kwargs = dict(
        merchant_id=_MERCHANT, platform="wix", requested_by="test_auto",
        force_refresh=True, missing_only=True, unattended=True, cooldown_seconds=3600,
    )
    kwargs.update(overrides)
    return enqueue_quality_backfill_if_needed(**kwargs)


async def test_first_unattended_request_enqueues():
    out = await _auto()
    assert out["enqueued"] is True and out["reason"] == "created"
    assert out["job"]["merchant_id"] == _MERCHANT and out["job"]["status"] == "queued"
    assert await _count() == 1


async def test_second_unattended_request_inside_the_cooldown_is_folded_into_the_first():
    first = await _auto()
    second = await _auto()
    assert second["enqueued"] is False
    assert second["reason"] == "recent_job_within_cooldown"
    assert second["job"]["job_id"] == first["job"]["job_id"]
    assert await _count() == 1


async def test_an_attended_request_is_never_deduped():
    """A merchant pressing Sync is the delivery path for their own edits."""
    await _auto()
    out = await _auto(unattended=False, requested_by="merchant_button")
    assert out["enqueued"] is True
    assert await _count() == 2


async def test_a_stronger_request_is_not_folded_into_a_weaker_job():
    """force_refresh after a missing-only job must still enqueue — otherwise the
    class of fix that #1612's follow-up made deliverable becomes undeliverable
    again for the length of the cooldown."""
    await _auto(force_refresh=False)
    out = await _auto(force_refresh=True)
    assert out["enqueued"] is True
    assert await _count() == 2
    # ...and the reverse direction IS folded: a missing-only request after a
    # force_refresh job would only repeat a strict subset of it.
    out = await _auto(force_refresh=False)
    assert out["enqueued"] is False
    assert await _count() == 2


async def test_a_job_older_than_the_cooldown_does_not_block():
    first = await _auto()
    await _backdate(first["job"]["job_id"], hours=2)
    out = await _auto(cooldown_seconds=3600)
    assert out["enqueued"] is True
    assert await _count() == 2


async def test_a_job_for_another_platform_does_not_block():
    await _auto(platform="wix")
    out = await _auto(platform="shopify")
    assert out["enqueued"] is True
    assert await _count() == 2


async def test_a_failed_job_does_not_block():
    """Only queued/running/completed jobs count as 'recent work'; a failed one
    proved nothing about the catalog."""
    first = await _auto()
    await database.execute(
        "UPDATE product_quality_backfill_jobs SET status = 'failed' WHERE job_id = :j",
        {"j": first["job"]["job_id"]},
    )
    out = await _auto()
    assert out["enqueued"] is True
    assert await _count() == 2


async def test_cooldown_zero_disables_the_dedupe():
    await _auto(cooldown_seconds=0)
    out = await _auto(cooldown_seconds=0)
    assert out["enqueued"] is True
    assert await _count() == 2


def test_cooldown_env_parsing(monkeypatch):
    monkeypatch.delenv(QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV, raising=False)
    assert quality_backfill_enqueue_cooldown_seconds() == DEFAULT_QUALITY_BACKFILL_ENQUEUE_COOLDOWN_SECONDS
    monkeypatch.setenv(QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV, "900")
    assert quality_backfill_enqueue_cooldown_seconds() == 900
    monkeypatch.setenv(QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV, "0")
    assert quality_backfill_enqueue_cooldown_seconds() == 0
    # A typo must not silently turn the churn back on (0 would).
    monkeypatch.setenv(QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV, "six hours")
    assert quality_backfill_enqueue_cooldown_seconds() == DEFAULT_QUALITY_BACKFILL_ENQUEUE_COOLDOWN_SECONDS


async def test_env_cooldown_is_what_the_helper_uses_when_none_is_passed(monkeypatch):
    """A 1-second cooldown from the env, then wait it out: the third call must
    enqueue. Any positive default would fold it, so this distinguishes "reads
    the env" from "uses some positive number" — the first version set 3600 and
    could not tell those apart."""
    import asyncio

    monkeypatch.setenv(QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV, "1")
    await _auto(cooldown_seconds=None)
    folded = await _auto(cooldown_seconds=None)
    assert folded["enqueued"] is False
    # 2.5s, not 1.2s: `_utcnow()` truncates to whole seconds and SQLite's
    # string comparison is inclusive at the exact second, so a 1-second window
    # needs the clock to have moved at least two full seconds past the write.
    await asyncio.sleep(2.5)
    out = await _auto(cooldown_seconds=None)
    assert out["enqueued"] is True
    assert await _count() == 2


async def test_the_folded_job_is_the_full_row():
    """Both paths return the same shape: a caller reading merchant_id or
    missing_only off the folded job must not KeyError."""
    first = await _auto()
    second = await _auto()
    assert second["enqueued"] is False
    assert set(first["job"]) == set(second["job"])
    assert second["job"]["merchant_id"] == _MERCHANT
    assert second["job"]["platform"] == "wix"


async def test_an_intervening_weaker_job_does_not_hide_a_stronger_one():
    """Strength is in the WHERE, not on the newest row: gateway auto-sync
    (force_refresh) → merchant presses Sync (attended, missing-only, always
    enqueues) → next gateway auto-sync must still fold into the first job."""
    strong = await _auto(force_refresh=True)
    # `_utcnow()` truncates to whole seconds, so without this the three jobs
    # tie on requested_at and "newest" is arbitrary — the first version of
    # this test passed against a newest-row rule for exactly that reason.
    await _age(strong["job"]["job_id"], minutes=30)
    weak = await _auto(unattended=False, force_refresh=False, requested_by="merchant_button")
    out = await _auto(force_refresh=True)
    assert out["enqueued"] is False
    # ...and into the STRONG job, not the newer weak one: folding a
    # force_refresh request into a missing-only job would lose the rescore.
    assert out["job"]["job_id"] == strong["job"]["job_id"]
    assert out["job"]["job_id"] != weak["job"]["job_id"]
    assert await _count() == 2


# ---------------------------------------------------------------------------
# Time zone. `requested_at` is written client-side as naive UTC (`_utcnow()`)
# into a column that is a naive `timestamp` in prod. The comparison must hold
# under ANY session time zone — the first version compared the naive column to
# `CURRENT_TIMESTAMP - interval`, which reinterprets stored UTC in the session
# zone: under America/Los_Angeles the 6h cooldown became ~13h, under
# Asia/Shanghai a brand-new job already failed it and the dedupe was a no-op.
#
# The matrix is over the NAIVE declaration only, switched in and restored:
# that is prod's shape, and it is the only one a session zone can affect. A
# TIMESTAMPTZ column compared to CURRENT_TIMESTAMP has no session-zone term,
# so cells on it could not fail against the bug and would only re-test the
# client-process encoding pinned by the module fixture above.
#
# Backdates are chosen to sit INSIDE the skew the bug produced: the "must not
# fold" row is 10h old against a 6h cooldown, and under Los Angeles the old
# comparison read that as ~3h old and folded it. A 20h backdate would step
# over the bug and pass either way.
# Postgres only: SQLite has neither session zones nor a timestamptz type.
# ---------------------------------------------------------------------------

_PG_ONLY = pytest.mark.skipif(not IS_POSTGRES, reason="session time zones are a Postgres property")


async def _with_requested_at_declared(kind: str):
    """Switch `requested_at` to `kind` for the body, restore afterwards. Same
    contract as test_quality_backfill_partial_update's helper; that file must
    stay LAST on the ride-along list because of exactly this switch."""
    before = await _requested_at_type()
    await database.execute(f"ALTER TABLE product_quality_backfill_jobs ALTER COLUMN requested_at TYPE {kind}")
    return before


@_PG_ONLY
@pytest.mark.parametrize("zone", ["Asia/Shanghai", "America/Los_Angeles", "UTC"])
async def test_cooldown_holds_under_any_session_zone_on_the_naive_column(zone):
    before = await _with_requested_at_declared("TIMESTAMP")
    try:
        async with database.connection() as conn:
            await conn.execute(f"SET TIME ZONE '{zone}'")
            try:
                # Brand-new job: MUST fold whatever the zone (Shanghai broke this).
                first = await _auto(cooldown_seconds=3600)
                folded = await _auto(cooldown_seconds=3600)
                assert folded["enqueued"] is False, zone
                # 10-hour-old job against a 6h cooldown: MUST NOT fold
                # (Los Angeles broke this — the cooldown silently became ~13h,
                # so a 10h-old row read as ~3h old).
                await _backdate(first["job"]["job_id"], hours=10)
                out = await _auto(cooldown_seconds=6 * 3600)
                assert out["enqueued"] is True, zone
                # ...and a 5-hour-old one still does.
                await _backdate(out["job"]["job_id"], hours=5)
                out2 = await _auto(cooldown_seconds=6 * 3600)
                assert out2["enqueued"] is False, zone
            finally:
                await conn.execute("RESET TIME ZONE")
    finally:
        await database.execute(
            f"ALTER TABLE product_quality_backfill_jobs ALTER COLUMN requested_at TYPE {before.upper()}"
        )
