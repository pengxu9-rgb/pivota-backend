"""PR-5: funnel recorder + analytics tests.

Pure-function coverage of:
  - infer_source_channel: heuristic dispatch
  - infer_stage: event_type → stage mapping
  - compute_stage_conversion_rates: rollup math + edge cases

DB-touching paths (record_funnel_event, fetch_stage_counts,
compute_funnel, /funnel endpoint) exercised end-to-end on staging.
"""

from __future__ import annotations

from typing import Any, Dict

from services.funnel_analytics import (
    STAGE_ORDER,
    compute_stage_conversion_rates,
)
from services.funnel_recorder import (
    infer_source_channel,
    infer_stage,
)
from db.funnel_events import SOURCE_CHANNELS, STAGES


# ---------------------------------------------------------------------------
# infer_source_channel
# ---------------------------------------------------------------------------


def test_channel_ai_agent_from_endpoint():
    """Pivota agent endpoints map to ai_agent."""
    assert infer_source_channel(endpoint="/agent/v1/beauty/products/search") == "ai_agent"
    assert infer_source_channel(endpoint="/api/agent-center/bd/cold-start-audit") == "ai_agent"
    assert infer_source_channel(endpoint="/api/agent/products") == "ai_agent"


def test_channel_social_from_utm():
    assert infer_source_channel(utm_source="tiktok") == "social_own"
    assert infer_source_channel(utm_source="instagram") == "social_own"
    assert infer_source_channel(utm_source="instagram-paid") == "social_own"
    assert infer_source_channel(utm_source="tiktok.bio.link") == "social_own"


def test_channel_seo_from_utm():
    assert infer_source_channel(utm_source="google") == "seo_organic"
    assert infer_source_channel(utm_source="bing-organic") == "seo_organic"


def test_channel_retail_from_utm():
    assert infer_source_channel(utm_source="amazon-affiliate") == "retail"
    assert infer_source_channel(utm_source="sephora.com") == "retail"
    assert infer_source_channel(utm_source="walmart-paid-ad") == "retail"


def test_channel_editorial_from_referrer_host_match():
    assert infer_source_channel(referrer="https://nymag.com/strategist/article/123") == "editorial"
    assert infer_source_channel(referrer="https://www.forbes.com/vetted/best-x") == "editorial"


def test_channel_direct_when_signal_present_but_unmatched():
    """utm_source or referrer present but doesn't match any rule —
    safer to call it 'direct' than 'unknown' (we know SOMETHING about
    the source)."""
    assert infer_source_channel(utm_source="random-newsletter") == "direct"
    assert infer_source_channel(referrer="https://random-blog.com") == "direct"


def test_channel_unknown_when_no_signal():
    assert infer_source_channel() == "unknown"
    assert infer_source_channel(endpoint=None, utm_source=None, referrer=None) == "unknown"


def test_channel_endpoint_takes_precedence_over_utm():
    """If endpoint says ai_agent, that wins even if utm hints
    something else (the endpoint is the highest-confidence signal)."""
    assert infer_source_channel(
        endpoint="/agent/products/search", utm_source="tiktok"
    ) == "ai_agent"


def test_all_inferred_channels_are_valid():
    """The inferer must only emit values from SOURCE_CHANNELS."""
    inputs = [
        {"endpoint": "/agent/x"},
        {"utm_source": "tiktok"},
        {"utm_source": "google"},
        {"utm_source": "amazon"},
        {"referrer": "nymag.com"},
        {"utm_source": "random"},
        {},
    ]
    for inp in inputs:
        assert infer_source_channel(**inp) in SOURCE_CHANNELS


# ---------------------------------------------------------------------------
# infer_stage
# ---------------------------------------------------------------------------


def test_stage_from_event_types():
    assert infer_stage("search") == "impression"
    assert infer_stage("catalog_query") == "impression"
    assert infer_stage("browse_category") == "impression"
    assert infer_stage("product_detail") == "pdp_view"
    assert infer_stage("pdp_view") == "pdp_view"
    assert infer_stage("click") == "click"
    assert infer_stage("ctr_event") == "click"
    assert infer_stage("cart_add") == "add_to_cart"
    assert infer_stage("wishlist_add") == "add_to_cart"
    assert infer_stage("order_created") == "conversion"
    assert infer_stage("checkout_completed") == "conversion"
    assert infer_stage("payment_succeeded") == "conversion"
    assert infer_stage("profile_visit") == "profile_visit"


