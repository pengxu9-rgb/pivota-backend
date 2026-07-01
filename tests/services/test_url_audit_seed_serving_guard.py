"""Guardrails for auto-seeding the commerce index from URL audits.

Two invariants this covers:

1. pick_canonical MUST never let an observed `url_audit` seed win canonical over
   a real (non-audit) row that shares its brand+title content_key. agent_pdp_view
   is keyed by content_key, so the canonical winner supplies the served
   title/description/image — a seed winning would overwrite a claimed product's
   PDP with audit-scraped content. This is the highest-severity risk of the
   seed-on-audit pipeline (the seed's content_key is non-unique / GTIN-less).

2. build_per_sku_report only upgrades a URL-audit product to an evidence-attachable
   pipe product_key when index-intake is ON (the seed must actually exist for the
   portal's evidence endpoints to resolve it).
"""

import os

from services.agent_pdp_view_assembler import pick_canonical
from services.agent_center_bd_report_service import _url_audit_seed_report_identity
from services.audit_index_intake import PLATFORM_URL_AUDIT, stable_source_id
from services.catalog_identity import make_content_key


def _row(product_key, *, platform="shopify", primary=False, signature=None):
    return {
        "product_key": product_key,
        "platform": platform,
        "group_is_primary": primary,
        "pivota_signature_id": signature,
    }


def test_url_audit_seed_never_wins_canonical_over_real_row():
    # The dangerous collision: a real row that is NEITHER group-primary NOR signed,
    # AND whose product_key sorts AFTER the seed's. Pre-guard, the seed's lower
    # product_key would win the final tiebreak; the guard must still pick the real
    # row so a claimed PDP is never overwritten with audit-seed content.
    seed = _row("prod::m::url_audit::aaaa", platform=PLATFORM_URL_AUDIT)
    real = _row("prod::m::shopify::zzzz", platform="shopify")
    assert pick_canonical([seed, real])["product_key"] == "prod::m::shopify::zzzz"
    assert pick_canonical([real, seed])["product_key"] == "prod::m::shopify::zzzz"


def test_url_audit_seed_loses_even_to_unsigned_non_primary_real_row():
    # Real row wins on tier-0 (non-audit) regardless of the lower tiers.
    seed = _row("prod::m::url_audit::a", platform=PLATFORM_URL_AUDIT, signature="sig_x")
    real = _row("prod::m::shopify::b", platform="shopify", signature=None)
    # Even though the SEED is 'signed' and the real row isn't, non-audit wins.
    assert pick_canonical([seed, real])["product_key"] == "prod::m::shopify::b"


def test_seed_only_cluster_still_resolves_deterministically():
    # When a content_key has ONLY audit seeds (a competitor/arbitrary URL — the
    # common case), pick_canonical still returns one deterministically. Such a
    # cluster isn't served anyway (no index_pipeline_state row).
    a = _row("prod::m::url_audit::a", platform=PLATFORM_URL_AUDIT)
    b = _row("prod::m::url_audit::b", platform=PLATFORM_URL_AUDIT)
    assert pick_canonical([b, a])["product_key"] == "prod::m::url_audit::a"


def test_non_audit_pick_order_is_unchanged_by_the_guard():
    # Regression: for clusters with no audit seed, the original ladder
    # (primary -> signature -> lowest product_key) is untouched.
    primary = _row("pk_b", primary=True)
    signed = _row("pk_a", signature="sig_1")
    assert pick_canonical([signed, primary])["product_key"] == "pk_b"
    lowest = _row("pk_a")
    higher = _row("pk_b")
    assert pick_canonical([higher, lowest])["product_key"] == "pk_a"


def test_report_identity_is_pipe_key_when_intake_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "1")
    product = {
        "canonical_url": "https://www.anua.com/products/heartleaf-toner",
        "brand": "Anua",
        "title": "Heartleaf 77% Soothing Toner",
    }
    pk, ck = _url_audit_seed_report_identity("m_anua", product, {})
    # Pipe form the portal's parseProductKey can split into platform + id, and the
    # id equals the seed's source_product_id so the evidence endpoint resolves it.
    source_id = stable_source_id(product["canonical_url"])
    assert pk == f"m_anua|{PLATFORM_URL_AUDIT}|{source_id}"
    assert ck == make_content_key("Anua", "Heartleaf 77% Soothing Toner")


def test_report_identity_is_none_when_intake_disabled(monkeypatch):
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    product = {
        "canonical_url": "https://www.anua.com/products/heartleaf-toner",
        "brand": "Anua",
        "title": "Heartleaf 77% Soothing Toner",
    }
    # Off => keep the ephemeral urlwedge key (no seed exists to attach to).
    assert _url_audit_seed_report_identity("m_anua", product, {}) == (None, None)


def test_report_identity_is_none_without_a_url(monkeypatch):
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "1")
    assert _url_audit_seed_report_identity("m_anua", {"title": "x"}, {}) == (None, None)
