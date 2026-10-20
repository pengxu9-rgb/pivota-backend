"""CRAWL_FRESH through the REAL writers, on Postgres with migration 246's column present.

The hand-built rows in tests/test_crawl_fresh_trust_postgres.py pin the statement's own cases; these
run the producers that actually write drain offers, so B1 (cross-market), B2 (declared market) and
the employee-edit path are proved on what prod writes, not on a fixture's idea of it:

  * the ingestion offer writer (catalog_enrichment_agent.ingestion._build_offer_inserts) builds the
    rows, the drain's own stamp (retailer_ingest.pipeline.stamp_declared_market) marks the declared
    market, and the real apply (apply_ingest_plan(price_read=True)) dates the prices;
  * the attached-listing lane (external_offer_dual_write.sync_attached_listing_offers) re-prices the
    listing row as an employee edit or a refresh read;
  * the drain's own stage (retailer_ingest.pipeline.run_stage -> _check, where the declared-market
    stamp runs) applying onto Postgres, with and without an explicit options.market.

Shares tests/test_sku_identity_upserts_postgres's fixture (tables from the models, rows cleaned by PK).
"""
import json
from datetime import timedelta

import pytest

from tests.test_offer_upsert_price_read_postgres import _with_price_check
from tests.test_sku_identity_upserts_postgres import (  # noqa: F401 -- `db` is the shared fixture
    DEST,
    PK,
    _plan,
    _planned_sku,
    _seed_product,
    db,
    pytestmark,
)

DRAIN = "catalog_enrichment_agent_v1"
SG_DEST = "https://sg.brand.example/products/sku-identity-gate"


# The shared gate database: the harness creates external_product_seeds NARROW (no `id`), and a module
# that sorts after this one (test_priced_offer_gate_postgres) joins `s.id`. Widen it additively, the
# "create minimal, then widen" rule (#1651), so running first here never starves a neighbour.
_SEEDS_WIDEN = (
    ("id", "text"), ("external_product_id", "text"), ("attached_product_key", "text"), ("status", "text"),
    ("seed_kind", "text"), ("merchant_id", "text"), ("source", "text"), ("product_key", "text"),
    ("source_product_id", "text"), ("domain", "text"), ("title", "text"), ("seed_data", "jsonb"),
    ("created_at", "timestamptz"), ("updated_at", "timestamptz"),
)


@pytest.fixture(autouse=True)
async def _widen_shared_seeds(db):
    for name, coltype in _SEEDS_WIDEN:
        await db.execute(f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {name} {coltype}")


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch):
    from services import catalog_row_trust_upserter as up

    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    monkeypatch.delenv(up.CRAWL_FRESH_DAYS_ENV, raising=False)


def _offers(*, sku_key, market, currency, dest=DEST, price=19.0, options=None):
    """Plan offer rows from the ingestion writer, stamped by the drain's own helper."""
    from services.catalog_enrichment_agent.ingestion import _build_offer_inserts
    from services.retailer_ingest.pipeline import stamp_declared_market

    rows = _build_offer_inserts(
        product_key=PK, sku_key=sku_key, source_domain="brand.example", currency=currency, market=market,
        offers=[{"destination_url": dest, "canonical_url": dest, "price": price, "in_stock": True,
                 "merchant_inferred": "Brand Example"}],
    )
    plan = {"offers": rows}
    stamp_declared_market(plan, options)
    return rows


async def _apply(database, offers, *, price_read=True):
    import services.catalog_enrichment_agent.apply as apply_mod

    planned = _canonical_sku()
    for offer in offers:
        offer["sku_key"] = planned["sku_key"]
    counts = await apply_mod.apply_ingest_plan(
        _plan([planned], offers), batch_label="crawl-fresh-real", db=database, batch=False, price_read=price_read)
    assert counts["offers"] == len(offers)
    return planned["sku_key"]


def _canonical_sku():
    """The product-level row: the attached lane prices `<pk>::canonical` from the seed's own price."""
    return _planned_sku(sku_key=f"{PK}::canonical")


async def _fresh(database, now=None):
    from services import catalog_row_trust_upserter as up

    fresh = await up._crawl_fresh_by_key(database, [{"product_key": PK, "source_system": DRAIN}], now)
    return (fresh or {}).get(PK)


async def _setup(database):
    await _with_price_check(database)
    await _seed_product(database)


