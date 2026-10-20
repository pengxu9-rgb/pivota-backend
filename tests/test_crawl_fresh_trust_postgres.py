"""CRAWL_FRESH on the production dialect: the freshness statement, the end-to-end
recompute through the real trust join, and the read-only census.

    DATABASE_URL=postgresql://127.0.0.1:55433/<tag>_test \\
        .venv/bin/python -m pytest tests/test_crawl_fresh_trust_postgres.py

ISOLATION. The dialect gate runs every file against ONE database, and this file
installs migration 246's trigger on catalog_offers, which would change every
other file's writes. So it never touches `public`: two per-process scratch
schemas (one with migration 246, one as a database that predates it), built
from the model plus the real migrations, dropped at teardown.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL -- see the module docstring",
)

_REPO = Path(__file__).resolve().parent.parent
_MIGRATIONS = _REPO / "db/migrations"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"crawl_fresh_test_{os.getpid()}"
_SCHEMA_NO_246 = f"crawl_fresh_no246_test_{os.getpid()}"

DRAIN = "catalog_enrichment_agent_v1"
RETAILER = "ext:retailer:0123456789abcdef0123456789abcdef"
RETAILER_2 = "ext:retailer:fedcba9876543210fedcba9876543210"
BRAND = "ext:serum-slug::0123abcd"
OTHER = "prod::external_seed::merch_obs_x::ext_1"

# Tables the trust join reaches that db.catalog does not own (minimum columns
# the query names), as tests/test_priced_offer_gate_postgres.py declares them.
_LIGHTWEIGHT_DDL = """
CREATE TABLE external_product_seeds (
  id text, external_product_id text, attached_product_key text, status text,
  seed_kind text, merchant_id text, source text, product_key text,
  source_product_id text, domain text, title text, seed_data jsonb, market text,
  created_at timestamptz, updated_at timestamptz, price_amount double precision
);
CREATE TABLE retailer_ingest_jobs (id text PRIMARY KEY, domain text, brand text, options jsonb);
CREATE TABLE index_pipeline_state (
  content_key text PRIMARY KEY, serving_eligible boolean, index_eligible boolean,
  pipeline_stage text, blocker_code text, blocker_detail text,
  content_quality_score double precision, quality_scored_at timestamptz,
  last_extracted_at timestamptz
);
CREATE TABLE content_canonical_election (
  content_key text PRIMARY KEY, canonical_sig_id text, basis text, elected_at timestamptz
);
CREATE TABLE pdp_identity_listing (
  source_listing_ref text, product_id text, merchant_id text, identity_status text,
  identity_confidence numeric, live_read_enabled boolean, review_required boolean,
  sellable_item_group_id text, product_line_id text, review_family_id text
);
CREATE TABLE pdp_identity_override (
  id text, source_listing_ref text, action_type text, active boolean,
  updated_at timestamptz, created_at timestamptz
);
CREATE TABLE merchant_stores (
  merchant_id text, platform text, domain text, status text, is_primary boolean,
  store_id text, last_sync timestamptz, updated_at timestamptz,
  created_at timestamptz, id text
)
"""


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _build_schema(schema: str, *, with_246: bool) -> None:
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata
    from db.sql_migrations import split_statements

    admin = create_engine(_url())
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    admin.dispose()
    engine = create_engine(_url(), connect_args={"options": f"-csearch_path={schema}"})
    metadata.create_all(engine, tables=[metadata.tables["catalog_products"],
                                        metadata.tables["catalog_offers"]])
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(stmt))
        names = ["134_catalog_source_quarantine.sql", "136_catalog_row_trust.sql"]
        if with_246:
            names.append("246_catalog_offers_price_checked_at.sql")
        for name in names:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                # The raw DBAPI cursor with no parameters: a `%` in a migration's
                # comment must not be read as a placeholder.
                conn.connection.cursor().execute(statement)
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def _schemas():
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} -- throwaway only")
    _build_schema(_SCHEMA, with_246=True)
    _build_schema(_SCHEMA_NO_246, with_246=False)
    yield
    from sqlalchemy import create_engine, text

    admin = create_engine(_url())
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA_NO_246} CASCADE"))
    admin.dispose()


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch):
    from services import catalog_row_trust_upserter as up

    monkeypatch.delenv(up.CRAWL_FRESH_ENV, raising=False)
    monkeypatch.delenv(up.CRAWL_FRESH_DAYS_ENV, raising=False)


async def _connect(schema):
    import databases

    db = databases.Database(_url(), server_settings={"search_path": schema})
    await db.connect()
    for table in ("catalog_offers", "catalog_products", "external_product_seeds", "retailer_ingest_jobs",
                  "index_pipeline_state", "content_canonical_election", "pdp_identity_listing",
                  "catalog_row_trust", "catalog_source_quarantine"):
        await db.execute(f"DELETE FROM {table}")
    return db


@pytest.fixture()
async def db():
    conn = await _connect(_SCHEMA)
    try:
        yield conn
    finally:
        await conn.disconnect()


@pytest.fixture()
async def db_no_246():
    conn = await _connect(_SCHEMA_NO_246)
    try:
        yield conn
    finally:
        await conn.disconnect()


NOW = datetime.now(timezone.utc).replace(microsecond=0)


async def _product(db, pk, *, source_system=DRAIN, role=None, host="shop.example.com",
                   seed=True, ips=True, sig=None, merchant="merch_obs_0123456789abcdef"):
    import json

    ck = f"ck_{pk[-8:]}"
    payload = json.dumps({"enrichment_meta": {"source_role": role}} if role else {})
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title,"
        " content_key, catalog_track, sync_status, pivota_signature_id, source_system, source_domain,"
        " product_payload)"
        " VALUES (:pk, :merchant, 'external_seed', :spid, 'fixture', :ck,"
        " 'external_referral', 'live', :sig, :ss, :host, CAST(:payload AS jsonb))",
        {"pk": pk, "merchant": merchant, "spid": f"slug_{pk[-8:]}", "ck": ck, "sig": sig or f"sig_{pk[-8:]}",
         "ss": source_system, "host": host, "payload": payload},
    )
    if ips:
        await db.execute(
            "INSERT INTO index_pipeline_state (content_key, serving_eligible) VALUES (:ck, TRUE)",
            {"ck": ck})
    if seed:
        await db.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, attached_product_key, status,"
            " seed_kind, domain, market, updated_at)"
            " VALUES (:id, :ext, :pk, 'active', 'cross', :host, 'US', now())",
            {"id": f"seed_{pk[-8:]}", "ext": f"ext_{pk[-8:]}", "pk": pk, "host": host})
    return ck


_N = [0]


async def _offer(db, pk, *, read_at, source_system=DRAIN, price=20.0, link="https://shop.example.com/p/1",
                 payload_link=None, suppressed=False, affiliate=False, market="US", currency="USD",
                 availability="in_stock", variant=None, read_source=None, declared=True):
    """A HAND-BUILT offer row (the statement's own cases). `declared` stamps
    offer_payload.declared_market = market, as the drain does for an explicit
    options.market; the real writers run in test_crawl_fresh_real_producers_postgres."""
    import json

    _N[0] += 1
    oid = f"offer_{_N[0]}"
    payload = {"validated_at": "affiliate_feed:net" if affiliate else "2026-10-01"}
    if declared:
        payload["declared_market"] = market
    if payload_link is not None:
        payload["destination_url"] = payload_link
    if read_source is not None:
        payload["price_read_source"] = read_source
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, list_price,"
        " merchant_effective_price, currency, market, source_system, source_ref, offer_payload,"
        " price_checked_at, suppressed_at, availability)"
        " VALUES (:oid, :sku, :pk, 'merch_obs_0123456789abcdef', :p, :p, :cur, :mkt, :ss, :link,"
        " CAST(:payload AS jsonb), :read_at, " + ("now()" if suppressed else "NULL") + ", :avail)",
        {"oid": oid, "sku": f"{pk}::v:{variant}" if variant else f"{pk}::canonical", "pk": pk,
         "p": price, "cur": currency, "mkt": market, "ss": source_system, "link": link,
         "payload": json.dumps(payload), "read_at": read_at, "avail": availability},
    )
    return oid


async def _reads(db, keys, now=None):
    from services.catalog_row_trust_upserter import CRAWL_ROW_PRICE_READ_SQL

    rows = await db.fetch_all(CRAWL_ROW_PRICE_READ_SQL, {"product_keys": keys, "now": now or _db_now()})
    return {r["product_key"]: r["row_price_read_at"] for r in rows}


def _db_now():
    from datetime import datetime as _dt

    return _dt.now(timezone.utc)


async def _trust(db, pk):
    return await db.fetch_one(
        "SELECT serving_decision, serving_reason_codes FROM catalog_row_trust"
        " WHERE subject_type = 'product' AND subject_key = :pk", {"pk": pk})


# ---------------------------------------------------------------------------
# The freshness statement, both ways
# ---------------------------------------------------------------------------


async def test_the_row_read_is_the_oldest_read_of_every_live_offer(db):
    await _product(db, RETAILER)
    oldest = NOW - timedelta(days=3)
    await _offer(db, RETAILER, read_at=oldest)
    await _offer(db, RETAILER, read_at=NOW - timedelta(days=1), variant="1")
    # NOT LIVE -- no reader serves them (suppressed, unpriced, or a destination the
    # gateway's `servable` test refuses): they neither date the row nor block it.
    await _offer(db, RETAILER, read_at=None, suppressed=True)
    await _offer(db, RETAILER, read_at=None, source_system="variant_identity_backfill_v1", suppressed=True)
    await _offer(db, RETAILER, read_at=None, link="ftp://shop.example.com/p/1")
    await _offer(db, RETAILER, read_at=None, link="/p/1")
    await _offer(db, RETAILER, read_at=None, link="HTTPS://shop.example.com/p/1")
    await _offer(db, RETAILER, read_at=None, price=0)
    await _offer(db, RETAILER, read_at=None, price=None)
    await _offer(db, RETAILER, read_at=None, link=None, payload_link="not a url")
    await _offer(db, RETAILER, read_at=None, market="AU", currency="AUD", suppressed=True)
    assert await _reads(db, [RETAILER]) == {RETAILER: oldest}


# Round 4, fail closed: each of these is a LIVE offer a reader serves (a no-market
# get_offers caller gets them all), so each is in the class -- and none is ever a
# valid read, however fresh its stamp. One beside a fresh declared US/USD drain
# offer makes the row NOT fresh. (Round 3 left every one of them outside the class.)
_LIVE_NEVER_VALID = [
    pytest.param(dict(market="AU", currency="AUD"), id="non_served_market"),
    pytest.param(dict(market="US", currency="SGD"), id="us_priced_sgd"),
    pytest.param(dict(market="SG", currency="USD"), id="sg_priced_usd"),
    pytest.param(dict(source_system="shopify_markets_us_localization"), id="market_sibling"),
    pytest.param(dict(source_system="variant_identity_backfill_v1", variant="9"), id="variant_copy"),
    pytest.param(dict(affiliate=True), id="affiliate"),
]


@pytest.mark.parametrize("live", _LIVE_NEVER_VALID)
async def test_a_live_offer_that_can_never_be_a_valid_read_makes_the_row_not_fresh(db, live):
    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=2))
    assert await _reads(db, [RETAILER]) == {RETAILER: NOW - timedelta(hours=2)}
    # stamped an hour AFTER the drain read, declared, in stock: still never valid
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=1), link="https://shop.example.com/p/2", **live)
    assert await _reads(db, [RETAILER]) == {}


async def test_a_live_link_the_reader_serves_is_in_the_class_even_with_a_space(db):
    """The class uses the reader's own servable test (`^https?://` once trimmed),
    so a link round 3 called not-a-listing but the reader serves is dated too."""
    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=2))
    oid = await _offer(db, RETAILER, read_at=None, link="https://shop.example.com/p 1")
    assert await _reads(db, [RETAILER]) == {}
    await db.execute("UPDATE catalog_offers SET price_checked_at = :t WHERE offer_id = :o",
                     {"t": NOW - timedelta(hours=3), "o": oid})
    assert await _reads(db, [RETAILER]) == {RETAILER: NOW - timedelta(hours=3)}


async def test_an_out_of_stock_live_offer_still_needs_its_read(db):
    """Availability never excuses an offer from freshness."""
    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=2))
    await _offer(db, RETAILER, read_at=NOW - timedelta(days=60), availability="out_of_stock", variant="3")
    assert await _reads(db, [RETAILER]) == {RETAILER: NOW - timedelta(days=60)}


@pytest.mark.parametrize("stale", [
    pytest.param(dict(market="AU", currency="AUD", link="https://shop.example.com/au/p"), id="au_drain_offer"),
    pytest.param(dict(source_system="variant_identity_backfill_v1", variant="7"), id="variant_copy"),
])
async def test_the_reviewer_case_a_fresh_us_offer_does_not_carry_a_60_day_old_one(db, monkeypatch, stale):
    """B1 (round 4): a fresh declared US/USD drain offer beside a live offer read 60
    days ago that a no-market caller is served -- the row must not go public."""
    from services import catalog_row_trust_upserter as up

    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=1))
    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    # the control: alone, the fresh offer flips the row
    await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER])
    assert (await _trust(db, RETAILER))["serving_decision"] == "public"
    await _offer(db, RETAILER, read_at=NOW - timedelta(days=60), **stale)
    assert await _reads(db, [RETAILER]) == {}
    await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER])
    row = await _trust(db, RETAILER)
    assert row["serving_decision"] == "shadow"
    assert "CRAWL_FRESH" not in list(row["serving_reason_codes"])
    # and the hourly recheck would have found it too
    await db.execute("UPDATE catalog_row_trust SET serving_decision = 'public',"
                     " serving_reason_codes = CAST(ARRAY['CRAWL_FRESH','PUBLIC_PASSTHROUGH'] AS text[])"
                     " WHERE subject_key = :pk", {"pk": RETAILER})
    assert await up.recheck_crawl_fresh_rows(db=db) == [RETAILER]
    assert (await _trust(db, RETAILER))["serving_decision"] == "shadow"


@pytest.mark.parametrize("bad", [
    dict(read_at=None),                                        # never read
    dict(read_at=NOW - timedelta(hours=1), read_source="employee_edit"),
    dict(read_at=NOW + timedelta(hours=1)),                    # dated in the future
    dict(read_at=NOW - timedelta(hours=1), declared=False),    # market defaulted, not declared
    dict(read_at=NOW - timedelta(hours=1), market="SG", currency="SGD", declared=False),
])
async def test_one_class_offer_without_a_valid_read_makes_the_row_unread(db, bad):
    """B1/B2/clock: one live served-market offer without a valid read -- whatever
    its market -- and the row has no read at all."""
    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=2))
    await _offer(db, RETAILER, link="https://shop.example.com/p/2", **bad)
    assert await _reads(db, [RETAILER]) == {}


async def test_a_fresh_market_does_not_carry_a_stale_one(db):
    """B1: an SG read 2h old next to a US read 60 days old dates the row 60 days."""
    await _product(db, RETAILER)
    stale = NOW - timedelta(days=60)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=2), market="SG", currency="SGD")
    await _offer(db, RETAILER, read_at=stale, link="https://shop.example.com/p/2")
    assert await _reads(db, [RETAILER]) == {RETAILER: stale}


async def test_at_least_one_class_offer_must_be_in_stock(db):
    await _product(db, RETAILER)
    oid = await _offer(db, RETAILER, read_at=NOW - timedelta(hours=1), availability="out_of_stock")
    assert await _reads(db, [RETAILER]) == {}
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=2), availability=" In_Stock ", variant="2")
    assert await _reads(db, [RETAILER]) == {RETAILER: NOW - timedelta(hours=2)}
    await db.execute("UPDATE catalog_offers SET availability = ' Sold_Out ' WHERE offer_id <> :o", {"o": oid})
    assert await _reads(db, [RETAILER]) == {}


async def test_a_stale_variant_makes_the_row_stale_and_a_suppressed_one_does_not(db):
    await _product(db, RETAILER)
    variant_read = NOW - timedelta(days=12)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=1))           # ::canonical, fresh
    oid = await _offer(db, RETAILER, read_at=variant_read, variant="111")  # the variant, old
    assert await _reads(db, [RETAILER]) == {RETAILER: variant_read}
    await db.execute("UPDATE catalog_offers SET suppressed_at = now() WHERE offer_id = :o", {"o": oid})
    assert await _reads(db, [RETAILER]) == {RETAILER: NOW - timedelta(hours=1)}


async def test_the_statement_binds_one_clock(db):
    """:now decides 'future', not the database clock."""
    await _product(db, RETAILER)
    read = NOW - timedelta(hours=1)
    await _offer(db, RETAILER, read_at=read)
    assert await _reads(db, [RETAILER], now=read - timedelta(seconds=1)) == {}
    assert await _reads(db, [RETAILER], now=read) == {RETAILER: read}


async def test_the_statement_reads_the_payload_link_first_and_trims_it(db):
    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW, link="garbage", payload_link="  https://shop.example.com/p/2 \n")
    assert await _reads(db, [RETAILER]) == {RETAILER: NOW}


async def test_the_statement_answers_only_for_the_keys_asked_and_omits_unread_rows(db):
    await _product(db, RETAILER)
    await _product(db, RETAILER_2)
    await _product(db, BRAND, role="brand_official")
    await _offer(db, RETAILER, read_at=NOW)
    await _offer(db, RETAILER_2, read_at=None)
    await _offer(db, BRAND, read_at=NOW - timedelta(days=2))
    assert await _reads(db, [RETAILER_2, BRAND]) == {BRAND: NOW - timedelta(days=2)}


async def test_a_price_moved_without_a_read_forgets_the_stamp_so_nothing_is_sticky(db):
    await _product(db, RETAILER)
    oid = await _offer(db, RETAILER, read_at=NOW)
    assert await _reads(db, [RETAILER]) == {RETAILER: NOW}
    await db.execute("UPDATE catalog_offers SET list_price = 25, merchant_effective_price = 25"
                     " WHERE offer_id = :o", {"o": oid})
    assert await _reads(db, [RETAILER]) == {}


# ---------------------------------------------------------------------------
# End to end through the real trust join
# ---------------------------------------------------------------------------


async def test_end_to_end_off_on_stale(db, monkeypatch):
    from services import catalog_row_trust_upserter as up

    await _product(db, RETAILER)
    await _product(db, BRAND, role="brand_official")
    await _offer(db, RETAILER, read_at=NOW - timedelta(days=2))
    oid = await _offer(db, BRAND, read_at=NOW - timedelta(hours=3), link="https://brand.example.com/p")

    assert await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER, BRAND]) == 2
    for pk in (RETAILER, BRAND):
        row = await _trust(db, pk)
        assert row["serving_decision"] == "shadow"
        assert list(row["serving_reason_codes"]) == ["IDENTITY_CONFIDENCE_NULL"]

    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    assert await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER, BRAND]) == 2
    for pk in (RETAILER, BRAND):
        row = await _trust(db, pk)
        assert row["serving_decision"] == "public"
        assert list(row["serving_reason_codes"]) == ["CRAWL_FRESH", "PUBLIC_PASSTHROUGH"]

    # The read ages out -> the next recompute is today's decision again.
    await db.execute("UPDATE catalog_offers SET price_checked_at = now() - interval '8 days'"
                     " WHERE offer_id = :o", {"o": oid})
    assert await up.upsert_catalog_row_trust(db=db, product_key=BRAND) is True
    row = await _trust(db, BRAND)
    assert row["serving_decision"] == "shadow"
    assert list(row["serving_reason_codes"]) == ["IDENTITY_CONFIDENCE_NULL"]

    # A narrower N is honoured.
    monkeypatch.setenv(up.CRAWL_FRESH_DAYS_ENV, "1")
    assert await up.upsert_catalog_row_trust(db=db, product_key=RETAILER) is True
    assert (await _trust(db, RETAILER))["serving_decision"] == "shadow"


async def test_end_to_end_without_the_246_column_nothing_breaks_and_nothing_moves(db_no_246, monkeypatch):
    from services import catalog_row_trust_upserter as up

    db = db_no_246
    await _product(db, RETAILER)
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, list_price, currency,"
        " source_system, source_ref) VALUES ('o_no246', 'sk', :pk, 'm', 10, 'USD', :ss,"
        " 'https://shop.example.com/p')", {"pk": RETAILER, "ss": DRAIN})
    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    # Inside a transaction: a statement naming the missing column would abort it.
    async with db.transaction():
        assert await up.upsert_catalog_row_trust(db=db, product_key=RETAILER) is True
        assert (await _trust(db, RETAILER))["serving_decision"] == "shadow"


async def _tick(monkeypatch, db, limit):
    import db.database as dbmod
    from jobs import catalog_row_trust_backfill_cron as cron

    monkeypatch.setattr(dbmod, "database", db)
    monkeypatch.setenv("CATALOG_TRUST_BACKFILL_LIMIT", str(limit))
    await cron.run_catalog_row_trust_backfill_tick()


async def _decay_fixture(db, monkeypatch):
    from services import catalog_row_trust_upserter as up

    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    await _product(db, RETAILER)
    oid = await _offer(db, RETAILER, read_at=NOW - timedelta(days=2))
    older = [f"ext:retailer:{i:032d}" for i in range(4)]
    for pk in older:
        await _product(db, pk, seed=False)
    await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER] + older)
    assert list((await _trust(db, RETAILER))["serving_reason_codes"]) == ["CRAWL_FRESH", "PUBLIC_PASSTHROUGH"]
    # Four rows the oldest-first pass reaches before it, with LIMIT 2.
    await db.execute("UPDATE catalog_row_trust SET updated_at = now() - interval '30 days'"
                     " WHERE subject_key <> :pk", {"pk": RETAILER})
    # The read ages out.
    await db.execute("UPDATE catalog_offers SET price_checked_at = now() - interval '8 days'"
                     " WHERE offer_id = :o", {"o": oid})
    return older


async def test_round_two_an_expired_crawl_fresh_row_returns_to_shadow_on_the_next_tick(db, monkeypatch):
    await _decay_fixture(db, monkeypatch)
    await _tick(monkeypatch, db, limit=2)
    row = await _trust(db, RETAILER)
    assert row["serving_decision"] == "shadow"
    assert list(row["serving_reason_codes"]) == ["IDENTITY_CONFIDENCE_NULL"]


async def test_round_two_without_the_recheck_the_expired_row_would_stay_public(db, monkeypatch):
    """The control: the defect the recheck closes, on the same fixture."""
    from services import catalog_row_trust_upserter as up

    await _decay_fixture(db, monkeypatch)

    async def no_recheck(**kw):
        return []

    monkeypatch.setattr(up, "recheck_crawl_fresh_rows", no_recheck)
    await _tick(monkeypatch, db, limit=2)
    assert (await _trust(db, RETAILER))["serving_decision"] == "public"


async def test_round_two_switching_off_rolls_every_crawl_fresh_row_back_on_the_next_tick(db, monkeypatch):
    from services import catalog_row_trust_upserter as up

    await _decay_fixture(db, monkeypatch)
    await db.execute("UPDATE catalog_offers SET price_checked_at = now() - interval '1 day'")
    monkeypatch.delenv(up.CRAWL_FRESH_ENV)
    await _tick(monkeypatch, db, limit=2)
    assert (await _trust(db, RETAILER))["serving_decision"] == "shadow"


async def test_round_two_a_row_whose_qualifying_offer_went_away_is_rechecked(db, monkeypatch):
    """No qualifying read at all (the fresh offer was suppressed after the flip)
    is the other way a row ages out: it must be picked up too."""
    from services import catalog_row_trust_upserter as up

    await _product(db, RETAILER)
    oid = await _offer(db, RETAILER, read_at=NOW - timedelta(days=1))
    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER])
    assert (await _trust(db, RETAILER))["serving_decision"] == "public"
    await db.execute("UPDATE catalog_offers SET suppressed_at = now() WHERE offer_id = :o", {"o": oid})
    assert await up.recheck_crawl_fresh_rows(db=db) == [RETAILER]
    assert (await _trust(db, RETAILER))["serving_decision"] != "public"


async def test_only_rows_that_have_aged_out_are_rechecked(db, monkeypatch):
    from services import catalog_row_trust_upserter as up

    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(days=6, hours=20))  # still inside N
    await _product(db, RETAILER_2)
    oid = await _offer(db, RETAILER_2, read_at=NOW - timedelta(days=1))
    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER, RETAILER_2])
    await db.execute("UPDATE catalog_offers SET price_checked_at = now() - interval '7 days 1 hour'"
                     " WHERE offer_id = :o", {"o": oid})
    assert await up.recheck_crawl_fresh_rows(db=db) == [RETAILER_2]
    assert (await _trust(db, RETAILER))["serving_decision"] == "public"
    assert (await _trust(db, RETAILER_2))["serving_decision"] == "shadow"


async def test_an_orphan_cannot_starve_the_recheck(db, monkeypatch):
    """A CRAWL_FRESH trust row whose catalog row is gone is demoted directly and
    never holds a slot: with limit=1 the live expired row is still re-derived."""
    from services import catalog_row_trust_upserter as up

    await _decay_fixture(db, monkeypatch)
    orphan = "ext:retailer:" + "f" * 32
    await db.execute(
        "INSERT INTO catalog_row_trust (subject_type, subject_key, product_key, source_lifecycle_state,"
        " identity_status, freshness_state, serving_decision, serving_reason_codes, policy_version, updated_at)"
        " VALUES ('product', :pk, :pk, 'active', 'unknown', 'fresh', 'public',"
        " CAST(ARRAY['CRAWL_FRESH','PUBLIC_PASSTHROUGH'] AS text[]), 'c1.v0.9', now() - interval '90 days')",
        {"pk": orphan})
    assert await up.recheck_crawl_fresh_rows(db=db, limit=1) == [RETAILER]
    row = await db.fetch_one("SELECT serving_decision, serving_reason_codes, source_lifecycle_state"
                             " FROM catalog_row_trust WHERE subject_key = :pk", {"pk": orphan})
    assert (row["serving_decision"], list(row["serving_reason_codes"]), row["source_lifecycle_state"]) == (
        "blocked", ["ROW_TOMBSTONED"], "tombstoned")
    assert (await _trust(db, RETAILER))["serving_decision"] == "shadow"
    # and the switched-off rollback is not starved by it either
    assert await up.recheck_crawl_fresh_rows(db=db, limit=1) == []


async def test_the_orphan_demote_leaves_a_row_re_derived_while_it_waited(db):
    """EvalPlanQual: the demote picked an orphan (snapshot: CRAWL_FRESH, no
    catalog row), but a concurrent transaction re-created the catalog row and
    re-derived the trust row first. The UPDATE re-checks its OUTER quals on the
    new version, so it must leave that row alone."""
    import asyncio

    from services import catalog_row_trust_upserter as up

    orphan = "ext:retailer:" + "9" * 32
    await db.execute(
        "INSERT INTO catalog_row_trust (subject_type, subject_key, product_key, source_lifecycle_state,"
        " identity_status, freshness_state, serving_decision, serving_reason_codes, policy_version, updated_at)"
        " VALUES ('product', :pk, :pk, 'active', 'unknown', 'fresh', 'public',"
        " CAST(ARRAY['CRAWL_FRESH','PUBLIC_PASSTHROUGH'] AS text[]), 'c1.v0.9', now())", {"pk": orphan})
    other = await _asyncpg(_SCHEMA)
    tx = other.transaction()
    await tx.start()
    try:
        await other.execute(
            "UPDATE catalog_row_trust SET serving_decision = 'shadow',"
            " serving_reason_codes = CAST(ARRAY['IDENTITY_CONFIDENCE_NULL'] AS text[])"
            " WHERE subject_key = $1", orphan)
        demote = asyncio.ensure_future(db.fetch_all(up._CRAWL_FRESH_ORPHAN_DEMOTE_SQL, {"limit": 10}))
        for _ in range(100):  # until the demote waits on the row lock
            waiting = await other.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                " AND query LIKE 'UPDATE catalog_row_trust crt%'")
            if waiting:
                break
            await asyncio.sleep(0.05)
        assert waiting, "the demote never waited on the re-derived row"
        assert not demote.done()
        # the catalog row comes back in the same transaction (the upserter's re-derive)
        await _product(_AsyncpgExec(other), orphan, seed=False, ips=False)
        await tx.commit()
    except BaseException:
        await tx.rollback()
        raise
    finally:
        demoted = await demote
        await other.close()
    assert demoted == []
    row = await _trust(db, orphan)
    assert (row["serving_decision"], list(row["serving_reason_codes"])) == ("shadow", ["IDENTITY_CONFIDENCE_NULL"])


class _AsyncpgExec:
    """`databases`-style execute(sql, {name: value}) over an asyncpg connection,
    so _product can write inside another session's open transaction."""

    def __init__(self, conn):
        self.conn = conn

    async def execute(self, sql, values=None):
        import re

        names = []

        def sub(m):
            names.append(m.group(1))
            return f"${len(names)}"
        return await self.conn.execute(re.sub(r"(?<!:):([a-z_]+)", sub, sql), *[values[n] for n in names])


async def test_a_failed_probe_inside_the_caller_s_transaction_is_absent_not_an_abort(db, monkeypatch):
    from services import catalog_row_trust_upserter as up
    from services import external_offer_dual_write as dw

    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=1))
    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    monkeypatch.setattr(dw, "PRICE_CHECK_COLUMN_SQL", "SELECT no_such_column AS present FROM pg_class LIMIT 1")
    async with db.transaction():
        assert await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER]) == 1
        assert await db.fetch_val("SELECT 1") == 1
    assert (await _trust(db, RETAILER))["serving_decision"] == "shadow"


