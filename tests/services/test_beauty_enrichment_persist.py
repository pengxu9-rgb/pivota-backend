from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import beauty_enrichment_persist as persist


class FakeDB:
    def __init__(self, cp=None, skus=None, profile=None):
        self.cp = cp
        self.skus = skus or []
        self.profile = profile
        self.executed = []
        self.is_connected = True

    async def fetch_one(self, query, params=None):
        if "FROM catalog_products" in query:
            return dict(self.cp) if self.cp else None
        if "FROM beauty_product_profiles" in query:
            return dict(self.profile) if self.profile else None
        return None

    async def fetch_all(self, query, params=None):
        if "beauty_sku_ingredients" in query:
            return [dict(s) for s in self.skus]
        return []

    async def execute(self, query, params=None):
        self.executed.append((query, params))


def _patch_recompute(monkeypatch, calls):
    async def _fake(content_key, *, reason=None):
        calls.append((content_key, reason))
        return True
    monkeypatch.setattr(persist, "recompute_serving_eligibility", _fake)


_CP = {
    "content_key": "ck1",
    "merchant_id": "merch_test",
    "title": "Centella Hydrating Serum",
    "description": "A soothing serum for dry, sensitive skin with niacinamide.",
    "product_type": "Serum",
    "category_path": "beauty/skincare/serum",
    "category_kind": "skincare",
}


def test_fills_empty_concerns_and_actives_and_recomputes(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp=_CP,
        skus=[{"sku_key": "s1", "raw_inci": "Water, Niacinamide, Centella Asiatica Extract",
               "active_ingredients_json": [], "concentration_notes_json": None,
               "source_system": "shopify_products_sync"}],
        profile={"concerns_json": []},
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db))

    assert res["status"] == "ok"
    assert "Niacinamide" in res["derived"]["active_ingredients"]
    assert res["derived"]["active_source"] == "inci"
    assert res["written"]["concerns"] is True
    assert res["written"]["actives_skus"] == ["s1"]
    assert res["serving_eligible"] is True
    # one profile insert + one sku update were issued, and recompute fired once.
    assert any("INSERT INTO beauty_product_profiles" in q for q, _ in db.executed)
    assert any("UPDATE beauty_sku_ingredients" in q for q, _ in db.executed)
    assert calls == [("ck1", "beauty_enrichment")]
    # The profile INSERT must carry merchant_id (the column is NOT NULL).
    profile_write = next(p for q, p in db.executed if "INSERT INTO beauty_product_profiles" in q)
    assert profile_write["mid"] == "merch_test"


def test_concerns_skipped_when_merchant_id_missing(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp={**_CP, "merchant_id": None},  # merchant-scoped table can't take a null
        skus=[],
        profile={"concerns_json": []},
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db))
    assert res["written"]["concerns"] is False
    assert not any("INSERT INTO beauty_product_profiles" in q for q, _ in db.executed)


def test_never_overwrites_existing_or_merchant_owned(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp=_CP,
        skus=[
            {"sku_key": "s1", "raw_inci": "Water, Niacinamide",
             "active_ingredients_json": [{"label": "Pre-existing"}],  # already populated -> skip
             "concentration_notes_json": None, "source_system": "shopify_products_sync"},
            {"sku_key": "s2", "raw_inci": "Water, Retinol",
             "active_ingredients_json": [],  # empty BUT merchant-owned -> hands off
             "concentration_notes_json": None, "source_system": "merchant_authored"},
        ],
        # existing concerns + authored evidence -> both skipped (never overwrite).
        profile={
            "concerns_json": ["dryness"],
            "evidence_profile": {
                "claims": [{"claim_text": "Authored", "source_ref": "x",
                            "substantiation_status": "substantiated"}],
                "review_state": "reviewed",
            },
        },
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db))

    assert res["written"]["concerns"] is False
    assert res["written"]["actives_skus"] == []
    assert res["written"]["evidence_claims"] is False
    assert res["serving_eligible"] is None  # nothing written -> no recompute
    assert db.executed == []
    assert calls == []


def test_dry_run_writes_nothing_but_reports_would_write(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp=_CP,
        skus=[{"sku_key": "s1", "raw_inci": "Water, Niacinamide",
               "active_ingredients_json": [], "concentration_notes_json": None,
               "source_system": "shopify_products_sync"}],
        profile={"concerns_json": []},
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db, dry_run=True))

    assert res["dry_run"] is True
    assert res["written"]["concerns"] is True       # would write
    assert res["written"]["actives_skus"] == ["s1"]  # would write
    assert db.executed == []                          # but wrote nothing
    assert calls == []                                # and did not recompute


def test_missing_product_returns_not_found(monkeypatch):
    _patch_recompute(monkeypatch, [])
    db = FakeDB(cp=None)
    res = asyncio.run(persist.enrich_and_persist_product("ghost", db=db))
    assert res["status"] == "not_found"


def test_enrich_skips_non_beauty_early(monkeypatch):
    _patch_recompute(monkeypatch, [])
    db = FakeDB(cp={**_CP, "category_kind": None})
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db))
    assert res["status"] == "skipped_non_beauty"
    assert db.executed == []  # no sku/profile reads or writes


def test_maybe_enrich_disabled_by_default(monkeypatch):
    monkeypatch.delenv(persist.AUTO_ENRICHMENT_FLAG, raising=False)
    called = []

    async def _fake(pk, *, db=None):
        called.append(pk)
        return {}

    monkeypatch.setattr(persist, "enrich_and_persist_product", _fake)
    res = asyncio.run(persist.maybe_enrich_synced_product("m", "shopify", "123"))
    assert res is None and called == []  # flag off -> never touches enrichment


