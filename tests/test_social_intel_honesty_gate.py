"""PR-9: honesty gate on social-intelligence sub-calls.

The post-PR-8 quality review found that `_infer_own_presence`,
`_infer_kol_endorsements`, and `_infer_competitive_social` in
`services/bd_brand_signals.py` accepted whatever follower counts /
creator lists / competitive figures the LLM returned — with no
verification that the response was actually grounded in a web source.
An ungrounded response answers from internal knowledge: plausible-
sounding numbers that are fabrication, not data.

The honesty gate's *trigger* changed with the search-then-extract
rework (2026-05-14). It used to be `_grounding_chunk_count(payload) > 0`
("did Gemini's google_search tool retrieve anything"). It's now the
deterministic `search_status` from `_search_then_extract`:
  - `_SEARCH_OK`        — SerpAPI returned usable results → grounded
  - `_SEARCH_EMPTY`     — SerpAPI returned nothing → ungrounded
  - `_SEARCH_TRANSPORT` — the search/extraction call failed → transport_error

Gate behavior per sub-call is UNCHANGED:
  - _infer_own_presence: ungrounded → null all metric fields, keep
    handle, mark grounding="ungrounded"
  - _infer_kol_endorsements: ungrounded → return None (a fabricated
    creator list is the highest-risk output; suppress entirely)
  - _infer_competitive_social: ungrounded → return None

These tests mock `_search_then_extract` so no network is touched.
`_grounding_chunk_count` is unchanged + still used by the 4 non-social
grounded callers — its primitive tests stay below.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest

from services.bd_brand_signals import (
    _SEARCH_EMPTY,
    _SEARCH_OK,
    _SEARCH_TRANSPORT,
    _grounding_chunk_count,
    _infer_competitive_social,
    _infer_kol_endorsements,
    _infer_own_presence,
)


# =========================================================================
# _grounding_chunk_count — still used by the 4 non-social grounded callers
# =========================================================================


def test_grounding_chunk_count_camelcase():
    payload = {
        "candidates": [{
            "groundingMetadata": {
                "groundingChunks": [
                    {"web": {"uri": "https://a.com", "title": "A"}},
                    {"web": {"uri": "https://b.com", "title": "B"}},
                ],
            },
        }],
    }
    assert _grounding_chunk_count(payload) == 2


def test_grounding_chunk_count_snakecase():
    """REST API has shipped both camelCase + snake_case shapes."""
    payload = {
        "candidates": [{
            "grounding_metadata": {
                "grounding_chunks": [{"web": {"uri": "https://a.com"}}],
            },
        }],
    }
    assert _grounding_chunk_count(payload) == 1


def test_grounding_chunk_count_zero_when_no_metadata():
    payload = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
    assert _grounding_chunk_count(payload) == 0


def test_grounding_chunk_count_zero_when_empty_chunks():
    payload = {"candidates": [{"groundingMetadata": {"groundingChunks": []}}]}
    assert _grounding_chunk_count(payload) == 0


def test_grounding_chunk_count_handles_garbage_input():
    assert _grounding_chunk_count(None) == 0
    assert _grounding_chunk_count({}) == 0
    assert _grounding_chunk_count({"candidates": []}) == 0
    assert _grounding_chunk_count({"candidates": ["not a dict"]}) == 0
    assert _grounding_chunk_count(
        {"candidates": [{"groundingMetadata": "not a dict"}]}
    ) == 0
    assert _grounding_chunk_count(
        {"candidates": [{"groundingMetadata": {"groundingChunks": "not a list"}}]}
    ) == 0


# =========================================================================
# Test payload builder — an extraction-call response (Gemini-shaped).
# =========================================================================


def _payload(text: str) -> Dict[str, Any]:
    """Build a Gemini-shaped extraction-call response with `text` as the
    body. (groundingMetadata is irrelevant post-rework — the search step
    decides groundedness, not the extraction response.)"""
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


_OWN_PRESENCE_JSON = (
    '{"follower_estimate": 662000, "follower_band": "100k-1M", '
    '"view_per_post_estimate": 71600, "content_focus": "product demos", '
    '"post_frequency": "3-4 per week", "verified_account": true}'
)
_KOL_JSON = (
    '[{"creator_handle": "@skinrocks", "follower_band": "100k-1M", '
    '"post_url": null, "view_count_estimate": 50000, '
    '"post_date": "2025-06-01", "content_summary": "routine feat. brand"}]'
)
_COMPETITIVE_JSON = (
    '[{"brand": "Drunk Elephant", "tiktok_followers_estimate": 500000, '
    '"instagram_followers_estimate": 2000000, '
    '"kol_endorsements_count_estimate": 12, "gap_summary": "ahead on IG"}]'
)


def _ste(payload, status):
    """An AsyncMock standing in for `_search_then_extract`."""
    return AsyncMock(return_value=(payload, status))


# =========================================================================
# _infer_own_presence — ungrounded nulls metrics, keeps handle
# =========================================================================


@pytest.mark.asyncio
async def test_own_presence_grounded_keeps_metrics():
    """search ok → extraction parsed → follower numbers survive,
    grounding marker = grounded, failure_reason None."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(_payload(_OWN_PRESENCE_JSON), _SEARCH_OK),
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "tiktok", "beautyofjoseon", "fake-key",
        )
    assert reason is None
    assert result is not None
    assert result["follower_estimate"] == 662000
    assert result["follower_band"] == "100k-1M"
    assert result["view_per_post_estimate"] == 71600
    assert result["content_focus"] == "product demos"
    assert result["verified_account"] is True
    assert result["grounding"] == "grounded"