async def test_a_failed_read_inside_the_caller_s_transaction_writes_flag_off_verdicts(db, monkeypatch):
    """Addition 5: fail closed per batch inside a SAVEPOINT -- the caller's
    transaction survives, the batch is written with flag-off verdicts."""
    from services import catalog_row_trust_upserter as up

    await _product(db, RETAILER)
    await _offer(db, RETAILER, read_at=NOW - timedelta(hours=1))
    monkeypatch.setenv(up.CRAWL_FRESH_ENV, "1")
    monkeypatch.setattr(up, "CRAWL_ROW_PRICE_READ_SQL",
                        up.CRAWL_ROW_PRICE_READ_SQL.replace("co.price_checked_at", "co.no_such_column", 1))
    async with db.transaction():
        assert await up.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER]) == 1
        # the outer transaction is still usable
        assert await db.fetch_val("SELECT 1") == 1
    row = await _trust(db, RETAILER)
    assert row["serving_decision"] == "shadow"
    assert list(row["serving_reason_codes"]) == ["IDENTITY_CONFIDENCE_NULL"]


async def test_the_column_probe_answers_both_ways(db, db_no_246):
    from services import catalog_row_trust_upserter as up

    assert await up._price_check_column_present(db) is True
    assert await up._price_check_column_present(db_no_246) is False


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


