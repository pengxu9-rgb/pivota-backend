"""P1 — audit -> commerce-index SKU mapping (pure, no DB).

The automatic index-population path: a fetched audit-product becomes a canonical
catalog_products row keyed by content_key + a stable URL identity.
"""

from services.audit_index_intake import (
    PLATFORM_URL_AUDIT,
    audit_product_to_index_fields,
    stable_source_id,
)
from services.catalog_identity import make_content_key
from services.catalog_sync_service import make_catalog_product_key


def _shopify_audit_product():
    return {
        "title": "Heartleaf 77% Soothing Toner",
        "raw_title": "Anua Heartleaf 77% Soothing Toner 250ml",
        "pdp_url": "https://www.anua.com/products/heartleaf-toner?variant=42",
        "vendor": "Anua",
        "product_type": "Toner",
        "attributes_raw": {"size": "250ml"},
    }


def test_maps_audit_product_to_canonical_fields():
    f = audit_product_to_index_fields("m_anua", _shopify_audit_product())
    assert f["merchant_id"] == "m_anua"
    assert f["platform"] == PLATFORM_URL_AUDIT
    assert f["title"] == "Heartleaf 77% Soothing Toner"
    assert f["brand"] == "Anua"
    assert f["content_key"] == make_content_key("Anua", "Heartleaf 77% Soothing Toner")
    assert f["canonical_url"].startswith("https://www.anua.com/products/heartleaf-toner")
    assert f["source_domain"] == "anua.com"  # www + scheme stripped
    assert f["product_type"] == "Toner"
    # product_key is the canonical catalog key (prod::merchant::platform::source),
    # not a bespoke pipe-joined format — so url_audit seeds parse like every other row.
    assert f["product_key"] == make_catalog_product_key(
        "m_anua", PLATFORM_URL_AUDIT, f["source_product_id"]
    )
    assert f["product_key"].startswith("prod::m_anua::")


def test_stable_source_id_dedups_url_variants():
    # trailing slash, www, scheme, query, fragment all normalize to the same id
    a = stable_source_id("https://www.anua.com/products/heartleaf-toner/")
    b = stable_source_id("http://anua.com/products/heartleaf-toner?utm=x#reviews")
    assert a == b == "anua.com/products/heartleaf-toner"
    # a different product is a different id
    assert stable_source_id("https://anua.com/products/cleanser") != a


def test_requires_title_and_url_identity():
    assert audit_product_to_index_fields("m1", {"pdp_url": "https://x.com/p"}) is None  # no title
    assert audit_product_to_index_fields("m1", {"title": "X"}) is None  # no URL identity
    assert audit_product_to_index_fields("", _shopify_audit_product()) is None  # no merchant
    assert audit_product_to_index_fields("m1", None) is None


def test_brand_optional():
    p = _shopify_audit_product()
    p.pop("vendor")
    f = audit_product_to_index_fields("m1", p)
    assert f["brand"] is None
    # content_key still mints from title alone? make_content_key needs brand -> None
    # (brand+title key); brandless seeds resolve via the identity gate downstream.
    assert "content_key" in f


def test_audit_intake_flag_defaults_off(monkeypatch):
    import services.audit_index_intake as intake

    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    assert intake.audit_intake_enabled() is False
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "yes")
    assert intake.audit_intake_enabled() is True


def test_upsert_orchestration_best_effort(monkeypatch):
    import asyncio

    import db.database
    import services.agent_pdp_view_assembler as asm
    import services.audit_index_intake as intake

    calls = {"execute": 0, "refresh": None}

    async def _fake_execute(stmt):
        calls["execute"] += 1

    async def _fake_refresh(ck, *, refresh_source, db=None):
        calls["refresh"] = (ck, refresh_source)
        return True

    monkeypatch.setattr(db.database.database, "execute", _fake_execute)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", _fake_refresh)

    out = asyncio.run(
        intake.upsert_audited_sku_to_index("m_anua", _shopify_audit_product())
    )
    assert calls["execute"] == 1  # catalog upsert attempted
    assert calls["refresh"][1] == "url_audit_intake"  # pdp refresh attempted
    assert out  # content_key returned


def test_upsert_skips_unmappable_product(monkeypatch):
    import asyncio

    import db.database
    import services.audit_index_intake as intake

    async def _boom(stmt):
        raise AssertionError("must not touch DB for an unmappable product")

    monkeypatch.setattr(db.database.database, "execute", _boom)
    out = asyncio.run(
        intake.upsert_audited_sku_to_index("m1", {"pdp_url": "https://x.com/p"})
    )
    assert out is None  # no title -> no mapping -> no DB write
