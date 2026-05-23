"""Unit tests for the scheduled external_seed mirror wrapper.

The mirror script itself has its own tests
(tests/test_mirror_external_seeds_to_catalog_products.py). These tests
cover only the thin scheduler wrapper:
  - import failure is caught and surfaced in `errors`
  - precheck (ok=False) short-circuits without calling _apply
  - empty mirror set short-circuits without calling _apply
  - happy path passes through inserted count + before/after missing
  - errors during _apply are caught and surfaced
"""

from __future__ import annotations

import sys
import types

import pytest

from jobs import external_seed_mirror_job


@pytest.fixture
def mirror_script_module(monkeypatch):
    """Install a fake scripts.mirror_external_seeds_to_catalog_products
    so tests don't touch the real DB."""
    fake = types.ModuleType("scripts.mirror_external_seeds_to_catalog_products")

    async def _default_build_report(*, sample_limit, limit, apply):
        return {"ok": True, "totals": {"missing_catalog_products": 0}}

    async def _default_apply(limit):
        return 0

    fake._build_report = _default_build_report
    fake._apply = _default_apply
    monkeypatch.setitem(sys.modules, "scripts.mirror_external_seeds_to_catalog_products", fake)
    return fake


@pytest.mark.asyncio
async def test_import_failure_is_caught(monkeypatch):
    # Force the import inside run_external_seed_mirror to raise.
    monkeypatch.setitem(
        sys.modules,
        "scripts.mirror_external_seeds_to_catalog_products",
        None,  # ImportError when Python tries to look up attributes
    )

    summary = await external_seed_mirror_job.run_external_seed_mirror()

    assert summary["errors"]
    assert "import" in summary["errors"][0]
    assert summary["inserted_catalog_products"] == 0


@pytest.mark.asyncio
async def test_precheck_failure_short_circuits(mirror_script_module):
    async def _failed_report(*, sample_limit, limit, apply):
        return {"ok": False, "error": "schema_missing"}

    mirror_script_module._build_report = _failed_report
    apply_called = {"n": 0}

    async def _track_apply(limit):
        apply_called["n"] += 1
        return 0

    mirror_script_module._apply = _track_apply

    summary = await external_seed_mirror_job.run_external_seed_mirror()

    assert summary["skipped"] is True
    assert apply_called["n"] == 0
    assert any("precheck" in e for e in summary["errors"])


@pytest.mark.asyncio
async def test_empty_missing_count_short_circuits(mirror_script_module):
    apply_called = {"n": 0}

    async def _track_apply(limit):
        apply_called["n"] += 1
        return 0

    mirror_script_module._apply = _track_apply

    summary = await external_seed_mirror_job.run_external_seed_mirror()

    assert summary["missing_before"] == 0
    assert apply_called["n"] == 0
    assert summary["inserted_catalog_products"] == 0


@pytest.mark.asyncio
async def test_happy_path_returns_inserted_counts(mirror_script_module):
    state = {"call": 0}

    async def _two_phase_report(*, sample_limit, limit, apply):
        state["call"] += 1
        # First call (before _apply): 5 missing. Second call (after): 0.
        missing = 5 if state["call"] == 1 else 0
        return {"ok": True, "totals": {"missing_catalog_products": missing}}

    async def _apply_five(limit):
        return 5

    mirror_script_module._build_report = _two_phase_report
    mirror_script_module._apply = _apply_five

    summary = await external_seed_mirror_job.run_external_seed_mirror()

    assert summary["missing_before"] == 5
    assert summary["inserted_catalog_products"] == 5
    assert summary["missing_after"] == 0
    assert summary["errors"] == []


@pytest.mark.asyncio
async def test_apply_error_is_caught(mirror_script_module):
    async def _five_missing(*, sample_limit, limit, apply):
        return {"ok": True, "totals": {"missing_catalog_products": 5}}

    async def _failing_apply(limit):
        raise RuntimeError("db timeout")

    mirror_script_module._build_report = _five_missing
    mirror_script_module._apply = _failing_apply

    summary = await external_seed_mirror_job.run_external_seed_mirror()

    assert summary["inserted_catalog_products"] == 0
    assert any("db timeout" in e for e in summary["errors"])
