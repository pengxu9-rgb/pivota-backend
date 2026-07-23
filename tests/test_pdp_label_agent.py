"""Tests for services/pdp_label_agent.py (Phase O-3a).

Module is the LLM-powered classifier for the long tail Phase O-2's
deterministic extractors couldn't fill. Most tests here are pure
(no HTTP), with a small set of mocked-Gemini integration tests for
the `classify_pdp` async path including retry logic.
"""

from __future__ import annotations

import json

import pytest

from services.pdp_label_agent import (
    DEMOGRAPHIC_VOCAB,
    LIFESTYLE_VOCAB,
    USE_CASE_VOCAB,
    _empty_result,
    build_label_prompt,
    classify_pdp,
    merge_classification_into_row,
    parse_label_response,
    should_classify,
)


# ---------------------------------------------------------------------------
# should_classify — pre-call gate
# ---------------------------------------------------------------------------


def test_should_classify_returns_true_when_demographic_null():
    assert should_classify({"demographic": None, "category_path": "x", "use_case_tags": [], "lifestyle_tags": []}) is True


def test_should_classify_returns_true_when_category_path_null():
    assert should_classify({"demographic": "women", "category_path": None, "use_case_tags": [], "lifestyle_tags": []}) is True


def test_should_classify_returns_true_when_typed_lists_null():
    """[] means 'extractor saw and emitted empty', NULL means 'never
    classified'. Only NULL should trigger LabelAgent — empty list
    shouldn't waste a Gemini call."""
    assert should_classify({"demographic": "women", "category_path": "x", "use_case_tags": None, "lifestyle_tags": []}) is True
    assert should_classify({"demographic": "women", "category_path": "x", "use_case_tags": [], "lifestyle_tags": None}) is True


def test_should_classify_returns_false_when_all_fields_have_values():
    """Empty list counts as 'classified'."""
    row = {"demographic": "women", "category_path": "beauty/x", "use_case_tags": [], "lifestyle_tags": []}
    assert should_classify(row) is False


def test_should_classify_handles_non_dict():
    assert should_classify(None) is False
    assert should_classify("not a row") is False


# ---------------------------------------------------------------------------
# build_label_prompt — pure prompt construction
# ---------------------------------------------------------------------------


def test_prompt_contains_all_vocab_tokens():
    """Future operators changing a vocab need the prompt to update.
    Pinning ensures regression."""
    prompt = build_label_prompt({"title": "x", "description": "y"})
    for token in DEMOGRAPHIC_VOCAB + USE_CASE_VOCAB + LIFESTYLE_VOCAB:
        assert token in prompt, f"vocab token {token!r} missing from prompt"


def test_prompt_includes_current_values():
    prompt = build_label_prompt(
        {
            "title": "Vegan Cream",
            "brand": "GoodSkin",
            "description": "for daily use",
            "demographic": "women",
            "category_path": "beauty/skincare/treat/serum",
            "tags": ["vegan", "k-beauty"],
        }
    )
    assert "Vegan Cream" in prompt
    assert "GoodSkin" in prompt
    assert "women" in prompt
    assert "beauty/skincare/treat/serum" in prompt
    assert "vegan" in prompt and "k-beauty" in prompt


def test_prompt_handles_missing_fields_with_placeholders():
    prompt = build_label_prompt({})
    assert "(no title)" in prompt
    assert "(no description)" in prompt
    assert "(unknown brand)" in prompt
    # demographic etc. should print "null" not throw
    assert "demographic: null" in prompt
    assert "category_path: null" in prompt


# ---------------------------------------------------------------------------
# parse_label_response — Gemini → typed fields
# ---------------------------------------------------------------------------


def _gemini_response(*, text: str) -> dict:
    """Build a minimal Gemini-shaped response wrapping the given text."""
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_parse_response_extracts_canonical_shape():
    payload = _gemini_response(
        text=json.dumps(
            {
                "demographic": "women",
                "use_case_tags": ["daily"],
                "lifestyle_tags": ["vegan", "cruelty_free"],
                "category_path": "beauty/skincare/treat/serum",
                "confidence": 0.85,
                "reasoning": "Clear from title.",
            }
        )
    )
    result = parse_label_response(payload)
    assert result["demographic"] == "women"
    assert result["use_case_tags"] == ["daily"]
    assert result["lifestyle_tags"] == ["vegan", "cruelty_free"]
    assert result["category_path"] == "beauty/skincare/treat/serum"
    assert result["confidence"] == 0.85
    assert result["reasoning"] == "Clear from title."
    assert result["drop_reason"] is None


