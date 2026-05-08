"""Pure-function tests for services/catalog_enrichment_agent/gemini_url_validator.py.

We can't easily test the live Gemini call, but the response parser and
the offline mock shape are deterministic and worth pinning."""

from __future__ import annotations

import asyncio
import json
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
import services.catalog_enrichment_agent.gemini_url_validator as validator_mod  # noqa: E402


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
    assert result["offers"] == []
    assert result["validation_drop_reason"] == "gemini_offers_empty"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "gemini_no_candidates"),
        ({"candidates": []}, "gemini_no_candidates"),
        ({"candidates": [{"content": {"parts": [{"text": ""}]}}]}, "gemini_no_text_parts"),
        (
            {"candidates": [{"content": {"parts": [{"text": "no json here at all"}]}}]},
            "gemini_json_no_balanced_block",
        ),
        (
            {"candidates": [{"content": {"parts": [{"text": '{"offers": [}'}]}}]},
            "gemini_json_decode_failed",
        ),
        (
            {"candidates": [{"content": {"parts": [{"text": '["not", "dict"]'}]}}]},
            "gemini_response_not_dict",
        ),
        (
            {"candidates": [{"content": {"parts": [{"text": '{"offers": "not a list"}'}]}}]},
            "gemini_offers_not_list",
        ),
        (
            {"candidates": [{"content": {"parts": [{"text": '{"offers": []}'}]}}]},
            "gemini_offers_empty",
        ),
    ],
)
def test_parse_gemini_response_drop_reasons(payload, reason):
    result = _parse_gemini_response(payload)
    assert result["offers"] == []
    assert result["validation_drop_reason"] == reason
    assert "validation_drop_detail" in result


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
    assert out["validation_drop_reason"] == "missing_input"


def test_mock_validation_empty_when_missing_fields():
    out = _mock_validation({"expected_url_domains": ["a.com"]})
    assert out["offers"] == []
    assert out["validation_drop_reason"] == "missing_input"


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


class _FakeResponse:
    def __init__(self, status_code, *, text="", payload=None, json_error=False):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise json.JSONDecodeError("bad json", self.text or "", 0)
        return self._payload


def _patch_async_client(monkeypatch, responses):
    calls = []

    class _FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": self.timeout})
            return responses.pop(0)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(validator_mod.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(validator_mod.asyncio, "sleep", _no_sleep)
    return calls


def _candidate():
    return {
        "brand": "Sony",
        "product_name": "WH-1000XM5",
        "category_path": "electronics/audio/headphones_noise_cancelling",
        "attribute_summary": "wireless ANC",
        "expected_url_domains": ["sony.com"],
    }


def test_validate_candidate_retries_once_on_429(monkeypatch):
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "offers": [
                                        {
                                            "merchant_inferred": "Sony",
                                            "domain": "sony.com",
                                            "canonical_url": "https://sony.com/p/wh-1000xm5",
                                            "price": 399.99,
                                            "currency": "USD",
                                            "in_stock": True,
                                            "confidence": 0.9,
                                        }
                                    ]
                                }
                            )
                        }
                    ]
                }
            }
        ]
    }
    calls = _patch_async_client(
        monkeypatch,
        [
            _FakeResponse(429, text="rate limited"),
            _FakeResponse(200, payload=payload),
        ],
    )

    result = asyncio.run(validate_candidate(_candidate(), api_key="test", max_retries=1))

    assert len(calls) == 2
    assert len(result["offers"]) == 1
    assert result["validation_attempts"] == 2
    assert result["validation_retried"] is True


def test_validate_candidate_does_not_retry_404(monkeypatch):
    calls = _patch_async_client(
        monkeypatch,
        [_FakeResponse(404, text="not found")],
    )

    result = asyncio.run(validate_candidate(_candidate(), api_key="test", max_retries=1))

    assert len(calls) == 1
    assert result["offers"] == []
    assert result["validation_drop_reason"] == "http_status_404"
    assert result["validation_attempts"] == 1
    assert result["validation_retried"] is False


def test_validate_candidate_records_non_json_body(monkeypatch):
    calls = _patch_async_client(
        monkeypatch,
        [_FakeResponse(200, text="<html>overloaded</html>", json_error=True)],
    )

    result = asyncio.run(validate_candidate(_candidate(), api_key="test", max_retries=0))

    assert len(calls) == 1
    assert result["offers"] == []
    assert result["validation_drop_reason"] == "http_body_not_json"
    assert result["validation_drop_detail"] == "<html>overloaded</html>"
