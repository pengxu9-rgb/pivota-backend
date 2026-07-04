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
    assert a == b
    # a different product is a different id
    assert stable_source_id("https://anua.com/products/cleanser") != a


def test_stable_source_id_is_url_path_safe():
    # The id flows into a URL path segment (evidence route + pipe product_key), so
    # it must be slash-free — a '/' 404s via %2F decoding. Host stays readable.
    sid = stable_source_id("https://www.anua.com/en/products/heartleaf-toner")
    assert "/" not in sid
    assert sid.startswith("anua.com~")
    # encodeURIComponent-safe chars only (unreserved: alnum - . _ ~)
    import re
    assert re.fullmatch(r"[A-Za-z0-9._~-]+", sid)


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
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS", raising=False)
    assert intake.audit_intake_enabled() is False
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "yes")
    assert intake.audit_intake_enabled() is True


def test_audit_intake_per_merchant_canary_allowlist(monkeypatch):
    import services.audit_index_intake as intake

    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS", "m_canary, m_two")
    # On for allowlisted merchants only — the safe prod canary.
    assert intake.audit_intake_enabled("m_canary") is True
    assert intake.audit_intake_enabled("m_two") is True
    # Off for everyone else, and off when no merchant is supplied.
    assert intake.audit_intake_enabled("m_other") is False
    assert intake.audit_intake_enabled() is False


def test_audit_intake_global_flag_overrides_allowlist(monkeypatch):
    import services.audit_index_intake as intake

    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "true")
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS", raising=False)
    # Graduation switch: on for all merchants regardless of the allowlist.
    assert intake.audit_intake_enabled("anyone") is True
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


# --- W5.1: per-merchant CSV canary on the legacy wedge path ------------------

def test_wedge_path_honors_per_merchant_csv_canary(monkeypatch):
    """The wedge audit's seed loop is gated by audit_intake_enabled(merchant_id),
    so the per-merchant CSV canary applies: a merchant in the allowlist seeds; one
    not in it does not. (Previously the wedge path called it WITHOUT merchant_id,
    so the CSV canary never took effect there.)"""
    import asyncio

    import routes.merchant_audit_routes as mar

    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS", "m_canary")

    seeded = []

    async def _spy_upsert(merchant_id, product):
        seeded.append((merchant_id, product))
        return "ck_x"

    async def _stop_after_seed(**kwargs):
        # Fires immediately after the seed loop — abort the (heavy) report path so
        # the test exercises only the intake gate. Recorded 'failed', then returns.
        raise RuntimeError("stop after seed loop")

    async def _noop_completed(**kwargs):
        return None

    monkeypatch.setattr(mar, "upsert_audited_sku_to_index", _spy_upsert)
    monkeypatch.setattr(mar, "recent_runs_for_merchant", _stop_after_seed)
    monkeypatch.setattr(mar, "record_audit_run_completed", _noop_completed)

    products = [_shopify_audit_product()]

    async def _run(mid):
        seeded.clear()
        await mar._run_wedge_audit_background(
            run_id="run_1",
            merchant_id=mid,
            merchant_name="Anua",
            merchant_domain="anua.com",
            audit_products=products,
            base_payload={},
        )

    asyncio.run(_run("m_canary"))       # allowlisted -> seed loop runs
    assert seeded == [("m_canary", products[0])]

    asyncio.run(_run("m_other"))        # not allowlisted -> seed loop skipped
    assert seeded == []


# --- W5.1: ADR-008 brand-fragmentation guard follows intake ------------------

_GUARD_CONFLICT_ROW = {
    "product_key": "prod::external_seed::external_seed::anua_1",
    "merchant_id": "external_seed",
    "content_key": "ck_existing_canonical",
    "pivota_signature_id": "sig_abc",
}


def _wire_guard_seams(monkeypatch, *, fetch_one):
    """Stub the DB + pdp-refresh + review-enqueue seams so upsert runs in-memory."""
    import db.database
    import services.agent_pdp_view_assembler as asm
    import services.audit_index_intake as intake

    calls = {"catalog_execute": 0, "enqueued": []}

    async def _fake_fetch_one(query, values=None):
        return fetch_one(query, values)

    async def _fake_execute(stmt):
        calls["catalog_execute"] += 1

    async def _fake_refresh(ck, *, refresh_source, db=None):
        return True

    async def _fake_enqueue(fields, match):
        calls["enqueued"].append(match)
        return "pdptask_x"

    monkeypatch.setattr(db.database.database, "fetch_one", _fake_fetch_one)
    monkeypatch.setattr(db.database.database, "execute", _fake_execute)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", _fake_refresh)
    monkeypatch.setattr(intake, "enqueue_audit_identity_review", _fake_enqueue)
    return calls


def test_brand_guard_active_when_intake_enabled_no_guard_env(monkeypatch):
    """The guard follows intake: enabled for the merchant + no opt-out env => the
    guard runs and skips a brand-fragmenting orphan mint (routes it to review)."""
    import asyncio

    import services.audit_index_intake as intake

    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "1")          # intake on
    monkeypatch.delenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", raising=False)
    monkeypatch.delenv("ENABLE_AUDIT_ER_GATE", raising=False)

    assert intake.audit_brand_fragmentation_guard_enabled("m_wedge_demo") is True

    calls = _wire_guard_seams(monkeypatch, fetch_one=lambda q, v: dict(_GUARD_CONFLICT_ROW))
    out = asyncio.run(intake.upsert_audited_sku_to_index("m_wedge_demo", _shopify_audit_product()))

    assert out is None                       # orphan mint suppressed by the guard
    assert calls["catalog_execute"] == 0     # no catalog upsert
    assert len(calls["enqueued"]) == 1       # routed to identity review
    assert calls["enqueued"][0]["matcher"] == "brand_host_fragmentation"


def test_brand_guard_opt_out_env_disables_it(monkeypatch):
    """DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD is the explicit opt-out escape
    hatch: with it set, the guard does not run even though intake is enabled and a
    same-brand conflict exists — the seed is minted."""
    import asyncio

    import services.audit_index_intake as intake

    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "1")             # intake on
    monkeypatch.setenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", "1")  # opt out
    monkeypatch.delenv("ENABLE_AUDIT_ER_GATE", raising=False)

    assert intake.audit_brand_fragmentation_guard_enabled("m_wedge_demo") is False

    calls = _wire_guard_seams(monkeypatch, fetch_one=lambda q, v: dict(_GUARD_CONFLICT_ROW))
    out = asyncio.run(intake.upsert_audited_sku_to_index("m_wedge_demo", _shopify_audit_product()))

    assert out                               # minted despite the conflict
    assert calls["catalog_execute"] == 1     # catalog upsert happened
    assert calls["enqueued"] == []           # nothing routed to review
