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


async def _backdate(job_id: str, hours: int) -> None:
    """Server-side, per dialect — no client clock is bound (see #1990)."""
    if IS_POSTGRES:
        sql = (
            "UPDATE product_quality_backfill_jobs SET requested_at = "
            f"CURRENT_TIMESTAMP - INTERVAL '{int(hours)} hours' WHERE job_id = :j"
        )
    else:
        sql = (
            "UPDATE product_quality_backfill_jobs SET requested_at = "
            f"datetime('now', '-{int(hours)} hours') WHERE job_id = :j"
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
    monkeypatch.setenv(QUALITY_BACKFILL_ENQUEUE_COOLDOWN_ENV, "3600")
    await _auto(cooldown_seconds=None)
    out = await _auto(cooldown_seconds=None)
    assert out["enqueued"] is False
