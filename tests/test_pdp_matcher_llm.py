"""Pure-function tests for services/pdp_matcher/llm_match.py.

Live Gemini calls aren't tested. Instead we pin:
- the response parser handles fenced / embedded / unparseable JSON
- the validator rejects -1 / low-confidence / out-of-range indices
- the offline mock returns None (matches the documented contract:
  no fabricated matches without a real LLM judging)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.pdp_matcher.llm_match import (  # noqa: E402
    MIN_LLM_MATCH_CONFIDENCE,
    _mock_match,
    _parse_response_payload,
    _validate_match,
    llm_match_seed,
)


def _candidates(*pks):
    return [{"product_key": pk, "title": f"title for {pk}", "brand": "MAC", "canonical_url": ""} for pk in pks]


def _payload(text: str):
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_parse_response_plain_json():
    payload = _payload('{"match_index": 1, "product_key": "pk_b", "confidence": 0.9, "reasoning": "ok"}')
    parsed = _parse_response_payload(payload)
    assert parsed is not None
    assert parsed["match_index"] == 1


def test_parse_response_strips_fence():
    payload = _payload('```json\n{"match_index": 0, "product_key": "pk_a", "confidence": 0.8}\n```')
    parsed = _parse_response_payload(payload)
    assert parsed is not None
    assert parsed["match_index"] == 0


def test_parse_response_finds_embedded_json():
    payload = _payload("Here is my answer:\n\n{\"match_index\": -1, \"confidence\": 1.0}\n\nThanks.")
    parsed = _parse_response_payload(payload)
    assert parsed is not None
    assert parsed["match_index"] == -1


def test_parse_response_unparseable_returns_none():
    assert _parse_response_payload(_payload("just words, no json")) is None


def test_parse_response_no_candidates():
    assert _parse_response_payload({}) is None
    assert _parse_response_payload({"candidates": []}) is None


def test_validate_match_accepts_high_confidence():
    cands = _candidates("pk_a", "pk_b", "pk_c")
    parsed = {"match_index": 1, "product_key": "pk_b", "confidence": 0.9, "reasoning": "same product"}
    result = _validate_match(parsed, cands)
    assert result is not None
    assert result["product_key"] == "pk_b"
    assert result["confidence"] == 0.9
    assert result["matcher"] == "llm_match_v1"
    assert "same product" in result["evidence"]


def test_validate_match_rejects_minus_one():
    cands = _candidates("pk_a")
    parsed = {"match_index": -1, "confidence": 1.0}
    assert _validate_match(parsed, cands) is None


def test_validate_match_rejects_below_threshold():
    cands = _candidates("pk_a")
    parsed = {"match_index": 0, "product_key": "pk_a", "confidence": MIN_LLM_MATCH_CONFIDENCE - 0.01}
    assert _validate_match(parsed, cands) is None


def test_validate_match_rejects_index_out_of_range():
    cands = _candidates("pk_a", "pk_b")
    parsed = {"match_index": 5, "product_key": "pk_x", "confidence": 0.9}
    assert _validate_match(parsed, cands) is None


def test_validate_match_uses_index_when_returned_key_disagrees():
    """Defense in depth — if the model echoes a hallucinated product_key
    but match_index points at a real candidate, trust the index."""
    cands = _candidates("pk_a", "pk_b")
    parsed = {"match_index": 0, "product_key": "pk_FAKE", "confidence": 0.9}
    result = _validate_match(parsed, cands)
    assert result is not None
    assert result["product_key"] == "pk_a"


def test_validate_match_handles_string_confidence():
    cands = _candidates("pk_a")
    parsed = {"match_index": 0, "product_key": "pk_a", "confidence": "0.95"}
    result = _validate_match(parsed, cands)
    assert result is not None
    assert result["confidence"] == 0.95


def test_validate_match_rejects_non_numeric_confidence():
    cands = _candidates("pk_a")
    parsed = {"match_index": 0, "product_key": "pk_a", "confidence": "not a number"}
    assert _validate_match(parsed, cands) is None


def test_validate_match_truncates_long_reasoning():
    cands = _candidates("pk_a")
    parsed = {"match_index": 0, "product_key": "pk_a", "confidence": 0.9, "reasoning": "X" * 500}
    result = _validate_match(parsed, cands)
    assert result is not None
    assert len(result["evidence"]) <= 200


def test_mock_match_returns_none():
    """Offline mode is intentionally pessimistic — we don't fabricate
    matches without a real LLM judging."""
    assert _mock_match(seed={"title": "anything"}, candidates=_candidates("pk_a")) is None


def test_llm_match_offline_returns_none_for_empty_candidates():
    result = asyncio.run(llm_match_seed(seed={"title": "x"}, candidates=[], api_key=""))
    assert result is None


def test_llm_match_offline_returns_none_with_candidates():
    result = asyncio.run(
        llm_match_seed(seed={"title": "x"}, candidates=_candidates("pk_a"), api_key="")
    )
    assert result is None
