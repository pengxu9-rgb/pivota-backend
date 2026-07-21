"""ADR-011 — door wiring for the resolve-or-attach primitive.

Covers the flag-gated pre-insert step at the doors this change guards or
re-plumbs (audit / brand-authored / enrichment), including:
  - flags OFF → the primitive is NOT called and the legacy path runs (rollout
    safety: default behavior byte-identical);
  - SKIP suppresses the insert (and, at the enrichment door, the skipped PDP's
    child sku/offer/seed rows);
  - ATTACH re-aligns content_key to the resolved identity;
  - R4 at the audit door: a same-merchant Tier-0 ATTACH re-keys the upsert
    onto the listing's existing (platform, source_product_id, product_key)
    identity + sig instead of minting a URL-fresh sibling.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import services.intake_identity as ii  # noqa: E402


class FakeDatabase:
    is_connected = True

    def __init__(self) -> None:
        self.executed: List[Any] = []

    async def execute(self, query: Any = None, values: Any = None) -> None:
        self.executed.append((query, values))

    async def fetch_one(self, *a: Any, **k: Any) -> None:
        return None

    async def fetch_all(self, *a: Any, **k: Any) -> List[Any]:
        return []

    def transaction(self) -> "FakeDatabase":
        return self

    async def __aenter__(self) -> "FakeDatabase":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def connect(self) -> None:
        return None


def _catalog_products_stmt(executed: List[Any]) -> Any:
    """The sqlalchemy insert targeting catalog_products (other recorded calls
    are raw-SQL strings or other tables' statements)."""
    return next(
        q for q, _ in executed
        if getattr(getattr(q, "table", None), "name", None) == "catalog_products"
    )


def _ident(action: str, content_key: Optional[str] = None,
           attach: Optional[Dict[str, Any]] = None,
           gtin: Optional[str] = None) -> Dict[str, Any]:
    return {
        "content_key": content_key,
        "product_group_id": "pg_x",
        "action": action,
        "gtin": gtin,
        "evidence": {"door": "?", "action": action, "matcher": "test", "evidence": {}},
        "attach": attach,
    }


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    """Patch METHODS on the real db singleton — never swap the object itself:
    collaborators (e.g. services.seller_identity) bind `database` at module
    import time, and an object swap would poison that binding for every later
    test in the session (the known db-singleton gotcha)."""
    import db.database

    fake = FakeDatabase()
    monkeypatch.setattr(db.database.database, "execute", fake.execute)
    monkeypatch.setattr(db.database.database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(db.database.database, "fetch_all", fake.fetch_all)
    return fake


def _stub_primitive(monkeypatch: pytest.MonkeyPatch, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    async def fake(brand: Any = None, title: Any = None, gtin: Any = None,
                   canonical_url: Any = None, source_product_id: Any = None,
                   door: str = "", merchant_ctx: Any = None) -> Dict[str, Any]:
        calls.append({
            "brand": brand, "title": title, "gtin": gtin,
            "canonical_url": canonical_url, "source_product_id": source_product_id,
            "door": door, "merchant_ctx": merchant_ctx,
        })
        return result

    monkeypatch.setattr(ii, "resolve_or_attach_content_identity", fake)
    return calls


def _forbid_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("primitive must not run when the door flag is OFF")

    monkeypatch.setattr(ii, "resolve_or_attach_content_identity", boom)


# --- Door 5: audit / URL-wedge -------------------------------------------------------


def _audit_product() -> Dict[str, Any]:
    return {
        "title": "Heartleaf 77% Soothing Toner",
        "pdp_url": "https://www.anua.com/products/heartleaf-toner",
        "vendor": "Anua",
        "product_type": "Toner",
        "attributes_raw": {"barcode": "8809640733458"},
    }


@pytest.mark.asyncio
async def test_audit_door_flag_off_never_calls_primitive(fake_db, monkeypatch):
    import services.audit_index_intake as intake

    _forbid_primitive(monkeypatch)

    async def guard(*a: Any, **k: Any) -> Dict[str, Any]:
        return {"action": "proceed"}

    monkeypatch.setattr(intake, "apply_audit_brand_fragmentation_guard", guard)
    out = await intake.upsert_audited_sku_to_index("m_anua", _audit_product())
    assert out  # legacy path completed (returns content_key)


@pytest.mark.asyncio
async def test_audit_door_skip_suppresses_insert(fake_db, monkeypatch):
    import services.audit_index_intake as intake

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_AUDIT", "1")
    _stub_primitive(monkeypatch, _ident(ii.ACTION_SKIP))
    out = await intake.upsert_audited_sku_to_index("m_anua", _audit_product())
    assert out is None
    assert fake_db.executed == []  # nothing written


@pytest.mark.asyncio
async def test_audit_door_attach_realigns_content_key_and_plumbs_gtin(fake_db, monkeypatch):
    import services.audit_index_intake as intake

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_AUDIT", "1")
    resolved_ck = "ck_" + "d" * 32
    calls = _stub_primitive(monkeypatch, _ident(ii.ACTION_ATTACH, resolved_ck))
    out = await intake.upsert_audited_sku_to_index("m_anua", _audit_product())
    assert out == resolved_ck
    # R3: the source barcode reached the primitive, canonicalized (GS1 GTIN-14)
    assert calls[0]["gtin"] == "08809640733458"
    assert calls[0]["door"] == ii.DOOR_URL_AUDIT
    assert calls[0]["merchant_ctx"]["merchant_id"] == "m_anua"
    # ...and is persisted as the gtin match-attribute on the row.
    from sqlalchemy.dialects import postgresql

    stmt = _catalog_products_stmt(fake_db.executed)
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert params["gtin"] == "08809640733458"
    assert params["content_key"] == resolved_ck


@pytest.mark.asyncio
async def test_audit_door_r4_rekeys_onto_same_merchant_listing(fake_db, monkeypatch):
    """R4 / ADR-010 D-6: same-merchant Tier-0 ATTACH must reuse the listing's
    existing source identity + sig — never mint a URL-fresh sibling sig."""
    import services.audit_index_intake as intake

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_AUDIT", "1")
    resolved_ck = "ck_" + "e" * 32
    existing_sig = "sig_" + "9" * 32
    _stub_primitive(monkeypatch, _ident(
        ii.ACTION_ATTACH, resolved_ck,
        attach={
            "product_key": "prod::m_anua::url_audit::anua.com~orig",
            "merchant_id": "m_anua",
            "platform": "url_audit",
            "source_product_id": "anua.com~orig",
            "pivota_signature_id": existing_sig,
            "pivota_canonical_url": f"https://agent.pivota.cc/products/{existing_sig}",
            "same_merchant": True,
        },
    ))

    singleton_calls: List[Dict[str, Any]] = []

    async def capture_singleton(**kwargs: Any) -> None:
        singleton_calls.append(kwargs)

    import services.product_group_autogrouper as autogrouper

    monkeypatch.setattr(
        autogrouper, "ensure_singleton_group_membership", capture_singleton
    )

    out = await intake.upsert_audited_sku_to_index("m_anua", _audit_product())
    assert out == resolved_ck
    # The catalog upsert was re-keyed onto the EXISTING listing identity.
    from sqlalchemy.dialects import postgresql

    stmt = _catalog_products_stmt(fake_db.executed)
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert params["product_key"] == "prod::m_anua::url_audit::anua.com~orig"
    assert params["source_product_id"] == "anua.com~orig"
    assert params["pivota_signature_id"] == existing_sig  # reused, not URL-fresh
    assert params["content_key"] == resolved_ck
    # The singleton pg stamp follows the re-keyed listing identity too.
    assert singleton_calls[0]["source_product_id"] == "anua.com~orig"
    assert singleton_calls[0]["content_key"] == resolved_ck


@pytest.mark.asyncio
async def test_audit_door_cross_merchant_attach_keeps_own_listing(fake_db, monkeypatch):
    """Cross-merchant ATTACH reuses the content identity but keeps the door's
    own per-merchant listing + sig (ADR-010 T5: sigs stay per-merchant)."""
    import services.audit_index_intake as intake

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_AUDIT", "1")
    resolved_ck = "ck_" + "a" * 32
    _stub_primitive(monkeypatch, _ident(
        ii.ACTION_ATTACH, resolved_ck,
        attach={
            "product_key": "prod::m_other::external_seed::x",
            "merchant_id": "m_other",
            "platform": "external_seed",
            "source_product_id": "x",
            "pivota_signature_id": "sig_" + "8" * 32,
            "pivota_canonical_url": None,
            "same_merchant": False,
        },
    ))
    await intake.upsert_audited_sku_to_index("m_anua", _audit_product())
    from sqlalchemy.dialects import postgresql

    stmt = _catalog_products_stmt(fake_db.executed)
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert params["merchant_id"] == "m_anua"          # own listing
    assert params["product_key"].startswith("prod::m_anua::url_audit::")
    assert params["pivota_signature_id"] != "sig_" + "8" * 32  # own sig
    assert params["content_key"] == resolved_ck       # shared content identity


# --- Door 3: brand-authored ----------------------------------------------------------


def _ba_fields() -> Dict[str, Any]:
    from services.brand_authored_intake import build_catalog_fields

    return build_catalog_fields(
        "m_brand", "ba-toner-abc123def456",
        title="Soothing Toner", brand="Anua", gtin="8809640733458",
    )


@pytest.mark.asyncio
async def test_brand_authored_flag_off_never_calls_primitive(fake_db, monkeypatch):
    from services.brand_authored_intake import upsert_brand_authored_catalog_row

    _forbid_primitive(monkeypatch)
    fields = _ba_fields()
    out = await upsert_brand_authored_catalog_row(fields)
    assert out == fields["product_key"]


@pytest.mark.asyncio
async def test_brand_authored_attach_realigns_content_key(fake_db, monkeypatch):
    from services.brand_authored_intake import upsert_brand_authored_catalog_row

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_BRAND_AUTHORED", "1")
    resolved_ck = "ck_" + "b" * 32
    calls = _stub_primitive(monkeypatch, _ident(ii.ACTION_ATTACH, resolved_ck))
    fields = _ba_fields()
    out = await upsert_brand_authored_catalog_row(fields)
    assert out == fields["product_key"]
    assert fields["content_key"] == resolved_ck
    # R3: the merchant-supplied GTIN reached the primitive, canonicalized
    assert calls[0]["gtin"] == "08809640733458"
    assert calls[0]["door"] == ii.DOOR_BRAND_AUTHORED
    # ...and persists as the gtin match-attribute column.
    from sqlalchemy.dialects import postgresql

    stmt = _catalog_products_stmt(fake_db.executed)
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert params["gtin"] == "08809640733458"
    assert params["content_key"] == resolved_ck


@pytest.mark.asyncio
async def test_brand_authored_skip_is_defensive_no_insert(fake_db, monkeypatch):
    from services.brand_authored_intake import upsert_brand_authored_catalog_row

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_BRAND_AUTHORED", "1")
    _stub_primitive(monkeypatch, _ident(ii.ACTION_SKIP))
    out = await upsert_brand_authored_catalog_row(_ba_fields())
    assert out is None
    assert fake_db.executed == []


# --- Door 4: catalog-enrichment apply -------------------------------------------------


def _plan() -> Dict[str, Any]:
    def pdp(key: str, brand: str) -> Dict[str, Any]:
        return {
            "product_key": key, "merchant_id": "m_ret", "platform": "retail_crawl",
            "source_product_id": key.split("::")[-1],
            "pivota_signature_id": None, "pivota_canonical_url": None,
            "pivota_signature_minted_at": None,
            "catalog_track": "external_referral", "truth_tier": "observed",
            "readiness_tier": "referral_only", "source_system": "enrichment",
            "source_domain": "shop.example", "title": f"Product {key[-1]}",
            "description": None, "brand": brand, "product_type": None,
            "category": None, "category_path": None, "category_confidence": None,
            "category_label_source": None, "canonical_url": None, "image_url": None,
            "product_payload": "{}", "tags": "[]", "price_tier": None,
            "use_case_tags": "[]", "lifestyle_tags": "[]", "demographic": None,
            "pdp_lifecycle_stage": None, "pdp_scope": "unverified",
            "pdp_scope_source": None, "content_key": "ck_" + "0" * 32,
            "barcode": "8809640733458" if brand == "BrandB" else None,
        }

    return {
        "merchants": [],
        "pdps": [pdp("pk::1", "BrandA"), pdp("pk::2", "BrandB")],
        "skus": [{"sku_key": "sku::pk::1::c", "product_key": "pk::1"},
                 {"sku_key": "sku::pk::2::c", "product_key": "pk::2"}],
        "offers": [],
        "seeds": [{"id": "seed1", "attached_product_key": "pk::1"}],
    }


@pytest.mark.asyncio
async def test_enrichment_door_skip_filters_pdp_and_children(monkeypatch):
    import services.catalog_enrichment_agent.apply as apply_mod

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", "1")
    resolved_ck = "ck_" + "c" * 32

    async def fake(brand: Any = None, title: Any = None, **kw: Any) -> Dict[str, Any]:
        if brand == "BrandA":
            return _ident(ii.ACTION_SKIP)
        return _ident(ii.ACTION_ATTACH, resolved_ck)

    monkeypatch.setattr(ii, "resolve_or_attach_content_identity", fake)

    async def no_audit_log(audit: Any, **kw: Any) -> None:
        return None

    async def no_offer_guard(offers: Any, **kw: Any):
        return [], {}, []

    monkeypatch.setattr(apply_mod, "write_writer_audit_log", no_audit_log)
    monkeypatch.setattr(apply_mod, "guard_catalog_offer_rows", no_offer_guard)

    db = FakeDatabase()
    counts = await apply_mod.apply_ingest_plan(_plan(), batch_label="t", db=db)
    assert counts["pdps"] == 1
    assert counts["pdps_skipped_identity"] == 1
    assert counts["skus"] == 1   # pk::1's sku filtered with its pdp
    assert counts["seeds"] == 0  # pk::1's seed filtered too
    pdp_inserts = [v for q, v in db.executed
                   if isinstance(q, str) and "INSERT INTO catalog_products" in q]
    assert len(pdp_inserts) == 1
    assert pdp_inserts[0]["product_key"] == "pk::2"
    assert pdp_inserts[0]["content_key"] == resolved_ck  # ATTACH re-aligned
    # R3: pk::2's barcode canonicalized into the gtin match-attribute column
    assert pdp_inserts[0]["gtin"] == "08809640733458"


@pytest.mark.asyncio
async def test_enrichment_door_flag_off_never_calls_primitive(monkeypatch):
    import services.catalog_enrichment_agent.apply as apply_mod

    _forbid_primitive(monkeypatch)

    async def no_audit_log(audit: Any, **kw: Any) -> None:
        return None

    async def no_offer_guard(offers: Any, **kw: Any):
        return [], {}, []

    monkeypatch.setattr(apply_mod, "write_writer_audit_log", no_audit_log)
    monkeypatch.setattr(apply_mod, "guard_catalog_offer_rows", no_offer_guard)

    db = FakeDatabase()
    counts = await apply_mod.apply_ingest_plan(_plan(), batch_label="t", db=db)
    assert counts["pdps"] == 2
    assert counts["pdps_skipped_identity"] == 0


# --- Door 2: mirror helper -----------------------------------------------------------


def test_seed_gtin_reads_top_level_and_variant():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "mirror_script",
        Path(__file__).resolve().parents[2]
        / "scripts" / "mirror_external_seeds_to_catalog_products.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._seed_gtin({"barcode": " 880964 "}) == "880964"
    assert mod._seed_gtin({"variants": [{"barcode": "12345"}]}) == "12345"
    assert mod._seed_gtin({"variants": [{}]}) is None
    assert mod._seed_gtin(None) is None
    assert mod._seed_gtin({"gtin13": "8809640733458", "barcode": "x"}) == "8809640733458"