async def _asyncpg(schema):
    import asyncpg

    return await asyncpg.connect(_url(), server_settings={"search_path": schema})


async def _census_fixture(db):
    from services import catalog_row_trust_upserter as up

    keys = {}
    # flips: retailer read 1d20h ago (US) and brand-official read 6h ago (SG)
    keys["r_fresh"] = "ext:retailer:" + "a" * 32
    await _product(db, keys["r_fresh"], role="retailer", host="shop.example.com")
    await _offer(db, keys["r_fresh"], read_at=NOW - timedelta(days=1, hours=20))
    keys["b_fresh"] = "ext:serum-a::00000001"
    await _product(db, keys["b_fresh"], role="brand_official", host="brand.example.com")
    await _offer(db, keys["b_fresh"], read_at=NOW - timedelta(hours=6), market="SG", currency="SGD",
                 link="https://brand.example.com/p")
    # fresh but a non-canonical duplicate: still blocked
    keys["r_dup"] = "ext:retailer:" + "b" * 32
    ck = await _product(db, keys["r_dup"], host="shop.example.com")
    await _offer(db, keys["r_dup"], read_at=NOW - timedelta(hours=1))
    await db.execute("INSERT INTO content_canonical_election (content_key, canonical_sig_id)"
                     " VALUES (:ck, 'sig_someone_else')", {"ck": ck})
    # fresh but no seed -> PDP would not render (a seller that is not an
    # observed seller, or the seed-less row would sit in the observed tier)
    keys["r_norender"] = "ext:retailer:" + "c" * 32
    await _product(db, keys["r_norender"], seed=False, host="other.example.com",
                   merchant="merch_retail_0123456789ab")
    await _offer(db, keys["r_norender"], read_at=NOW - timedelta(days=1, hours=1))
    # stale (10d) and never read
    keys["r_stale"] = "ext:retailer:" + "d" * 32
    await _product(db, keys["r_stale"], host="other.example.com")
    await _offer(db, keys["r_stale"], read_at=NOW - timedelta(days=10))
    keys["b_never"] = "ext:serum-b::00000002"
    await _product(db, keys["b_never"], role="brand_official", host="brand.example.com")
    await _offer(db, keys["b_never"], read_at=None, link="https://brand.example.com/q")
    # not counted: an `ext:` key under another writer, and a suppressed drain row
    await _product(db, "ext:mirror-slug::0123abcd", source_system="external_product_seeds_mirror_v1", seed=False)
    await _offer(db, "ext:mirror-slug::0123abcd", read_at=NOW, source_system="external_product_seeds_mirror_v1")
    await _product(db, "ext:retailer:" + "e" * 32, seed=False)
    await _offer(db, "ext:retailer:" + "e" * 32, read_at=NOW)
    await db.execute("UPDATE catalog_products SET suppression_reason = 'retired' WHERE product_key = :pk",
                     {"pk": "ext:retailer:" + "e" * 32})
    # not a drain-lane row at all
    await _product(db, OTHER, source_system="external_product_seeds_mirror_v1")
    await _offer(db, OTHER, read_at=NOW, source_system="external_product_seeds_mirror_v1")
    await db.execute("INSERT INTO external_product_seeds (id, status, market) VALUES"
                     " ('pool_1', 'active', 'US'), ('pool_2', 'active', 'us'), ('pool_3', 'inactive', 'US'),"
                     " ('pool_4', 'active', 'SG')")
    # B2's trade-off: drain jobs with and without an explicit market
    await db.execute("INSERT INTO retailer_ingest_jobs (id, domain, brand, options) VALUES"
                     " ('j1', 'shop.example.com', 'x', CAST('{}' AS jsonb)),"
                     " ('j2', 'shop.example.com', 'y', CAST(:us AS jsonb)),"
                     " ('j3', 'brand.example.com', 'z', CAST(:sg AS jsonb))",
                     {"us": '{"market": "US"}', "sg": '{"market": "SG"}'})
    # employee-edit dry run: b_never's seed price was set by hand (it differs from the page read)
    snapshot = '{"snapshot": {"price_amount": "20.00"}}'
    await db.execute("UPDATE external_product_seeds SET price_amount = 25, seed_data = CAST(:sd AS jsonb)"
                     " WHERE attached_product_key = :pk", {"pk": keys["b_never"], "sd": snapshot})
    for unedited in ("r_fresh", "r_stale"):  # stored price == page read: not an edit
        await db.execute("UPDATE external_product_seeds SET price_amount = 20, seed_data = CAST(:sd AS jsonb)"
                         " WHERE attached_product_key = :pk", {"pk": keys[unedited], "sd": snapshot})
    await up.upsert_catalog_row_trust_many(db=db, product_keys=list(keys.values()) + [OTHER])
    return keys


