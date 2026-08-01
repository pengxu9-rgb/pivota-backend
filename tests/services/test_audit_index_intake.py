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


def test_audit_intake_flag_removed(monkeypatch):
    """W5 P2: seeding is the unconditional main line — the ENABLE_AUDIT_INDEX_INTAKE
    flag helper is GONE. This locks in that no code path can re-introduce a gate."""
    import services.audit_index_intake as intake

    assert not hasattr(intake, "audit_intake_enabled")


def test_upsert_orchestration_best_effort(monkeypatch):
    import asyncio

    import db.database
    import services.agent_pdp_view_assembler as asm
    import services.audit_index_intake as intake
    import services.catalog_sync_service as css
    import services.seller_identity as si

    calls = {"execute": 0, "refresh": None}

    async def _fake_execute(stmt):
        calls["execute"] += 1

    async def _fake_upsert_merchant(**kwargs):
        pass

    async def _fake_refresh(ck, *, refresh_source, db=None):
        calls["refresh"] = (ck, refresh_source)
        return True

    monkeypatch.setattr(db.database.database, "execute", _fake_execute)
    # The merchant upserts are mocked out so `execute` counts ONLY the
    # catalog_products write this test is about. Both bindings, deliberately:
    # services/seller_identity.py:49 does a MODULE-LEVEL
    # `from services.catalog_sync_service import upsert_catalog_merchant`, so
    # patching `css` alone intercepts it only when seller_identity happens to be
    # imported for the first time inside this test.
    #
    # Without these, `execute` counts 2 on a production-shaped catalog_merchants:
    # the intake's own merchant upsert lands as well. It counted 1 only while a
    # narrow test fixture made that upsert fail into its best-effort `except` —
    # i.e. the assertion was pinning a swallowed exception, and it flipped red as
    # soon as the fixture matched the real schema.
    monkeypatch.setattr(css, "upsert_catalog_merchant", _fake_upsert_merchant)
    monkeypatch.setattr(si, "upsert_catalog_merchant", _fake_upsert_merchant)
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


# --- W5 P2: the legacy wedge path seeds UNCONDITIONALLY -----------------------

def test_wedge_path_seeds_unconditionally(monkeypatch):
    """W5 P2: seeding is the unconditional main line — the wedge audit's seed loop
    runs for EVERY merchant with no flag set. (Previously it was gated on
    audit_intake_enabled(merchant_id)'s per-merchant CSV canary; that gate is gone.)"""
    import asyncio

    import routes.merchant_audit_routes as mar

    # No flag env at all — seeding must still happen.
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS", raising=False)

    seeded = []

    async def _spy_upsert(merchant_id, product):
        seeded.append((merchant_id, product))
        return "ck_x"

    async def _stop_after_seed(**kwargs):
        # Fires immediately after the seed loop — abort the (heavy) report path so
        # the test exercises only the seed loop. Recorded 'failed', then returns.
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

    # Any merchant seeds — no allowlist, no flag.
    asyncio.run(_run("m_any_merchant"))
    assert seeded == [("m_any_merchant", products[0])]

    asyncio.run(_run("m_other_merchant"))
    assert seeded == [("m_other_merchant", products[0])]


# --- W5 P2: ADR-008 brand-fragmentation guard runs unconditionally -----------

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
    import services.catalog_sync_service as css
    import services.index_pipeline_state_service as ips

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

    # W5 P3's best-effort merchant upsert + eligibility recompute are stubbed to
    # no-ops so this guard test isolates the single product upsert it asserts on.
    async def _fake_upsert_merchant(**kwargs):
        return "m_row"

    async def _fake_recompute(content_key, *, reason=None, **kwargs):
        return None

    monkeypatch.setattr(db.database.database, "fetch_one", _fake_fetch_one)
    monkeypatch.setattr(db.database.database, "execute", _fake_execute)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", _fake_refresh)
    monkeypatch.setattr(intake, "enqueue_audit_identity_review", _fake_enqueue)
    monkeypatch.setattr(css, "upsert_catalog_merchant", _fake_upsert_merchant)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", _fake_recompute)
    return calls