def test_stage_defaults_to_impression():
    assert infer_stage(None) == "impression"
    assert infer_stage("") == "impression"
    assert infer_stage("totally_unknown_event") == "impression"


def test_all_inferred_stages_are_valid():
    samples = ["search", "view", "click", "cart_add", "order", "profile", "garbage"]
    for s in samples:
        assert infer_stage(s) in STAGES


# ---------------------------------------------------------------------------
# compute_stage_conversion_rates
# ---------------------------------------------------------------------------


def test_conversion_rates_full_funnel():
    """A complete funnel with every stage populated."""
    counts = {
        "impression": 1000,
        "click": 300,
        "pdp_view": 250,
        "add_to_cart": 50,
        "conversion": 25,
    }
    out = compute_stage_conversion_rates(counts)
    by_stage = {r["stage"]: r for r in out}

    # impression → profile_visit (next in canonical order); profile_visit count = 0
    # so conversion_to_next = 0
    assert by_stage["impression"]["count"] == 1000
    # profile_visit count = 0 → conversion to it = 0/1000 = 0
    assert by_stage["impression"]["conversion_to_next"] == 0.0

    # The interesting transitions: click → pdp_view (300 → 250 = 0.833)
    assert by_stage["click"]["conversion_to_next"] == round(250 / 300, 4)
    assert by_stage["pdp_view"]["conversion_to_next"] == round(50 / 250, 4)
    assert by_stage["add_to_cart"]["conversion_to_next"] == round(25 / 50, 4)

    # Last stage: conversion has no next
    assert by_stage["conversion"]["conversion_to_next"] is None
    assert by_stage["conversion"]["drop_off_pct"] is None


def test_conversion_rates_canonical_order_preserved():
    """Output is always in STAGE_ORDER, even when input has fewer
    stages."""
    out = compute_stage_conversion_rates({"impression": 100})
    stages = [r["stage"] for r in out]
    assert stages == STAGE_ORDER


def test_conversion_rates_zero_upstream_yields_null():
    """When upstream stage has 0 events, downstream conversion_to_next
    is null (avoid division by zero / misleading 0/0=0%)."""
    out = compute_stage_conversion_rates({"click": 0, "pdp_view": 50})
    by_stage = {r["stage"]: r for r in out}
    assert by_stage["click"]["count"] == 0
    assert by_stage["click"]["conversion_to_next"] is None
    assert by_stage["click"]["drop_off_pct"] is None


def test_conversion_rates_empty_input():
    """No events anywhere → every stage count=0, every transition null."""
    out = compute_stage_conversion_rates({})
    assert all(r["count"] == 0 for r in out)
    assert all(r["conversion_to_next"] is None for r in out)


def test_conversion_rates_drop_off_complements_conversion():
    """drop_off_pct + conversion_to_next = 1.0 when both defined."""
    counts = {"impression": 1000, "profile_visit": 700}
    out = compute_stage_conversion_rates(counts)
    impression_row = next(r for r in out if r["stage"] == "impression")
    assert impression_row["conversion_to_next"] + impression_row["drop_off_pct"] == 1.0


def test_conversion_rates_handles_string_counts_defensively():
    """If callers pass string counts, coerce safely."""
    out = compute_stage_conversion_rates({"impression": "100", "click": "30"})
    by_stage = {r["stage"]: r for r in out}
    assert by_stage["impression"]["count"] == 100
    assert by_stage["click"]["count"] == 30


def test_conversion_rates_handles_none_count_as_zero():
    out = compute_stage_conversion_rates({"impression": None, "click": 50})
    by_stage = {r["stage"]: r for r in out}
    assert by_stage["impression"]["count"] == 0
    assert by_stage["click"]["count"] == 50
