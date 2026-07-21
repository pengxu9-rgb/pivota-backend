"""
Unit tests for the Pivota canonical PDP signature + URL helpers
(services.catalog_sync_service.make_pivota_signature_id /
pivota_canonical_pdp_url / make_pivota_canonical_fields).
"""

from __future__ import annotations

import pytest


def test_signature_is_deterministic():
    """Same identity tuple → same sig forever. This is the contract
    that lets re-syncs be idempotent + lets the sig appear in
    sitemaps without churn."""
    from services.catalog_sync_service import make_pivota_signature_id
    sig1 = make_pivota_signature_id("merch_a", "shopify", "12345")
    sig2 = make_pivota_signature_id("merch_a", "shopify", "12345")
    assert sig1 == sig2
    assert sig1.startswith("sig_")
    assert len(sig1) == len("sig_") + 32  # 32-hex digest


def test_signature_separates_merchants():
    """Same source_product_id under different merchants must NOT
    collide. This is the cross-tenant safety property."""
    from services.catalog_sync_service import make_pivota_signature_id
    sig_a = make_pivota_signature_id("merch_a", "shopify", "12345")
    sig_b = make_pivota_signature_id("merch_b", "shopify", "12345")
    assert sig_a != sig_b


def test_signature_separates_platforms():
    """Same source_product_id across platforms (e.g. shopify vs wix
    using the same numeric ID) must NOT collide."""
    from services.catalog_sync_service import make_pivota_signature_id
    sig_shopify = make_pivota_signature_id("merch_a", "shopify", "12345")
    sig_wix = make_pivota_signature_id("merch_a", "wix", "12345")
    assert sig_shopify != sig_wix


def test_signature_rejects_empty_inputs():
    """Empty/None for any input should raise — sig minted from
    incomplete identity would silently break uniqueness."""
    from services.catalog_sync_service import make_pivota_signature_id
    for args in [
        ("", "shopify", "x"),
        ("m", "", "x"),
        ("m", "shopify", ""),
    ]:
        with pytest.raises(ValueError):
            make_pivota_signature_id(*args)


def test_canonical_url_uses_checkout_ui_base_env(monkeypatch: pytest.MonkeyPatch):
    """URL builder respects CHECKOUT_UI_BASE_URL — same env var the
    rest of the route layer (order_routes / buyer_api / agent_payment_sdk)
    keys off, so dev/staging environments stay consistent."""
    from services.catalog_sync_service import pivota_canonical_pdp_url
    monkeypatch.setenv("CHECKOUT_UI_BASE_URL", "https://staging.pivota.cc")
    url = pivota_canonical_pdp_url("sig_abcdef")
    assert url == "https://staging.pivota.cc/products/sig_abcdef"


def test_canonical_url_default_is_production(monkeypatch: pytest.MonkeyPatch):
    from services.catalog_sync_service import pivota_canonical_pdp_url
    monkeypatch.delenv("CHECKOUT_UI_BASE_URL", raising=False)
    url = pivota_canonical_pdp_url("sig_abc")
    assert url == "https://agent.pivota.cc/products/sig_abc"


def test_canonical_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch):
    from services.catalog_sync_service import pivota_canonical_pdp_url
    monkeypatch.setenv("CHECKOUT_UI_BASE_URL", "https://staging.pivota.cc/")
    url = pivota_canonical_pdp_url("sig_x")
    assert url == "https://staging.pivota.cc/products/sig_x"


def test_make_pivota_canonical_fields_shape():
    """Convenience wrapper returns the canonical fields the catalog upsert wants."""
    from services.catalog_sync_service import make_pivota_canonical_fields
    out = make_pivota_canonical_fields("m1", "shopify", "p1")
    assert set(out.keys()) == {
        "pivota_signature_id",
        "pivota_canonical_url",
        "pivota_signature_minted_at",
    }
    assert out["pivota_signature_id"].startswith("sig_")
    assert out["pivota_canonical_url"].endswith(out["pivota_signature_id"])
    assert out["pivota_signature_minted_at"] is not None
