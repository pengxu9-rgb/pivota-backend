from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jobs.external_seed_catalog_materialization_job as job_module  # noqa: E402
from jobs.external_seed_catalog_materialization_job import (  # noqa: E402
    ENV_BATCH_SIZE,
    ENV_ENABLED,
    run_external_seed_catalog_materialization_tick,
)


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
    applied = False
    released = False

    async def fake_build_report(*, sample_limit: int, limit: int, apply: bool):
        return {"ok": True, "totals": {"missing_catalog_products": 0}}

    async def fake_apply(limit: int) -> int:
        nonlocal applied
        applied = True
        return limit

    async def fake_release() -> None:
        nonlocal released
        released = True

    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setattr(job_module, "_try_acquire_materialization_lock", lambda: _true())
    monkeypatch.setattr(job_module, "_release_materialization_lock", fake_release)
    monkeypatch.setattr(job_module, "_build_mirror_report", fake_build_report)
    monkeypatch.setattr(job_module, "_apply_mirror", fake_apply)

    summary = await run_external_seed_catalog_materialization_tick()

    assert summary["applied"] is False
    assert summary["missing_before"] == 0
    assert applied is False
    assert released is True


@pytest.mark.asyncio
async def test_materialization_job_applies_capped_batch(monkeypatch) -> None:
    reports = [
        {"ok": True, "totals": {"missing_catalog_products": 12}},
        {
            "ok": True,
            "totals": {
                "missing_catalog_products": 7,
                "catalog_products_external_seed_with_sig": 55,
            },
        },
    ]
    apply_limits = []
    build_calls = []

    async def fake_build_report(*, sample_limit: int, limit: int, apply: bool):
        build_calls.append({"sample_limit": sample_limit, "limit": limit, "apply": apply})
        return reports.pop(0)

    async def fake_apply(limit: int) -> int:
        apply_limits.append(limit)
        return 5

    monkeypatch.setenv(ENV_ENABLED, "true")
    monkeypatch.setenv(ENV_BATCH_SIZE, "5")
    monkeypatch.setattr(job_module, "_try_acquire_materialization_lock", lambda: _true())
    monkeypatch.setattr(job_module, "_release_materialization_lock", _noop)
    monkeypatch.setattr(job_module, "_build_mirror_report", fake_build_report)
    monkeypatch.setattr(job_module, "_apply_mirror", fake_apply)

    summary = await run_external_seed_catalog_materialization_tick()

    assert apply_limits == [5]
    assert build_calls == [
        {"sample_limit": 0, "limit": 5, "apply": False},
        {"sample_limit": 0, "limit": 5, "apply": True},
    ]
    assert summary["applied"] is True
    assert summary["missing_before"] == 12
    assert summary["inserted_catalog_products"] == 5
    assert summary["missing_after"] == 7
    assert summary["catalog_products_external_seed_with_sig"] == 55


async def _true() -> bool:
    return True


async def _noop() -> None:
    return None