def test_parse_response_filters_invented_tokens():
    """Model hallucinated 'matte' under lifestyle_tags. Vocab filter drops it."""
    payload = _gemini_response(
        text=json.dumps(
            {
                "demographic": "WOMEN",  # capitalization shouldn't break
                "use_case_tags": ["daily", "made_up_token"],
                "lifestyle_tags": ["matte", "vegan"],  # matte is a use_case-ish color, not lifestyle
                "category_path": "beauty/skincare/x",
                "confidence": 0.5,
            }
        )
    )
    result = parse_label_response(payload)
    assert result["demographic"] == "women"  # case-folded
    assert result["use_case_tags"] == ["daily"]  # made_up_token filtered
    assert result["lifestyle_tags"] == ["vegan"]  # matte filtered


def test_parse_response_handles_markdown_fence():
    """Sometimes the model still wraps even with structured output enabled."""
    payload = _gemini_response(
        text="```json\n"
        + json.dumps(
            {
                "demographic": "men",
                "use_case_tags": [],
                "lifestyle_tags": [],
                "confidence": 0.9,
            }
        )
        + "\n```"
    )
    result = parse_label_response(payload)
    assert result["demographic"] == "men"
    assert result["confidence"] == 0.9


def test_parse_response_returns_drop_reason_when_no_candidates():
    result = parse_label_response({"candidates": []})
    assert result["drop_reason"] == "gemini_no_candidates"
    assert result["demographic"] is None
    assert result["use_case_tags"] == []


def test_parse_response_returns_drop_reason_when_no_text_parts():
    payload = {"candidates": [{"content": {"parts": []}}]}
    result = parse_label_response(payload)
    assert result["drop_reason"] == "gemini_no_text_parts"


def test_parse_response_returns_drop_reason_when_no_json_block():
    payload = _gemini_response(text="I'm just chatting, no JSON here.")
    result = parse_label_response(payload)
    assert result["drop_reason"] == "gemini_json_no_balanced_block"


def test_parse_response_returns_drop_reason_when_json_malformed():
    """Balanced braces but invalid JSON inside — direct json.loads
    fails AND the regex extraction yields the same un-parseable
    block. Should land at gemini_json_decode_failed (regex found a
    block, json.loads still failed)."""
    payload = _gemini_response(text='{"demographic": "women", confidence: 0.5,}')
    result = parse_label_response(payload)
    assert result["drop_reason"] == "gemini_json_decode_failed"


def test_parse_response_clamps_confidence_to_unit_interval():
    payload = _gemini_response(
        text=json.dumps({"use_case_tags": [], "lifestyle_tags": [], "confidence": 1.5})
    )
    assert parse_label_response(payload)["confidence"] == 1.0
    payload = _gemini_response(
        text=json.dumps({"use_case_tags": [], "lifestyle_tags": [], "confidence": -0.3})
    )
    assert parse_label_response(payload)["confidence"] == 0.0


def test_parse_response_handles_non_numeric_confidence():
    payload = _gemini_response(
        text=json.dumps(
            {"use_case_tags": [], "lifestyle_tags": [], "confidence": "high"}
        )
    )
    assert parse_label_response(payload)["confidence"] == 0.0


# ---------------------------------------------------------------------------
# merge_classification_into_row — preserve-merchant semantics
# ---------------------------------------------------------------------------


def test_merge_does_not_overwrite_existing_demographic():
    row = {"demographic": "women", "category_path": None, "use_case_tags": None, "lifestyle_tags": None}
    result = {
        "demographic": "men",
        "use_case_tags": ["daily"],
        "lifestyle_tags": ["vegan"],
        "category_path": "beauty/x",
    }
    merged = merge_classification_into_row(row, result)
    assert merged["demographic"] == "women"  # merchant value preserved
    assert merged["category_path"] == "beauty/x"  # was NULL, agent fills
    assert merged["use_case_tags"] == ["daily"]
    assert merged["lifestyle_tags"] == ["vegan"]


