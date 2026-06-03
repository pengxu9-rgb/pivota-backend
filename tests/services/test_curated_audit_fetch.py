"""Phase A unit tests: per-URL curated fetch + wedge honesty helpers.

Covers the pure/IO-isolated pieces behind the merchant-curated wedge:
  - services.bd_cold_start_service.fetch_curated_audit_product (Shopify .json
    first, then generic JSON-LD/OpenGraph) + _walk_jsonld_for_product
  - routes.merchant_audit_routes._strip_unverified_hedge (honesty post-process)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import routes.merchant_audit_routes as mar
import services.bd_cold_start_service as bdcs


# --------------------------------------------------------------------------
# fetch_curated_audit_product
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_curated_shopify_native_first(monkeypatch):
    async def fake_native(url):
        return {
            "title": "[Bundle] Collagen Glow (Marine Collagen), 30 sticks x 2box",
            "vendor": "BB Lab",
            "product_type": "Supplements",
        }

    async def fake_pdp(url):  # must NOT be called when native wins
        raise AssertionError("generic PDP fetch should not run for Shopify")

    monkeypatch.setattr(bdcs, "_fetch_shopify_native", fake_native)
    monkeypatch.setattr(bdcs, "_fetch_pdp_metadata", fake_pdp)

    product, reason = await bdcs.fetch_curated_audit_product(
        "https://bblab.shop/products/collagen-glow"
    )
    assert reason is None
    assert product == {
        "title": "Collagen Glow",
        "raw_title": "[Bundle] Collagen Glow (Marine Collagen), 30 sticks x 2box",
        "pdp_url": "https://bblab.shop/products/collagen-glow",
        "vendor": "BB Lab",
        "product_type": "Supplements",
    }


@pytest.mark.asyncio
async def test_fetch_curated_generic_fallback(monkeypatch):
    async def no_native(url):
        return None

    async def fake_pdp(url):
        return {
            "title": "[Set] Wix Serum, 2 pack",
            "vendor": "Acme",
            "product_type": None,
        }

    monkeypatch.setattr(bdcs, "_fetch_shopify_native", no_native)
    monkeypatch.setattr(bdcs, "_fetch_pdp_metadata", fake_pdp)

    product, reason = await bdcs.fetch_curated_audit_product(
        "https://acme.com/shop/serum"
    )
    assert reason is None
    assert product["title"] == "Wix Serum"
    assert product["raw_title"] == "[Set] Wix Serum, 2 pack"
    assert product["vendor"] == "Acme"
    assert product["pdp_url"] == "https://acme.com/shop/serum"


@pytest.mark.asyncio
async def test_fetch_curated_unresolved_returns_reason(monkeypatch):
    async def none(url):
        return None

    monkeypatch.setattr(bdcs, "_fetch_shopify_native", none)
    monkeypatch.setattr(bdcs, "_fetch_pdp_metadata", none)

    product, reason = await bdcs.fetch_curated_audit_product(
        "https://acme.com/shop/serum"
    )
    assert product is None
    assert reason and "couldn't read a product" in reason


@pytest.mark.asyncio
async def test_fetch_curated_rejects_non_http():
    product, reason = await bdcs.fetch_curated_audit_product("ftp://x/y")
    assert product is None
    assert "valid URL" in reason

    product, reason = await bdcs.fetch_curated_audit_product("   ")
    assert product is None
    assert reason == "empty URL"


def test_walk_jsonld_for_product_handles_graph_and_lists():
    # @graph wrapper
    node = {"@context": "x", "@graph": [
        {"@type": "BreadcrumbList"},
        {"@type": "Product", "name": "Graphed"},
    ]}
    assert bdcs._walk_jsonld_for_product(node)["name"] == "Graphed"
    # bare list
    assert bdcs._walk_jsonld_for_product(
        [{"@type": "WebPage"}, {"@type": ["Thing", "Product"], "name": "L"}]
    )["name"] == "L"
    # no product
    assert bdcs._walk_jsonld_for_product({"@type": "Organization"}) is None


# --------------------------------------------------------------------------
# _strip_unverified_hedge — every known variant loses the hedge, keeps prose
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,must_keep,must_drop",
    [
        # V1: hedge sentence followed by actionable "Possible causes…"
        (
            "None of 3 queries cited your url. Gemini grounded its answers in "
            "third-party sources including iherb.com. We did not verify whether "
            "those sources mention your brand or products. Possible causes are "
            "covered by the action items below.",
            ["iherb.com", "Possible causes are covered by the action items below."],
            ["did not verify"],
        ),
        # V2: trailing standalone hedge sentence
        (
            "Cited in 1 of 3 queries; the other 2 grounded in third-party "
            "sources including iherb.com. We did not verify whether those "
            "sources mention your brand or products.",
            ["iherb.com"],
            ["did not verify"],
        ),
        # V3: inline "...sources we did not verify."
        (
            "Other queries grounded their answers in third-party sources we "
            "did not verify.",
            ["third-party sources."],
            ["did not verify"],
        ),
        # V4: semicolon connector + "the brand"
        (
            "Buyer-intent queries grounded in retailers rather than the "
            "merchant's own URL; we did not verify whether those sources "
            "mention the brand.",
            ["merchant's own URL."],
            ["did not verify"],
        ),
        # V5: em-dash continuation + "your brand"
        (
            "The other 2 grounded answers in third-party sources including "
            "iherb.com — we did not verify whether those sources mention your "
            "brand.",
            ["including iherb.com."],
            ["did not verify"],
        ),
    ],
)
def test_strip_unverified_hedge(text, must_keep, must_drop):
    out = mar._strip_unverified_hedge(text)
    for frag in must_drop:
        assert frag not in out, f"{frag!r} should be stripped from: {out!r}"
    for frag in must_keep:
        assert frag in out, f"{frag!r} should remain in: {out!r}"
    assert "  " not in out  # whitespace normalized
    assert ".." not in out  # no doubled periods left behind


def test_strip_hedge_noop_when_absent():
    clean = "AI agents cite your URL in 3 of 3 queries. Strong."
    assert mar._strip_unverified_hedge(clean) == clean


def test_scrub_report_in_place_removes_industry_context_everywhere():
    report = {
        "industry_context": {"market_size_billions_usd": 6500},
        "per_product": [
            {"industry_context": {"blurb": "x"},
             "explanation": "Grounded in sources we did not verify."},
        ],
    }
    mar._scrub_wedge_report_in_place(report)
    assert "industry_context" not in report
    assert "industry_context" not in report["per_product"][0]
    assert "did not verify" not in report["per_product"][0]["explanation"]
