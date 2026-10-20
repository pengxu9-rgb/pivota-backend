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
  created_at timestamptz, updated_at timestamptz
);
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
    for table in ("catalog_offers", "catalog_products", "external_product_seeds",
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
                 availability="in_stock", variant=None, read_source=None):
    import json

    _N[0] += 1
    oid = f"offer_{_N[0]}"
    payload = {"validated_at": "affiliate_feed:net" if affiliate else "2026-10-01"}
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


async def _reads(db, keys):
    from services.catalog_row_trust_upserter import CRAWL_LAST_PRICE_READ_SQL

    rows = await db.fetch_all(CRAWL_LAST_PRICE_READ_SQL, {"product_keys": keys})
    return {r["product_key"]: r["last_price_read_at"] for r in rows}


async def _trust(db, pk):
    return await db.fetch_one(
        "SELECT serving_decision, serving_reason_codes FROM catalog_row_trust"
        " WHERE subject_type = 'product' AND subject_key = :pk", {"pk": pk})


# ---------------------------------------------------------------------------
# The freshness statement, both ways
# ---------------------------------------------------------------------------


async def test_the_statement_takes_the_newest_qualifying_read_only(db):
    await _product(db, RETAILER)
    newest = NOW - timedelta(days=1)
    await _offer(db, RETAILER, read_at=NOW - timedelta(days=3))
    await _offer(db, RETAILER, read_at=newest)
    # Each of these is NEWER and must be ignored:
    await _offer(db, RETAILER, read_at=NOW, suppressed=True)
    await _offer(db, RETAILER, read_at=NOW, source_system="shopify_markets_us_localization")
    await _offer(db, RETAILER, read_at=NOW, affiliate=True)
    await _offer(db, RETAILER, read_at=NOW, link="ftp://shop.example.com/p/1")
    await _offer(db, RETAILER, read_at=NOW, link="https://shop.example.com/p 1")
    await _offer(db, RETAILER, read_at=NOW, link="/p/1")
    await _offer(db, RETAILER, read_at=NOW, price=0)
    await _offer(db, RETAILER, read_at=NOW, price=None)
    await _offer(db, RETAILER, read_at=NOW, link=None, payload_link="not a url")
    await _offer(db, RETAILER, read_at=None)
    assert await _reads(db, [RETAILER]) == {RETAILER: newest}


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
    old = NOW - timedelta(days=5)
    await _offer(db, RETAILER, read_at=old)
    oid = await _offer(db, RETAILER, read_at=NOW)
    await db.execute("UPDATE catalog_offers SET list_price = 25, merchant_effective_price = 25"
                     " WHERE offer_id = :o", {"o": oid})
    assert await _reads(db, [RETAILER]) == {RETAILER: old}


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
    """Round 2 (b) and (c): the per-market rule against the max-any rule."""
    from scripts import census_retailer_crawl_fresh_trust as census
    from services import catalog_row_trust_upserter as up

    keys = {k: "ext:retailer:" + c * 32 for k, c in
            (("ok", "1"), ("oos", "2"), ("cross", "3"), ("variant", "4"), ("future", "5"), ("edit", "6"),
             ("cur", "7"))}
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
    await up.upsert_catalog_row_trust_many(db=db, product_keys=list(keys.values()))

    conn = await _asyncpg(_SCHEMA)
    try:
        lines = await census.collect(conn, max_age_days=7)
    finally:
        await conn.close()
    total = _by(lines, "summary", lane="all")[0]
    assert total["would_flip_max_any_rule"] == 7
    assert total["would_flip_per_market_rule"] == 1
    assert total["would_flip_max_any_not_per_market"] == 6
    assert total["would_flip_on_out_of_stock_only_offer"] == 1
    markets = {line["market"]: line for line in _by(lines, "by_market", lane="all")}
    assert markets["US"]["would_flip_per_market_rule"] == 1
    assert markets["AU"]["would_flip"] == 1 and markets["AU"].get("would_flip_per_market_rule", 0) == 0
    proj = {(line["rule"], line["scenario"]): line for line in _by(lines, "projection", lane="all")}
    assert proj[("max_any", "no_reread")]["by_day"]["0"] == 7
    assert proj[("per_market", "no_reread")]["by_day"]["0"] == 1


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
    expected = await _reads(db, list(keys.values()))
    conn = await _asyncpg(_SCHEMA)
    try:
        rows = {r["product_key"]: r["last_price_read_at"] for r in await conn.fetch(census.census_sql())}
    finally:
        await conn.close()
    assert set(rows) == set(keys.values())
    assert {k: v for k, v in rows.items() if v is not None} == expected
