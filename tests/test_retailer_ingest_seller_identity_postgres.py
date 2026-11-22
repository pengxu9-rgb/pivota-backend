"""The seller_id_split reads (services/retailer_ingest/pipeline.py) on real PostgreSQL.

What only Postgres can prove: the host expression (lower-cased, a trailing `:port` and dot dropped, `www.` matched
through :hosts) and the jsonb payload-domain fallback; that a suppressed offer or a suppressed product (a retired
retailer chain) is not live; that the claim lookup is a suffix compare, so `_` and `%` in a domain are characters;
and that every query runs through the dry run's SELECT-only handle. Isolated schema built from the production
model plus migration 160; never run against production. Placeholder hosts and brands only.
"""
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgres"), reason="requires test PostgreSQL"
)

HOST = "brand-a.example"
HOSTS = [HOST, "www." + HOST]
GONE = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture
async def catalog():
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql
    import db.database as dbmod
    import db.catalog  # noqa: F401 -- registers the catalog tables on the shared metadata

    url = os.environ["DATABASE_URL"]
    schema = "seller_identity_" + uuid.uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        for name in ("catalog_merchants", "catalog_products", "catalog_offers"):
            table = dbmod.metadata.tables[name]
            await admin.execute(str(sa.schema.CreateTable(table).compile(dialect=postgresql.dialect())))
        ddl = (Path(__file__).resolve().parents[1] / "db/migrations/160_brand_claims.sql").read_text()
        await admin.execute(ddl.replace("BEGIN;", "").replace("COMMIT;", ""))
        database = databases.Database(url, server_settings={"search_path": schema})
        await database.connect()
        yield database, admin
    finally:
        if database is not None:
            await database.disconnect()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _merchant(admin, mid, source_ref):
    await admin.execute("INSERT INTO catalog_merchants (merchant_id, source_ref) VALUES ($1, $2)", mid, source_ref)


async def _offer(admin, mid, key, *, source_domain=None, payload_domain=None, brand="Brand A",
                 offer_gone=False, product_gone=False):
    await admin.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,
                                         suppressed_at, suppression_reason)
           VALUES ($1, 'merch_obs_0000000000000000', 'external_seed', $1, 'Row', $2, $3, $4)
           ON CONFLICT (product_key) DO NOTHING""",
        key, brand, GONE if product_gone else None,
        "retailer_chain_superseded_by_brand_official" if product_gone else None)
    await admin.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, source_domain, offer_payload,
                                       suppressed_at, suppression_reason)
           VALUES ($1, $2, $3, $4, $5, CAST($6 AS jsonb), $7, $8)""",
        "offer:" + uuid.uuid4().hex, key + "::canonical", key, mid, source_domain,
        json.dumps({"domain": payload_domain}) if payload_domain else None,
        GONE if offer_gone else None, "catalog_product_suppressed" if offer_gone else None)


async def _read(database, sql, values):
    from scripts import onboard_curated_brands as cli
    return [dict(r) for r in await cli._SelectOnlyHandle(database).fetch_all(sql, values)]


async def test_the_candidate_and_liveness_reads(catalog):
    from services.retailer_ingest import pipeline

    database, admin = catalog
    await _merchant(admin, "agent_seed::brand-a-cosmetics", "WWW.Brand-A.Example.")
    await _merchant(admin, "agent_seed::brand-a-port", HOST + ":443")
    await _merchant(admin, "agent_seed::elsewhere", "other.example")
    await _merchant(admin, "agent_seed::no-ref", None)
    on_host = {r["merchant_id"] for r in await _read(database, pipeline.SELLERS_ON_HOST_SQL, {"hosts": HOSTS})}
    assert on_host == {"agent_seed::brand-a-cosmetics", "agent_seed::brand-a-port"}

    await _offer(admin, "agent_seed::brand-a-cosmetics", "ext:k1::1", source_domain="www." + HOST)
    await _offer(admin, "agent_seed::brand-a-cosmetics", "ext:k2::1", payload_domain=HOST + ".")  # payload fallback
    await _offer(admin, "agent_seed::brand-a-port", "ext:k3::1", source_domain=HOST, offer_gone=True)
    await _offer(admin, f"agent_seed::retailer::{HOST}", "ext:retailer:k4", source_domain=HOST, product_gone=True)
    await _offer(admin, "agent_seed::elsewhere", "ext:k5::1", source_domain="other.example")
    await _offer(admin, "agent_seed::brand-b", "ext:k6::1", source_domain="shop." + HOST, brand="Brand B")
    ids = ["agent_seed::brand-a-cosmetics", "agent_seed::brand-a-port", f"agent_seed::retailer::{HOST}",
           "agent_seed::elsewhere", "agent_seed::brand-b"]
    live = await _read(database, pipeline.SELLER_LIVE_AT_HOST_SQL, {"ids": ids, "hosts": HOSTS})
    assert [(r["merchant_id"], r["brand"], r["offers"]) for r in live] == [
        ("agent_seed::brand-a-cosmetics", "Brand A", 2)]

    planned = await _read(database, pipeline.OFFER_SELLERS_ON_PLANNED_SQL, {"keys": ["ext:k1::1", "ext:k5::1"]})
    assert {r["merchant_id"] for r in planned} == {"agent_seed::brand-a-cosmetics", "agent_seed::elsewhere"}
    stored = await _read(database, pipeline.PRODUCT_SELLERS_SQL, {"keys": ["ext:k1::1", "ext:missing"]})
    assert stored == [{"product_key": "ext:k1::1", "merchant_id": "merch_obs_0000000000000000"}]

    hosts = await _read(database, pipeline.PINNED_LIVE_HOSTS_SQL, {"mid": "agent_seed::brand-a-cosmetics"})
    assert sorted(pipeline._norm_host(r["host"]) for r in hosts) == [HOST]
    assert await _read(database, pipeline.PINNED_LIVE_HOSTS_SQL, {"mid": "agent_seed::brand-a-port"}) == []
    assert await _read(database, pipeline.PINNED_SELLER_SQL, {"mid": "agent_seed::no-ref"}) == [
        {"merchant_id": "agent_seed::no-ref"}]