def test_maybe_enrich_enabled_builds_key_and_calls(monkeypatch):
    monkeypatch.setenv(persist.AUTO_ENRICHMENT_FLAG, "true")
    called = []

    async def _fake(pk, *, db=None):
        called.append(pk)
        return {"status": "ok", "product_key": pk}

    monkeypatch.setattr(persist, "enrich_and_persist_product", _fake)
    res = asyncio.run(persist.maybe_enrich_synced_product("merch_x", "shopify", "999"))
    assert called == ["prod::merch_x::shopify::999"]  # canonical product_key
    assert res["status"] == "ok"


def test_maybe_enrich_swallows_errors(monkeypatch):
    monkeypatch.setenv(persist.AUTO_ENRICHMENT_FLAG, "true")

    async def _boom(pk, *, db=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(persist, "enrich_and_persist_product", _boom)
    # Best-effort: never propagates -> returns None so it can't break the sync.
    assert asyncio.run(persist.maybe_enrich_synced_product("m", "shopify", "1")) is None


def test_backfill_drive_aggregates(monkeypatch):
    from scripts import run_beauty_enrichment_backfill as runner

    async def _fake_persist(pk, *, db=None, dry_run=False):
        if pk == "pk_fill":
            return {"product_key": pk, "status": "ok",
                    "written": {"concerns": True, "actives_skus": ["s1"]},
                    "serving_eligible": True}
        return {"product_key": pk, "status": "ok",
                "written": {"concerns": False, "actives_skus": []},
                "serving_eligible": None}

    monkeypatch.setattr(runner, "enrich_and_persist_product", _fake_persist)
    fake_db = types.SimpleNamespace(is_connected=True)
    args = types.SimpleNamespace(
        product_keys=["pk_fill", "pk_noop"], product_keys_file=None,
        merchant=None, category=None, limit=10, dry_run=False,
    )
    report = asyncio.run(runner._drive(args, db=fake_db))
    assert report["n"] == 2 and report["ok"] == 2
    assert report["concerns_filled"] == 1
    assert report["actives_filled"] == 1
    assert report["serving_eligible_after"] == 1


# --- (B) INCI -> substantiated claims for the `justify` dimension --------------

def test_inci_actives_emit_substantiated_claims_into_evidence(monkeypatch):
    import json as _json
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp=_CP,
        skus=[{"sku_key": "s1",
               "raw_inci": "Water, Niacinamide, Centella Asiatica Extract",
               "active_ingredients_json": [], "concentration_notes_json": None,
               "source_system": "shopify_products_sync"}],
        profile={"concerns_json": [], "evidence_profile": None},
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db))

    assert any("Niacinamide" in c for c in res["derived"]["substantiated_claims"])
    assert res["written"]["evidence_claims"] is True
    ev_writes = [p for q, p in db.executed if "evidence_profile" in q]
    assert ev_writes, "expected an evidence_profile write"
    assert ev_writes[0]["mid"] == "merch_test"  # merchant-scoped row
    payload = _json.loads(ev_writes[0]["evidence"])
    assert payload["claims"][0]["substantiation_status"] == "substantiated"
    assert payload["claims"][0]["source_ref"] == "INCI"


def test_text_source_actives_do_not_earn_substantiated_claims(monkeypatch):
    """No INCI -> actives from text -> NOT substantiated -> no evidence write."""
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp=_CP,  # description mentions niacinamide
        skus=[{"sku_key": "s1", "raw_inci": None,
               "active_ingredients_json": [], "concentration_notes_json": None,
               "source_system": "shopify_products_sync"}],
        profile={"concerns_json": [], "evidence_profile": None},
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db))

    assert res["derived"]["active_source"] == "text"
    assert res["derived"]["substantiated_claims"] == []
    assert res["written"]["evidence_claims"] is False
    assert not any("evidence_profile" in q for q, _ in db.executed)


def test_evidence_skipped_when_merchant_id_missing(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp={**_CP, "merchant_id": None},
        skus=[{"sku_key": "s1", "raw_inci": "Water, Niacinamide",
               "active_ingredients_json": [], "concentration_notes_json": None,
               "source_system": "shopify_products_sync"}],
        profile={"concerns_json": [], "evidence_profile": None},
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db))
    assert res["written"]["evidence_claims"] is False
    assert not any("evidence_profile" in q for q, _ in db.executed)


def test_dry_run_reports_evidence_without_writing(monkeypatch):
    calls = []
    _patch_recompute(monkeypatch, calls)
    db = FakeDB(
        cp=_CP,
        skus=[{"sku_key": "s1", "raw_inci": "Water, Niacinamide",
               "active_ingredients_json": [], "concentration_notes_json": None,
               "source_system": "shopify_products_sync"}],
        profile={"concerns_json": [], "evidence_profile": None},
    )
    res = asyncio.run(persist.enrich_and_persist_product("pk1", db=db, dry_run=True))
    assert res["written"]["evidence_claims"] is True
    assert db.executed == []  # nothing written under dry_run


def test_inci_claims_helper_is_strict_on_source():
    actives = [
        {"label": "Retinol", "source": "inci"},
        {"label": "Niacinamide", "source": "text"},  # excluded (not verified)
        {"label": "Retinol", "source": "inci"},  # dup -> deduped
        {"label": "", "source": "inci"},  # empty -> skipped
    ]
    claims = persist._inci_substantiated_claims(actives)
    assert [c["claim_text"] for c in claims] == ["Contains Retinol"]
    assert claims[0]["substantiation_status"] == "substantiated"
