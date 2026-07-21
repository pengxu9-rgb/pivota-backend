"""Phase 5.5 — frontend_agent_cite verifier tests.

Validates the JSON-LD extraction + brand-match logic.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest


# =====================================================================
# Fixtures
# =====================================================================


def _sample_product() -> Dict[str, Any]:
    return {
        "merchant_id": "merch-1",
        "product_key": "shopify::sp-1",
        "title": "Wellness Greens Gummies",
        "brand": "Test Brand",
        "pivota_signature_id": "sig_abc",
        "pivota_canonical_url": (
            "https://agent.pivota.cc/products/sig_abc"
        ),
    }


def _ctx():
    from services.verification_run_worker import VerifierContext
    return VerifierContext(
        verify_id="v-1",
        audit_run_id="audit-1",
        verifier_id="frontend_agent_cite",
        product_key="shopify::sp-1",
    )


class _FakeResponse:
    def __init__(self, *, status_code: int, text: str = "",
                 content_type: str = "text/html"):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


def _patch_product_loader(monkeypatch, product=None):
    async def fake_load(*, audit_run_id, product_key):
        return product

    from services.verifiers import frontend_agent_cite
    monkeypatch.setattr(
        frontend_agent_cite, "load_product_context", fake_load,
    )


def _patch_httpx(monkeypatch, *, response=None, raise_exc=None):
    """Replace httpx.AsyncClient with a recorder fake. Returns
    {gets: [(url, headers), ...]} for assertions."""
    captured: Dict[str, Any] = {"gets": []}

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            captured["gets"].append((url, headers or {}))
            if raise_exc is not None:
                raise raise_exc
            return response

    from services.verifiers import frontend_agent_cite
    monkeypatch.setattr(
        frontend_agent_cite.httpx, "AsyncClient", _Client,
    )
    return captured


# =====================================================================
# JSON-LD helpers (pure)
# =====================================================================


def test_parse_jsonld_blocks_extracts_multiple():
    from services.verifiers.frontend_agent_cite import (
        _parse_jsonld_blocks,
    )
    html = (
        '<html><script type="application/ld+json">'
        '{"@type":"Product","name":"X"}</script>'
        '<script type="application/ld+json">'
        '{"@type":"Organization","name":"Y"}</script>'
        "</html>"
    )
    out = _parse_jsonld_blocks(html)
    assert len(out) == 2
    assert out[0]["@type"] == "Product"
    assert out[1]["@type"] == "Organization"


def test_parse_jsonld_blocks_skips_unparseable():
    from services.verifiers.frontend_agent_cite import (
        _parse_jsonld_blocks,
    )
    html = (
        '<script type="application/ld+json">{ not valid json }</script>'
        '<script type="application/ld+json">{"@type":"Product"}</script>'
    )
    out = _parse_jsonld_blocks(html)
    assert len(out) == 1
    assert out[0]["@type"] == "Product"


def test_find_product_in_jsonld_returns_product_typed():
    from services.verifiers.frontend_agent_cite import (
        _find_product_in_jsonld,
    )
    blocks = [
        {"@type": "Organization", "name": "Y"},
        {"@type": "Product", "name": "X", "brand": "TB"},
    ]
    out = _find_product_in_jsonld(blocks)
    assert out["@type"] == "Product"


def test_find_product_handles_at_graph_nesting():
    """JSON-LD @graph wraps multiple typed objects; the helper
    must recurse."""
    from services.verifiers.frontend_agent_cite import (
        _find_product_in_jsonld,
    )
    blocks = [{
        "@graph": [
            {"@type": "WebPage"},
            {"@type": "Product", "name": "X"},
        ],
    }]
    out = _find_product_in_jsonld(blocks)
    assert out["@type"] == "Product"


def test_extract_brand_handles_string_and_object_shape():
    """JSON-LD brand can be either a plain string or an
    Organization-nested object. Both must extract."""
    from services.verifiers.frontend_agent_cite import (
        _extract_brand_from_jsonld,
    )
    assert _extract_brand_from_jsonld({"brand": "Test Brand"}) == (
        "Test Brand"
    )
    assert _extract_brand_from_jsonld({
        "brand": {"@type": "Brand", "name": "Test Brand"},
    }) == "Test Brand"
    assert _extract_brand_from_jsonld({}) is None
    assert _extract_brand_from_jsonld({"brand": "  "}) is None


# =====================================================================
# run_frontend_agent_cite — happy path
# =====================================================================


@pytest.mark.asyncio
async def test_succeeded_when_brand_matches_in_html_jsonld(monkeypatch):
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    html = (
        "<html><head>"
        '<script type="application/ld+json">'
        + json.dumps({
            "@type": "Product",
            "name": "Wellness Greens Gummies",
            "brand": "Test Brand",
        })
        + "</script></head></html>"
    )
    captured = _patch_httpx(
        monkeypatch,
        response=_FakeResponse(status_code=200, text=html),
    )
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "succeeded"
    assert result.evidence_jsonb["has_jsonld_structured_data"] is True
    assert result.evidence_jsonb["brand_match"] is True
    # Confirm we sent the agent-style headers
    url, sent_headers = captured["gets"][0]
    assert "PivotaAgentCiteCheck" in sent_headers["User-Agent"]
    assert "application/ld+json" in sent_headers["Accept"]


@pytest.mark.asyncio
async def test_succeeded_when_response_is_pure_json_ld(monkeypatch):
    """Content-type application/ld+json → parse the whole body
    as one JSON-LD object."""
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    body = json.dumps({
        "@type": "Product", "brand": "Test Brand",
    })
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(
            status_code=200, text=body,
            content_type="application/ld+json",
        ),
    )
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "succeeded"


# =====================================================================
# run_frontend_agent_cite — failure modes
# =====================================================================


@pytest.mark.asyncio
async def test_failed_when_brand_mismatches(monkeypatch):
    """JSON-LD present but brand doesn't match — retryable."""
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    html = (
        '<script type="application/ld+json">'
        + json.dumps({
            "@type": "Product", "brand": "Different Brand",
        })
        + "</script>"
    )
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(status_code=200, text=html),
    )
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "failed"
    assert "brand_mismatch" in result.error_message
    assert result.evidence_jsonb["brand_match"] is False


@pytest.mark.asyncio
async def test_failed_when_no_jsonld_blocks(monkeypatch):
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(
            status_code=200, text="<html>nothing structured</html>",
        ),
    )
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "failed"
    assert "no_jsonld_blocks" in result.error_message


@pytest.mark.asyncio
async def test_failed_when_no_product_typed_jsonld(monkeypatch):
    """JSON-LD present but no @type Product — incomplete schema."""
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    html = (
        '<script type="application/ld+json">'
        '{"@type":"Organization","name":"Test"}</script>'
    )
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(status_code=200, text=html),
    )
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "failed"
    assert "no_product_typed_jsonld" in result.error_message


@pytest.mark.asyncio
async def test_blocked_on_404(monkeypatch):
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(status_code=404),
    )
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "blocked"
    assert "pdp_404" in result.error_message


@pytest.mark.asyncio
async def test_failed_on_5xx(monkeypatch):
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_httpx(
        monkeypatch,
        response=_FakeResponse(status_code=503),
    )
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_blocked_when_no_product_context(monkeypatch):
    from services.verifiers import frontend_agent_cite as v
    _patch_product_loader(monkeypatch, product=None)
    result = await v.run_frontend_agent_cite(_ctx())
    assert result.status == "blocked"
