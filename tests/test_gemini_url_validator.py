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


# ---------------------------------------------------------------------------
# Record-level currency. #2180 made ingestion refuse a pdp with no currency
# (`currency_unproven`) where it used to default USD. This lane never put one on
# the pdp -- only on offers -- so after #2180 every audit_candidate onboard-queue
# item failed. The pdp currency must be PROVEN from the offers, never defaulted.
# ---------------------------------------------------------------------------

from services.catalog_enrichment_agent.gemini_url_validator import (  # noqa: E402
    _observed_currency,
    _record_currency,
)
from services.catalog_enrichment_agent.ingestion import ingest_validated_record  # noqa: E402


@pytest.mark.parametrize("value,expected", [
    ("usd", "USD"), (" SGD ", "SGD"),
    (None, None), ("", None), ("US$", None), ("$", None), (5, None), ("dollars", None),
])
def test_observed_currency_is_the_reported_code_or_none_never_a_default(value, expected):
    assert _observed_currency(value) == expected


@pytest.mark.parametrize("offers,expected", [
    ([{"price": 10.0, "currency": "SGD"}, {"price": 12.0, "currency": "SGD"}], "SGD"),
    # An unpriced offer states no money, so its missing code does not veto.
    ([{"price": 10.0, "currency": "USD"}, {"price": None, "currency": None}], "USD"),
    ([{"price": None, "currency": "USD"}], "USD"),
    # Disagreement is not resolved by picking one.
    ([{"price": 10.0, "currency": "USD"}, {"price": 14.0, "currency": "SGD"}], None),
    ([{"price": 10.0, "currency": "USD"}, {"price": None, "currency": "SGD"}], None),
    # A priced offer without a code makes the whole record unproven.
    ([{"price": 10.0, "currency": "USD"}, {"price": 9.0, "currency": None}], None),
    ([{"price": None, "currency": None}], None),
    ([], None),
])
def test_record_currency_is_proven_only_by_agreeing_priced_offers(offers, expected):
    assert _record_currency(offers) == expected


_CANDIDATE = {
    "brand": "MAC",
    "product_name": "Ruby Woo",
    "category_path": "beauty/makeup/lip/lipstick",
    "attribute_summary": "matte red",
    "expected_url_domains": ["maccosmetics.com"],
}


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, offers):
        import json as _json
        self._payload = {"candidates": [{"content": {"parts": [
            {"text": _json.dumps({"offers": offers})}]}}]}

    def json(self):
        return self._payload


def _live_validate(monkeypatch, offers):
    """Drive the REAL (credentialed) branch of validate_candidate with a canned
    model reply -- the mock branch never reaches the offer-building loop."""
    from services.catalog_enrichment_agent import gemini_url_validator as v

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResponse(offers)

    async def _headers(_key):
        return {}

    monkeypatch.setattr(v.vertex_gemini, "credentials_available", lambda _k: True)
    monkeypatch.setattr(v.vertex_gemini, "generate_content_url", lambda *a, **k: "https://gemini.invalid")
    monkeypatch.setattr(v.vertex_gemini, "auth_headers", _headers)
    monkeypatch.setattr(v.httpx, "AsyncClient", _Client)
    return asyncio.run(v.validate_candidate(dict(_CANDIDATE), api_key="k"))


_OFFER = {
    "merchant_inferred": "MAC",
    "domain": "maccosmetics.com",
    "canonical_url": "https://www.maccosmetics.com/product/ruby-woo",
    "price": 24.0,
    "in_stock": True,
    "confidence": 0.9,
}


def test_a_validated_record_with_a_reported_currency_plans_instead_of_raising(monkeypatch):
    """THE regression. Before the fix the pdp carried no currency, so this raised
    `currency_unproven` and the onboard-queue item was marked failed."""
    record = _live_validate(monkeypatch, [{**_OFFER, "currency": "usd"}])
    assert record["pdp"]["currency"] == "USD"
    plan = ingest_validated_record(record)
    assert plan and plan.get("pdp"), plan
    assert {o["currency"] for o in plan["offers"]} == {"USD"}


def test_a_non_usd_page_is_ingested_in_its_own_currency(monkeypatch):
    record = _live_validate(monkeypatch, [{**_OFFER, "currency": "SGD"}])
    plan = ingest_validated_record(record)
    assert {o["currency"] for o in plan["offers"]} == {"SGD"}


def test_a_model_reply_without_currency_is_refused_not_stamped_usd(monkeypatch):
    """The counterpart: the fix must not buy the lane back by re-introducing the
    USD default #2180 removed. An unreported code stays unproven, loudly."""
    record = _live_validate(monkeypatch, [dict(_OFFER)])
    assert record["offers"][0]["currency"] is None
    assert record["pdp"]["currency"] is None
    with pytest.raises(ValueError, match="currency_unproven"):
        ingest_validated_record(record)


def test_the_prompt_no_longer_dictates_usd():
    """The reply schema used to contain the literal `"currency": "USD"`, so the
    model's "observed" currency was the template's. A code copied from that
    would prove nothing."""
    from services.catalog_enrichment_agent.gemini_url_validator import _build_prompt

    assert '"currency": "USD"' not in _build_prompt(dict(_CANDIDATE))
