"""CRAWL_FRESH through the REAL writers, on Postgres with migration 246's column present.

The hand-built rows in tests/test_crawl_fresh_trust_postgres.py pin the statement's own cases; these
run the producers that actually write drain offers, so B1 (cross-market), B2 (declared market) and
the employee-edit path are proved on what prod writes, not on a fixture's idea of it:

  * the ingestion offer writer (catalog_enrichment_agent.ingestion._build_offer_inserts) builds the
    rows, the drain's own stamp (retailer_ingest.pipeline.stamp_declared_market) marks the declared
    market, and the real apply (apply_ingest_plan(price_read=True)) dates the prices;
  * the attached-listing lane (external_offer_dual_write.sync_attached_listing_offers) re-prices the
    listing row as an employee edit or a refresh read.

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


async def test_the_batch_clock_bounds_a_future_stamp(db):
    await _setup(db)
    sku = _canonical_sku()["sku_key"]
    await _apply(db, _offers(sku_key=sku, market="US", currency="USD", options={"market": "US"}))
    stamped = await db.fetch_val("SELECT price_checked_at FROM catalog_offers WHERE product_key = :pk", {"pk": PK})
    assert await _fresh(db, now=stamped - timedelta(seconds=1)) is False  # the stamp is after the batch clock
    assert await _fresh(db, now=stamped) is True
    assert await _fresh(db, now=(stamped + timedelta(days=7)).replace(tzinfo=None)) is True  # naive = UTC
    assert await _fresh(db, now=stamped + timedelta(days=7, seconds=1)) is False
