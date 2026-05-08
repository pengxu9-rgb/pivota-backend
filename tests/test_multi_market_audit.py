"""
Phase C scaffolding tests — multi-market audit.

Status: scaffolding only. Per-market dispatch (running N audits in
parallel, one per locale) lands in a follow-up PR after staging
load test confirms concurrency caps from #384 hold under multi-
market load.

These tests verify the data shapes + emission gating + honesty
rules:
  - Flag OFF (default) → empty markets aggregate, no localization
    action emitted
  - Flag ON + insufficient data → no localization action (don't
    fabricate "you should localize" advice)
  - Flag ON + clear gap → localization action surfaces with the
    gap evidence
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.fixture
def multi_market_off(monkeypatch):
    from config import settings as settings_module
    monkeypatch.setattr(
        settings_module.settings, "phase_c_multi_market_enabled", False,
    )


@pytest.fixture
def multi_market_on(monkeypatch):
    from config import settings as settings_module
    monkeypatch.setattr(
        settings_module.settings, "phase_c_multi_market_enabled", True,
    )


# -----------------------------------------------------------------
# Settings + flag plumbing
# -----------------------------------------------------------------


def test_is_multi_market_enabled_default_false(multi_market_off):
    from services.multi_market_audit import is_multi_market_enabled
    assert is_multi_market_enabled() is False


def test_is_multi_market_enabled_when_flag_on(multi_market_on):
    from services.multi_market_audit import is_multi_market_enabled
    assert is_multi_market_enabled() is True


def test_get_enabled_markets_returns_default_when_flag_off(
    multi_market_off, monkeypatch,
):
    """When the flag is OFF, return only the default market — no
    multi-market dispatch."""
    from config import settings as settings_module
    from services.multi_market_audit import get_enabled_markets
    monkeypatch.setattr(
        settings_module.settings, "audit_default_market_locale", "en-US",
    )
    monkeypatch.setattr(
        settings_module.settings, "phase_c_enabled_markets_raw", "en-US,en-GB,ja-JP",
    )
    assert get_enabled_markets() == ["en-US"]


def test_get_enabled_markets_returns_list_when_flag_on(multi_market_on, monkeypatch):
    from config import settings as settings_module
    from services.multi_market_audit import get_enabled_markets
    monkeypatch.setattr(
        settings_module.settings, "phase_c_enabled_markets_raw", "en-US, en-GB, ja-JP",
    )
    assert get_enabled_markets() == ["en-US", "en-GB", "ja-JP"]


def test_get_enabled_markets_falls_back_when_list_empty(multi_market_on, monkeypatch):
    """Defensive: if the flag is on but the list is unset/blank,
    fall back to the default market — don't accidentally fire a
    zero-market audit."""
    from config import settings as settings_module
    from services.multi_market_audit import get_enabled_markets
    monkeypatch.setattr(
        settings_module.settings, "phase_c_enabled_markets_raw", "",
    )
    monkeypatch.setattr(
        settings_module.settings, "audit_default_market_locale", "en-US",
    )
    assert get_enabled_markets() == ["en-US"]


# -----------------------------------------------------------------
# Aggregate shape
# -----------------------------------------------------------------


def test_empty_aggregate_shape():
    from services.multi_market_audit import empty_markets_aggregate
    out = empty_markets_aggregate()
    assert out["enabled"] is False
    assert out["default_market"] is None
    assert out["per_market"] == []


def test_build_aggregate_carries_results_through(multi_market_on):
    from services.multi_market_audit import build_markets_aggregate
    rows = [
        {"market_locale": "en-US", "visibility_score": 80, "attribution_score": 70,
         "category_visibility_score": 75, "verdict_label": "STRONG",
         "probed_at": "2026-05-08T20:00:00+00:00"},
        {"market_locale": "en-GB", "visibility_score": 30, "attribution_score": 10,
         "category_visibility_score": 25, "verdict_label": "INVISIBLE",
         "probed_at": "2026-05-08T20:01:00+00:00"},
    ]
    out = build_markets_aggregate(default_market="en-US", per_market_results=rows)
    assert out["enabled"] is True
    assert out["default_market"] == "en-US"
    assert out["per_market"] == rows


# -----------------------------------------------------------------
# Localization action emission gating
# -----------------------------------------------------------------


def test_no_action_when_multi_market_disabled(multi_market_off):
    from services.multi_market_audit import (
        build_localization_action,
        empty_markets_aggregate,
    )
    out = build_localization_action(
        markets_aggregate=empty_markets_aggregate(),
    )
    assert out is None


def test_no_action_when_only_one_market(multi_market_on):
    from services.multi_market_audit import (
        build_localization_action,
        build_markets_aggregate,
    )
    rows = [
        {"market_locale": "en-US", "visibility_score": 80,
         "attribution_score": 70, "category_visibility_score": 75,
         "verdict_label": "STRONG", "probed_at": "..."},
    ]
    aggregate = build_markets_aggregate(
        default_market="en-US", per_market_results=rows,
    )
    assert build_localization_action(markets_aggregate=aggregate) is None


def test_no_action_when_gap_below_threshold(multi_market_on):
    """A 25-point gap is below the 30-point threshold — don't
    fabricate localization advice for marginal differences."""
    from services.multi_market_audit import (
        build_localization_action,
        build_markets_aggregate,
    )
    rows = [
        {"market_locale": "en-US", "visibility_score": 60,
         "attribution_score": 50, "category_visibility_score": 55,
         "verdict_label": "PARTIAL", "probed_at": "..."},
        {"market_locale": "en-GB", "visibility_score": 35,
         "attribution_score": 20, "category_visibility_score": 30,
         "verdict_label": "PARTIAL", "probed_at": "..."},
    ]
    aggregate = build_markets_aggregate(
        default_market="en-US", per_market_results=rows,
    )
    assert build_localization_action(markets_aggregate=aggregate) is None


def test_action_emits_when_gap_above_threshold(multi_market_on):
    from services.multi_market_audit import (
        build_localization_action,
        build_markets_aggregate,
    )
    rows = [
        {"market_locale": "en-US", "visibility_score": 80,
         "attribution_score": 70, "category_visibility_score": 75,
         "verdict_label": "STRONG", "probed_at": "..."},
        {"market_locale": "en-GB", "visibility_score": 20,
         "attribution_score": 10, "category_visibility_score": 15,
         "verdict_label": "INVISIBLE", "probed_at": "..."},
    ]
    aggregate = build_markets_aggregate(
        default_market="en-US", per_market_results=rows,
    )
    out = build_localization_action(markets_aggregate=aggregate)
    assert out is not None
    assert out["lever"] == "market_localization"
    assert out["severity"] == "high"
    # Best market named in title + body
    assert "en-GB" in out["title"]
    assert "en-US" in out["body"]
    assert "en-GB" in out["body"]
    # Gap surfaced in evidence
    assert out["evidence"]["best_market"] == "en-US"
    assert out["evidence"]["worst_market"] == "en-GB"
    assert out["evidence"]["gap_points"] == 60


def test_action_picks_widest_gap_among_many_markets(multi_market_on):
    """Three markets with US strong + GB middle + JP weak. Action
    targets JP (worst) vs US (best), not the middle one."""
    from services.multi_market_audit import (
        build_localization_action,
        build_markets_aggregate,
    )
    rows = [
        {"market_locale": "en-US", "visibility_score": 80,
         "attribution_score": 70, "category_visibility_score": 75,
         "verdict_label": "STRONG", "probed_at": "..."},
        {"market_locale": "en-GB", "visibility_score": 50,
         "attribution_score": 40, "category_visibility_score": 45,
         "verdict_label": "PARTIAL", "probed_at": "..."},
        {"market_locale": "ja-JP", "visibility_score": 5,
         "attribution_score": 0, "category_visibility_score": 5,
         "verdict_label": "INVISIBLE", "probed_at": "..."},
    ]
    aggregate = build_markets_aggregate(
        default_market="en-US", per_market_results=rows,
    )
    out = build_localization_action(markets_aggregate=aggregate)
    assert out["evidence"]["best_market"] == "en-US"
    assert out["evidence"]["worst_market"] == "ja-JP"
    assert out["evidence"]["gap_points"] == 75


# -----------------------------------------------------------------
# End-to-end via _build_merchant_view
# -----------------------------------------------------------------


def test_merchant_view_receipts_includes_empty_markets_when_flag_off(
    multi_market_off,
):
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )
    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=None,
    )
    markets = mv["receipts"]["markets"]
    assert markets["enabled"] is False
    assert markets["per_market"] == []


def test_merchant_view_no_localization_action_when_flag_off(multi_market_off):
    """Even if the audit somehow had per-market data, with the flag
    OFF no localization action emits."""
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )
    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=None,
    )
    actions = mv["actions"]
    assert all(a.get("lever") != "market_localization" for a in actions)
