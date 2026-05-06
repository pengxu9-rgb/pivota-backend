"""Pure-function tests for services/catalog_enrichment_agent/gemini_url_validator.py.

We can't easily test the live Gemini call, but the response parser and
the offline mock shape are deterministic and worth pinning."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.catalog_enrichment_agent.gemini_url_validator import (  # noqa: E402
    _mock_validation,
    _parse_gemini_response,
    _slugify,
    validate_candidate,
)


def test_slugify_basic():
    assert _slugify("MAC Ruby Woo Lipstick") == "mac-ruby-woo-lipstick"
    assert _slugify("Charlotte Tilbury — Pillow Talk!") == "charlotte-tilbury-pillow-talk"
    assert _slugify(None) == ""
    assert _slugify("") == ""


def test_parse_gemini_response_plain_json():
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": '{"offers":[{"merchant_inferred":"MAC","canonical_url":"https://x.com/y"}]}'}]}}
        ]
    }
    result = _parse_gemini_response(payload)
    assert result["offers"][0]["canonical_url"] == "https://x.com/y"


def test_parse_gemini_response_strips_json_fence():
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": "```json\n{\"offers\":[{\"canonical_url\":\"https://a.com/b\"}]}\n```"}]}}
        ]
    }
    result = _parse_gemini_response(payload)
    assert len(result["offers"]) == 1


def test_parse_gemini_response_finds_embedded_json():
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": "Here's the result:\n\n{\"offers\": []}\n\nHope that helps."}]}}
        ]
    }
    result = _parse_gemini_response(payload)
    assert result == {"offers": []}


def test_parse_gemini_response_empty_when_unparseable():
    payload = {
        "candidates": [{"content": {"parts": [{"text": "no json here at all"}]}}]
    }
    assert _parse_gemini_response(payload) == {"offers": []}


def test_parse_gemini_response_no_candidates():
    assert _parse_gemini_response({}) == {"offers": []}
    assert _parse_gemini_response({"candidates": []}) == {"offers": []}


def test_parse_gemini_response_offers_must_be_list():
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": '{"offers": "not a list"}'}]}}
        ]
    }
    result = _parse_gemini_response(payload)
    assert result["offers"] == []


def test_mock_validation_picks_first_domain():
    candidate = {
        "brand": "MAC",
        "product_name": "Ruby Woo Matte Lipstick",
        "expected_url_domains": ["maccosmetics.com", "sephora.com"],
    }
    out = _mock_validation(candidate)
    assert len(out["offers"]) == 1
    offer = out["offers"][0]
    assert offer["domain"] == "maccosmetics.com"
    assert offer["canonical_url"].startswith("https://maccosmetics.com/products/")
    assert "mac-ruby-woo-matte-lipstick" in offer["canonical_url"]
    assert offer["notes"] == "mock_no_gemini_key"
    assert offer["confidence"] < 0.5  # mock confidence is intentionally low


def test_mock_validation_empty_when_no_domains():
    out = _mock_validation({"brand": "MAC", "product_name": "X", "expected_url_domains": []})
    assert out["offers"] == []


def test_mock_validation_empty_when_missing_fields():
    out = _mock_validation({"expected_url_domains": ["a.com"]})
    assert out["offers"] == []


def test_validate_candidate_offline_returns_pdp_and_offers():
    """When no API key is set, validate_candidate uses _mock_validation and
    still produces a valid {pdp, offers} envelope."""
    candidate = {
        "brand": "MAC",
        "product_name": "Ruby Woo",
        "category_path": "beauty/makeup/lip/lipstick",
        "attribute_summary": "matte red",
        "expected_url_domains": ["maccosmetics.com"],
    }
    # Force key=None to bypass any env that might leak in.
    result = asyncio.run(validate_candidate(candidate, api_key=""))
    assert result["pdp"]["brand"] == "MAC"
    assert result["pdp"]["product_name"] == "Ruby Woo"
    assert result["pdp"]["category_path"] == "beauty/makeup/lip/lipstick"
    assert len(result["offers"]) == 1
    assert result["offers"][0]["validated_at"]
