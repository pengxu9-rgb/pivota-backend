"""PR-10: per-competitor social benchmark probes.

The PR-8 prod run reported the merchant's own follower counts
(662k TikTok / 1.21M Instagram) but `competitive_comparison` came
back null — leaving those numbers with no benchmark. "662k TikTok"
is meaningless without a peer to compare against.

PR-10: `infer_social_intelligence` now runs `_infer_own_presence`
for the top `_COMPETITOR_BENCHMARK_CAP` (2) named competitors —
the SAME probe used for the merchant, so the comparison is
apples-to-apples and PR-9's grounding honesty gate applies
per-competitor. The result surfaces as a `competitor_presence`
field: {competitor_name: {tiktok: {...}|null, instagram: {...}|null}}.

These tests mock `_infer_own_presence` / `_infer_kol_endorsements` /
`_infer_competitive_social` so no network is touched, and verify the
orchestration in `infer_social_intelligence`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.bd_brand_signals import (
    _COMPETITOR_BENCHMARK_CAP,
    infer_social_intelligence,
)


def _presence(handle: str, followers: int, *, grounded: bool = True) -> Dict[str, Any]:
    """Shape of an _infer_own_presence return."""
    return {
        "platform": "tiktok",
        "handle": handle,
        "follower_estimate": followers if grounded else None,
        "follower_band": "100k-1M" if grounded else None,
        "view_per_post_estimate": 50000 if grounded else None,
        "content_focus": "product demos" if grounded else None,
        "post_frequency": "3-4 per week" if grounded else None,
        "verified_account": True if grounded else None,
        "grounding": "grounded" if grounded else "ungrounded",
    }


@pytest.fixture
def patched_subcalls():
    """Patch the three sub-call helpers + the api-key resolver so
    `infer_social_intelligence` runs its orchestration without
    network. `_infer_own_presence` is given a side_effect that
    returns a presence dict keyed off the brand arg so each
    competitor probe is distinguishable."""

    async def fake_own_presence(brand, platform, handle, api_key):
        # Each brand gets a distinct follower count so tests can
        # assert which probe produced which result.
        followers = {
            "Beauty of Joseon": 662000,
            "Drunk Elephant": 510000,
            "Glow Recipe": 430000,
            "COSRX": 380000,
        }.get(brand, 100000)
        return _presence(brand.lower().replace(" ", ""), followers)

    async def fake_kol(brand, platform, api_key):
        return []  # empty list = grounded-but-no-endorsements

    async def fake_competitive(brand, competitors, api_key):
        return None  # the fragile single call — null is its common outcome

    with patch(
        "services.bd_brand_signals._resolve_gemini_api_key",
        return_value="fake-key",
    ), patch(
        # Handle-detection fallback fetches the homepage when no
        # detected_handles were threaded in — mock it so the suite
        # never touches the network.
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.bd_brand_signals._infer_own_presence",
        new_callable=AsyncMock,
        side_effect=fake_own_presence,
    ) as mock_own, patch(
        "services.bd_brand_signals._infer_kol_endorsements",
        new_callable=AsyncMock,
        side_effect=fake_kol,
    ), patch(
        "services.bd_brand_signals._infer_competitive_social",
        new_callable=AsyncMock,
        side_effect=fake_competitive,
    ):
        yield mock_own


# =========================================================================
# Per-competitor probe orchestration
# =========================================================================


@pytest.mark.asyncio
async def test_competitor_presence_populated_for_named_competitors(patched_subcalls):
    """Two named competitors → competitor_presence has an entry per
    competitor, each with tiktok + instagram presence dicts."""
    result = await infer_social_intelligence(
        "Beauty of Joseon",
        "beautyofjoseon.com",
        competitor_brands=["Drunk Elephant", "Glow Recipe"],
    )
    cp = result["competitor_presence"]
    assert cp is not None
    assert set(cp.keys()) == {"Drunk Elephant", "Glow Recipe"}
    assert cp["Drunk Elephant"]["tiktok"]["follower_estimate"] == 510000
    assert cp["Glow Recipe"]["tiktok"]["follower_estimate"] == 430000
    # Own presence still populated independently.
    assert result["own_presence"]["tiktok"]["follower_estimate"] == 662000


@pytest.mark.asyncio
async def test_competitor_benchmark_capped_at_2(patched_subcalls):
    """Even with 4 named competitors, only the top
    _COMPETITOR_BENCHMARK_CAP (2) get a benchmark probe — bounds the
    added grounded-call count."""
    assert _COMPETITOR_BENCHMARK_CAP == 2
    result = await infer_social_intelligence(
        "Beauty of Joseon",
        "beautyofjoseon.com",
        competitor_brands=["Drunk Elephant", "Glow Recipe", "COSRX", "Some Brand"],
    )
    cp = result["competitor_presence"]
    assert len(cp) == 2
    # The FIRST two competitors in the list are benchmarked.
    assert set(cp.keys()) == {"Drunk Elephant", "Glow Recipe"}


@pytest.mark.asyncio
async def test_no_competitors_means_no_competitor_presence(patched_subcalls):
    """Empty competitor_brands → competitor_presence is None, only
    own-presence + KOL probes run."""
    result = await infer_social_intelligence(
        "Beauty of Joseon",
        "beautyofjoseon.com",
        competitor_brands=[],
    )
    assert result["competitor_presence"] is None
    # Own presence still works.
    assert result["own_presence"]["tiktok"]["follower_estimate"] == 662000


@pytest.mark.asyncio
async def test_probe_count_scales_with_competitors(patched_subcalls):
    """Verify the actual number of _infer_own_presence calls:
    2 (own: tt+ig) + 2*N (per-competitor) where N is capped at 2.
    With 2 competitors → 2 + 4 = 6 own-presence calls."""
    mock_own = patched_subcalls
    await infer_social_intelligence(
        "Beauty of Joseon",
        "beautyofjoseon.com",
        competitor_brands=["Drunk Elephant", "Glow Recipe"],
    )
    # 2 for the merchant (tiktok + instagram) + 4 for 2 competitors.
    assert mock_own.await_count == 6


@pytest.mark.asyncio
async def test_probe_count_with_one_competitor(patched_subcalls):
    """1 competitor → 2 (own) + 2 (competitor) = 4 own-presence calls."""
    mock_own = patched_subcalls
    await infer_social_intelligence(
        "Beauty of Joseon",
        "beautyofjoseon.com",
        competitor_brands=["Drunk Elephant"],
    )
    assert mock_own.await_count == 4


# =========================================================================
# Honesty gate applies per-competitor (PR-9 interaction)
# =========================================================================


@pytest.mark.asyncio
async def test_ungrounded_competitor_probe_still_surfaces_with_nulled_metrics():
    """PR-9's grounding gate runs inside _infer_own_presence — so an
    ungrounded competitor probe returns a dict with nulled metrics +
    grounding='ungrounded'. It still surfaces in competitor_presence
    (the renderer shows "not verified"); it's not silently dropped."""

    async def mixed_own_presence(brand, platform, handle, api_key):
        # Merchant grounded; competitor ungrounded.
        grounded = brand == "Beauty of Joseon"
        return _presence(
            brand.lower().replace(" ", ""), 662000, grounded=grounded,
        )

    async def fake_kol(brand, platform, api_key):
        return []

    async def fake_competitive(brand, competitors, api_key):
        return None

    with patch(
        "services.bd_brand_signals._resolve_gemini_api_key",
        return_value="fake-key",
    ), patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.bd_brand_signals._infer_own_presence",
        new_callable=AsyncMock,
        side_effect=mixed_own_presence,
    ), patch(
        "services.bd_brand_signals._infer_kol_endorsements",
        new_callable=AsyncMock,
        side_effect=fake_kol,
    ), patch(
        "services.bd_brand_signals._infer_competitive_social",
        new_callable=AsyncMock,
        side_effect=fake_competitive,
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon",
            "beautyofjoseon.com",
            competitor_brands=["Drunk Elephant"],
        )
    comp = result["competitor_presence"]["Drunk Elephant"]
    # Surfaced, but every metric nulled + marked ungrounded.
    assert comp["tiktok"]["grounding"] == "ungrounded"
    assert comp["tiktok"]["follower_estimate"] is None
    # Merchant's own grounded data is unaffected.
    assert result["own_presence"]["tiktok"]["follower_estimate"] == 662000