def _by(lines, kind, **kw):
    return [line for line in lines if line["kind"] == kind and all(line.get(k) == v for k, v in kw.items())]


async def test_census_counts_by_lane_bucket_market_host(db):
    from scripts import census_retailer_crawl_fresh_trust as census

    await _census_fixture(db)
    conn = await _asyncpg(_SCHEMA)
    try:
        lines = await census.collect(conn, max_age_days=7, refresh_per_night=2, seed_pool=None)
    finally:
        await conn.close()

    params = _by(lines, "params")[0]
    # active seeds: the 6 attached fixture seeds (all US) + pool_1/pool_2 ('us' folds) = 8 US, 1 SG
    assert params["active_seeds_by_market"] == {"SG": 1, "US": 8}
    assert params["seed_pool"] == 8 and params["nightly_reread_probability"] == 0.25

    total = _by(lines, "summary", lane="all")[0]
    assert total["rows"] == 6  # the mirror row is not a drain-lane row
    assert total["by_decision"] == {"shadow": 6}
    assert total["would_flip"] == 2
    assert total["would_flip_per_market_rule"] == 2
    assert total["would_flip_on_out_of_stock_only_offer"] == 0
    jobs = _by(lines, "drain_job_markets")[0]
    assert (jobs["jobs"], jobs["jobs_with_explicit_market"]) == (3, 2)
    assert jobs["by_market"] == {"(default)": {"jobs": 1, "hosts": 1}, "US": {"jobs": 1, "hosts": 1},
                                 "SG": {"jobs": 1, "hosts": 1}}
    edited = _by(lines, "employee_edit_dry_run")[0]
    assert (edited["rows_with_hand_edited_seed"], edited["hand_edited_seeds"]) == (1, 1)
    assert _by(lines, "served_market_currency")[0]["pairs"] == [["SG", "SGD"], ["US", "USD"]]
    assert total["would_flip_lost_to_undeclared_market"] == 0
    assert total["fresh_in_one_market_stale_in_another"] == 0
    coverage = _by(lines, "backfill_coverage")[0]
    assert coverage["catalog_products"] == 9 and coverage["trust_rows"] == 7
    assert coverage["crawl_fresh_rows"] == 0
    assert coverage["backfill_limit_default"] == 50000 and coverage["over_default_limit"] is False
    assert total["fresh_but_not_renderable"] == 1
    assert total["fresh_still_blocked_by_reason"] == {"non_canonical": 1}
    assert total["would_flip_if_n_days"] == {"1": 1, "3": 2, "7": 2, "14": 3}
    retailer = _by(lines, "summary", lane="retailer")[0]
    brand = _by(lines, "summary", lane="brand_official")[0]
    assert (retailer["rows"], retailer["would_flip"]) == (4, 1)
    assert (brand["rows"], brand["would_flip"]) == (2, 1)
    # r_dup and r_norender carry no source_role -> 'unlabelled'... except they are
    # retailer keys, so the key decides: the lane split is retailer vs the rest.
    assert {line["lane"] for line in _by(lines, "summary")} == {"all", "retailer", "brand_official"}

    buckets = {line["bucket"]: line for line in _by(lines, "by_age_bucket", lane="all")}
    assert buckets["le_1d"]["rows"] == 2 and buckets["le_1d"]["would_flip"] == 1
    assert buckets["le_3d"]["rows"] == 2 and buckets["le_3d"]["would_flip"] == 1
    assert buckets["le_7d"]["rows"] == 0
    assert buckets["le_14d"]["rows"] == 1
    assert buckets["gt_14d"]["rows"] == 0
    assert buckets["null"]["rows"] == 1

    markets = {line["market"]: line for line in _by(lines, "by_market", lane="all")}
    assert markets["US"]["would_flip"] == 1 and markets["SG"]["would_flip"] == 1

    hosts = {(line["lane"], line["host"]): line for line in _by(lines, "by_host")}
    assert hosts[("retailer", "shop.example.com")]["would_flip"] == 1
    assert hosts[("retailer", "shop.example.com")]["fresh_still_blocked_by_reason"] == 1
    assert hosts[("retailer", "other.example.com")]["fresh_not_renderable"] == 1
    assert hosts[("brand_official", "brand.example.com")]["would_flip"] == 1

    proj = {line["scenario"]: line for line in _by(lines, "projection", lane="all", rule="max_any")}
    # candidates: r_fresh, b_fresh, r_stale, b_never (renderable, coverage-only, with an offer)
    no = proj["no_reread"]["by_day"]
    assert no["0"] == 2 and no["5"] == 2 and no["6"] == 1 and no["7"] == 0
    assert proj["no_reread"]["steady_state"] == 0
    weekly = proj["drain_weekly"]
    assert weekly["by_day"]["7"] == 4 and weekly["steady_state"] == 4
    refresh = proj["refresh_pro_rata"]
    assert refresh["by_day"]["0"] == 2
    assert refresh["steady_state_round_robin"] == 4  # 7 * 0.25 >= 1


