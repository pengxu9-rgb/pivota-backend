"""Phase 2.5 — llm_probe_runs cost-telemetry tests.

Mirror the test pattern of P2.1: pure-logic tests for the cost
helper + accessor signatures, with a documented skip for the DB
round-trip surface (Postgres SUM/aggregation + date_trunc don't
round-trip cleanly under SQLite).

Also tests that the worker's _aggregate_cost_summary_for_run
helper falls back to the placeholder shape when llm_probe_runs
returns no rows for the audit_run_id.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

import pytest


# =====================================================================
# Pure helpers
# =====================================================================


def test_compute_cost_usd_basic_arithmetic():
    from db.llm_probe_runs import compute_cost_usd
    # 1500 input @ $0.001/1k + 500 output @ $0.002/1k
    # = 0.0015 + 0.001 = 0.0025
    cost = compute_cost_usd(
        input_tokens=1500, output_tokens=500,
        cost_per_1k_input_tokens_usd=0.001,
        cost_per_1k_output_tokens_usd=0.002,
    )
    assert cost == Decimal("0.0025")


def test_compute_cost_usd_returns_none_when_both_token_counts_missing():
    from db.llm_probe_runs import compute_cost_usd
    cost = compute_cost_usd(
        input_tokens=None, output_tokens=None,
        cost_per_1k_input_tokens_usd=0.001,
        cost_per_1k_output_tokens_usd=0.002,
    )
    assert cost is None


def test_compute_cost_usd_treats_one_missing_count_as_zero():
    """If one count is present and the other isn't, treat missing
    as zero rather than returning None — partial token data is
    better than no telemetry."""
    from db.llm_probe_runs import compute_cost_usd
    cost = compute_cost_usd(
        input_tokens=1000, output_tokens=None,
        cost_per_1k_input_tokens_usd=0.001,
        cost_per_1k_output_tokens_usd=0.002,
    )
    assert cost == Decimal("0.001")


def test_valid_statuses_contains_expected_set():
    from db.llm_probe_runs import (
        VALID_STATUSES, STATUS_SUCCEEDED, STATUS_FAILED,
        STATUS_RATE_LIMITED, STATUS_TIMEOUT, STATUS_COST_CAPPED,
        STATUS_MOCK_FALLBACK,
    )
    assert STATUS_SUCCEEDED in VALID_STATUSES
    assert STATUS_FAILED in VALID_STATUSES
    assert STATUS_RATE_LIMITED in VALID_STATUSES
    assert STATUS_TIMEOUT in VALID_STATUSES
    assert STATUS_COST_CAPPED in VALID_STATUSES
    assert STATUS_MOCK_FALLBACK in VALID_STATUSES
    # Sanity: all are short snake_case strings
    assert all(
        isinstance(s, str) and " " not in s for s in VALID_STATUSES
    )


# =====================================================================
# Worker integration: _aggregate_cost_summary_for_run fallback
# =====================================================================


@pytest.mark.asyncio
async def test_aggregate_falls_back_to_placeholder_when_no_rows(
    monkeypatch,
):
    """If llm_probe_runs has no rows for this audit_run_id (e.g.,
    record_probe_run wiring hasn't shipped to every probe site
    yet), the worker's helper falls back to the placeholder shape.
    Production deploys can land P2.5 before P2.5b without cost_summary
    becoming None mid-flight."""
    import db.llm_probe_runs as lpr
    import services.audit_run_worker as worker

    async def fake_aggregate(*, audit_run_id):
        return None  # no rows recorded
    monkeypatch.setattr(lpr, "aggregate_cost_for_run", fake_aggregate)

    brand_report = {
        "aggregate": {"products_succeeded": 3, "products_failed": 0},
    }
    summary = await worker._aggregate_cost_summary_for_run(
        run_id="r-1", brand_report=brand_report,
    )
    assert summary is not None
    assert summary["_telemetry_source"] == (
        "placeholder_no_probe_runs_recorded"
    )
    assert summary["products_probed"] == 3


@pytest.mark.asyncio
async def test_aggregate_uses_real_rollup_when_rows_exist(monkeypatch):
    """When llm_probe_runs has rows for this audit_run_id, the
    worker writes the real rollup into cost_summary_jsonb."""
    import db.llm_probe_runs as lpr
    import services.audit_run_worker as worker

    real_rollup = {
        "providers": [
            {"provider": "gemini", "calls": 9,
             "input_tokens": 12000, "output_tokens": 4500,
             "cost_usd": 0.045},
        ],
        "llm_calls": 9,
        "total_input_tokens": 12000,
        "total_output_tokens": 4500,
        "estimated_cost_usd": 0.045,
        "_telemetry_source": "llm_probe_runs",
    }

    async def fake_aggregate(*, audit_run_id):
        return real_rollup
    monkeypatch.setattr(lpr, "aggregate_cost_for_run", fake_aggregate)

    summary = await worker._aggregate_cost_summary_for_run(
        run_id="r-2", brand_report={"aggregate": {}},
    )
    assert summary == real_rollup


@pytest.mark.asyncio
async def test_aggregate_falls_back_when_db_raises(monkeypatch):
    """Any exception from the rollup query is swallowed (best-effort)
    and the worker still emits a placeholder rather than failing
    the whole transition."""
    import db.llm_probe_runs as lpr
    import services.audit_run_worker as worker

    async def boom(*, audit_run_id):
        raise RuntimeError("rollup-blew-up")
    monkeypatch.setattr(lpr, "aggregate_cost_for_run", boom)

    summary = await worker._aggregate_cost_summary_for_run(
        run_id="r-3",
        brand_report={"aggregate": {"products_succeeded": 1}},
    )
    assert summary is not None
    assert summary["_telemetry_source"] == (
        "placeholder_no_probe_runs_recorded"
    )


# =====================================================================
# DB round-trip — skipped (Postgres SUM/aggregation + date_trunc
# don't round-trip cleanly under SQLite, same rationale as P2.1)
# =====================================================================


@pytest.mark.skip(
    reason="Round-trip integration via in-memory SQLite is fragile "
    "for the Numeric(10,6) + date_trunc('day', NOW()) surface "
    "this module uses (matches the existing skip rationale in "
    "test_phase2_audit_runs_lifecycle.py). The accessors are "
    "exercised against real Postgres in the staged Railway deploy."
)
async def test_round_trip_postgres_only():
    pass