async def test_b2_a_declared_market_counts_and_a_defaulted_one_does_not(db):
    await _setup(db)
    sku = _canonical_sku()["sku_key"]
    # the job named no market: job_market() falls back to US, the stamp refuses to call that a declaration
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", options={}))
    payload = await db.fetch_val("SELECT offer_payload FROM catalog_offers WHERE product_key = :pk", {"pk": PK})
    assert "declared_market" not in (json.loads(payload) if isinstance(payload, str) else payload)
    assert await _fresh(db) is False
    # the same crawl from a job that declared US
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", options={"market": "US"}))
    assert await _fresh(db) is True


async def test_b1_a_fresh_sg_read_does_not_carry_a_stale_us_offer(db):
    await _setup(db)
    sku = _canonical_sku()["sku_key"]
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", options={"market": "US"})
                 + _offers(sku_key=sku, market="SG", currency="SGD", dest=SG_DEST, options={"market": "SG"}))
    assert await _fresh(db) is True
    # The US read ages to 60 days; the SG read stays 2h old.
    await db.execute("UPDATE catalog_offers SET price_checked_at = now() - interval '60 days'"
                     " WHERE product_key = :pk AND market = 'US'", {"pk": PK})
    await db.execute("UPDATE catalog_offers SET price_checked_at = now() - interval '2 hours'"
                     " WHERE product_key = :pk AND market = 'SG'", {"pk": PK})
    assert await _fresh(db) is False


async def test_an_employee_edit_through_the_real_lane_is_not_a_crawl_and_a_refresh_is(db, monkeypatch):
    from services import external_offer_dual_write as mod

    await _setup(db)
    sku = _canonical_sku()["sku_key"]
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", options={"market": "US"}))
    assert await _fresh(db) is True
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})
    seed = {"id": "seed:crawl-fresh-real", "attached_product_key": PK, "destination_url": DEST,
            "price_amount": 21.0, "price_currency": "USD", "market": "US",
            "seed_variants": None, "snapshot_variants": None, "variant_refresh_status": None}
    result = await mod.sync_attached_listing_offers(seed, source="employee_edit")
    assert result["status"] == "synced", result
    assert await _fresh(db) is False
    result = await mod.sync_attached_listing_offers({**seed, "price_amount": 22.0}, source="refresh",
                                                    currency_read=True)
    assert result["status"] == "synced", result
    assert await _fresh(db) is True


async def _offer_valid(database, now=None):
    """The offer-level VALID read (the upserter's own fragment) of the one listing offer."""
    from datetime import datetime, timezone

    from services import catalog_row_trust_upserter as up

    return await database.fetch_val(
        f"SELECT ({up.CRAWL_OFFER_VALID_READ_SQL}) IS NOT NULL FROM catalog_offers co WHERE co.product_key = :pk",
        {"pk": PK, "now": now or datetime.now(timezone.utc)})


async def test_l5_the_attached_lane_s_source_decides_validity_through_the_chain(db, monkeypatch):
    """L5, chained on the real writers: drain apply (declared) -> attached-lane write as
    `employee_edit` (that offer is not VALID, the row is not fresh) -> a drain write that READ no
    price keeps the marker with the stamp (#2658's keep arm), still not VALID -> attached-lane
    `refresh` (VALID again, fresh) -> a later `employee_edit` -> a drain write that READ the price
    drops the marker it no longer describes (VALID, fresh)."""
    from services import external_offer_dual_write as mod

    await _setup(db)
    sku = _canonical_sku()["sku_key"]
    us = {"market": "US"}
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", options=us))
    assert (await _offer_valid(db), await _fresh(db)) == (True, True)
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})
    seed = {"id": "seed:crawl-fresh-real", "attached_product_key": PK, "destination_url": DEST,
            "price_amount": 21.0, "price_currency": "USD", "market": "US",
            "seed_variants": None, "snapshot_variants": None, "variant_refresh_status": None}

    async def attached(source, price):
        result = await mod.sync_attached_listing_offers({**seed, "price_amount": price}, source=source,
                                                        currency_read=True)
        assert result["status"] == "synced", result
        return await db.fetch_val("SELECT offer_payload->>'price_read_source' FROM catalog_offers"
                                  " WHERE product_key = :pk", {"pk": PK})

    assert await attached("employee_edit", 21.0) == "employee_edit"
    assert (await _offer_valid(db), await _fresh(db)) == (False, False)
    # a drain write that read no price: the stored stamp and its source stand together
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", price=21.0, options=us), price_read=False)
    assert (await _offer_valid(db), await _fresh(db)) == (False, False)
    assert await attached("refresh", 22.0) == "refresh"
    assert (await _offer_valid(db), await _fresh(db)) == (True, True)
    assert await attached("employee_edit", 23.0) == "employee_edit"
    assert await _fresh(db) is False
    # a drain write that READ the price dated it itself: the old source no longer describes it
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", price=24.0, options=us))
    marker = await db.fetch_val("SELECT offer_payload->>'price_read_source' FROM catalog_offers"
                                " WHERE product_key = :pk", {"pk": PK})
    assert marker is None
    assert (await _offer_valid(db), await _fresh(db)) == (True, True)
    with pytest.raises(ValueError):
        await mod.sync_attached_listing_offers(seed, source="crawl", currency_read=True)


