"""PR-8: bd_brand_signals.infer_social_intelligence wired into the
main audit pipeline (Option A step 3).

The recent Winona + Beauty of Joseon audits surfaced no social /
creator analysis even though the brand-signals infrastructure
already exists and is wired to the BD cold-start flow. PR-8 routes
that function into `run_brand_report` so every merchant audit can
surface brand TikTok/Instagram presence + KOL endorsements +
competitive social comparison.

Off by default (`include_social_intelligence=False`) per the
LLM-multiplier-safety rule. Tests cover:

  - Default behavior: kwarg=False → social_intelligence is None,
    no Gemini calls dispatched.
  - Opt-in: kwarg=True → social_intelligence populated from the
    mocked bd_brand_signals.infer_social_intelligence return.
  - Competitor brands feed: peer_brand_names from
    per_product.competitive_pressure.peers_named is deduped, capped
    at 10, and threaded to infer_social_intelligence.
  - Empty merchant_domain → skipped even when kwarg=True (no
    fabricated handles).
  - Exception from the function does NOT block the audit return.
  - Render path: markdown renderer surfaces section when
    `available: true`; skips section otherwise.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from services.agent_center_bd_report_service import (
    render_brand_markdown,
    run_brand_report,
)


def _fake_run_bd_probes_factory():
    """Build a stub for run_bd_probes that returns enough data to
    let build_structured_report complete + populate the
    competitive_pressure.peers_named path the social-intel feed
    reads from."""

    async def fake_run_bd_probes(**kwargs):
        return {
            "visibility": {
                "provider": "gemini",
                "scores": {"visibility_score": 0},
                "raw_runs": [],
                "findings": [],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
            "attribution": {
                "provider": "gemini",
                "scores": {"visibility_score": 0},
                "raw_runs": [],
                "findings": [],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
            "category_visibility": None,
        }

    return fake_run_bd_probes


@pytest.fixture
def stubbed_probes(monkeypatch):
    """Stub `run_bd_probes` so the audit pipeline completes without
    real Gemini calls. Each test still passes a real `products`
    list — run_brand_report iterates per-product."""
    from services import agent_center_bd_report_service as bd
    monkeypatch.setattr(bd, "run_bd_probes", _fake_run_bd_probes_factory())
    return None


def _example_products() -> List[Dict[str, Any]]:
    return [{
        "title": "Revive Under Eye Patch",
        "pdp_url": "https://agent.pivota.cc/products/sig_dacaf022d6c6a9ed86ecab1f",
        "vendor": "Beauty of Joseon",
        "product_type": "eye patch",
    }]


# =========================================================================
# Default behavior: off
# =========================================================================


@pytest.mark.asyncio
async def test_social_intelligence_off_by_default(stubbed_probes):
    """LLM-multiplier-safety: off unless explicitly opted in. No
    bd_brand_signals call should be issued."""
    with patch(
        "services.bd_brand_signals.infer_social_intelligence",
        new_callable=AsyncMock,
    ) as mock_fn:
        result = await run_brand_report(
            merchant_name="Beauty of Joseon",
            merchant_domain="beautyofjoseon.com",
            products=_example_products(),
            provider="gemini",
        )
        mock_fn.assert_not_called()
    assert result["social_intelligence"] is None


# =========================================================================
# Opt-in: ON
# =========================================================================


@pytest.mark.asyncio
async def test_social_intelligence_on_populates_field(stubbed_probes):
    """When kwarg=True, run_brand_report calls infer_social_intelligence
    and surfaces the returned dict as `social_intelligence` in the
    response."""
    fake_intel = {
        "own_presence": {
            "tiktok": {
                "handle": "beautyofjoseon",
                "follower_band": "100k_500k",
                "content_focus": "skincare routines",
            },
            "instagram": {
                "handle": "beauty_of_joseon",
                "follower_band": "500k_1m",
                "content_focus": "Korean beauty",
            },
        },
        "kol_endorsements": {"tiktok": [], "instagram": []},
        "competitive_comparison": None,
        "available": True,
    }
    with patch(
        "services.bd_brand_signals.infer_social_intelligence",
        new_callable=AsyncMock,
        return_value=fake_intel,
    ) as mock_fn:
        result = await run_brand_report(
            merchant_name="Beauty of Joseon",
            merchant_domain="beautyofjoseon.com",
            products=_example_products(),
            provider="gemini",
            include_social_intelligence=True,
        )
        mock_fn.assert_awaited_once()
    si = result["social_intelligence"]
    assert si is not None
    assert si["available"] is True
    assert si["own_presence"]["tiktok"]["handle"] == "beautyofjoseon"


@pytest.mark.asyncio
async def test_social_intelligence_threads_peers_named_as_competitors(
    stubbed_probes, monkeypatch,
):
    """The audit's `competitive_pressure.peers_named` flows into
    `infer_social_intelligence(competitor_brands=...)`. Verifies the
    dedup + cap (10) behavior."""
    from services import agent_center_bd_report_service as bd

    # Patch build_structured_report to return a per_product entry
    # with the peers_named the social call should consume.
    orig_build = bd.build_structured_report

    def patched_build(**kwargs):
        report = orig_build(**kwargs)
        report["competitive_pressure"] = {
            "peers_named": (
                [{"name": "Drunk Elephant"}, {"name": "drunk elephant"}]  # dedup
                + [{"name": f"Brand {i}"} for i in range(15)]  # cap@10
            ),
        }
        return report

    monkeypatch.setattr(bd, "build_structured_report", patched_build)

    with patch(
        "services.bd_brand_signals.infer_social_intelligence",
        new_callable=AsyncMock,
        return_value={"available": False},
    ) as mock_fn:
        await run_brand_report(
            merchant_name="Beauty of Joseon",
            merchant_domain="beautyofjoseon.com",
            products=_example_products(),
            provider="gemini",
            include_social_intelligence=True,
        )
    mock_fn.assert_awaited_once()
    kwargs = mock_fn.call_args.kwargs
    competitor_brands = kwargs["competitor_brands"]
    assert len(competitor_brands) == 10, (
        "competitor_brands must be capped at 10"
    )
    # Dedup: "Drunk Elephant" + "drunk elephant" → 1 entry
    lower_unique = {c.lower() for c in competitor_brands}
    assert len(lower_unique) == 10
    assert "drunk elephant" in lower_unique


# =========================================================================
# Safety guards: empty domain, exception
# =========================================================================


@pytest.mark.asyncio
async def test_social_intelligence_skipped_when_merchant_domain_empty(stubbed_probes):
    """Without a merchant_domain we can't run brand-handle queries —
    skip the call, return social_intelligence=None. The function
    itself also guards this (returns available:false) but we skip
    the call entirely so no Gemini quota is consumed."""
    with patch(
        "services.bd_brand_signals.infer_social_intelligence",
        new_callable=AsyncMock,
    ) as mock_fn:
        result = await run_brand_report(
            merchant_name="Beauty of Joseon",
            merchant_domain=None,
            products=_example_products(),
            provider="gemini",
            include_social_intelligence=True,
        )
        mock_fn.assert_not_called()
    assert result["social_intelligence"] is None


@pytest.mark.asyncio
async def test_social_intelligence_exception_does_not_block_audit(stubbed_probes):
    """If the bd_brand_signals function raises, the audit still
    returns a valid report with social_intelligence=None. Mirror of
    bd_cold_start_service's exception-isolation pattern."""
    with patch(
        "services.bd_brand_signals.infer_social_intelligence",
        new_callable=AsyncMock,
        side_effect=RuntimeError("upstream gemini timeout"),
    ):
        result = await run_brand_report(
            merchant_name="Beauty of Joseon",
            merchant_domain="beautyofjoseon.com",
            products=_example_products(),
            provider="gemini",
            include_social_intelligence=True,
        )
    # Audit completes; the report is valid; social_intelligence is null.
    assert "per_product" in result
    assert result["social_intelligence"] is None


