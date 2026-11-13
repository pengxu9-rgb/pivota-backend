"""The crawled-code verifier (services/deal_code_verifier.py) on real Postgres: the daily cart cap's one-statement
UPSERT under concurrency, the seed read (`LIKE ... ESCAPE`, JSONB seed_data, `lower(domain)`), the verification
writes against migration 275's CHECKs (a verified row must carry the whole reference cart), the refusal hash and
retention's 30-day purge, and migration 283 itself. The rules: tests/test_deal_code_verifier.py (SQLite).

    DATABASE_URL=postgresql://pgtest@127.0.0.1:55471/pivota_avail_utf8_dialect_check \\
        .venv/bin/python -m pytest tests/test_deal_code_verifier_postgres.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")]

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
_MIG_275 = _MIGRATIONS / "275_deal_lane_tables.sql"
_MIG_283 = _MIGRATIONS / "283_deal_code_refusals.sql"
_TABLES = ("observed_store_offer_verifications", "observed_store_offers", "deal_lane_optout",
           "deal_verify_host_daily_carts", "deal_code_refusals")
_SEED_PREFIX = "dcv-gate-"
# The gate shares ONE database across files: other files create these with narrower schemas.
_DDL = (
    "ALTER TABLE catalog_products ADD COLUMN IF NOT EXISTS source_domain text",
    "CREATE TABLE IF NOT EXISTS external_product_seeds (id text)",
    *(f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {c} {t}" for c, t in (
        ("market", "text"), ("domain", "text"), ("destination_url", "text"), ("canonical_url", "text"),
        ("price_currency", "text"), ("availability", "text"), ("seed_data", "jsonb"), ("status", "text"))),
)


@pytest.fixture
async def db(monkeypatch):
    from sqlalchemy import create_engine

    from db.catalog import catalog_products
    from db.database import database, metadata
    from db.sql_migrations import split_statements

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to write deal rows in {dbname!r}; throwaway only")
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, tables=[catalog_products], checkfirst=True)
    engine.dispose()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    for mig in (_MIG_275, _MIG_283):
        for stmt in split_statements(mig.read_text()):  # IF NOT EXISTS throughout
            await database.execute(stmt)
    for stmt in _DDL:
        await database.execute(stmt)

    async def _clean():
        for table in _TABLES:
            await database.execute(f"DELETE FROM {table}")
        await database.execute("DELETE FROM external_product_seeds WHERE id LIKE :p", {"p": _SEED_PREFIX + "%"})

    await _clean()
    from services import deal_code_capture

    deal_code_capture.reset_stats()
    from tests.test_deal_code_verifier import _on

    _on(monkeypatch)
    for name in ("DEAL_VERIFY_MAX_CARTS_PER_RUN", "DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY", "DEAL_VERIFY_TTL_HOURS"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield database
    finally:
        await _clean()
        if not was_connected:
            await database.disconnect()


async def _seed(db, sid, *, host="glow.example", variants=None, www=False):
    variants = variants if variants is not None else [
        {"shopify_variant_id": "44000000000001", "price_amount": "30.00", "price_currency": "USD",
         "availability": "In Stock"}]
    url = f"https://{'www.' if www else ''}{host}/products/{sid}"
    await db.execute(
        "INSERT INTO external_product_seeds (id, market, domain, destination_url, canonical_url, price_currency,"
        " availability, seed_data, status) VALUES (:id, 'US', :d, :u, :u, 'USD', 'in_stock', CAST(:sd AS jsonb),"
        " 'active')",
        {"id": _SEED_PREFIX + sid, "d": ("WWW." if www else "") + host.upper(), "u": url,
         "sd": json.dumps({"snapshot": {"variants": variants}})})


async def test_the_daily_cap_holds_under_concurrent_reservations(db, monkeypatch):
    from db.deal_lane import deal_verify_host_daily_carts as caps
    from services import deal_code_verifier as dv
    from tests.test_deal_code_verifier import NOW

    monkeypatch.setenv("DEAL_VERIFY_MAX_CARTS_PER_HOST_PER_DAY", "3")
    got = await asyncio.gather(*[dv.reserve_cart("glow.example", NOW) for _ in range(8)])
    assert sum(got) == 3
    [row] = await db.fetch_all(caps.select())
    assert row["carts"] == 3 and row["utc_day"] == NOW.date()


async def test_a_run_on_postgres_verifies_refuses_and_retries_as_on_sqlite(db):
    from db.deal_lane import deal_code_refusals, observed_store_offer_verifications as v
    from services import deal_code_retention
    from tests.test_deal_code_verifier import APPLIED, NOT_PRICED, NOW, REJECTED, StubClient, offer, run

    await _seed(db, "a", www=True)
    await _seed(db, "b", host="shop_x.example")  # "_" in a host is a LIKE wildcard: it must not match "shopyx"
    await _seed(db, "c", host="brand.example", variants=[
        {"shopify_variant_id": "44000000000007", "price_amount": 19.5, "price_currency": "USD"}])
    ok = await offer(db, "ok", observed=NOW - timedelta(hours=1))
    dead = await offer(db, "dead", seller="shop_x.example", observed=NOW - timedelta(hours=2), code="DEAD10")
    later = await offer(db, "later", seller="brand.example", observed=NOW - timedelta(hours=3))
    await offer(db, "nomatch", seller="shopyx.example", observed=NOW - timedelta(hours=4))
    client = StubClient([APPLIED, REJECTED, NOT_PRICED])
    out = await run(client)
    assert (out["applied"], out["rejected"], out["not_priced"], out["skipped_no_reference_variant"]) == (1, 1, 1, 1)
    assert client.calls[0]["json"]["discovery_host"] == "www.glow.example"
    assert client.calls[0]["json"]["variant_gid"] == "44000000000001"
    assert client.calls[2]["json"]["variant_gid"] == "44000000000007"  # the seed's own stock, its text price
    rows = {r["deal_id"]: dict(r) for r in await db.fetch_all(v.select())}
    assert rows[ok]["state"] == "checkout_verified" and rows[ok]["ref_discount_amount"] == Decimal("4.5000")
    assert rows[ok]["expires_at"] - rows[ok]["verified_at"] == timedelta(hours=24)
    assert rows[ok]["ref_automatic_discounts"] == []
    assert rows[dead]["state"] == "failed" and rows[later]["state"] == "unverified"
    [h] = await db.fetch_all(deal_code_refusals.select())
    assert h["refusal_hash"] == __import__("services.deal_code_verifier", fromlist=["x"]).refusal_hash(
        "shop_x.example", "US", "DEAD10")
    await db.execute(deal_code_refusals.update().values(refused_at=NOW - timedelta(days=31)))
    assert (await deal_code_retention.run(now=NOW))["deleted_refusal_hashes"] == 1


async def test_a_verified_row_without_a_whole_reference_cart_is_refused_by_the_table(db):
    """The guard is in the code (verified_values); this pins that the table would refuse it too."""
    from db.deal_lane import observed_store_offer_verifications as v
    from tests.test_deal_code_verifier import NOW, offer

    deal = await offer(db, "chk")
    with pytest.raises(Exception):
        await db.execute(v.insert().values(deal_id=deal, variant_id="44000000000001", state="checkout_verified",
                                           verified_at=NOW, expires_at=NOW + timedelta(hours=1),
                                           verified_via="ucp_checkout", authorized_by="q2_approval",
                                           ref_unit_price=30, ref_subtotal=30, ref_currency="USD",
                                           ref_discount_amount=0, updated_at=NOW, created_at=NOW))


async def test_migration_283_is_idempotent_and_matches_the_declared_table(db):
    from db.sql_migrations import split_statements

    for stmt in split_statements(_MIG_283.read_text()):
        await db.execute(stmt)
    cols = await db.fetch_all(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'deal_code_refusals' ORDER BY ordinal_position")
    assert [(c["column_name"], c["is_nullable"]) for c in cols] == [
        ("refusal_hash", "NO"), ("reason", "YES"), ("refused_at", "NO")]


async def test_a_wildcard_in_the_sellers_host_never_lets_another_hosts_seeds_crowd_out_its_own(db, monkeypatch):
    """`_` in a host is a LIKE wildcard. The Python host check already drops another host's seeds; the escape keeps
    them out of the bounded read, so they cannot use up its rows."""
    from services import deal_code_verifier as dv
    from tests.test_deal_code_verifier import NOW, offer

    monkeypatch.setattr(dv, "MAX_SEEDS_PER_HOST", 1)
    await _seed(db, "a", host="shopyx.example")  # sorts first by id
    await _seed(db, "b", host="shop_x.example")
    await offer(db, "w", seller="shop_x.example")
    [c] = await dv._candidates(NOW)
    ref, why = await dv.reference_variant(c)
    assert ref is not None and why == ""