@pytest.mark.asyncio
async def test_own_presence_ungrounded_nulls_metrics_keeps_handle():
    """search empty → no extraction runs → every metric nulled, handle
    survives, grounding marker = ungrounded. The result still SURFACES
    (handle present) so failure_reason is None."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(None, _SEARCH_EMPTY),
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "tiktok", "beautyofjoseon", "fake-key",
        )
    assert result is not None  # handle was supplied → still surfaces
    assert reason is None      # surfaced (with grounding=ungrounded), not a failure
    assert result["handle"] == "beautyofjoseon"
    assert result["follower_estimate"] is None
    assert result["follower_band"] is None
    assert result["view_per_post_estimate"] is None
    assert result["content_focus"] is None
    assert result["post_frequency"] is None
    assert result["verified_account"] is None
    assert result["grounding"] == "ungrounded"


@pytest.mark.asyncio
async def test_own_presence_ungrounded_no_handle_suppressed():
    """search empty AND no caller-supplied handle → every field nulled
    → emptiness check returns (None, "ungrounded")."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(None, _SEARCH_EMPTY),
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "instagram", None, "fake-key",
        )
    assert result is None
    assert reason == "ungrounded"


@pytest.mark.asyncio
async def test_own_presence_transport_error():
    """search OR extraction call failed → (None, "transport_error")."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(None, _SEARCH_TRANSPORT),
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "tiktok", "boj", "fake-key",
        )
    assert result is None
    assert reason == "transport_error"


@pytest.mark.asyncio
async def test_own_presence_parse_error_when_not_json():
    """search ok but the extraction body doesn't parse to a dict →
    (None, "parse_error")."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(_payload("this is not json at all"), _SEARCH_OK),
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "tiktok", "boj", "fake-key",
        )
    assert result is None
    assert reason == "parse_error"


@pytest.mark.asyncio
async def test_own_presence_instagram_grounded():
    """Instagram path uses engagement_rate_estimate metric key."""
    ig_json = (
        '{"follower_estimate": 1213393, "follower_band": "1M-10M", '
        '"engagement_rate_estimate": "2.1%", "content_focus": "K-beauty"}'
    )
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(_payload(ig_json), _SEARCH_OK),
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "instagram", "boj", "fake-key",
        )
    assert reason is None
    assert result["follower_estimate"] == 1213393
    assert result["engagement_rate_estimate"] == "2.1%"
    assert result["grounding"] == "grounded"