# =========================================================================
# Markdown renderer surfaces section
# =========================================================================


def _brand_report_with_social(social: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "merchant_name": "Beauty of Joseon",
        "merchant_domain": "beautyofjoseon.com",
        "timestamp": "2026-05-13T23:30:00Z",
        "provider": "gemini",
        "per_product": [],
        "aggregate": {
            "avg_visibility": 0,
            "avg_attribution": 0,
            "avg_category_visibility": 33,
            "brand_verdict_label": "CATEGORY MENTION",
            "brand_verdict_explanation": "x",
            "products_count": 1,
            "products_succeeded": 1,
            "products_failed": 0,
        },
        "cross_product_competitors": [],
        "social_intelligence": social,
        "failed": [],
    }


def test_renders_social_section_when_available():
    report = _brand_report_with_social({
        "own_presence": {
            "tiktok": {
                "handle": "beautyofjoseon",
                "follower_band": "100k_500k",
                "content_focus": "skincare routines",
            },
            "instagram": None,
        },
        "kol_endorsements": {
            "tiktok": [
                {
                    "creator_name": "@skinrocks",
                    "follower_band": "1m_5m",
                    "post_summary": "K-beauty routine featuring product",
                },
            ],
            "instagram": None,
        },
        "competitive_comparison": [
            {
                "brand": "Drunk Elephant",
                "tiktok_followers": "500k",
                "instagram_followers": "2m",
                "notes": "Heavy K-beauty crossover",
            },
        ],
        "available": True,
    })
    md = render_brand_markdown(report)
    assert "## Social channel intelligence" in md
    assert "TikTok: `@beautyofjoseon`" in md
    assert "@skinrocks" in md
    assert "Drunk Elephant" in md


