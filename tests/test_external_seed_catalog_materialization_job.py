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

# Shape returned by services.vertical_profiles.summarize_unresolved_vertical.
_CLEAN_GUARD = {
    "unresolved_vertical": 0,
    "total": 5,
    "share": 0.0,
    "threshold": 0.35,
    "summary": "unresolved_vertical: 0/5 (0.0%)",
    "should_fail": False,
}
_TRIPPED_GUARD = {
    "unresolved_vertical": 4,
    "total": 5,
    "share": 0.8,
    "threshold": 0.35,
    "summary": "unresolved_vertical: 4/5 (80.0%)",
    "should_fail": True,
}


async def _true() -> bool:
    return True


async def _noop() -> None:
    return None


def _stub_tick(monkeypatch, *, missing, schema=None, inserted=5, sig=55, guard=None):
    """Wire the tick's four DB seams. `missing` is a list consumed one entry per
    count call, so a test can distinguish the before-count from the after-count.

    Patched on the MIRROR MODULE, not on the job's wrappers. Stubbing
    `job_module._count_mirrors_with_signature` & co. would skip the wrappers
    entirely, so their lazy imports would never execute and a typo'd import name
    (or a wrapper quietly rewired back to `_build_report`) would ship green.
    Patching one level deeper means every test here runs the real wrapper
    bodies.
    """
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

    async def fake_apply(limit: int):
        calls["apply_limits"].append(limit)
        # The real `_apply` returns a dict, not a bare int — mirror that here or
        # the fake asserts a contract production does not honour.
        return {
            "inserted": inserted,
            "vertical_guard": guard if guard is not None else _CLEAN_GUARD,
        }

    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setattr(job_module, "_try_acquire_materialization_lock", lambda: _true())
    monkeypatch.setattr(job_module, "_release_materialization_lock", _noop)
    monkeypatch.setattr(mirror_module, "_required_schema", fake_schema)
    monkeypatch.setattr(mirror_module, "count_missing_catalog_mirrors", fake_missing)
    monkeypatch.setattr(
        mirror_module, "count_external_seed_mirrors_with_signature", fake_sig
    )
    monkeypatch.setattr(mirror_module, "_apply", fake_apply)
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
    # Assert against the mirror script's own constant, NOT a copy of the string.
    # The tick no longer gets this text from `_build_report`, so a literal here
    # would just assert the job's spelling back at itself and let the two drift.
    assert summary["error"] == mirror_module.SCHEMA_REQUIRED_ERROR
    # It must bail BEFORE counting or applying.
    assert calls["missing_calls"] == 0
    assert calls["apply_limits"] == []


@pytest.mark.asyncio
async def test_inserted_count_is_an_int_on_both_paths(monkeypatch) -> None:
    """`_apply` returns {"inserted": int, "vertical_guard": dict}, but the
    no-work path sets `inserted_catalog_products` to a bare 0. Passing the dict
    straight through made one summary key an int on one path and a dict on the
    other — a silent-observability bug, since the scheduler only logs this."""
    _stub_tick(monkeypatch, missing=[12, 7], inserted=5)
    worked = await run_external_seed_catalog_materialization_tick()

    _stub_tick(monkeypatch, missing=[0])
    quiet = await run_external_seed_catalog_materialization_tick()

    assert worked["inserted_catalog_products"] == 5
    assert isinstance(worked["inserted_catalog_products"], int)
    assert isinstance(quiet["inserted_catalog_products"], int)


@pytest.mark.asyncio
async def test_tripped_intake_brake_is_surfaced_not_dropped(monkeypatch) -> None:
    """The unresolved-vertical brake exists to refuse to treat a run as clean.
    The CLI `_run` sets report ok=False on it; this job is the caller that
    actually ingests unattended every 15 minutes and used to reach neither that
    nor the stderr line — it discarded the guard entirely."""
    _stub_tick(monkeypatch, missing=[12, 7], guard=_TRIPPED_GUARD)

    summary = await run_external_seed_catalog_materialization_tick()

    assert summary["ok"] is False
    assert summary["applied"] is True
    assert summary["vertical_guard"] == _TRIPPED_GUARD
    assert summary["warnings"] == [_TRIPPED_GUARD["summary"]]
    # The rows it DID insert are still reported — the brake is a cleanliness
    # verdict, not a rollback.
    assert summary["inserted_catalog_products"] == 5


