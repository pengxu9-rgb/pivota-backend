"""PR: social-intel failure-reason surfacing.

The PR-8 prod run showed `competitive_comparison: null` with no
explanation — an operator couldn't tell if the lookup failed to
parse, was rate-limited, or just found nothing. Each social
sub-call now returns a `(result, failure_reason)` tuple;
`infer_social_intelligence` assembles them into a `failure_reasons`
dict, and the markdown renderer maps each reason to a
merchant-readable one-liner instead of silently omitting the
sub-section.

These tests cover the orchestration (failure_reasons assembly) and
the renderer (explanation lines). Per-sub-call failure_reason
production is covered in test_social_intel_honesty_gate.py.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.agent_center_bd_report_service import render_brand_markdown
from services.bd_brand_signals import infer_social_intelligence


def _presence(handle: str, followers: int) -> Dict[str, Any]:
    return {
        "platform": "tiktok",
        "handle": handle,
        "follower_estimate": followers,
        "follower_band": "100k-1M",
        "view_per_post_estimate": 50000,
        "content_focus": "demos",
        "post_frequency": None,
        "verified_account": None,
        "grounding": "grounded",
    }


def _patch_subcalls(
    *,
    own_presence,
    kol,
    competitive,
    homepage=None,
):
    """Patch the api-key resolver, homepage fetch, and the three
    sub-calls. Each sub-call arg is a side_effect callable or a
    fixed (result, reason) tuple return_value."""
    own_mock = (
        AsyncMock(side_effect=own_presence)
        if callable(own_presence)
        else AsyncMock(return_value=own_presence)
    )
    kol_mock = (
        AsyncMock(side_effect=kol)
        if callable(kol)
        else AsyncMock(return_value=kol)
    )
    comp_mock = (
        AsyncMock(side_effect=competitive)
        if callable(competitive)
        else AsyncMock(return_value=competitive)
    )
    return patch.multiple(
        "services.bd_brand_signals",
        _resolve_gemini_api_key=lambda: "fake-key",
        _fetch_homepage_html=AsyncMock(return_value=homepage),
        _infer_own_presence=own_mock,
        _infer_kol_endorsements=kol_mock,
        _infer_competitive_social=comp_mock,
    )


# =========================================================================
# infer_social_intelligence assembles failure_reasons
# =========================================================================


@pytest.mark.asyncio
async def test_failure_reasons_dict_present_in_return():
    """Every infer_social_intelligence return carries a
    failure_reasons dict with the expected keys."""
    with _patch_subcalls(
        own_presence=(_presence("boj", 662000), None),
        kol=(None, "no_data"),
        competitive=(None, None),
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com",
        )
    fr = result["failure_reasons"]
    assert set(fr.keys()) == {
        "own_presence_tiktok", "own_presence_instagram",
        "kol_tiktok", "kol_instagram",
        "competitive_comparison", "competitor_presence",
    }


@pytest.mark.asyncio
async def test_failure_reasons_none_when_subcall_succeeds():
    """A sub-call that returned data → its failure_reason is None."""
    with _patch_subcalls(
        own_presence=(_presence("boj", 662000), None),
        kol=([{"creator_handle": "x"}], None),
        competitive=([{"brand": "Y"}], None),
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com",
            competitor_brands=["Drunk Elephant"],
        )
    fr = result["failure_reasons"]
    assert fr["own_presence_tiktok"] is None
    assert fr["own_presence_instagram"] is None
    assert fr["kol_tiktok"] is None
    assert fr["competitive_comparison"] is None


@pytest.mark.asyncio
async def test_failure_reasons_capture_per_subcall_tokens():
    """Distinct failure reasons per sub-call are threaded into the
    failure_reasons dict — own_presence parse_error, kol ungrounded,
    competitive transport_error all surface separately."""

    async def own_fail(brand, platform, handle, api_key):
        return (None, "parse_error")

    async def kol_fail(brand, platform, api_key):
        return (None, "ungrounded")

    async def comp_fail(brand, competitors, api_key):
        return (None, "transport_error")

    with _patch_subcalls(
        own_presence=own_fail, kol=kol_fail, competitive=comp_fail,
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com",
            competitor_brands=["Drunk Elephant"],
        )
    fr = result["failure_reasons"]
    assert fr["own_presence_tiktok"] == "parse_error"
    assert fr["own_presence_instagram"] == "parse_error"
    assert fr["kol_tiktok"] == "ungrounded"
    assert fr["kol_instagram"] == "ungrounded"
    assert fr["competitive_comparison"] == "transport_error"


@pytest.mark.asyncio
async def test_failure_reasons_per_competitor():
    """A failed per-competitor benchmark probe records its reason
    under failure_reasons.competitor_presence[<name>]."""

    async def own_presence(brand, platform, handle, api_key):
        # Merchant succeeds; the competitor probe fails to parse.
        if brand == "Beauty of Joseon":
            return (_presence("boj", 662000), None)
        return (None, "parse_error")

    with _patch_subcalls(
        own_presence=own_presence,
        kol=(None, "no_data"),
        competitive=(None, None),
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com",
            competitor_brands=["Drunk Elephant"],
        )
    comp_fr = result["failure_reasons"]["competitor_presence"]
    assert comp_fr is not None
    assert comp_fr["Drunk Elephant"]["tiktok"] == "parse_error"
    assert comp_fr["Drunk Elephant"]["instagram"] == "parse_error"


@pytest.mark.asyncio
async def test_failure_reasons_exception_becomes_transport_error():
    """An exception escaping a sub-call coro → the _safe() wrapper
    records "transport_error" rather than letting the failure vanish."""

    async def own_raises(brand, platform, handle, api_key):
        raise RuntimeError("unexpected boom")

    with _patch_subcalls(
        own_presence=own_raises,
        kol=(None, "no_data"),
        competitive=(None, None),
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com",
        )
    fr = result["failure_reasons"]
    assert fr["own_presence_tiktok"] == "transport_error"
    assert fr["own_presence_instagram"] == "transport_error"


@pytest.mark.asyncio
async def test_failure_reasons_empty_shape_when_no_api_key():
    """The {available:false} early-return carries a failure_reasons
    dict with all-None values so downstream consumers can rely on
    the key + sub-keys existing."""
    with patch(
        "services.bd_brand_signals._resolve_gemini_api_key",
        return_value=None,
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon", "beautyofjoseon.com",
        )
    assert result["available"] is False
    fr = result["failure_reasons"]
    assert fr["own_presence_tiktok"] is None
    assert fr["competitive_comparison"] is None
    assert fr["competitor_presence"] is None


# =========================================================================
# Renderer surfaces failure-reason explanation lines
# =========================================================================


def _brand_report(social: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "merchant_name": "Beauty of Joseon",
        "merchant_domain": "beautyofjoseon.com",
        "timestamp": "2026-05-14T00:00:00Z",
        "provider": "gemini",
        "per_product": [],
        "aggregate": {
            "avg_visibility": 0, "avg_attribution": 0,
            "avg_category_visibility": 33,
            "brand_verdict_label": "CATEGORY MENTION",
            "brand_verdict_explanation": "x",
            "products_count": 1, "products_succeeded": 1, "products_failed": 0,
        },
        "cross_product_competitors": [],
        "social_intelligence": social,
        "failed": [],
    }


def test_renderer_shows_competitive_comparison_failure_note():
    """competitive_comparison null + reason=parse_error → the
    renderer shows an explanation line, not silent omission."""
    report = _brand_report({
        "own_presence": {
            "tiktok": {"handle": "boj", "follower_estimate": 662000},
            "instagram": None,
        },
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": None,
        "competitor_presence": None,
        "failure_reasons": {
            "own_presence_tiktok": None,
            "own_presence_instagram": None,
            "kol_tiktok": None,
            "kol_instagram": None,
            "competitive_comparison": "parse_error",
            "competitor_presence": None,
        },
        "available": True,
    })
    md = render_brand_markdown(report)
    # parse_error → the honest "returned source evidence" wording (the renderer
    # distinguishes failure modes rather than a generic "unavailable").
    assert "Competitive social comparison: not verified from the returned source evidence" in md


def test_renderer_shows_own_presence_failure_note():
    """own_presence_instagram null + reason=transport_error → the
    renderer shows the explanation under the own-presence block."""
    report = _brand_report({
        "own_presence": {
            "tiktok": {"handle": "boj", "follower_estimate": 662000},
            "instagram": None,
        },
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": None,
        "competitor_presence": None,
        "failure_reasons": {
            "own_presence_tiktok": None,
            "own_presence_instagram": "transport_error",
            "kol_tiktok": None,
            "kol_instagram": None,
            "competitive_comparison": None,
            "competitor_presence": None,
        },
        "available": True,
    })
    md = render_brand_markdown(report)
    # transport_error → "source check did not complete".
    assert "Instagram presence: not verified because the source check did not complete" in md


def test_renderer_shows_kol_no_data_note():
    """kol null + reason=no_data → "found nothing for this brand"
    explanation, distinguishing "checked, none found" from "failed"."""
    report = _brand_report({
        "own_presence": {
            "tiktok": {"handle": "boj", "follower_estimate": 662000},
            "instagram": None,
        },
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": None,
        "competitor_presence": None,
        "failure_reasons": {
            "own_presence_tiktok": None,
            "own_presence_instagram": None,
            "kol_tiktok": "no_data",
            "kol_instagram": "no_data",
            "competitive_comparison": None,
            "competitor_presence": None,
        },
        "available": True,
    })
    md = render_brand_markdown(report)
    # no_data → "no live source evidence found" (checked, found nothing — NOT a
    # transport/parse failure).
    assert "creator endorsements: no live source evidence found for this brand" in md.lower()


def test_renderer_no_failure_note_when_subcall_succeeded():
    """When a sub-call succeeded (reason None) the renderer shows the
    data, not an explanation line."""
    report = _brand_report({
        "own_presence": {
            "tiktok": {"handle": "boj", "follower_estimate": 662000},
            "instagram": {"handle": "boj_ig", "follower_estimate": 1200000},
        },
        "kol_endorsements": {"tiktok": None, "instagram": None},
        "competitive_comparison": [
            {"brand": "Drunk Elephant", "tiktok_followers": "500k"},
        ],
        "competitor_presence": None,
        "failure_reasons": {
            "own_presence_tiktok": None,
            "own_presence_instagram": None,
            "kol_tiktok": None,
            "kol_instagram": None,
            "competitive_comparison": None,
            "competitor_presence": None,
        },
        "available": True,
    })
    md = render_brand_markdown(report)
    assert "unavailable" not in md.lower()
    assert "662000" in md
    assert "Drunk Elephant" in md