async def test_census_per_market_rule_out_of_stock_and_variants(db):
    """The final per-market rule against the max-any rule, B1 and B2 sized."""
    from scripts import census_retailer_crawl_fresh_trust as census
    from services import catalog_row_trust_upserter as up

    keys = {k: "ext:retailer:" + c * 32 for k, c in
            (("ok", "1"), ("oos", "2"), ("cross", "3"), ("variant", "4"), ("future", "5"), ("edit", "6"),
             ("cur", "7"), ("b1", "8"), ("undeclared", "9"))}
    for pk in keys.values():
        await _product(db, pk)
    fresh, stale = NOW - timedelta(days=1), NOW - timedelta(days=20)
    await _offer(db, keys["ok"], read_at=fresh, availability="in_stock")
    # the only fresh offer is out of stock
    await _offer(db, keys["oos"], read_at=fresh, availability="out_of_stock")
    await _offer(db, keys["oos"], read_at=stale, availability="in_stock")
    # fresh only in a non-served market; the US offer is weeks old
    await _offer(db, keys["cross"], read_at=fresh, market="AU", currency="AUD")
    await _offer(db, keys["cross"], read_at=stale)
    # variant-scoped: the canonical row is fresh, the variant row is not
    await _offer(db, keys["variant"], read_at=fresh)
    await _offer(db, keys["variant"], read_at=stale, variant="111")
    # dated in the future
    await _offer(db, keys["future"], read_at=NOW + timedelta(days=2))
    # dated by an employee price edit
    await _offer(db, keys["edit"], read_at=fresh, read_source="employee_edit")
    # US market, but priced in another currency
    await _offer(db, keys["cur"], read_at=fresh, currency="SGD")
    # B1: fresh in SG, a stale live US offer
    await _offer(db, keys["b1"], read_at=fresh, market="SG", currency="SGD")
    await _offer(db, keys["b1"], read_at=stale, link="https://shop.example.com/p/2")
    # B2: fresh, but its market was defaulted, not declared
    await _offer(db, keys["undeclared"], read_at=fresh, declared=False)
    await up.upsert_catalog_row_trust_many(db=db, product_keys=list(keys.values()))

    conn = await _asyncpg(_SCHEMA)
    try:
        lines = await census.collect(conn, max_age_days=7)
    finally:
        await conn.close()
    total = _by(lines, "summary", lane="all")[0]
    assert total["would_flip_max_any_rule"] == 9
    assert total["would_flip_per_market_rule"] == 1
    assert total["would_flip_max_any_not_per_market"] == 8
    assert total["would_flip_on_out_of_stock_only_offer"] == 1
    assert total["would_flip_without_declared_market_requirement"] == 2
    assert total["would_flip_lost_to_undeclared_market"] == 1
    assert total["fresh_in_one_market_stale_in_another"] == 1
    markets = {line["market"]: line for line in _by(lines, "by_market", lane="all")}
    assert markets["US"]["would_flip_per_market_rule"] == 1
    assert markets["AU"]["would_flip"] == 1 and markets["AU"].get("would_flip_per_market_rule", 0) == 0
    proj = {(line["rule"], line["scenario"]): line for line in _by(lines, "projection", lane="all")}
    assert proj[("max_any", "no_reread")]["by_day"]["0"] == 9
    assert proj[("per_market", "no_reread")]["by_day"]["0"] == 1