async def test_the_batch_clock_bounds_a_future_stamp(db):
    await _setup(db)
    sku = _canonical_sku()["sku_key"]
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", options={"market": "US"}))
    stamped = await db.fetch_val("SELECT price_checked_at FROM catalog_offers WHERE product_key = :pk", {"pk": PK})
    assert await _fresh(db, now=stamped - timedelta(seconds=1)) is False  # the stamp is after the batch clock
    assert await _fresh(db, now=stamped) is True
    assert await _fresh(db, now=(stamped + timedelta(days=7)).replace(tzinfo=None)) is True  # naive = UTC
    assert await _fresh(db, now=stamped + timedelta(days=7, seconds=1)) is False


# ---------------------------------------------------------------------------
# B2 through the drain itself: run_stage -> _check (where the stamp runs) ->
# the real apply onto Postgres. The state machine's ledger, crawl and readback
# are the pipeline suite's in-memory fixture (tests/services/
# test_retailer_ingest_pipeline `env`); only the catalog write is real.
# ---------------------------------------------------------------------------

from services.catalog_enrichment_agent import apply as _apply_mod  # noqa: E402
from tests.services.test_retailer_ingest_pipeline import env, job, run_flags  # noqa: E402,F401 -- fixture

_REAL_APPLY_INGEST_PLAN = _apply_mod.apply_ingest_plan  # `env` replaces it with a fake


async def _run_drain_apply(database, env, monkeypatch, **options):  # noqa: F811
    from services.retailer_ingest import pipeline

    seen = {}

    async def real_apply(plan, **kw):
        env.applied.append(plan)
        seen["price_read"] = kw.get("price_read")
        kw.update(db=database, primary_readiness=False)  # readiness is the fixture's to fake
        return await _REAL_APPLY_INGEST_PLAN(plan, **kw)

    async def group_ok(*args, **kwargs):
        return True  # product_group_members' prod shape is not this fixture's; not under test here

    monkeypatch.setattr(_apply_mod, "apply_ingest_plan", real_apply)
    monkeypatch.setattr(_apply_mod, "_ensure_primary_retailer_group", group_ok)
    await _with_price_check(database)
    try:
        out = await pipeline.run_stage(job("apply_due", **options), db=env.db)
        assert (out["status"], out["outcome"]) == ("done", "applied"), (out, run_flags(env))
        assert seen["price_read"] is True
        checks = env.ledger.runs[sorted(env.ledger.runs)[-1]]["checks"]
        keys = [p["product_key"] for p in env.applied[-1]["pdps"]]
        rows = await database.fetch_all(
            "SELECT product_key, market, offer_payload FROM catalog_offers WHERE product_key = ANY(:k)",
            {"k": keys})
    finally:
        # The gate shares one database: remove exactly the rows this apply wrote.
        keys = [p["product_key"] for plan in env.applied for p in plan["pdps"]]
        for table in ("catalog_offers", "catalog_skus", "catalog_products"):
            await database.execute(f"DELETE FROM {table} WHERE product_key = ANY(:k)", {"k": keys})
        await database.execute("DELETE FROM external_product_seeds WHERE attached_product_key = ANY(:k)",
                               {"k": keys})
    assert rows, "the drain apply wrote no offer"
    payloads = [json.loads(r["offer_payload"]) if isinstance(r["offer_payload"], str) else r["offer_payload"]
                for r in rows]
    return checks, [(r["market"], p) for r, p in zip(rows, payloads)]


async def test_b2_the_drain_s_check_stamps_an_explicit_market_on_what_it_writes(db, env, monkeypatch):  # noqa: F811
    checks, offers = await _run_drain_apply(db, env, monkeypatch, market="US")
    assert checks["declared_market_offers"] == len(offers)
    assert {(market, payload.get("declared_market")) for market, payload in offers} == {("US", "US")}


async def test_b2_the_drain_s_check_stamps_nothing_without_an_explicit_market(db, env, monkeypatch):  # noqa: F811
    checks, offers = await _run_drain_apply(db, env, monkeypatch)
    assert checks["declared_market_offers"] == 0
    assert {market for market, _ in offers} == {"US"}  # job_market()'s fallback, written but not declared
    assert all("declared_market" not in payload for _, payload in offers)