@pytest.mark.asyncio
async def test_competitor_dropped_when_both_platforms_return_none():
    """If both a competitor's platform probes return None (fully
    failed, not just ungrounded), that competitor is dropped from
    competitor_presence — same suppress-fully-empty rule as
    own_presence."""

    async def own_presence_brand_only(brand, platform, handle, api_key):
        # Merchant returns data; competitor returns None entirely.
        if brand == "Beauty of Joseon":
            return _presence("beautyofjoseon", 662000)
        return None

    async def fake_kol(brand, platform, api_key):
        return []

    async def fake_competitive(brand, competitors, api_key):
        return None

    with patch(
        "services.bd_brand_signals._resolve_gemini_api_key",
        return_value="fake-key",
    ), patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.bd_brand_signals._infer_own_presence",
        new_callable=AsyncMock,
        side_effect=own_presence_brand_only,
    ), patch(
        "services.bd_brand_signals._infer_kol_endorsements",
        new_callable=AsyncMock,
        side_effect=fake_kol,
    ), patch(
        "services.bd_brand_signals._infer_competitive_social",
        new_callable=AsyncMock,
        side_effect=fake_competitive,
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon",
            "beautyofjoseon.com",
            competitor_brands=["Drunk Elephant"],
        )
    # Competitor had no data on either platform → dropped → dict
    # empty → competitor_presence is None.
    assert result["competitor_presence"] is None
    # Merchant's own data still surfaced; available stays True.
    assert result["own_presence"]["tiktok"]["follower_estimate"] == 662000
    assert result["available"] is True


