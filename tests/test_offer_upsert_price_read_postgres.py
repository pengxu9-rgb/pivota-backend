"""The ingest offer upsert dates a price only when its caller read it (migration 246).

`apply_ingest_plan(price_read=True)` -- the retailer_ingest drain's storefront crawl, which reads every
offer price from the store in the same execution -- stamps `catalog_offers.price_checked_at = NOW()`.
Every other caller keeps the pre-246 statement, and the 246 trigger keeps clearing the stamp of a price
that moves without a read. EXECUTED against Postgres with the migration's own trigger: the stamp, the
trigger and the reviewed-price hold only interact in a real database.

Shares tests/test_sku_identity_upserts_postgres's fixture (tables from the models, rows cleaned by PK).
"""
import glob
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_sku_identity_upserts_postgres import (  # noqa: F401 -- `db` is the shared fixture
    PK,
    _plan,
    _planned_offer,
    _planned_sku,
    _seed_product,
    db,
    pytestmark,
)

HOLD_PK = "ext:jungsaemmool-lip-pression-metal-serum-gloss::66a3c8a4"


async def _with_price_check(database):
    """The column and the migration's own trigger, from the file, as schema_guard installs them in prod."""
    from db.sql_migrations import split_statements

    body = open(glob.glob("db/migrations/246_catalog_offers_price_checked_at.sql")[0]).read()
    for statement in split_statements(body):
        await database.execute(statement)


async def _apply(plan, *, batch, price_read):
    import services.catalog_enrichment_agent.apply as apply_mod
    from db.database import database

    return await apply_mod.apply_ingest_plan(
        plan, batch_label="price-read-gate", db=database, batch=batch, price_read=price_read)


async def _offer(database, offer_id):
    return await database.fetch_one(
        "SELECT list_price, price_checked_at FROM catalog_offers WHERE offer_id = :id", {"id": offer_id})


def _recent(stamp):
    return stamp is not None and abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=5)


async def _write(database, *, batch, price_read, price=19.0):
    planned = _planned_sku()
    offer = _planned_offer(sku_key=planned["sku_key"], price=price)
    counts = await _apply(_plan([planned], [offer]), batch=batch, price_read=price_read)
    assert counts["offers"] == 1
    return offer["offer_id"]


@pytest.mark.parametrize("batch", [False, True])
async def test_a_vouched_read_dates_a_new_offer_and_an_unvouched_one_does_not(db, batch):
    await _with_price_check(db)
    await _seed_product(db)
    offer_id = await _write(db, batch=batch, price_read=False)
    assert (await _offer(db, offer_id))["price_checked_at"] is None
    await db.execute("DELETE FROM catalog_offers WHERE product_key = :pk", {"pk": PK})
    offer_id = await _write(db, batch=batch, price_read=True)
    assert _recent((await _offer(db, offer_id))["price_checked_at"])


@pytest.mark.parametrize("batch", [False, True])
async def test_a_vouched_re_read_re_dates_the_price_whether_or_not_it_moved(db, batch):
    await _with_price_check(db)
    await _seed_product(db)
    offer_id = await _write(db, batch=batch, price_read=True)
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    await db.execute("UPDATE catalog_offers SET price_checked_at = :t WHERE offer_id = :id", {"t": old, "id": offer_id})
    await _write(db, batch=batch, price_read=True)  # same price
    assert _recent((await _offer(db, offer_id))["price_checked_at"])
    await db.execute("UPDATE catalog_offers SET price_checked_at = :t WHERE offer_id = :id", {"t": old, "id": offer_id})
    await _write(db, batch=batch, price_read=True, price=21.0)  # moved
    row = await _offer(db, offer_id)
    assert float(row["list_price"]) == 21.0 and _recent(row["price_checked_at"])


@pytest.mark.parametrize("batch", [False, True])
async def test_an_unvouched_write_keeps_a_read_of_the_same_price_and_clears_it_when_the_price_moves(db, batch):
    await _with_price_check(db)
    await _seed_product(db)
    offer_id = await _write(db, batch=batch, price_read=True)
    stamped = (await _offer(db, offer_id))["price_checked_at"]
    await _write(db, batch=batch, price_read=False)  # same price: the read still dates it
    assert (await _offer(db, offer_id))["price_checked_at"] == stamped
    await _write(db, batch=batch, price_read=False, price=23.0)  # moved without a read
    assert (await _offer(db, offer_id))["price_checked_at"] is None


async def _source(database, offer_id):
    return await database.fetch_val(
        "SELECT offer_payload->>'price_read_source' FROM catalog_offers WHERE offer_id = :id", {"id": offer_id})