def test_merge_does_not_overwrite_existing_category_path():
    row = {"demographic": None, "category_path": "beauty/skincare/x", "use_case_tags": None, "lifestyle_tags": None}
    result = {
        "demographic": "women",
        "use_case_tags": [],
        "lifestyle_tags": [],
        "category_path": "beauty/skincare/y",  # agent suggests different — IGNORED
    }
    merged = merge_classification_into_row(row, result)
    assert merged["category_path"] == "beauty/skincare/x"


def test_merge_does_not_overwrite_existing_empty_list():
    """[] from the deterministic extractor means 'looked, found
    nothing'. The LabelAgent shouldn't second-guess that."""
    row = {"demographic": None, "category_path": None, "use_case_tags": [], "lifestyle_tags": []}
    result = {
        "demographic": "unisex",
        "use_case_tags": ["daily"],
        "lifestyle_tags": ["vegan"],
        "category_path": "beauty/x",
    }
    merged = merge_classification_into_row(row, result)
    assert merged["use_case_tags"] == []  # NOT overwritten
    assert merged["lifestyle_tags"] == []
    assert merged["demographic"] == "unisex"  # was NULL, fill OK
    assert merged["category_path"] == "beauty/x"


def test_merge_returns_new_dict_does_not_mutate_input():
    row = {"demographic": None, "category_path": None, "use_case_tags": None, "lifestyle_tags": None}
    result = {"demographic": "women", "use_case_tags": [], "lifestyle_tags": [], "category_path": None}
    merged = merge_classification_into_row(row, result)
    assert row["demographic"] is None  # input untouched
    assert merged["demographic"] == "women"


# ---------------------------------------------------------------------------
# classify_pdp — async with mocked HTTP
# ---------------------------------------------------------------------------


class _MockResponse:
    def __init__(self, *, status_code: int, body: str = "", json_payload: dict = None):
        self.status_code = status_code
        self.text = body
        self._json = json_payload

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _MockHttpClient:
    """Drop-in replacement for httpx.AsyncClient: async context manager
    that yields self with a mocked .post()."""

    def __init__(self, responses: list, capture: dict = None):
        self._responses = list(responses)
        self.calls: list = []
        self._capture = capture if capture is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "json": dict(json or {})})
        if not self._responses:
            raise RuntimeError("mock client out of responses")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_classify_pdp_returns_no_api_key_drop_reason_when_env_missing(monkeypatch):
    """Without an API key, the function returns a deterministic empty
    result tagged with drop_reason='no_api_key' so the batch worker
    can count it."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PIVOTA_GEMINI_API_KEY", raising=False)
    result = await classify_pdp({"title": "x"})
    assert result["drop_reason"] == "no_api_key"
    assert result["confidence"] == 0.0


@pytest.mark.asyncio
async def test_classify_pdp_happy_path(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    classification = {
        "demographic": "women",
        "use_case_tags": ["daily"],
        "lifestyle_tags": ["vegan"],
        "category_path": "beauty/skincare/treat/serum",
        "confidence": 0.9,
        "reasoning": "Clear",
    }
    gemini_response = _MockResponse(
        status_code=200,
        json_payload={
            "candidates": [{"content": {"parts": [{"text": json.dumps(classification)}]}}]
        },
    )
    client = _MockHttpClient(responses=[gemini_response])
    result = await classify_pdp({"title": "Vegan Daily Cream", "description": "for women"}, http_client=client)
    assert result["demographic"] == "women"
    assert result["use_case_tags"] == ["daily"]
    assert result["lifestyle_tags"] == ["vegan"]
    assert result["category_path"] == "beauty/skincare/treat/serum"
    assert result["confidence"] == 0.9
    assert result["drop_reason"] is None
    # Sanity: structured output config in the request
    body = client.calls[0]["json"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseSchema" in body["generationConfig"]
    # Grounding intentionally OFF
    assert "tools" not in body


@pytest.mark.asyncio
async def test_classify_pdp_retries_on_429(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    classification = {"use_case_tags": [], "lifestyle_tags": [], "confidence": 0.5}
    success_response = _MockResponse(
        status_code=200,
        json_payload={"candidates": [{"content": {"parts": [{"text": json.dumps(classification)}]}}]},
    )
    rate_limited = _MockResponse(status_code=429, body="Too Many Requests")
    client = _MockHttpClient(responses=[rate_limited, success_response])
    result = await classify_pdp({"title": "x"}, max_retries=1, http_client=client)
    assert result["drop_reason"] is None  # retried + succeeded
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_classify_pdp_does_not_retry_on_400(monkeypatch):
    """4xx-non-rate-limit signals a request shape problem; no point
    retrying. The result drops with the drop_reason set."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    bad = _MockResponse(status_code=400, body="Bad Request")
    client = _MockHttpClient(responses=[bad])
    result = await classify_pdp({"title": "x"}, max_retries=1, http_client=client)
    assert result["drop_reason"] == "http_status_400"
    assert len(client.calls) == 1
    assert result.get("drop_detail") == "Bad Request"