async def test_census_sizes_round_four_by_reason(db):
    """`would_flip_but_carries_live_non_class_offer`: rows round 3's class would
    flip that carry a live offer round 4 refuses, per reason, and each row once."""
    from scripts import census_retailer_crawl_fresh_trust as census
    from services import catalog_row_trust_upserter as up

    keys = {k: "ext:retailer:" + c * 32 for k, c in
            (("clean", "a"), ("aff", "b"), ("copy", "c"), ("au", "d"), ("cur", "e"), ("multi", "f"),
             ("stale", "0"))}
    for pk in keys.values():
        await _product(db, pk)
    fresh, later = NOW - timedelta(days=1), NOW - timedelta(hours=1)
    for name, pk in keys.items():
        await _offer(db, pk, read_at=NOW - timedelta(days=20) if name == "stale" else fresh)
    other = "https://shop.example.com/p/2"
    await _offer(db, keys["aff"], read_at=later, affiliate=True, link=other)
    await _offer(db, keys["copy"], read_at=later, source_system="variant_identity_backfill_v1", variant="2",
                 link=other)
    await _offer(db, keys["au"], read_at=later, market="AU", currency="AUD", link=other)
    await _offer(db, keys["cur"], read_at=later, currency="SGD", link=other)
    await _offer(db, keys["multi"], read_at=later, affiliate=True, link=other)
    await _offer(db, keys["multi"], read_at=later, market="SG", currency="USD", link=other + "x")
    await _offer(db, keys["multi"], read_at=later, market="AU", currency="USD", link=other + "y")
    await _offer(db, keys["stale"], read_at=later, affiliate=True, link=other)  # round 3 would not flip it
    await up.upsert_catalog_row_trust_many(db=db, product_keys=list(keys.values()))

    conn = await _asyncpg(_SCHEMA)
    try:
        lines = await census.collect(conn, max_age_days=7)
        db_now = await conn.fetchval("SELECT now()")
    finally:
        await conn.close()
    total = _by(lines, "summary", lane="all")[0]
    assert total["would_flip_round3_class_rule"] == 6
    assert total["would_flip_per_market_rule"] == 1  # clean
    assert total["would_flip_but_carries_live_non_class_offer"] == {
        "affiliate": 2, "non_drain_source": 1, "non_served_market": 2, "currency_mismatch": 2, "total": 5}
    # total = round 3's flips that round 4 refuses
    assert total["would_flip_round3_class_rule"] - total["would_flip_per_market_rule"] == 5
    assert await _reads(db, list(keys.values()), now=db_now) == {keys["clean"]: fresh}