@pytest.mark.asyncio
async def test_clean_guard_leaves_the_tick_ok(monkeypatch) -> None:
    """Contrast case for the test above: a guard that did not trip must not
    flip `ok` or invent a warnings key."""
    _stub_tick(monkeypatch, missing=[12, 7], guard=_CLEAN_GUARD)

    summary = await run_external_seed_catalog_materialization_tick()

    assert summary["ok"] is True
    assert summary["vertical_guard"] == _CLEAN_GUARD
    assert "warnings" not in summary


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

    The trap covers EVERY seam because `_stub_tick` patches the mirror module
    rather than the job's wrappers, so all four wrapper bodies really run. An
    earlier version of this test stubbed the wrappers themselves, which meant
    rewiring any one of them back through `_build_report` still passed.
    """

    def exploding_build_report(*args, **kwargs):
        raise AssertionError(
            "run_external_seed_catalog_materialization_tick() built the full "
            "mirror report; use count_missing_catalog_mirrors() instead"
        )

    monkeypatch.setattr(mirror_module, "_build_report", exploding_build_report)
    calls = _stub_tick(monkeypatch, missing=missing)

    summary = await run_external_seed_catalog_materialization_tick()

    # Assert the tick actually reached the end of the path under test, so the
    # trap had something to fire on.
    assert summary["ok"] is True
    if missing[0] == 0:
        assert summary["applied"] is False
        assert calls["missing_calls"] == 1
    else:
        assert summary["applied"] is True
        assert calls["apply_limits"] and calls["sig_calls"] == 1


@pytest.mark.asyncio
async def test_tick_counts_missing_through_the_cheap_chain(monkeypatch) -> None:
    """The job's counting seam must resolve to the mirror script's cheap helper,
    not to something that reconstructs the count from the report."""
    seen = {}

    async def fake_count() -> int:
        seen["called"] = True
        return 0

    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setattr(job_module, "_try_acquire_materialization_lock", lambda: _true())
    monkeypatch.setattr(job_module, "_release_materialization_lock", _noop)

    async def fake_schema():
        return _OK_SCHEMA

    monkeypatch.setattr(mirror_module, "_required_schema", fake_schema)
    monkeypatch.setattr(mirror_module, "count_missing_catalog_mirrors", fake_count)

    summary = await run_external_seed_catalog_materialization_tick()

    assert seen.get("called") is True
    assert summary["missing_before"] == 0


@pytest.mark.asyncio
async def test_invariants_endpoint_uses_the_cheap_missing_count(monkeypatch) -> None:
    """GET /admin/catalog-products/invariants was the other reader paying ~50s
    for this one number. Nothing else pins that it now calls the cheap helper —
    the SQL-equivalence test proves the two chains agree, not that the endpoint
    picked the right one."""
    import routes.admin_catalog_debug as route_module

    seen = {}

    async def fake_count() -> int:
        seen["called"] = True
        return 4242

    class _FakeDB:
        async def fetch_val(self, sql, values=None):
            # Anything the endpoint asks for other than the missing-mirror count.
            return 0

        async def fetch_all(self, sql, values=None):
            return []

    monkeypatch.setattr(mirror_module, "count_missing_catalog_mirrors", fake_count)
    monkeypatch.setattr(route_module, "database", _FakeDB())

    payload = await route_module.catalog_products_invariants(_=None)

    assert seen.get("called") is True, "endpoint did not use the cheap helper"
    assert payload["missing_external_mirror"] == 4242


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