@pytest.mark.asyncio
async def test_classify_pdp_retries_on_parse_drop(monkeypatch):
    """Gemini occasionally returns prose-wrapped output even with
    structured output enabled. The O-6 canonical dry-runs showed
    these drops are NON-overlapping across runs (Sennheiser drops
    once but classifies fine the next call), so a single retry
    converts most of these into successes."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    # First call: no balanced JSON block in the response text
    no_block = _MockResponse(
        status_code=200,
        json_payload={
            "candidates": [{"content": {"parts": [{"text": "Sure! Here's my analysis without JSON."}]}}]
        },
    )
    # Retry succeeds
    classification = {"use_case_tags": ["daily"], "lifestyle_tags": [], "confidence": 0.9}
    success = _MockResponse(
        status_code=200,
        json_payload={
            "candidates": [{"content": {"parts": [{"text": json.dumps(classification)}]}}]
        },
    )
    client = _MockHttpClient(responses=[no_block, success])
    result = await classify_pdp({"title": "x"}, max_retries=1, http_client=client)
    assert result["drop_reason"] is None  # retry recovered
    assert result["use_case_tags"] == ["daily"]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_classify_pdp_parse_drop_exhausts_retries(monkeypatch):
    """If both attempts return prose without JSON, the final
    drop_reason should reflect the parse failure (not 'exhausted_retries')
    so the runner can attribute the drop reason correctly."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake_key")
    no_block = _MockResponse(
        status_code=200,
        json_payload={
            "candidates": [{"content": {"parts": [{"text": "Just chatting, no JSON."}]}}]
        },
    )
    client = _MockHttpClient(responses=[no_block, no_block])
    result = await classify_pdp({"title": "x"}, max_retries=1, http_client=client)
    assert result["drop_reason"] == "gemini_json_no_balanced_block"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_classify_pdp_gate_follows_credentials_not_raw_key(monkeypatch):
    """The gate must follow credentials_available(), not raw key presence: with a
    key present but credentials unavailable, classify_pdp still returns no_api_key.
    The old raw-key gate would have proceeded to a doomed call."""
    monkeypatch.setenv("GEMINI_API_KEY", "present-but-unusable")
    from services import vertex_gemini

    monkeypatch.setattr(vertex_gemini, "credentials_available", lambda *a, **k: False)
    result = await classify_pdp({"title": "x"})
    assert result["drop_reason"] == "no_api_key"


@pytest.mark.asyncio
async def test_classify_pdp_proceeds_on_vertex_without_api_key(monkeypatch):
    """The switch: on Vertex the credential is ADC, so a missing GEMINI_API_KEY
    must NOT short-circuit as no_api_key. Gating on credentials_available() lets a
    keyless Vertex call proceed to the model."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("PIVOTA_GEMINI_API_KEY", raising=False)
    from services import vertex_gemini

    monkeypatch.setattr(vertex_gemini, "credentials_available", lambda *a, **k: True)

    async def _fake_auth_headers(api_key=None):
        return {"content-type": "application/json"}

    monkeypatch.setattr(vertex_gemini, "auth_headers", _fake_auth_headers)

    classification = {
        "demographic": "women",
        "use_case_tags": ["daily"],
        "lifestyle_tags": ["vegan"],
        "category_path": "beauty/skincare/treat/serum",
        "confidence": 0.9,
        "reasoning": "ok",
    }
    ok = _MockResponse(
        status_code=200,
        json_payload={
            "candidates": [{"content": {"parts": [{"text": json.dumps(classification)}]}}]
        },
    )
    client = _MockHttpClient(responses=[ok])
    result = await classify_pdp({"title": "Vegan Daily Cream"}, http_client=client)
    assert result["drop_reason"] != "no_api_key"
    assert len(client.calls) == 1  # reached the model despite no key