def test_brand_guard_active_unconditionally_no_guard_env(monkeypatch):
    """W5 P2: the guard runs unconditionally — no ENABLE flag, no opt-out env =>
    the guard runs and skips a brand-fragmenting orphan mint (routes it to
    review)."""
    import asyncio

    import services.audit_index_intake as intake

    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
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
    hatch: with it set, the guard does not run even though seeding is on and a
    same-brand conflict exists — the seed is minted."""
    import asyncio

    import services.audit_index_intake as intake

    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    monkeypatch.setenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", "1")  # opt out
    monkeypatch.delenv("ENABLE_AUDIT_ER_GATE", raising=False)

    assert intake.audit_brand_fragmentation_guard_enabled("m_wedge_demo") is False

    calls = _wire_guard_seams(monkeypatch, fetch_one=lambda q, v: dict(_GUARD_CONFLICT_ROW))
    out = asyncio.run(intake.upsert_audited_sku_to_index("m_wedge_demo", _shopify_audit_product()))

    assert out                               # minted despite the conflict
    assert calls["catalog_execute"] == 1     # catalog upsert happened
    assert calls["enqueued"] == []           # nothing routed to review

# --- W5 P3: canonical PDP minting on url_audit seeds (ADR-007) -----------------

def test_seed_mints_deterministic_pivota_signature():
    from datetime import datetime

    from services.catalog_sync_service import make_pivota_signature_id

    f = audit_product_to_index_fields("m_anua", _shopify_audit_product())
    # The offer-free citation identity is minted AT SEED TIME (ADR-007) — keyed on
    # the SAME (merchant, platform, source_id) tuple that keys product_key.
    assert f["pivota_signature_id"] == make_pivota_signature_id(
        "m_anua", PLATFORM_URL_AUDIT, f["source_product_id"]
    )
    assert f["pivota_signature_id"].startswith("sig_")
    # canonical URL embeds the sig (agent.pivota.cc/products/<sig>).
    assert f["pivota_canonical_url"].endswith(
        "/products/" + f["pivota_signature_id"]
    )
    # minted_at is a real timestamp (arc-phase honest, not a static caveat).
    assert isinstance(f["pivota_signature_minted_at"], datetime)


def test_reseed_same_url_yields_same_signature_no_duplicate():
    # Re-auditing the same URL (same merchant) mints the SAME sig + product_key —
    # idempotent, so the upsert updates ONE row and preserves ONE signature.
    a = audit_product_to_index_fields("m_anua", _shopify_audit_product())
    variant = _shopify_audit_product()
    variant["pdp_url"] = "http://anua.com/products/heartleaf-toner?utm=x#reviews"
    b = audit_product_to_index_fields("m_anua", variant)
    assert a["source_product_id"] == b["source_product_id"]
    assert a["product_key"] == b["product_key"]
    assert a["pivota_signature_id"] == b["pivota_signature_id"]
    # A different merchant NEVER collides (merchant_id is in the sig input).
    c = audit_product_to_index_fields("m_other", _shopify_audit_product())
    assert c["pivota_signature_id"] != a["pivota_signature_id"]


def test_upsert_wires_merchant_row_and_eligibility_recompute(monkeypatch):
    import asyncio

    import db.database
    import services.agent_pdp_view_assembler as asm
    import services.audit_index_intake as intake
    import services.catalog_sync_service as css
    import services.index_pipeline_state_service as ips
    import services.seller_identity as si

    executed = []
    merchant_calls = []
    recompute_calls = []

    async def _fake_execute(stmt):
        executed.append(stmt)

    async def _fake_upsert_merchant(**kwargs):
        merchant_calls.append(kwargs)

    async def _fake_refresh(ck, *, refresh_source, db=None):
        return True

    async def _fake_recompute(content_key, *, reason=None):
        # Thin seed stays ineligible until E1 enrichment graduates it — the
        # classifier decides, the intake never force-sets eligibility.
        recompute_calls.append((content_key, reason))
        return False

    monkeypatch.setattr(db.database.database, "execute", _fake_execute)
    monkeypatch.setattr(css, "upsert_catalog_merchant", _fake_upsert_merchant)
    # seller_identity binds upsert_catalog_merchant at MODULE level (line 49), so
    # patching `css` alone catches its call only when seller_identity is first
    # imported inside this test. Patch both so `merchant_calls` is deterministic.
    monkeypatch.setattr(si, "upsert_catalog_merchant", _fake_upsert_merchant)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", _fake_refresh)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", _fake_recompute)

    out = asyncio.run(
        intake.upsert_audited_sku_to_index("m_anua", _shopify_audit_product())
    )
    assert out  # content_key returned

    # Serve chain: a catalog_merchants row is upserted (indexable default true,
    # status='active') so the by-signature PDP inner-join resolves for a
    # store-less URL-tier merchant.
    #
    # Filtered to the AUDITING merchant rather than asserting a bare
    # `len(merchant_calls) == 1`. On a production-shaped catalog_merchants the
    # intake upserts TWO merchant rows — this one, plus the observed seller that
    # ensure_observed_seller resolves from the destination domain
    # (merch_obs_<hash>). The bare count read 1 only while a narrow test fixture
    # made the seller lane fail into its best-effort `except`, so it pinned a
    # swallowed exception and flipped red the moment the fixture matched the real
    # schema. Filtering also keeps this test off the seller lane's back.
    audit_upserts = [c for c in merchant_calls if c["merchant_id"] == "m_anua"]
    assert len(audit_upserts) == 1
    assert audit_upserts[0]["primary_platform"] == PLATFORM_URL_AUDIT

    # The count stays TOTAL. Filtering to m_anua alone would constrain only that
    # id and silently permit any OTHER merchant upsert — including a write to the
    # `external_seed` placeholder bucket that services/seller_identity.py:61
    # (BANNED_BUCKET_MERCHANT_ID, ADR-009 D2) exists to forbid. Verified: adding
    # such an upsert to the intake leaves the whole suite green without this line
    # and turns this test red with it.
    unexpected = [
        c["merchant_id"]
        for c in merchant_calls
        if c["merchant_id"] != "m_anua" and not c["merchant_id"].startswith("merch_obs_")
    ]
    assert unexpected == [], f"unexpected catalog_merchants upserts: {unexpected}"

    # exactly one catalog_products upsert (the merchant upsert is mocked out).
    assert len(executed) == 1

    # Eligibility recomputed through the REAL classifier entrypoint, keyed on the
    # returned content_key, with the intake's reason.
    assert len(recompute_calls) == 1
    assert recompute_calls[0][0] == out
    assert recompute_calls[0][1] == "url_audit_intake"