async def test_census_runs_read_only_with_a_statement_timeout(db):
    from scripts import census_retailer_crawl_fresh_trust as census

    await _census_fixture(db)
    conn = await _asyncpg(_SCHEMA)
    seen = {}

    class Spy:
        def transaction(self, **kw):
            return conn.transaction(**kw)

        async def execute(self, sql, *args):
            return await conn.execute(sql, *args)

        async def fetchval(self, sql, *args):
            return await conn.fetchval(sql, *args)

        async def fetchrow(self, sql, *args):
            return await conn.fetchrow(sql, *args)

        async def fetch(self, sql, *args):
            if "LEFT JOIN LATERAL" in sql:
                seen["read_only"] = await conn.fetchval("SHOW transaction_read_only")
                seen["timeout"] = await conn.fetchval("SHOW statement_timeout")
            return await conn.fetch(sql, *args)

    try:
        await census.collect(Spy(), max_age_days=7)
        assert seen == {"read_only": "on", "timeout": "5min"}
        # and the session is back to normal afterwards (SET LOCAL)
        assert await conn.fetchval("SHOW statement_timeout") == "0"
    finally:
        await conn.close()


async def test_census_refuses_a_database_without_the_246_column(db_no_246):
    from scripts import census_retailer_crawl_fresh_trust as census

    conn = await _asyncpg(_SCHEMA_NO_246)
    try:
        lines = await census.collect(conn, max_age_days=7)
    finally:
        await conn.close()
    assert [line["kind"] for line in lines] == ["error"]