def test_does_not_render_social_section_when_unavailable():
    report = _brand_report_with_social({"available": False})
    md = render_brand_markdown(report)
    assert "## Social channel intelligence" not in md


def test_does_not_render_social_section_when_field_null():
    """Back-compat: brand reports written before PR-8 don't have a
    `social_intelligence` field at all. Renderer must not crash."""
    report = _brand_report_with_social(None)
    md = render_brand_markdown(report)
    assert "## Social channel intelligence" not in md


def test_renders_competitor_benchmark_table():
    """PR-10: competitor_presence renders a brand-vs-competitor
    benchmark table — the merchant's own row plus a row per
    benchmarked competitor, follower counts from the same probe."""
    report = _brand_report_with_social({
        "own_presence": {
            "tiktok": {"handle": "beautyofjoseon", "follower_estimate": 662000},
            "instagram": {"handle": "boj", "follower_estimate": 1210000},
        },
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": None,
        "competitor_presence": {
            "Drunk Elephant": {
                "tiktok": {"handle": "drunkelephant", "follower_estimate": 510000},
                "instagram": {"handle": "drunkelephant", "follower_estimate": 1800000},
            },
            "Glow Recipe": {
                "tiktok": {"handle": "glowrecipe", "follower_band": "100k-1M"},
                "instagram": None,
            },
        },
        "available": True,
    })
    md = render_brand_markdown(report)
    assert "Brand vs. competitor social benchmark" in md
    # Merchant's own row, marked "(you)".
    assert "(you)" in md
    assert "662000" in md
    # Competitor rows.
    assert "Drunk Elephant" in md
    assert "510000" in md
    assert "Glow Recipe" in md
    # Glow Recipe instagram is None → renders as "—".
    # Glow Recipe tiktok has only a band → renders the band.
    assert "100k-1M" in md


def test_competitor_benchmark_shows_not_verified_for_ungrounded():
    """PR-9 + PR-10 interaction: an ungrounded competitor probe has
    nulled metrics — the benchmark table shows "not verified" rather
    than a fabricated number."""
    report = _brand_report_with_social({
        "own_presence": {
            "tiktok": {"handle": "beautyofjoseon", "follower_estimate": 662000},
            "instagram": None,
        },
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": None,
        "competitor_presence": {
            "Drunk Elephant": {
                "tiktok": {
                    "handle": "drunkelephant",
                    "follower_estimate": None,
                    "follower_band": None,
                    "grounding": "ungrounded",
                },
                "instagram": None,
            },
        },
        "available": True,
    })
    md = render_brand_markdown(report)
    assert "Brand vs. competitor social benchmark" in md
    assert "not verified" in md


def test_does_not_render_competitor_benchmark_when_absent():
    """No competitor_presence → no benchmark table, but the rest of
    the social section still renders."""
    report = _brand_report_with_social({
        "own_presence": {
            "tiktok": {"handle": "beautyofjoseon", "follower_estimate": 662000},
            "instagram": None,
        },
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": None,
        "competitor_presence": None,
        "available": True,
    })
    md = render_brand_markdown(report)
    assert "## Social channel intelligence" in md
    assert "Brand vs. competitor social benchmark" not in md
