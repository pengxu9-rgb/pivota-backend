"""Phase 5.3 — pdp_renders / pdp_in_sitemap / pivota_internal_retrieval
verifier tests.

Each verifier is exercised end-to-end via monkey-patched httpx +
product_context loader. Tests cover the canonical success path +
the distinct failure modes (failed=retryable vs blocked=terminal).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# =====================================================================
# Test helpers
# =====================================================================


def _sample_product() -> Dict[str, Any]:
    return {
        "merchant_id": "merch-1",
        "product_key": "shopify::sp-1",
        "title": "Wellness Greens Gummies",
        "brand": "Test Brand",
        "pivota_signature_id": "sig_abc123",
        "pivota_canonical_url": (
            "https://agent.pivota.cc/products/sig_abc123"
        ),
        "canonical_url": "https://testbrand.com/products/x",
    }


def _ctx(
    product_key: Optional[str] = "shopify::sp-1",
    audit_run_id: str = "audit-1",
):
    from services.verification_run_worker import VerifierContext
    return VerifierContext(
        verify_id="v-1",
        audit_run_id=audit_run_id,
        verifier_id="pdp_renders",
        product_key=product_key,
    )


class _FakeResponse:
    """Minimal httpx.Response stand-in."""
    def __init__(self, *, status_code: int, text: str = "",
                 json_body: Optional[Dict[str, Any]] = None):
        self.status_code = status_code
        self.text = text
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("not JSON")
        return self._json


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in. Records GETs + returns the
    pre-configured response. Tests inject this via monkey-patching
    httpx.AsyncClient on the verifier module."""
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        self.gets.append(url)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.response


def _patch_product_loader(monkeypatch, product=None):
    """Replace load_product_context across all 3 verifiers."""
    async def fake_load(*, audit_run_id, product_key):
        return product

    from services.verifiers import (
        pdp_renders, pdp_in_sitemap, pivota_internal_retrieval,
    )
    monkeypatch.setattr(
        pdp_renders, "load_product_context", fake_load,
    )
    monkeypatch.setattr(
        pdp_in_sitemap, "load_product_context", fake_load,
    )
    monkeypatch.setattr(
        pivota_internal_retrieval, "load_product_context", fake_load,
    )


def _patch_httpx_for(monkeypatch, module, *, response=None, raise_exc=None):
    """Replace httpx.AsyncClient in a verifier module with a fake.
    Returns the fake so tests can assert what URLs were hit.

    NOTE: verifier modules do a plain `import httpx`, so `module.httpx`
    is the global httpx module — the patch must go through monkeypatch
    or it leaks into every later test in the session."""
    fake = _FakeAsyncClient(response=response, raise_exc=raise_exc)

    class _Factory:
        def __init__(self_inner, **kwargs):
            pass

        async def __aenter__(self_inner):
            return fake

        async def __aexit__(self_inner, *args):
            return False

    monkeypatch.setattr(module.httpx, "AsyncClient", _Factory)
    return fake


# =====================================================================
# Registration sanity
# =====================================================================


def test_all_three_verifiers_register_at_import():
    """The package __init__ side-effect imports all 3 modules.
    By the time tests import the package, the registry has all 3."""
    import services.verifiers  # noqa: F401 — side-effect
    from services.verification_run_worker import (
        get_registered_verifier_ids,
    )
    registered = get_registered_verifier_ids()
    assert "pdp_renders" in registered
    assert "pdp_in_sitemap" in registered
    assert "pivota_internal_retrieval" in registered


# =====================================================================
# pdp_renders
# =====================================================================


@pytest.mark.asyncio
async def test_pdp_renders_succeeded_when_status_200_with_markers(
    monkeypatch,
):
    """200 + JSON-LD + product name in body → succeeded."""
    from services.verifiers import pdp_renders
    _patch_product_loader(monkeypatch, product=_sample_product())
    html = (
        "<html><head>"
        '<script type="application/ld+json">{"@type":"Product"}</script>'
        "</head><body>Wellness Greens Gummies by Test Brand</body></html>"
    )
    fake = _patch_httpx_for(
        monkeypatch,
        pdp_renders,
        response=_FakeResponse(status_code=200, text=html),
    )
    result = await pdp_renders.run_pdp_renders(_ctx())
    assert result.status == "succeeded"
    assert result.evidence_jsonb["has_schema_org_jsonld"] is True
    assert result.evidence_jsonb["has_product_name_in_body"] is True
    assert fake.gets == [
        "https://agent.pivota.cc/products/sig_abc123",
    ]


