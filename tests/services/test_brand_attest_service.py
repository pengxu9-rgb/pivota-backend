"""P1 — brand attestation service: maps to product_enrichment (the AGENT-READ
overlay), gated on claimed, tenant-scoped."""

import asyncio

import services.brand_attest_service as attest
from services.brand_attest_service import attestation_to_enrichment


def test_attestation_to_enrichment_maps_only_served_fields():
    out = attestation_to_enrichment(
        {
            "title": "Anua Heartleaf Toner",
            "description": "Soothing toner",
            "bullet_points": ["77% heartleaf"],  # now served -> maps
            "usage_scenarios": ["AM/PM after cleansing"],  # now served -> maps
            "audience_tags": ["sensitive skin"],  # not yet served -> dropped
            "unknown_field": "ignored",
            "summary": "",  # empty -> dropped
        }
    )
    assert out["title_override"] == "Anua Heartleaf Toner"
    assert out["description_markdown"] == "Soothing toner"
    assert out["bullet_points"] == ["77% heartleaf"]
    assert out["usage_scenarios"] == ["AM/PM after cleansing"]
    # Only the fields the assembler actually serves map through; not-yet-served,
    # unknown, and empty fields are dropped so the API doesn't promise serving
    # it can't deliver.
    assert "audience_tags" not in out
    assert "unknown_field" not in out and "summary_short" not in out
    assert out["updated_by_employee_id"] == "brand_attestation"
    assert attestation_to_enrichment({}) == {}  # nothing -> no provenance stamp


def _row(claim_state="claimed", merchant="m1"):
    return {
        "merchant_id": merchant,
        "platform": "url_audit",
        "source_product_id": "anua.com/p/x",
        "content_key": "ck_x",
        "claim_state": claim_state,
        "product_key": "m1|url_audit|anua.com/p/x",
    }


def _wire(monkeypatch, row):
    import db.database
    import db.product_enrichment
    import services.agent_pdp_view_assembler as asm

    calls = {"enrich": None, "advanced": False, "refreshed": False}

    async def _fetch_one(query):
        return row

    async def _upsert(mid, plat, spid, geo, data):
        calls["enrich"] = (mid, plat, spid, data)

    async def _advance(*, merchant_id, product_key):
        calls["advanced"] = True
        return True

    async def _refresh(ck, *, refresh_source, db=None):
        calls["refreshed"] = True
        return True

    monkeypatch.setattr(db.database.database, "fetch_one", _fetch_one)
    monkeypatch.setattr(db.product_enrichment, "upsert_enrichment", _upsert)
    monkeypatch.setattr(attest, "advance_product_to_attested", _advance)
    monkeypatch.setattr(asm, "refresh_agent_pdp_view_for_content_key", _refresh)
    return calls


def test_attest_claimed_writes_served_overlay(monkeypatch):
    calls = _wire(monkeypatch, _row(claim_state="claimed"))
    res = asyncio.run(
        attest.attest_product(
            merchant_id="m1",
            product_key="m1|url_audit|anua.com/p/x",
            fields={"description": "Brand copy", "title": "Anua Toner"},
        )
    )
    assert res["status"] == "attested"
    # written to product_enrichment (the served, agent-read overlay):
    assert calls["enrich"][3]["description_markdown"] == "Brand copy"
    assert calls["enrich"][3]["title_override"] == "Anua Toner"
    assert calls["advanced"] is True  # claim_state -> attested
    assert calls["refreshed"] is True  # served PDP refreshed so agents see it


def test_attest_blocked_when_unclaimed(monkeypatch):
    calls = _wire(monkeypatch, _row(claim_state="unclaimed"))
    res = asyncio.run(
        attest.attest_product(
            merchant_id="m1", product_key="pk", fields={"description": "x"}
        )
    )
    assert res["status"] == "not_claimed"
    assert calls["enrich"] is None  # gate: nothing reaches the served overlay


def test_attest_cross_tenant_not_found(monkeypatch):
    calls = _wire(monkeypatch, _row(merchant="OTHER", claim_state="claimed"))
    res = asyncio.run(
        attest.attest_product(
            merchant_id="m1", product_key="pk", fields={"description": "x"}
        )
    )
    assert res["status"] == "not_found"
    assert calls["enrich"] is None


def test_attest_no_known_fields(monkeypatch):
    calls = _wire(monkeypatch, _row(claim_state="claimed"))
    res = asyncio.run(
        attest.attest_product(
            merchant_id="m1", product_key="pk", fields={"unknown": "x"}
        )
    )
    assert res["status"] == "no_attestable_fields"
    assert calls["enrich"] is None
