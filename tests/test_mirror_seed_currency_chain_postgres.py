"""The mirror's seed-currency refusals, executed on real Postgres (PR controller review of #2652).

Each test pins a fix that otherwise exists only in code. Every one of them survived as a mutant
against the rest of the suite:

  * M1: the mirror chain refuses BEFORE its canonical-sku write. A seed whose column and page read
    disagree leaves neither a sku nor an offer behind.
  * M6: the canonical sku of a currency-less seed carries no currency. It used to be
    `price_currency or "USD"`, exactly the regression this PR exists to stop.
  * M3: sync_offer_for_seed reads the page currency through its own column projection
    (`seed_data -> 'snapshot' ->> 'price_currency' AS snapshot_price_currency`). The refusal runs on
    a row that came out of Postgres, not out of a fake.

PRIVATE DATABASE, created and dropped here. The catalog tables come from their db.catalog models;
external_product_seeds has no model, so it gets the columns the sync path selects.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB_NAME = f"mirror_seed_ccy_{os.getpid()}"
SELLER = "merch_obs_9cbce09d1019844b"


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


@pytest.fixture(scope="module")
def db_url():
    import psycopg2
    from sqlalchemy import create_engine, text

    from db.catalog import catalog_merchants, catalog_offers, catalog_products, catalog_skus

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()
    engine = create_engine(_private_url(), future=True)
    try:
        catalog_offers.metadata.create_all(
            engine, tables=[catalog_merchants, catalog_products, catalog_skus, catalog_offers], checkfirst=True)
        with engine.begin() as conn:
            # Prod's column (migration 246). The writer caches price_check_column_present() for the
            # process, so a suite that ran earlier on a DB with the column decides which statement
            # this one gets: the private table must match prod, not the model.
            conn.execute(text("ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS price_checked_at timestamptz"))
            conn.execute(text(
                "CREATE TABLE external_product_seeds (id text PRIMARY KEY, external_product_id text, destination_url text, "
                " canonical_url text, domain text, price_amount numeric, price_currency text, availability text, "
                " market text, attached_product_key text, status text, seed_data jsonb)"))
    finally:
        engine.dispose()
    try:
        yield _private_url()
    finally:
        admin = psycopg2.connect(os.environ["DATABASE_URL"])
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                        (_DB_NAME,))
            cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        admin.close()


def _run(db_url, monkeypatch, body):
    from databases import Database

    import scripts.mirror_external_seeds_to_catalog_products as mirror
    import services.external_offer_dual_write as dual_write

    # The writer caches price_check_column_present() for the process. Start this DB's answer fresh,
    # and leave the process's cache as it was found (monkeypatch restores both keys).
    monkeypatch.setitem(dual_write._price_check_column, "present", False)
    monkeypatch.setitem(dual_write._price_check_column, "checked", None)

    async def go():
        db = Database(db_url)
        await db.connect()
        monkeypatch.setattr(dual_write, "database", db)
        monkeypatch.setattr(mirror, "database", db)
        try:
            return await body(db, mirror, dual_write)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _seed_row(pk, **over):
    row = {"id": f"eps_{pk[-6:]}", "external_product_id": pk.split("::")[-1], "title": "Glow Cushion & Peptide Lip Serum Set 2pcs",
           "destination_url": "https://skintific.com/products/glow-cushion-peptide-lip-serum-set-2pcs",
           "canonical_url": "https://skintific.com/products/glow-cushion-peptide-lip-serum-set-2pcs",
           "domain": "skintific.com", "availability": "in_stock", "market": "US"}
    row.update(over)
    return row


async def _mirror_product(db, pk):
    """The catalog_products row the mirror writes before its chain (the offer upsert requires it)."""
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, source_system, title) "
        "VALUES (:pk, :m, 'external_seed', :spid, 'external_product_seeds_mirror_v1', 'Glow Cushion Set')",
        {"pk": pk, "m": SELLER, "spid": pk.split("::")[-1]})


async def _chain_rows(db, pk):
    skus = [dict(r) for r in await db.fetch_all("SELECT sku_key, currency FROM catalog_skus WHERE product_key = :pk", {"pk": pk})]
    offers = [dict(r) for r in await db.fetch_all("SELECT offer_id, currency, list_price FROM catalog_offers WHERE product_key = :pk", {"pk": pk})]
    return skus, offers


def test_m1_a_refused_seed_writes_neither_its_sku_nor_its_offer(db_url, monkeypatch):
    pk = "prod::merch_obs_9cbce09d1019844b::external_seed::ext_m1_refused"

    async def body(db, mirror, _dual_write):
        await _mirror_product(db, pk)
        # skintific.com's shape: column USD (an older read) over a page read in AUD.
        status = await mirror._write_mirror_chain(pk, _seed_row(
            pk, price_amount=23.56, price_currency="USD", seed_data={"snapshot": {"price_currency": "AUD"}}), merchant_id=SELLER)
        return status, await _chain_rows(db, pk)

    status, (skus, offers) = _run(db_url, monkeypatch, body)
    assert status == "refused_currency"
    assert (skus, offers) == ([], [])


def test_m6_a_currency_less_seed_writes_a_currency_less_sku_never_usd(db_url, monkeypatch):
    pk = "prod::merch_obs_9cbce09d1019844b::external_seed::ext_m6_no_currency"

    async def body(db, mirror, _dual_write):
        await _mirror_product(db, pk)
        status = await mirror._write_mirror_chain(pk, _seed_row(
            pk, price_amount=None, price_currency=None, seed_data={"snapshot": {"price_amount": None}}), merchant_id=SELLER)
        return status, await _chain_rows(db, pk)

    status, (skus, offers) = _run(db_url, monkeypatch, body)
    assert status == "written"
    assert [s["currency"] for s in skus] == [None]
    assert [(o["currency"], o["list_price"]) for o in offers] == [(None, None)]


def test_m6_control_a_priced_agreeing_seed_writes_its_own_currency(db_url, monkeypatch):
    pk = "prod::merch_obs_9cbce09d1019844b::external_seed::ext_m6_control"

    async def body(db, mirror, _dual_write):
        await _mirror_product(db, pk)
        status = await mirror._write_mirror_chain(pk, _seed_row(
            pk, price_amount=56, price_currency="AUD", seed_data={"snapshot": {"price_currency": "AUD"}}), merchant_id=SELLER)
        return status, await _chain_rows(db, pk)

    status, (skus, offers) = _run(db_url, monkeypatch, body)
    assert status == "written"
    assert [s["currency"] for s in skus] == ["AUD"]
    assert [(o["currency"], float(o["list_price"])) for o in offers] == [("AUD", 56.0)]


def test_m3_sync_reads_the_page_currency_through_its_own_projection(db_url, monkeypatch):
    """The real sync path: the seed and its mirror product come out of Postgres, and the page
    currency only through `_SEED_OFFER_COLUMNS`. With that projection replaced by NULL the column
    USD would be written over the AUD page read."""
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    pk = "prod::merch_obs_9cbce09d1019844b::external_seed::ext_m3_sync"
    seed_id = "eps_m3_sync"

    async def body(db, _mirror, dual_write):
        await db.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, destination_url, canonical_url, domain, price_amount, "
            " price_currency, availability, market, status, seed_data) VALUES (:id, 'ext_m3_sync', :url, :url, 'skintific.com', "
            " 23.56, 'USD', 'in_stock', 'US', 'active', CAST(:sd AS jsonb))",
            {"id": seed_id, "url": "https://skintific.com/products/m3", "sd": json.dumps({"snapshot": {"price_currency": "AUD"}})})
        await db.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, source_system, source_ref, title) "
            "VALUES (:pk, :m, 'external_seed', 'ext_m3_sync', 'external_product_seeds_mirror_v1', :sid, 'M3')",
            {"pk": pk, "m": SELLER, "sid": seed_id})
        result = await dual_write.sync_offer_for_seed(seed_id)
        return result, await _chain_rows(db, pk)

    result, (_skus, offers) = _run(db_url, monkeypatch, body)
    assert result["status"] == "refused_currency" and "page read AUD" in result["error"]
    assert offers == []


@pytest.mark.parametrize("label, seed_data, expected", [
    ("snapshot as a JSON string", {"snapshot": '{"price_currency": "USD"}'}, "refused_currency"),
    ("snapshot as an array", {"snapshot": ["USD"]}, "refused_currency"),
    ("snapshot as a number", {"snapshot": 7}, "refused_currency"),
    ("snapshot as a boolean", {"snapshot": True}, "refused_currency"),
    ("seed_data as a JSON string", '{"snapshot": {"price_currency": "USD"}}', "refused_currency"),
    ("seed_data as an array", [1, 2], "refused_currency"),
    ("seed_data as a number", 7, "refused_currency"),
    ("snapshot currency 0", {"snapshot": {"price_currency": 0}}, "refused_currency"),
    ("snapshot currency false", {"snapshot": {"price_currency": False}}, "refused_currency"),
    ("an agreeing object snapshot", {"snapshot": {"price_currency": "USD"}}, "synced"),
    ("no snapshot: the column stands", {}, "synced"),
])
def test_sync_writes_only_what_the_readers_would_serve(db_url, monkeypatch, label, seed_data, expected):
    """PR controller review of #2652: the sync path read only the projected snapshot currency, which a
    driver flattens to NULL for a snapshot that is not an object, so it WROTE offers every reader then
    withholds. It now projects the readers' own answer (seed_currency_sql) and refuses those."""
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    slug = "".join(ch for ch in label if ch.isalnum())[:24]
    pk = f"prod::merch_obs_9cbce09d1019844b::external_seed::ext_shape_{slug}"
    seed_id = f"eps_shape_{slug}"

    async def body(db, _mirror, dual_write):
        await db.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, destination_url, canonical_url, domain, price_amount, "
            " price_currency, availability, market, status, seed_data) VALUES (:id, :ext, :url, :url, 'skintific.com', "
            " 23.56, 'USD', 'in_stock', 'US', 'active', CAST(:sd AS jsonb))",
            {"id": seed_id, "ext": f"ext_shape_{slug}", "url": f"https://skintific.com/products/{slug}", "sd": json.dumps(seed_data)})
        await _mirror_product(db, pk)
        await db.execute("UPDATE catalog_products SET source_ref = :sid WHERE product_key = :pk", {"sid": seed_id, "pk": pk})
        await db.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id, title) "
            "VALUES (:sk, :pk, :m, 'external_seed', :spid, :pk, 'Glow Cushion Set')",
            {"sk": f"{pk}::canonical", "pk": pk, "m": SELLER, "spid": f"ext_shape_{slug}"})
        result = await dual_write.sync_offer_for_seed(seed_id)
        return result, await _chain_rows(db, pk)

    result, (_skus, offers) = _run(db_url, monkeypatch, body)
    assert result["status"] == expected, (label, result)
    if expected == "refused_currency":
        assert offers == []
    else:
        assert [(o["currency"], float(o["list_price"])) for o in offers] == [("USD", 23.56)]