@pytest.mark.asyncio
async def test_pdp_renders_failed_when_markers_missing(monkeypatch):
    """200 but missing JSON-LD → failed (retryable; markers may
    appear after re-deploy)."""
    from services.verifiers import pdp_renders
    _patch_product_loader(monkeypatch, product=_sample_product())
    html = "<html><body>Wellness Greens Gummies</body></html>"  # no JSON-LD
    _patch_httpx_for(
        monkeypatch,
        pdp_renders,
        response=_FakeResponse(status_code=200, text=html),
    )
    result = await pdp_renders.run_pdp_renders(_ctx())
    assert result.status == "failed"
    assert "jsonld=False" in result.error_message
    assert result.evidence_jsonb["has_schema_org_jsonld"] is False


@pytest.mark.asyncio
async def test_pdp_renders_blocked_on_404(monkeypatch):
    """404 → blocked (NOT retryable — the sig doesn't exist; a
    future audit may produce a different sig)."""
    from services.verifiers import pdp_renders
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_httpx_for(
        monkeypatch,
        pdp_renders,
        response=_FakeResponse(status_code=404, text="not found"),
    )
    result = await pdp_renders.run_pdp_renders(_ctx())
    assert result.status == "blocked"
    assert "pdp_404" in result.error_message


@pytest.mark.asyncio
async def test_pdp_renders_failed_on_5xx(monkeypatch):
    """5xx → failed (retryable transient)."""
    from services.verifiers import pdp_renders
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_httpx_for(
        monkeypatch,
        pdp_renders,
        response=_FakeResponse(status_code=503, text="bad gateway"),
    )
    result = await pdp_renders.run_pdp_renders(_ctx())
    assert result.status == "failed"
    assert "pdp_upstream_5xx_503" in result.error_message


@pytest.mark.asyncio
async def test_pdp_renders_blocked_when_no_product_context(monkeypatch):
    """Missing audit_run / catalog row → blocked. Marking
    blocked (not failed) prevents retrying when the gap is
    structural."""
    from services.verifiers import pdp_renders
    _patch_product_loader(monkeypatch, product=None)
    result = await pdp_renders.run_pdp_renders(_ctx())
    assert result.status == "blocked"
    assert result.evidence_jsonb["resolved_product"] is False


@pytest.mark.asyncio
async def test_pdp_renders_failed_on_timeout(monkeypatch):
    """httpx timeout → failed (retryable)."""
    import httpx
    from services.verifiers import pdp_renders
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_httpx_for(
        monkeypatch,
        pdp_renders,
        raise_exc=httpx.TimeoutException("timed out"),
    )
    result = await pdp_renders.run_pdp_renders(_ctx())
    assert result.status == "failed"
    assert "timeout" in result.error_message


# =====================================================================
# pdp_in_sitemap
# =====================================================================


@pytest.mark.asyncio
async def test_pdp_in_sitemap_succeeded_when_url_present(monkeypatch):
    from services.verifiers import pdp_in_sitemap
    _patch_product_loader(monkeypatch, product=_sample_product())
    sitemap = """<?xml version="1.0"?>
    <urlset>
      <url><loc>https://agent.pivota.cc/products/sig_other</loc></url>
      <url><loc>https://agent.pivota.cc/products/sig_abc123</loc></url>
    </urlset>"""
    _patch_httpx_for(
        monkeypatch,
        pdp_in_sitemap,
        response=_FakeResponse(status_code=200, text=sitemap),
    )
    result = await pdp_in_sitemap.run_pdp_in_sitemap(_ctx())
    assert result.status == "succeeded"
    assert result.evidence_jsonb["found_in_sitemap"] is True
    assert result.evidence_jsonb["sitemap_url_count"] == 2


@pytest.mark.asyncio
async def test_pdp_in_sitemap_failed_when_url_missing(monkeypatch):
    """PDP URL not in sitemap → failed (retryable; sitemap
    regenerates periodically so newly-minted PDPs may appear on
    the next cycle)."""
    from services.verifiers import pdp_in_sitemap
    _patch_product_loader(monkeypatch, product=_sample_product())
    sitemap = """<?xml version="1.0"?>
    <urlset>
      <url><loc>https://agent.pivota.cc/products/sig_other</loc></url>
    </urlset>"""
    _patch_httpx_for(
        monkeypatch,
        pdp_in_sitemap,
        response=_FakeResponse(status_code=200, text=sitemap),
    )
    result = await pdp_in_sitemap.run_pdp_in_sitemap(_ctx())
    assert result.status == "failed"
    assert result.error_message == "pdp_not_in_sitemap"
    assert result.evidence_jsonb["found_in_sitemap"] is False


@pytest.mark.asyncio
async def test_pdp_in_sitemap_failed_on_sitemap_error(monkeypatch):
    """Sitemap 5xx → failed retryable."""
    from services.verifiers import pdp_in_sitemap
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_httpx_for(
        monkeypatch,
        pdp_in_sitemap,
        response=_FakeResponse(status_code=500),
    )
    result = await pdp_in_sitemap.run_pdp_in_sitemap(_ctx())
    assert result.status == "failed"
    assert "sitemap_status_500" in result.error_message