# =========================================================================
# Guards
# =========================================================================


@pytest.mark.asyncio
async def test_no_api_key_returns_empty_with_competitor_presence_field():
    """The {available:false} early-return shape includes the new
    competitor_presence field (None) so downstream consumers can rely
    on the key existing."""
    with patch(
        "services.bd_brand_signals._resolve_gemini_api_key",
        return_value=None,
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon",
            "beautyofjoseon.com",
            competitor_brands=["Drunk Elephant"],
        )
    assert result["available"] is False
    assert "competitor_presence" in result
    assert result["competitor_presence"] is None


@pytest.mark.asyncio
async def test_available_true_when_only_competitor_presence_succeeds():
    """If own-presence + KOL all fail but a competitor probe lands,
    `available` is still True — competitor_presence counts as a
    surfaced signal."""

    async def own_presence_competitor_only(brand, platform, handle, api_key):
        if brand == "Beauty of Joseon":
            return None  # merchant's own probes all fail
        return _presence("drunkelephant", 510000)

    async def fake_kol(brand, platform, api_key):
        return None  # KOL also fails

    async def fake_competitive(brand, competitors, api_key):
        return None

    with patch(
        "services.bd_brand_signals._resolve_gemini_api_key",
        return_value="fake-key",
    ), patch(
        "services.bd_brand_signals._fetch_homepage_html",
        new_callable=AsyncMock,
        return_value=None,
    ), patch(
        "services.bd_brand_signals._infer_own_presence",
        new_callable=AsyncMock,
        side_effect=own_presence_competitor_only,
    ), patch(
        "services.bd_brand_signals._infer_kol_endorsements",
        new_callable=AsyncMock,
        side_effect=fake_kol,
    ), patch(
        "services.bd_brand_signals._infer_competitive_social",
        new_callable=AsyncMock,
        side_effect=fake_competitive,
    ):
        result = await infer_social_intelligence(
            "Beauty of Joseon",
            "beautyofjoseon.com",
            competitor_brands=["Drunk Elephant"],
        )
    assert result["available"] is True
    assert result["competitor_presence"]["Drunk Elephant"]["tiktok"]["follower_estimate"] == 510000