async def test_the_claim_lookup_is_a_suffix_compare_not_a_wildcard(catalog):
    from db.brand_claims import STATUS_VERIFIED
    from services.retailer_ingest import pipeline

    database, admin = catalog
    for mid, domain, status in (("merch_tenant_a", "shop.brand-a.example", STATUS_VERIFIED),
                                ("merch_tenant_apex", "brand-a.example", STATUS_VERIFIED),
                                ("merch_tenant_pending", "brand-a.example", "pending"),
                                ("merch_tenant_lookalike", "xbrand-a.example", STATUS_VERIFIED),
                                ("merch_tenant_wild", "shop.brand_a.example", STATUS_VERIFIED)):
        await admin.execute("INSERT INTO brand_claims (claim_id, merchant_id, brand_domain, claim_method, "
                            "verification_status) VALUES ($1, $2, $3, 'dns', $4)", uuid.uuid4(), mid, domain, status)
    rows = await _read(database, pipeline.CLAIMED_TENANT_SQL, {"verified": STATUS_VERIFIED, "reg": HOST})
    assert {r["merchant_id"] for r in rows} == {"merch_tenant_a", "merch_tenant_apex"}
    # `_` in the registrable is a character: it matches only itself, never "any one character"
    rows = await _read(database, pipeline.CLAIMED_TENANT_SQL, {"verified": STATUS_VERIFIED, "reg": "brand_a.example"})
    assert {r["merchant_id"] for r in rows} == {"merch_tenant_wild"}
    rows = await _read(database, pipeline.CLAIMED_TENANT_SQL, {"verified": STATUS_VERIFIED, "reg": "brand%.example"})
    assert rows == []


EDGE_HOSTS = ("WWW.Brand-A.Example.", "brand-a.example..", "brand-a.example...:443", "\tbrand-a.example\n",
              "\r\nwww.brand-a.example:8080 ", "Brand-A.Example", "www.www.brand-a.example", "brand-a.example:443.",
              "", "www.")


async def test_the_sql_host_fold_is_the_python_one(catalog):
    """pipeline._host_sql run by Postgres over the edge spellings equals pipeline._norm_host on each."""
    from services.retailer_ingest import pipeline

    _, admin = catalog
    sql = f"SELECT {pipeline._host_sql('$1::text')}"
    for raw in EDGE_HOSTS:
        assert await admin.fetchval(sql, raw) == pipeline._norm_host(raw), repr(raw)
    # A non-ASCII capital folds as the database's collation folds it; Python always folds it. Where the server
    # folds it too the two agree; where it does not, only that letter differs (a pin then holds: fail-closed).
    raw = "MARKE-Ä.example"
    folded = await admin.fetchval(sql, raw)
    if await admin.fetchval("SELECT lower('Ä') = 'ä'"):
        assert folded == pipeline._norm_host(raw)
    else:
        assert folded == "marke-Ä.example"


async def test_the_candidate_reads_skip_sellers_of_record_and_stop_at_the_cap(catalog):
    from services.retailer_ingest import pipeline

    database, admin = catalog
    await _merchant(admin, "merch_obs_00000000000000aa", HOST)
    for i in range(pipeline.SELLER_CANDIDATE_CAP + 5):
        await _merchant(admin, f"agent_seed::seller-{i:04d}", HOST)
    rows = await _read(database, pipeline.SELLERS_ON_HOST_SQL, {"hosts": HOSTS})
    assert len(rows) == pipeline.SELLER_CANDIDATE_CAP + 1
    assert not [r for r in rows if r["merchant_id"].startswith("merch_obs_")]
    await _offer(admin, "merch_obs_00000000000000aa", "ext:k9::1", source_domain=HOST, brand=None)
    await _offer(admin, "agent_seed::seller-0000", "ext:k9::1", source_domain=HOST, brand=None)
    planned = await _read(database, pipeline.OFFER_SELLERS_ON_PLANNED_SQL, {"keys": ["ext:k9::1"]})
    assert planned == [{"merchant_id": "agent_seed::seller-0000"}]
    live = await _read(database, pipeline.SELLER_LIVE_AT_HOST_SQL, {"ids": ["agent_seed::seller-0000"], "hosts": HOSTS})
    assert [(r["merchant_id"], r["brand"]) for r in live] == [("agent_seed::seller-0000", None)]