# =====================================================================
# pivota_internal_retrieval
# =====================================================================


def _patch_backend_url(monkeypatch, url: str = "http://test-backend:8000"):
    """P5.8.5: verifier reads from settings.pivota_backend_internal_url.
    Tests need to set it (or monkey-patch _backend_base_url directly)
    so the verifier doesn't return blocked:not_configured."""
    from services.verifiers import pivota_internal_retrieval
    monkeypatch.setattr(
        pivota_internal_retrieval,
        "_backend_base_url",
        lambda: url,
    )


@pytest.mark.asyncio
async def test_internal_retrieval_blocked_when_backend_url_unconfigured(
    monkeypatch,
):
    """P5.8.5: when PIVOTA_BACKEND_INTERNAL_URL is unset (no
    fallback), the verifier returns blocked:not_configured. The
    original P5.3 code defaulted to localhost:8000 which would
    retry-storm against the wrong target in Railway prod."""
    from services.verifiers import pivota_internal_retrieval
    _patch_product_loader(monkeypatch, product=_sample_product())
    # Simulate the env var being unset
    from services.verifiers import pivota_internal_retrieval as v
    monkeypatch.setattr(v, "_backend_base_url", lambda: None)
    result = (
        await v.run_pivota_internal_retrieval(_ctx())
    )
    assert result.status == "blocked"
    assert "not_configured" in result.error_message


@pytest.mark.asyncio
async def test_internal_retrieval_succeeded_on_roundtrip_match(
    monkeypatch,
):
    """200 + product_key + merchant_id match → succeeded."""
    from services.verifiers import pivota_internal_retrieval
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_backend_url(monkeypatch)  # P5.8.5
    body = {
        "product": {
            "product_key": "shopify::sp-1",
            "merchant_id": "merch-1",
            "title": "Wellness Greens Gummies",
        },
        "updated_at": "2026-05-12T10:00:00+00:00",
    }
    _patch_httpx_for(
        monkeypatch,
        pivota_internal_retrieval,
        response=_FakeResponse(
            status_code=200, text="", json_body=body,
        ),
    )
    result = (
        await pivota_internal_retrieval.run_pivota_internal_retrieval(
            _ctx(),
        )
    )
    assert result.status == "succeeded"
    assert result.evidence_jsonb["resolved_product_key"] == "shopify::sp-1"


@pytest.mark.asyncio
async def test_internal_retrieval_blocked_on_404(monkeypatch):
    """404 on resolver → blocked (NOT retryable; the sig is lost
    from catalog, which is a structural gap)."""
    from services.verifiers import pivota_internal_retrieval
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_backend_url(monkeypatch)  # P5.8.5
    _patch_httpx_for(
        monkeypatch,
        pivota_internal_retrieval,
        response=_FakeResponse(status_code=404),
    )
    result = (
        await pivota_internal_retrieval.run_pivota_internal_retrieval(
            _ctx(),
        )
    )
    assert result.status == "blocked"
    assert "backend_404" in result.error_message


@pytest.mark.asyncio
async def test_internal_retrieval_blocked_on_roundtrip_mismatch(
    monkeypatch,
):
    """200 but the resolved product_key differs from the audit's
    → blocked. The sig now points at a different product (catalog
    re-key, sig collision). Not retryable."""
    from services.verifiers import pivota_internal_retrieval
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_backend_url(monkeypatch)  # P5.8.5
    body = {
        "product": {
            "product_key": "shopify::sp-DIFFERENT",
            "merchant_id": "merch-1",
        },
    }
    _patch_httpx_for(
        monkeypatch,
        pivota_internal_retrieval,
        response=_FakeResponse(
            status_code=200, text="", json_body=body,
        ),
    )
    result = (
        await pivota_internal_retrieval.run_pivota_internal_retrieval(
            _ctx(),
        )
    )
    assert result.status == "blocked"
    assert "sig_collision_or_remap" in result.error_message


@pytest.mark.asyncio
async def test_internal_retrieval_failed_on_5xx(monkeypatch):
    """5xx → failed (retryable)."""
    from services.verifiers import pivota_internal_retrieval
    _patch_product_loader(monkeypatch, product=_sample_product())
    _patch_backend_url(monkeypatch)  # P5.8.5
    _patch_httpx_for(
        monkeypatch,
        pivota_internal_retrieval,
        response=_FakeResponse(status_code=503),
    )
    result = (
        await pivota_internal_retrieval.run_pivota_internal_retrieval(
            _ctx(),
        )
    )
    assert result.status == "failed"
    assert "backend_5xx_503" in result.error_message
