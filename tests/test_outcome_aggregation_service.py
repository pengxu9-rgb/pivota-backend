"""Tests for the outcome-aggregation pipeline — the honesty gating is load-bearing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import outcome_aggregation_service as svc  # noqa: E402


def test_build_row_suppresses_rate_below_min_sample() -> None:
    # A handful of orders → counts are real, but refund_rate stays NULL (no implied return rate).
    row = svc._build_row("merchant", "all_time", {
        "subject_key": "m1", "transacted_count": 3, "paid_count": 2, "refunded_count": 1,
        "gmv_units": 60.0, "currency": "USD",
    })
    assert row["transacted_count"] == 3
    assert row["refunded_count"] == 1
    assert row["refund_rate"] is None        # suppressed — below MIN_SAMPLE_SIZE
    assert row["min_sample_met"] is False
    assert row["gmv_cents"] == 6000          # 60.00 units → cents
    assert row["aov_cents"] == 2000


def test_build_row_emits_rate_at_or_above_min_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "MIN_SAMPLE_SIZE", 20)
    row = svc._build_row("merchant", "all_time", {
        "subject_key": "m1", "transacted_count": 25, "paid_count": 20, "refunded_count": 5,
        "gmv_units": 500.0, "currency": "USD",
    })
    assert row["min_sample_met"] is True
    assert row["refund_rate"] == 0.2         # 5/25, only because the sample is large enough
    assert row["gmv_cents"] == 50000


# --- W8: outcome -> seller_trust envelope (pure) ---------------------------------

def test_seller_trust_none_when_no_outcome_row() -> None:
    assert svc.seller_trust_from_outcome(None) is None


def test_seller_trust_none_when_zero_transacted() -> None:
    # No transacted orders → no signal, never a fabricated zero-trust.
    assert svc.seller_trust_from_outcome({
        "subject_key": "m1", "transacted_count": 0, "min_sample_met": False,
    }) is None


def test_seller_trust_below_sample_carries_counts_but_no_rate() -> None:
    env = svc.seller_trust_from_outcome({
        "subject_key": "m1", "window_key": "all_time",
        "transacted_count": 4, "paid_count": 3, "refunded_count": 1,
        "refund_rate": None, "gmv_cents": 6000, "currency": "USD",
        "min_sample_met": False,
    })
    assert env is not None
    assert env["transacted_count"] == 4          # honest volume still exposed
    assert env["sample_backed"] is False
    assert env["return_rate"] is None            # no rate claim below sample
    assert env["return_rate_band"] is None


def test_seller_trust_sample_backed_emits_rate_and_band() -> None:
    env = svc.seller_trust_from_outcome({
        "subject_key": "m2", "window_key": "all_time",
        "transacted_count": 40, "paid_count": 38, "refunded_count": 2,
        "refund_rate": 0.05, "gmv_cents": 120000, "currency": "USD",
        "min_sample_met": True,
    })
    assert env["sample_backed"] is True
    assert env["return_rate"] == 0.05
    assert env["return_rate_band"] == "low"      # <= 0.05


def test_seller_trust_band_thresholds() -> None:
    assert svc._return_rate_band(None) is None
    assert svc._return_rate_band(0.05) == "low"
    assert svc._return_rate_band(0.12) == "moderate"
    assert svc._return_rate_band(0.30) == "elevated"


def test_seller_trust_ignores_stored_rate_when_sample_not_met() -> None:
    # Defensive: even if a stray rate is present, don't surface it without sample.
    env = svc.seller_trust_from_outcome({
        "subject_key": "m3", "transacted_count": 5, "refund_rate": 0.4,
        "min_sample_met": False,
    })
    assert env["return_rate"] is None
    assert env["return_rate_band"] is None


def test_build_row_product_uses_gmv_cents_directly() -> None:
    # Product rows carry gmv already in cents (from commerce_attribution_edges).
    row = svc._build_row("product", "trailing_90d", {
        "subject_key": "cp_1", "transacted_count": 2, "paid_count": 2, "refunded_count": 0,
        "gmv_cents": 3380, "currency": "USD",
    })
    assert row["gmv_cents"] == 3380
    assert row["refund_rate"] is None
    assert row["subject_type"] == "product"
    assert row["window_key"] == "trailing_90d"


def test_build_row_zero_transacted_is_safe() -> None:
    row = svc._build_row("merchant", "all_time", {
        "subject_key": "m1", "transacted_count": 0, "paid_count": 0, "refunded_count": 0,
        "gmv_units": 0, "currency": None,
    })
    assert row["aov_cents"] is None
    assert row["refund_rate"] is None
    assert row["min_sample_met"] is False


class _FakeDB:
    def __init__(self, rows_by_marker: Dict[str, List[Dict[str, Any]]]) -> None:
        self._rows = rows_by_marker
        self.upserts: List[Dict[str, Any]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        if isinstance(params, dict) and "subject_type" in params:
            self.upserts.append(params)

    async def fetch_all(self, sql: str, params: Any = None) -> List[Dict[str, Any]]:
        if "FROM orders o" in sql:
            return self._rows.get("merchant", [])
        if "commerce_attribution_edges" in sql:
            return self._rows.get("product", [])
        return []

    async def fetch_one(self, sql: str, params: Any = None):
        return None


@pytest.mark.asyncio
async def test_refresh_all_outcomes_upserts_both_grains(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB({
        "merchant": [{"subject_key": "m1", "transacted_count": 5, "paid_count": 4, "refunded_count": 1, "gmv_units": 100.0, "currency": "USD"}],
        "product": [{"subject_key": "cp_1", "transacted_count": 2, "paid_count": 2, "refunded_count": 0, "gmv_cents": 3380, "currency": "USD"}],
    })
    monkeypatch.setattr(svc, "database", fake)
    svc._DDL_READY = True  # skip ensure_table DDL against the fake
    counts = await svc.refresh_all_outcomes()
    # merchant + product × 2 windows
    assert counts["merchant:all_time"] == 1
    assert counts["product:all_time"] == 1
    assert counts["merchant:trailing_90d"] == 1
    assert counts["product:trailing_90d"] == 1
    # Every upsert went through _build_row → rates suppressed (small samples).
    assert all(u["refund_rate"] is None for u in fake.upserts)
    assert any(u["subject_type"] == "merchant" for u in fake.upserts)
    assert any(u["subject_type"] == "product" for u in fake.upserts)
