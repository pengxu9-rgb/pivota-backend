"""Unit tests for the ADR-012 Phase 0b invariant runner (glue only — the SQL
is Postgres-specific and exercised in prod via /__catalog_invariants; these
verify thresholding, sampling, and one-bad-check isolation with a fake db).
"""

from __future__ import annotations

import pytest

from services.catalog_invariant_checks import (
    _CHECKS,
    run_catalog_invariant_checks,
)


class FakeDb:
    def __init__(self, counts, fail_on=None):
        # counts: {check_name_fragment: count}
        self._counts = counts
        self._fail_on = fail_on

    def _match(self, sql):
        for check in _CHECKS:
            if check["count_sql"] == sql or check["sample_sql"] == sql:
                return check["name"]
        raise AssertionError("unknown sql")

    async def fetch_one(self, sql, values=None):
        name = self._match(sql)
        if name == self._fail_on:
            raise RuntimeError("boom")
        return {"c": self._counts.get(name, 0)}

    async def fetch_all(self, sql, values=None):
        name = self._match(sql)
        n = min(self._counts.get(name, 0), 5)
        return [{"subject_key": f"pk_{name}_{i}"} for i in range(n)]


@pytest.mark.asyncio
async def test_all_clean_reports_zero_violations():
    report = await run_catalog_invariant_checks(FakeDb({}))
    assert report["violated_count"] == 0
    assert len(report["checks"]) == len(_CHECKS)
    assert all(not c.get("violated") for c in report["checks"])


@pytest.mark.asyncio
async def test_violation_over_threshold_carries_samples():
    report = await run_catalog_invariant_checks(
        FakeDb({"public_but_suppressed": 3})
    )
    entry = next(c for c in report["checks"] if c["name"] == "public_but_suppressed")
    assert entry["violated"] is True
    assert entry["count"] == 3
    assert len(entry["sample_keys"]) == 3
    assert report["violated_count"] == 1


@pytest.mark.asyncio
async def test_count_at_threshold_is_not_violated():
    # public_not_renderable default threshold is 500 (known c1.v0.5 gap)
    report = await run_catalog_invariant_checks(
        FakeDb({"public_not_renderable": 500})
    )
    entry = next(c for c in report["checks"] if c["name"] == "public_not_renderable")
    assert entry["violated"] is False


@pytest.mark.asyncio
async def test_one_erroring_check_does_not_sink_the_rest():
    report = await run_catalog_invariant_checks(
        FakeDb({"missing_trust_rows": 999}, fail_on="orphan_trust_rows")
    )
    errored = next(c for c in report["checks"] if c["name"] == "orphan_trust_rows")
    assert "error" in errored
    flagged = next(c for c in report["checks"] if c["name"] == "missing_trust_rows")
    assert flagged["violated"] is True