async def test_census_last_read_equals_the_upserter_statement(db):
    """The census re-spells the offer predicate (the image may predate it):
    on the same rows it must date every drain-lane row exactly as the
    upserter's statement does."""
    from scripts import census_retailer_crawl_fresh_trust as census

    keys = await _census_fixture(db)
    await _offer(db, keys["r_stale"], read_at=NOW, affiliate=True)
    await _offer(db, keys["r_stale"], read_at=NOW, suppressed=True)
    # round 2 exclusions, each NEWER than the row's qualifying read
    await _offer(db, keys["r_fresh"], read_at=NOW, market="AU", currency="AUD")
    await _offer(db, keys["r_fresh"], read_at=NOW, currency="SGD")
    await _offer(db, keys["r_fresh"], read_at=NOW, availability="out_of_stock")
    await _offer(db, keys["r_fresh"], read_at=NOW, read_source="employee_edit")
    await _offer(db, keys["r_dup"], read_at=NOW + timedelta(days=1))
    await _offer(db, keys["b_fresh"], read_at=NOW - timedelta(days=9), variant="9")
    # a SUPPRESSED variant does not make r_fresh variant-scoped
    await _offer(db, keys["r_fresh"], read_at=None, variant="5", suppressed=True)
    conn = await _asyncpg(_SCHEMA)
    try:
        db_now = await conn.fetchval("SELECT now()")
        rows = {r["product_key"]: census.classify(dict(r), now=db_now, max_age_days=7)["strict_read_at"]
                for r in await conn.fetch(census.census_sql())}
    finally:
        await conn.close()
    expected = await _reads(db, list(keys.values()), now=db_now)
    assert set(rows) == set(keys.values())
    assert {k: v for k, v in rows.items() if v is not None} == expected
    # r_fresh: its out-of-stock and employee-edited offers are in the class, so it has no read
    assert keys["r_fresh"] not in expected
    assert expected[keys["b_fresh"]] == NOW - timedelta(days=9)  # every class offer counts
