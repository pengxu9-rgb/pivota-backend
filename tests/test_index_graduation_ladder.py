"""Unit tests for the P1.5 commerce-index graduation ladder.

Pure-logic + mocked-database tests (no real SQL execution) so they run on the
default SQLite test backend. The ladder's SQL portability + monotonicity are
asserted by inspecting the SQL/params the module builds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import index_graduation_ladder as svc  # noqa: E402


class FakeDB:
    """Routes fetch_one by SQL content; records executed statements."""

    def __init__(self, *, count=0, ips_flags=None):
        self.count = count
        self.ips_flags = ips_flags or {}
        self.executed = []

    async def fetch_one(self, sql, params=None):
        s = str(sql)
        if "index_eligible" in s:
            return dict(self.ips_flags) if self.ips_flags else None
        if "COUNT(*)" in s:
            return {"n": self.count}
        return None

    async def execute(self, sql, params=None):
        self.executed.append({"sql": str(sql), "params": dict(params or {})})


def test_target_readiness_tier_mapping():
    assert svc.target_readiness_tier(index_eligible=True, serving_eligible=True) == "commerce_ready"
    assert svc.target_readiness_tier(index_eligible=True, serving_eligible=False) == "knowledge_ready"
    assert svc.target_readiness_tier(index_eligible=False, serving_eligible=False) is None
    # serving implies index in practice, but serving alone still maps to top.
    assert svc.target_readiness_tier(index_eligible=False, serving_eligible=True) == "commerce_ready"


def test_tiers_below_is_monotonic_prefix():
    assert svc._tiers_below("referral_only") == []
    assert svc._tiers_below("knowledge_ready") == ["referral_only"]
    assert svc._tiers_below("commerce_ready") == ["referral_only", "knowledge_ready"]


def test_build_where_pins_observed_track_and_price_clause():
    where, params = svc._build_where(["referral_only"], require_row_price=False)
    assert "catalog_track = :observed_track" in where
    assert "truth_tier = :observed_truth" in where
    assert "readiness_tier IN (:b0)" in where
    assert params["observed_track"] == "external_referral"
    assert params["observed_truth"] == "observed"
    assert params["b0"] == "referral_only"
    assert "catalog_offers" not in where

    where_p, _ = svc._build_where(
        ["referral_only", "knowledge_ready"], require_row_price=True
    )
    assert "readiness_tier IN (:b0, :b1)" in where_p
    assert "catalog_offers" in where_p and "list_price > 0" in where_p


@pytest.mark.asyncio
async def test_advance_is_dark_by_default(monkeypatch):
    monkeypatch.delenv("INDEX_GRADUATION_LADDER_ENABLED", raising=False)
    fake = FakeDB(count=5)
    monkeypatch.setattr(svc, "database", fake)
    advanced = await svc._advance_to(
        "ck1", "knowledge_ready", require_row_price=False, reason="t"
    )
    assert advanced == 0
    assert fake.executed == []  # no write while dark


@pytest.mark.asyncio
async def test_advance_floor_target_is_noop(monkeypatch):
    monkeypatch.setenv("INDEX_GRADUATION_LADDER_ENABLED", "1")
    fake = FakeDB(count=9)
    monkeypatch.setattr(svc, "database", fake)
    # referral_only is the floor — nothing can advance UP to it.
    advanced = await svc._advance_to(
        "ck1", "referral_only", require_row_price=False, reason="t"
    )
    assert advanced == 0
    assert fake.executed == []


@pytest.mark.asyncio
async def test_advance_updates_when_enabled_and_rows_qualify(monkeypatch):
    monkeypatch.setenv("INDEX_GRADUATION_LADDER_ENABLED", "on")
    fake = FakeDB(count=2)
    monkeypatch.setattr(svc, "database", fake)
    advanced = await svc._advance_to(
        "ck1", "knowledge_ready", require_row_price=False, reason="t"
    )
    assert advanced == 2
    assert len(fake.executed) == 1
    upd = fake.executed[0]
    assert "UPDATE catalog_products SET readiness_tier = :target" in upd["sql"]
    assert upd["params"]["target"] == "knowledge_ready"
    assert upd["params"]["content_key"] == "ck1"
    assert upd["params"]["b0"] == "referral_only"


@pytest.mark.asyncio
async def test_advance_no_qualifying_rows_skips_write(monkeypatch):
    monkeypatch.setenv("INDEX_GRADUATION_LADDER_ENABLED", "1")
    fake = FakeDB(count=0)
    monkeypatch.setattr(svc, "database", fake)
    advanced = await svc._advance_to(
        "ck1", "commerce_ready", require_row_price=True, reason="t"
    )
    assert advanced == 0
    assert fake.executed == []


@pytest.mark.asyncio
async def test_advance_from_state_selects_target(monkeypatch):
    monkeypatch.setenv("INDEX_GRADUATION_LADDER_ENABLED", "1")
    fake = FakeDB(count=1)
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.advance_from_state(
        "ck1", {"index_eligible": True, "serving_eligible": False}
    )
    assert result == {"content_key": "ck1", "target": "knowledge_ready", "advanced": 1}


@pytest.mark.asyncio
async def test_advance_from_state_below_floor_is_noop(monkeypatch):
    monkeypatch.setenv("INDEX_GRADUATION_LADDER_ENABLED", "1")
    fake = FakeDB(count=3)
    monkeypatch.setattr(svc, "database", fake)
    result = await svc.advance_from_state(
        "ck1", {"index_eligible": False, "serving_eligible": False}
    )
    assert result["target"] is None
    assert result["advanced"] == 0
    assert fake.executed == []


@pytest.mark.asyncio
async def test_graduate_content_key_recomputes_then_advances(monkeypatch):
    monkeypatch.setenv("INDEX_GRADUATION_LADDER_ENABLED", "1")
    fake = FakeDB(count=1, ips_flags={"index_eligible": True, "serving_eligible": True})
    monkeypatch.setattr(svc, "database", fake)

    calls = {"recompute": 0}

    async def fake_recompute(content_key, *, reason=None):
        calls["recompute"] += 1
        return True

    monkeypatch.setattr(
        "services.index_pipeline_state_service.recompute_serving_eligibility",
        fake_recompute,
    )

    result = await svc.graduate_content_key("ck1")
    assert calls["recompute"] == 1
    assert result["target"] == "commerce_ready"
    assert result["advanced"] == 1
    # commerce_ready target must carry the per-row price guard in the write.
    assert any("catalog_offers" in e["sql"] for e in fake.executed)
