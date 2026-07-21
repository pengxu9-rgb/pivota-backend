"""ADR-008 brand-fragmentation guard: an audit seed for a brand already canonical
under another merchant is routed to review and NOT minted as an orphan."""
import asyncio

import db.database
import services.agent_pdp_view_assembler as asm
import services.audit_index_intake as intake
import services.catalog_sync_service as css
import services.index_pipeline_state_service as ips
# Imported EAGERLY so _wire can patch its module-level upsert_catalog_merchant
# binding. upsert_audited_sku_to_index imports this module lazily; if the first
# import happened while css.upsert_catalog_merchant was monkeypatched, the stub
# leaked into seller_identity's namespace forever — and if it happened in an
# unpatched test first, the REAL upsert ran here and inflated the execute count.
import services.seller_identity as seller_identity


def _audit_product():
    return {
        "title": "Heartleaf 77% Soothing Toner",
        "raw_title": "Anua Heartleaf 77% Soothing Toner 250ml",
        "pdp_url": "https://www.anua.com/products/heartleaf-toner",
        "vendor": "Anua",
        "product_type": "Toner",
        "attributes_raw": {"size": "250ml"},
    }


_CONFLICT_ROW = {
    "product_key": "prod::external_seed::external_seed::anua_1",
    "merchant_id": "external_seed",
    "content_key": "ck_existing_canonical",
    "pivota_signature_id": "sig_abc",
}


def _wire(monkeypatch, *, fetch_one, enqueue_spy=None):
    calls = {"catalog_execute": 0, "refresh": None, "enqueued": []}

    async def _fake_fetch_one(query, values=None):
        return fetch_one(query, values)

    async def _fake_execute(stmt):
        calls["catalog_execute"] += 1

    async def _fake_refresh(ck, *, refresh_source, db=None):
        calls["refresh"] = (ck, refresh_source)
        return True

    async def _fake_enqueue(fields, match):
        calls["enqueued"].append(match)
        return "pdptask_x"

    # W5 P3 added a best-effort catalog_merchant upsert + serving-eligibility
    # recompute inside upsert_audited_sku_to_index. Stub both to no-ops so the
    # guard tests isolate the single PRODUCT upsert they assert on (the merchant
    # upsert otherwise routes through the same execute seam and inflates the count).
    async def _fake_upsert_merchant(**kwargs):
        return "m_row"

    async def _fake_recompute(content_key, *, reason=None, **kwargs):
        return None

    monkeypatch.setattr(db.database.database, "fetch_one", _fake_fetch_one)
    monkeypatch.setattr(db.database.database, "execute", _fake_execute)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", _fake_refresh)
    monkeypatch.setattr(intake, "enqueue_audit_identity_review", enqueue_spy or _fake_enqueue)
    monkeypatch.setattr(css, "upsert_catalog_merchant", _fake_upsert_merchant)
    monkeypatch.setattr(seller_identity, "upsert_catalog_merchant", _fake_upsert_merchant)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", _fake_recompute)
    return calls


def test_guard_off_when_opt_out_set_never_queries(monkeypatch):
    # W5 P2: the guard runs unconditionally; the ONLY way to turn it off is the
    # explicit opt-out env. With it set the guard does not run (and never queries).
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS", raising=False)
    monkeypatch.setenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", "1")

    def _should_not_run(query, values):
        raise AssertionError("guard must not query when the opt-out env is set")

    _wire(monkeypatch, fetch_one=_should_not_run)
    guard = asyncio.run(
        intake.apply_audit_brand_fragmentation_guard("m_wedge", {"brand": "Anua", "source_domain": "anua.com"})
    )
    assert guard["action"] == "proceed"


def test_guard_skips_mint_on_brand_conflict(monkeypatch):
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)  # guard runs unconditionally
    monkeypatch.delenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", raising=False)
    monkeypatch.delenv("ENABLE_AUDIT_ER_GATE", raising=False)  # ER gate disabled
    calls = _wire(monkeypatch, fetch_one=lambda q, v: dict(_CONFLICT_ROW))

    out = asyncio.run(intake.upsert_audited_sku_to_index("m_wedge_demo", _audit_product()))

    assert out is None                       # orphan mint suppressed
    assert calls["catalog_execute"] == 0     # no catalog upsert
    assert calls["refresh"] is None          # no pdp refresh
    assert len(calls["enqueued"]) == 1       # routed to identity review
    assert calls["enqueued"][0]["matcher"] == "brand_host_fragmentation"
    assert calls["enqueued"][0]["evidence"]["conflict_merchant_id"] == "external_seed"


def test_guard_proceeds_when_no_conflict(monkeypatch):
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)  # guard runs unconditionally
    monkeypatch.delenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", raising=False)
    monkeypatch.delenv("ENABLE_AUDIT_ER_GATE", raising=False)
    calls = _wire(monkeypatch, fetch_one=lambda q, v: None)

    out = asyncio.run(intake.upsert_audited_sku_to_index("m_wedge_demo", _audit_product()))

    assert out                               # content_key returned (minted)
    assert calls["catalog_execute"] == 1     # catalog upsert happened
    assert calls["refresh"][1] == "url_audit_intake"
    assert calls["enqueued"] == []           # nothing routed to review


def test_guard_fails_open_on_db_error(monkeypatch):
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)  # guard runs unconditionally
    monkeypatch.delenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", raising=False)
    monkeypatch.delenv("ENABLE_AUDIT_ER_GATE", raising=False)

    def _boom(query, values):
        raise RuntimeError("db down")

    calls = _wire(monkeypatch, fetch_one=_boom)
    out = asyncio.run(intake.upsert_audited_sku_to_index("m_wedge_demo", _audit_product()))

    assert out                               # fail-open: seed still minted
    assert calls["catalog_execute"] == 1
    assert calls["enqueued"] == []


def test_exact_sku_align_bypasses_brand_guard(monkeypatch):
    """An exact SKU dedup ('align') is the correct outcome and must NOT be
    diverted by the brand guard."""
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)  # guard runs unconditionally
    calls = _wire(monkeypatch, fetch_one=lambda q, v: dict(_CONFLICT_ROW))

    async def _align(fields):
        return {"content_key": "ck_matched", "action": "align"}

    def _guard_must_not_run(merchant_id, fields):
        raise AssertionError("brand guard must not run after an exact align")

    monkeypatch.setattr(intake, "apply_audit_er_gate", _align)
    monkeypatch.setattr(intake, "apply_audit_brand_fragmentation_guard", _guard_must_not_run)

    out = asyncio.run(intake.upsert_audited_sku_to_index("m_wedge_demo", _audit_product()))
    assert out == "ck_matched"
    assert calls["catalog_execute"] == 1     # minted under the aligned key
