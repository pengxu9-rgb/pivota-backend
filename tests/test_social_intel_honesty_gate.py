"""PR-9: honesty gate on social-intelligence sub-calls.

The post-PR-8 quality review found that `_infer_own_presence`,
`_infer_kol_endorsements`, and `_infer_competitive_social` in
`services/bd_brand_signals.py` accepted whatever follower counts /
creator lists / competitive figures the LLM returned — with no
verification that the response was actually grounded in a web
source. An ungrounded Gemini response answers from internal
knowledge: plausible-sounding numbers that are fabrication, not data.

PR-9 adds `_grounding_chunk_count(payload)`: every `_gemini_grounded_call`
uses the `google_search` tool, so a grounded response carries
`candidates[0].groundingMetadata.groundingChunks`. Zero chunks means
the model didn't consult the web.

Gate behavior per sub-call:
  - _infer_own_presence: ungrounded → null all metric fields, keep
    handle, mark grounding="ungrounded"
  - _infer_kol_endorsements: ungrounded → return None (a fabricated
    creator list is the highest-risk output; suppress entirely)
  - _infer_competitive_social: ungrounded → return None

These tests mock `_gemini_grounded_call` so no network is touched.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from services.bd_brand_signals import (
    _grounding_chunk_count,
    _infer_competitive_social,
    _infer_kol_endorsements,
    _infer_own_presence,
)


# =========================================================================
# _grounding_chunk_count — the gate primitive
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
    """Ungrounded response: candidate has content but no groundingMetadata."""
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
# Test payload builders
# =========================================================================


def _payload(text: str, *, grounded: bool) -> Dict[str, Any]:
    """Build a Gemini-shaped payload with `text` as the response and
    either 2 grounding chunks (grounded) or none (ungrounded)."""
    candidate: Dict[str, Any] = {
        "content": {"parts": [{"text": text}]},
    }
    if grounded:
        candidate["groundingMetadata"] = {
            "groundingChunks": [
                {"web": {"uri": "https://socialblade.com/x", "title": "SB"}},
                {"web": {"uri": "https://tiktok.com/@brand", "title": "TT"}},
            ],
        }
    return {"candidates": [candidate]}


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


# =========================================================================
# _infer_own_presence — ungrounded nulls metrics, keeps handle
# All three sub-calls now return a (result, failure_reason) tuple.
# =========================================================================


@pytest.mark.asyncio
async def test_own_presence_grounded_keeps_metrics():
    """Grounded response (2 chunks) → follower numbers survive,
    grounding marker = grounded, failure_reason None."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(_OWN_PRESENCE_JSON, grounded=True),
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
    """Ungrounded response (0 chunks) → every metric nulled, handle
    survives, grounding marker = ungrounded. The result still
    SURFACES (handle present) so failure_reason is None — the
    ungrounded state is carried by the `grounding` field."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(_OWN_PRESENCE_JSON, grounded=False),
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
    """Ungrounded AND no caller-supplied handle → every field nulled
    → emptiness check returns (None, "ungrounded")."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(_OWN_PRESENCE_JSON, grounded=False),
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "instagram", None, "fake-key",
        )
    assert result is None
    assert reason == "ungrounded"


@pytest.mark.asyncio
async def test_own_presence_transport_error_when_payload_none():
    """_gemini_grounded_call returns None (HTTP/timeout/429) →
    (None, "transport_error")."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result, reason = await _infer_own_presence(
            "Beauty of Joseon", "tiktok", "boj", "fake-key",
        )
    assert result is None
    assert reason == "transport_error"


@pytest.mark.asyncio
async def test_own_presence_parse_error_when_not_json():
    """Response body that doesn't parse to a dict → (None, "parse_error")."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload("this is not json at all", grounded=True),
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
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(ig_json, grounded=True),
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
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(_KOL_JSON, grounded=True),
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
    """Ungrounded creator list is fabrication — suppress entirely.
    Even though the JSON parses fine, 0 grounding chunks means the
    model invented these creators → (None, "ungrounded")."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(_KOL_JSON, grounded=False),
    ):
        result, reason = await _infer_kol_endorsements(
            "Beauty of Joseon", "tiktok", "fake-key",
        )
    assert result is None
    assert reason == "ungrounded"


@pytest.mark.asyncio
async def test_kol_no_data_when_grounded_but_empty():
    """Grounded + parsed but the model found no creators → the list
    is empty → (None, "no_data"). A legitimate "we checked, found
    nothing" outcome — distinct from a failure."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload("[]", grounded=True),
    ):
        result, reason = await _infer_kol_endorsements(
            "Beauty of Joseon", "tiktok", "fake-key",
        )
    assert result is None
    assert reason == "no_data"


@pytest.mark.asyncio
async def test_kol_transport_error_when_payload_none():
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=None,
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
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(_COMPETITIVE_JSON, grounded=True),
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
    """Ungrounded competitor figures — suppress → (None, "ungrounded")."""
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload(_COMPETITIVE_JSON, grounded=False),
    ):
        result, reason = await _infer_competitive_social(
            "Beauty of Joseon", ["Drunk Elephant"], "fake-key",
        )
    assert result is None
    assert reason == "ungrounded"


@pytest.mark.asyncio
async def test_competitive_parse_error_when_not_array():
    with patch(
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
        return_value=_payload("not an array", grounded=True),
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
        "services.bd_brand_signals._gemini_grounded_call",
        new_callable=AsyncMock,
    ) as mock_call:
        result, reason = await _infer_competitive_social(
            "Beauty of Joseon", [], "fake-key",
        )
        mock_call.assert_not_called()
    assert result is None
    assert reason is None
