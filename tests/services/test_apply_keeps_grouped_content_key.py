"""M3: with INTAKE_IDENTITY_ENGINE_MOVE_GUARD on, the crawl writer never writes a planned content_key over
the stored one of a row the identity engine moved, unless the resolver chose it. With the flag off it is
origin/main's writer: no lookup, the plan's key written.

With ENABLE_INTAKE_IDENTITY_ENRICHMENT off, `_PDP_UPSERT_SQL`'s `COALESCE(EXCLUDED.content_key, stored)`
would write the plan's make_content_key(brand, title[, gtin]) back over a listing the identity engine
moved: the listing snaps back onto its old family while its membership stays in the new group. Both
executors run `_keep_grouped_content_keys` before writing (the SQL itself is exercised on Postgres in
tests/test_identity_merge_hardening_postgres.py); with the gate on the resolver's key is written as is.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services import intake_identity as identity
from services.catalog_enrichment_agent import apply as writer
from tests.services.test_partial_apply_skip_reasons import CatalogDB, two_product_plan

STORED_CK = "ck_" + "b" * 32


@pytest.fixture(autouse=True)
def later_stages(monkeypatch):
    """Stages after the PDP upsert read the process pool; they are not what these tests are about
    (the same stubs as test_partial_apply_skip_reasons.real_resolver)."""
    async def accept_live_offers(offers, *, require_live_links=False):
        return offers, {}, []

    monkeypatch.setattr(writer, "write_writer_audit_log", AsyncMock())
    monkeypatch.setattr(writer, "guard_catalog_offer_rows", AsyncMock(side_effect=accept_live_offers))
    monkeypatch.setattr(writer, "_derive_seed_seller_for_plan_row", AsyncMock(return_value=("seller", "cross")))
    monkeypatch.setattr(writer, "_apply_inci_rows", AsyncMock(return_value={}))


class GroupedDB(CatalogDB):
    """CatalogDB whose first planned product is stored, grouped, on STORED_CK."""

    def __init__(self, grouped_key):
        super().__init__()
        self.grouped_key = grouped_key
        self.written: dict = {}
        self.grouped_reads = 0

    async def fetch_all(self, query, values=None):
        if query is writer._GROUPED_STORED_CONTENT_KEY_SQL:
            self.grouped_reads += 1
            return [{"product_key": self.grouped_key, "content_key": STORED_CK}]
        return []

    async def execute(self, query, values=None):
        values = values or {}
        if "INSERT INTO catalog_products" in query:
            for k, v in values.items():
                if k == "product_key" or k.startswith("product_key__"):
                    suffix = k[len("product_key"):]
                    self.written[v] = values["content_key" + suffix]
        return await super().execute(query, values)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_gate_off_keeps_a_grouped_rows_stored_content_key(monkeypatch, batch):
    monkeypatch.delenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", raising=False)
    monkeypatch.setenv(identity.ENGINE_MOVE_GUARD_ENV, "1")
    plan = two_product_plan()
    grouped, other = (p["product_key"] for p in plan["pdps"])
    planned_other = plan["pdps"][1]["content_key"]
    assert plan["pdps"][0]["content_key"] != STORED_CK
    db = GroupedDB(grouped)
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch)
    assert db.grouped_reads == 1
    assert db.written == {grouped: STORED_CK, other: planned_other}
    assert counts["pdp_content_key_kept_for_grouped_row"] == 1


@pytest.mark.asyncio
async def test_gate_on_writes_the_resolvers_key_and_skips_the_lookup(monkeypatch):
    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", "1")
    monkeypatch.setenv(identity.ENGINE_MOVE_GUARD_ENV, "1")
    plan = two_product_plan()
    keys = [p["product_key"] for p in plan["pdps"]]

    async def resolve(**kw):
        return {"content_key": "ck_resolver", "product_group_id": "pg_resolver", "action": "ATTACH",
                "gtin": None, "evidence": {"matcher": "content_key", "evidence": {}}, "attach": None}

    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", AsyncMock(side_effect=resolve))
    db = GroupedDB(keys[0])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db)
    assert db.grouped_reads == 0
    assert db.written == {k: "ck_resolver" for k in keys}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_parity_flag_off_writes_the_plan_and_reads_nothing(monkeypatch, batch):
    monkeypatch.delenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", raising=False)
    monkeypatch.delenv(identity.ENGINE_MOVE_GUARD_ENV, raising=False)
    plan = two_product_plan()
    planned = {p["product_key"]: p["content_key"] for p in plan["pdps"]}
    db = GroupedDB(next(iter(planned)))
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch)
    assert db.grouped_reads == 0 and db.written == planned
    assert "pdp_content_key_kept_for_grouped_row" not in counts


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_an_unverified_kept_listing_is_written_without_failing_the_store(monkeypatch, batch):
    """The resolver kept an engine-moved listing's stored key but could not read its group: the writer
    writes it, keeps its existing membership (no group target), and the store's apply stays complete."""
    from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan, require_primary_apply

    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", "1")
    monkeypatch.setenv(identity.ENGINE_MOVE_GUARD_ENV, "1")
    plan = two_product_plan()
    kept_key, other_key = (p["product_key"] for p in plan["pdps"])
    assert kept_key.startswith("ext:retailer:")

    async def resolve(**kw):
        pk = kw["merchant_ctx"]["product_key"]
        if pk == kept_key:
            return {"content_key": STORED_CK, "product_group_id": None, "action": "ATTACH", "gtin": None,
                    "evidence": {"matcher": identity.MATCHER_ENGINE_MOVED_UNVERIFIED,
                                 "evidence": {"unverified": "group_unreadable"}}, "attach": None}
        return {"content_key": "ck_resolver", "product_group_id": "pg_resolver", "action": "ATTACH",
                "gtin": None, "evidence": {"matcher": "content_key", "evidence": {}}, "attach": None}

    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", AsyncMock(side_effect=resolve))
    db = GroupedDB(kept_key)
    db.groups[(plan["pdps"][0]["merchant_id"], plan["pdps"][0]["platform"],
               plan["pdps"][0]["source_product_id"])] = "pg_existing"
    report = inspect_primary_plan(plan)
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch)
    assert db.written == {kept_key: STORED_CK, other_key: "ck_resolver"}
    assert counts["product_groups_failed"] == 0 and counts["pdps_skipped_identity"] == 0
    assert require_primary_apply(report, counts)["status"] == "applied"
