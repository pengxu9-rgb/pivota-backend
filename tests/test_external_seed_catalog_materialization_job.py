from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs.external_seed_catalog_materialization_job as job_module  # noqa: E402
import scripts.mirror_external_seeds_to_catalog_products as mirror_module  # noqa: E402
from jobs.external_seed_catalog_materialization_job import (  # noqa: E402
    ENV_BATCH_SIZE,
    ENV_ENABLED,
    run_external_seed_catalog_materialization_tick,
)

_OK_SCHEMA = {"tables": {}, "identity_unique_indexes": [{"indexname": "x"}], "ok": True}


async def _true() -> bool:
    return True


async def _noop() -> None:
    return None


def _stub_tick(monkeypatch, *, missing, schema=None, inserted=5, sig=55):
    """Wire the tick's four DB seams. `missing` is a list consumed one entry per
    count call, so a test can distinguish the before-count from the after-count."""
    counts = list(missing)
    calls = {"apply_limits": [], "missing_calls": 0, "sig_calls": 0}

    async def fake_schema():
        return schema if schema is not None else _OK_SCHEMA

    async def fake_missing() -> int:
        calls["missing_calls"] += 1
        return counts.pop(0)

    async def fake_sig() -> int:
        calls["sig_calls"] += 1
        return sig

    async def fake_apply(limit: int) -> int:
        calls["apply_limits"].append(limit)
        return inserted

    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setattr(job_module, "_try_acquire_materialization_lock", lambda: _true())
    monkeypatch.setattr(job_module, "_release_materialization_lock", _noop)
    monkeypatch.setattr(job_module, "_required_schema", fake_schema)
    monkeypatch.setattr(job_module, "_count_missing_mirrors", fake_missing)
    monkeypatch.setattr(job_module, "_count_mirrors_with_signature", fake_sig)
    monkeypatch.setattr(job_module, "_apply_mirror", fake_apply)
    return calls


@pytest.mark.asyncio
async def test_materialization_job_disabled_does_not_apply(monkeypatch) -> None:
    applied = False

    async def fake_apply(limit: int) -> int:
        nonlocal applied
        applied = True
        return limit

    monkeypatch.setenv(ENV_ENABLED, "false")
    monkeypatch.setattr(job_module, "_apply_mirror", fake_apply)

    summary = await run_external_seed_catalog_materialization_tick()

    assert summary == {"ok": True, "skipped": "disabled", "applied": False}
    assert applied is False


@pytest.mark.asyncio
async def test_materialization_job_no_missing_rows_skips_apply(monkeypatch) -> None:
    released = False

    async def fake_release() -> None:
        nonlocal released
        released = True

    calls = _stub_tick(monkeypatch, missing=[0])
    monkeypatch.setattr(job_module, "_release_materialization_lock", fake_release)

    summary = await run_external_seed_catalog_materialization_tick()

    assert summary["applied"] is False
    assert summary["missing_before"] == 0
    assert calls["apply_limits"] == []
    assert released is True
    # A quiet tick asks the cheap counter exactly once and stops there — it must
    # not go on to price the signature count or anything else.
    assert calls["missing_calls"] == 1
    assert calls["sig_calls"] == 0


@pytest.mark.asyncio
async def test_materialization_job_applies_capped_batch(monkeypatch) -> None:
    monkeypatch.setenv(ENV_BATCH_SIZE, "5")
    calls = _stub_tick(monkeypatch, missing=[12, 7], inserted=5, sig=55)

    summary = await run_external_seed_catalog_materialization_tick()

    assert calls["apply_limits"] == [5]
    assert summary["applied"] is True
    assert summary["ok"] is True
    assert summary["batch_size"] == 5
    assert summary["missing_before"] == 12
    assert summary["inserted_catalog_products"] == 5
    assert summary["missing_after"] == 7
    assert summary["catalog_products_external_seed_with_sig"] == 55


@pytest.mark.asyncio
async def test_materialization_job_reports_schema_failure(monkeypatch) -> None:
    """A missing table / identity index must still fail the tick closed, the way
    the report's own `ok: False` used to."""
    bad_schema = {
        "tables": {"external_product_seeds": True, "catalog_products": False},
        "identity_unique_indexes": [],
        "ok": False,
    }
    calls = _stub_tick(monkeypatch, missing=[99], schema=bad_schema)

    summary = await run_external_seed_catalog_materialization_tick()

    assert summary["ok"] is False
    assert summary["applied"] is False
    assert summary["schema"] == bad_schema
    assert "identity unique index" in summary["error"]
    # It must bail BEFORE counting or applying.
    assert calls["missing_calls"] == 0
    assert calls["apply_limits"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [[0], [12, 7]], ids=["quiet_tick", "tick_with_work"])
async def test_tick_never_builds_the_full_report(monkeypatch, missing) -> None:
    """THE REGRESSION THIS PR EXISTS FOR.

    The tick used to call `_build_report(sample_limit=0)` purely to read
    `totals.missing_catalog_products`, so every quiet tick paid a full
    seed_data-detoasting report build: measured on production 2026-08-17 at a
    ~125s mean that never once completed under 69s, ~83 hours of database time
    over 36 days. Booby-trap `_build_report` so ANY path that reintroduces it
    fails loudly instead of quietly costing a database core again.

    Both the quiet path and the has-work path are covered — the old code paid it
    twice on a tick with work (preflight + `after` report).
    """

    def exploding_build_report(*args, **kwargs):
        raise AssertionError(
            "run_external_seed_catalog_materialization_tick() built the full "
            "mirror report; use count_missing_catalog_mirrors() instead"
        )

    monkeypatch.setattr(mirror_module, "_build_report", exploding_build_report)
    _stub_tick(monkeypatch, missing=missing)

    summary = await run_external_seed_catalog_materialization_tick()

    assert summary["ok"] is True


@pytest.mark.asyncio
async def test_tick_counts_missing_through_the_cheap_chain(monkeypatch) -> None:
    """The job's counting seam must resolve to the mirror script's cheap helper,
    not to something that reconstructs the count from the report. Patched one
    level deeper than the other tests so the real `_count_missing_mirrors`
    indirection is exercised."""
    seen = {}

    async def fake_count() -> int:
        seen["called"] = True
        return 0

    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setattr(job_module, "_try_acquire_materialization_lock", lambda: _true())
    monkeypatch.setattr(job_module, "_release_materialization_lock", _noop)

    async def fake_schema():
        return _OK_SCHEMA

    monkeypatch.setattr(job_module, "_required_schema", fake_schema)
    monkeypatch.setattr(mirror_module, "count_missing_catalog_mirrors", fake_count)

    summary = await run_external_seed_catalog_materialization_tick()

    assert seen.get("called") is True
    assert summary["missing_before"] == 0


@pytest.mark.asyncio
async def test_lock_is_released_when_apply_raises(monkeypatch) -> None:
    """The advisory lock is held for the whole tick; an exception mid-apply must
    not strand it and wedge every future tick behind `lock_not_acquired`."""
    released = False

    async def fake_release() -> None:
        nonlocal released
        released = True

    async def boom(limit: int) -> int:
        raise RuntimeError("apply exploded")

    _stub_tick(monkeypatch, missing=[3])
    monkeypatch.setattr(job_module, "_release_materialization_lock", fake_release)
    monkeypatch.setattr(job_module, "_apply_mirror", boom)

    with pytest.raises(RuntimeError):
        await run_external_seed_catalog_materialization_tick()

    assert released is True
