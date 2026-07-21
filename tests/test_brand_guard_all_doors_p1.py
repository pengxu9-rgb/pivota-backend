"""Convergence P1.4 — ADR-008 brand-fragmentation guard on ALL intake doors.

Previously only the audit door prevented a same-brand+host orphan mint under a
different merchant. Covers:
  - shared guard: block_on_conflict=True → 'skip' (observed doors),
    False → 'flag' (first-party sync door) — both enqueue review;
  - audit wrapper keeps its original contract ('skip');
  - sync door: conflict FLAGS but the ingest PROCEEDS (first-party truth is
    never blocked), guard runs once per distinct brand per run;
  - fail-open on guard errors everywhere.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import services.audit_index_intake as intake  # noqa: E402


_CONFLICT = {
    "product_key": "prod::merch_other::external_seed::x",
    "merchant_id": "merch_other",
    "content_key": "ck_conflict",
}


def _fields() -> Dict[str, Any]:
    return {
        "product_key": "prod::merch_new::url_audit::y",
        "brand": "TestBrand",
        "source_domain": "brand.example",
        "canonical_url": "https://brand.example/p/y",
        "content_key": "ck_new",
    }


@pytest.fixture()
def _conflicting(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    reviews: List[Dict[str, Any]] = []

    async def fake_conflict(merchant_id, fields):
        return dict(_CONFLICT)

    async def fake_review(fields, match):
        reviews.append({"fields": fields, "match": match})

    monkeypatch.setattr(intake, "_existing_brand_canonical_conflict", fake_conflict)
    monkeypatch.setattr(intake, "enqueue_audit_identity_review", fake_review)
    return reviews


@pytest.mark.asyncio
async def test_block_mode_skips_and_enqueues_review(_conflicting):
    out = await intake.apply_intake_brand_fragmentation_guard(
        "merch_new", _fields(), door="external_seed_mirror", block_on_conflict=True
    )
    assert out["action"] == "skip"
    assert out["conflict_merchant_id"] == "merch_other"
    assert len(_conflicting) == 1
    assert _conflicting[0]["match"]["evidence"]["door"] == "external_seed_mirror"


@pytest.mark.asyncio
async def test_flag_mode_proceeds_and_enqueues_review(_conflicting):
    out = await intake.apply_intake_brand_fragmentation_guard(
        "merch_new", _fields(), door="catalog_sync", block_on_conflict=False
    )
    assert out["action"] == "flag"  # NOT skip — first-party truth proceeds
    assert len(_conflicting) == 1
    assert _conflicting[0]["match"]["evidence"]["door"] == "catalog_sync"


@pytest.mark.asyncio
async def test_audit_wrapper_contract_unchanged(_conflicting):
    out = await intake.apply_audit_brand_fragmentation_guard("merch_new", _fields())
    assert out["action"] == "skip"
    assert _conflicting[0]["match"]["evidence"]["door"] == "url_audit_intake"


@pytest.mark.asyncio
async def test_fail_open_on_lookup_error(monkeypatch: pytest.MonkeyPatch):
    async def boom(merchant_id, fields):
        raise RuntimeError("db down")

    monkeypatch.setattr(intake, "_existing_brand_canonical_conflict", boom)

    out = await intake.apply_intake_brand_fragmentation_guard(
        "merch_new", _fields(), door="external_seed_mirror", block_on_conflict=True
    )
    assert out["action"] == "proceed"
    assert out["reason"] == "error"


@pytest.mark.asyncio
async def test_no_conflict_proceeds(monkeypatch: pytest.MonkeyPatch):
    async def none_conflict(merchant_id, fields):
        return None

    monkeypatch.setattr(intake, "_existing_brand_canonical_conflict", none_conflict)

    out = await intake.apply_intake_brand_fragmentation_guard(
        "merch_new", _fields(), door="catalog_sync", block_on_conflict=False
    )
    assert out["action"] == "proceed"


@pytest.mark.asyncio
async def test_sync_door_flags_once_per_brand_and_never_blocks(monkeypatch: pytest.MonkeyPatch):
    """Integration-shaped: ingest_standard_products with two products of the
    SAME conflicting brand → both rows ingest (first-party never blocked),
    guard consulted exactly once."""
    import services.catalog_sync_service as css

    guard_calls: List[Dict[str, Any]] = []

    async def fake_guard(merchant_id, fields, *, door, block_on_conflict):
        guard_calls.append({"door": door, "block": block_on_conflict, "brand": fields.get("brand")})
        return {"action": "flag", "conflict_merchant_id": "merch_other"}

    monkeypatch.setattr(intake, "apply_intake_brand_fragmentation_guard", fake_guard)

    # No live DB in unit tests: fake the module-level database (transaction +
    # noop I/O) and the row writers, mirroring the repo's catalog-sync test
    # conventions (stub database/_upsert_by_pk, drive the real function).
    class _Tx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeDb:
        def transaction(self):
            return _Tx()

        async def execute(self, *a, **kw):
            return None

        async def execute_many(self, *a, **kw):
            return None

        async def fetch_one(self, *a, **kw):
            return None

        async def fetch_all(self, *a, **kw):
            return []

    async def _noop_upsert(table, pk_name, values):
        return None

    async def _noop_merchant(**kwargs):
        return None

    fake_db = _FakeDb()
    monkeypatch.setattr(css, "database", fake_db)
    monkeypatch.setattr(css, "_upsert_by_pk", _noop_upsert)
    monkeypatch.setattr(css, "upsert_catalog_merchant", _noop_merchant)
    # Collaborators deep in the ingest pipeline import the db singleton
    # directly — neutralize it globally for this test.
    from db.database import database as real_db

    monkeypatch.setattr(real_db, "execute", fake_db.execute)
    monkeypatch.setattr(real_db, "execute_many", fake_db.execute_many)
    monkeypatch.setattr(real_db, "fetch_one", fake_db.fetch_one)
    monkeypatch.setattr(real_db, "fetch_all", fake_db.fetch_all)
    monkeypatch.setattr(real_db, "transaction", lambda *a, **kw: _Tx())

    payloads = [
        {
            "id": f"p{i}",
            "merchant_id": "merch_sync",
            "platform": "shopify",
            "title": f"Product {i}",
            "vendor": "TestBrand",
            "price": 10.0,
            "currency": "USD",
        }
        for i in range(2)
    ]

    result = await css.ingest_standard_products(
        merchant_id="merch_sync",
        platform="shopify",
        product_payloads=payloads,
        source_system="universal_product_sync",
        source_ref="test",
        source_domain="brand.example",
    )

    assert len(guard_calls) == 1  # once per distinct brand per run
    assert guard_calls[0] == {"door": "catalog_sync", "block": False, "brand": "TestBrand"}
    assert result["brand_conflicts_flagged"] == 1
    # first-party rows were NOT blocked by the flag
    assert result["products_ingested"] == 2