@pytest.mark.parametrize("batch", [False, True])
async def test_a_write_that_read_no_price_keeps_who_vouched_and_a_read_drops_it(db, batch):
    """offer_payload.price_read_source (the attached lane's marker) belongs to the stored stamp: a plan that
    read no price leaves both standing; a crawl that read the price dates it itself, so the marker goes."""
    await _with_price_check(db)
    await _seed_product(db)
    offer_id = await _write(db, batch=batch, price_read=True)
    await db.execute(
        "UPDATE catalog_offers SET offer_payload = jsonb_set(offer_payload, '{price_read_source}',"
        " to_jsonb(CAST('employee_edit' AS text))) WHERE offer_id = :id", {"id": offer_id})
    await _write(db, batch=batch, price_read=False)
    assert await _source(db, offer_id) == "employee_edit"
    await _write(db, batch=batch, price_read=False, price=24.0)  # still no read: the marker stays
    assert await _source(db, offer_id) == "employee_edit"
    await _write(db, batch=batch, price_read=True)
    assert await _source(db, offer_id) is None
    assert _recent((await _offer(db, offer_id))["price_checked_at"])


async def test_without_the_column_a_vouched_apply_writes_the_pre_246_statement(db, monkeypatch):
    """A database that has not healed the column yet: the vouch is dropped, the offer is still written."""
    import services.catalog_enrichment_agent.apply as apply_mod

    await _with_price_check(db)
    await _seed_product(db)

    async def absent(_database):
        return False

    monkeypatch.setattr(apply_mod, "_offer_price_check_present", absent)
    executed = []
    real_execute = db.execute

    async def spy(query, values=None):
        executed.append(str(query))
        return await real_execute(query, values)

    monkeypatch.setattr(db, "execute", spy)
    offer_id = await _write(db, batch=False, price_read=True)
    assert (await _offer(db, offer_id))["price_checked_at"] is None
    offer_sql = [q for q in executed if "INSERT INTO catalog_offers" in q]
    assert offer_sql and all("price_checked_at" not in q for q in offer_sql)


async def test_the_column_check_reads_this_database(db):
    import services.catalog_enrichment_agent.apply as apply_mod

    await _with_price_check(db)
    assert await apply_mod._offer_price_check_present(db) is True


async def test_the_reviewed_price_hold_keeps_its_own_stamp(db):
    """The SGD 30 hold keeps the stored price over a stale 28.20 replay; a price this run did not write is
    not a price this run dated, so the stored stamp stays too."""
    import services.catalog_enrichment_agent.apply as apply_mod

    await _with_price_check(db)
    old = datetime(2026, 9, 20, tzinfo=timezone.utc)
    row = _planned_offer(sku_key=HOLD_PK + "::canonical", price=28.20)
    row.update(offer_id="price-read-hold-test", product_key=HOLD_PK, currency="SGD", is_first_party=False,
               offer_payload=json.dumps({"meitu_sg_price_reviewed_0920": "SGD30"}))
    try:
        await db.execute(apply_mod._OFFER_UPSERT_SQL, {**row, "list_price": 30, "merchant_effective_price": 30,
                                                       "estimated_best_price": 30})
        await db.execute("UPDATE catalog_offers SET price_checked_at = :t WHERE offer_id = :id",
                         {"t": old, "id": row["offer_id"]})
        await db.execute(apply_mod._OFFER_UPSERT_PRICE_READ_SQL, row)
        held = await _offer(db, row["offer_id"])
        assert float(held["list_price"]) == 30 and held["price_checked_at"] == old
        # Outside the hold the same statement dates the read.
        await db.execute(apply_mod._OFFER_UPSERT_PRICE_READ_SQL, {**row, "list_price": 31,
                                                                  "merchant_effective_price": 31,
                                                                  "estimated_best_price": 31})
        moved = await _offer(db, row["offer_id"])
        assert float(moved["list_price"]) == 31 and _recent(moved["price_checked_at"])
    finally:
        await db.execute("DELETE FROM catalog_offers WHERE offer_id = :id", {"id": row["offer_id"]})


async def test_a_row_stored_in_another_currency_is_never_dated(db):
    """The upsert keeps the stored currency label (currency is insert-only), so this run's amount lands
    under it. That amount is not a read in the row's currency: NULL, never NOW()."""
    import services.catalog_enrichment_agent.apply as apply_mod

    await _with_price_check(db)
    await _seed_product(db)
    offer_id = await _write(db, batch=False, price_read=True)  # stored USD, dated
    planned = _planned_sku()
    offer = _planned_offer(sku_key=planned["sku_key"], price=26.0)
    offer.update(currency="SGD", is_first_party=False)  # the apply's own row defaults (_with_offer_write_defaults)
    await db.execute(apply_mod._OFFER_UPSERT_PRICE_READ_SQL, offer)
    row = await db.fetch_one("SELECT currency, list_price, price_checked_at FROM catalog_offers WHERE offer_id = :id",
                             {"id": offer_id})
    assert row["currency"] == "USD" and float(row["list_price"]) == 26.0
    assert row["price_checked_at"] is None