# =========================================================================
# _infer_kol_endorsements — (result, failure_reason)
# =========================================================================


@pytest.mark.asyncio
async def test_kol_grounded_returns_list():
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(_payload(_KOL_JSON), _SEARCH_OK),
    ):
        result, reason = await _infer_kol_endorsements(
            "Beauty of Joseon", "tiktok", "fake-key",
        )
    assert reason is None
    assert result is not None
    assert len(result) == 1
    assert result[0]["creator_handle"] == "skinrocks"


@pytest.mark.asyncio
async def test_kol_ungrounded_returns_none():
    """search empty → there is nothing to extract from → suppress the
    creator list entirely → (None, "ungrounded")."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(None, _SEARCH_EMPTY),
    ):
        result, reason = await _infer_kol_endorsements(
            "Beauty of Joseon", "tiktok", "fake-key",
        )
    assert result is None
    assert reason == "ungrounded"


@pytest.mark.asyncio
async def test_kol_no_data_when_grounded_but_empty():
    """search ok + extraction parsed but the model found no creators →
    empty list → (None, "no_data"). A legitimate "we checked, found
    nothing" — distinct from a failure."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(_payload("[]"), _SEARCH_OK),
    ):
        result, reason = await _infer_kol_endorsements(
            "Beauty of Joseon", "tiktok", "fake-key",
        )
    assert result is None
    assert reason == "no_data"


@pytest.mark.asyncio
async def test_kol_transport_error():
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(None, _SEARCH_TRANSPORT),
    ):
        result, reason = await _infer_kol_endorsements(
            "Beauty of Joseon", "tiktok", "fake-key",
        )
    assert result is None
    assert reason == "transport_error"


# =========================================================================
# _infer_competitive_social — (result, failure_reason)
# =========================================================================


@pytest.mark.asyncio
async def test_competitive_grounded_returns_list():
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(_payload(_COMPETITIVE_JSON), _SEARCH_OK),
    ):
        result, reason = await _infer_competitive_social(
            "Beauty of Joseon", ["Drunk Elephant"], "fake-key",
        )
    assert reason is None
    assert result is not None
    assert len(result) == 1
    assert result[0]["brand"] == "Drunk Elephant"
    assert result[0]["tiktok_followers_estimate"] == 500000


@pytest.mark.asyncio
async def test_competitive_ungrounded_returns_none():
    """search empty → suppress competitor figures → (None, "ungrounded")."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(None, _SEARCH_EMPTY),
    ):
        result, reason = await _infer_competitive_social(
            "Beauty of Joseon", ["Drunk Elephant"], "fake-key",
        )
    assert result is None
    assert reason == "ungrounded"


@pytest.mark.asyncio
async def test_competitive_parse_error_when_not_array():
    with patch(
        "services.bd_brand_signals._search_then_extract",
        _ste(_payload("not an array"), _SEARCH_OK),
    ):
        result, reason = await _infer_competitive_social(
            "Beauty of Joseon", ["Drunk Elephant"], "fake-key",
        )
    assert result is None
    assert reason == "parse_error"


@pytest.mark.asyncio
async def test_competitive_empty_competitors_returns_none():
    """Pre-existing guard still holds — no competitors → no call.
    Not a failure (nothing was attempted) → reason is None."""
    with patch(
        "services.bd_brand_signals._search_then_extract",
        new_callable=AsyncMock,
    ) as mock_call:
        result, reason = await _infer_competitive_social(
            "Beauty of Joseon", [], "fake-key",
        )
        mock_call.assert_not_called()
    assert result is None
    assert reason is None
