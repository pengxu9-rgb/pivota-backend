"""The purchase path refuses offers copied off a seed in another currency (real Postgres).

Peng 2026-10-19: a price's currency is the buyer market's, never defaulted, never relabelled. That
day US search served SKINTIFIC's "Glow Cushion & Peptide Lip Serum Set" at USD 23.56: 20 offers the
variant backfill had copied off seed_data.variants, while the seed itself had been re-read in AUD
(snapshot AUD 57, column AUD 56, mirror offer AUD 56). pivota-backend#2652 bounds agent_pdp_view's
offer read and PIVOTA-Agent#2467 bounds gateway search. This file covers what buys and links:

  * offers.resolve's catalog-offers arm (routes/agent_shop_gateway), which mints the redirect link;
  * the Reap purchase reads (routes/agent_commerce_reap `_OFFER_SQL`, `_CART_OFFER_SQL`,
    `_CART_ALL_OFFERS_SQL`), which price the cart. Those statements are one text on SQLite and
    Postgres, so they apply the rule's Python twin; the parity test below runs the twin and the SQL
    predicate over the same stored rows;
  * index_pipeline_state's has_price / has_serving_region_offer under
    SERVING_PRICE_REQUIRES_SEED_CURRENCY (default off).

The listings are the shapes the 10-19 censuses found (spccy-1019 .. spccy5-1019b): skintific (USD
variant-backfill copies on a seed re-read in AUD), nourwish (a USD mirror stamp on an SGD seed),
column USD over a QAR page read, an orphan whose only seed is retired, and a Shopify-Markets USD read
on the skintific listing, which is the store's own US price and must still be offered.

The arm runs the real handler on the shared gate database, as tests/test_offers_resolve_catalog_arm
_postgres.py does. Everything else runs in a private schema created and dropped here.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from decimal import Decimal

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

SKIN = "prod::external_seed::external_seed::ext_d7ce5dea9d90103468e9640d"
NOUR = "prod::external_seed::external_seed::ext_nourwish_sgd_cushion"
QAR = "prod::merch_obs_qar::external_seed::ext_qar_page_read"
ORPHAN = "prod::merch_obs_orphan::external_seed::ext_retired_seed"
UPPER = "prod::merch_obs_up::external_seed::ext_upper_status"
LISTINGS = (SKIN, NOUR, QAR, ORPHAN, UPPER)
MIRROR = "external_product_seeds_mirror_v1"
BACKFILL = "variant_identity_backfill_v1"
MARKETS = "shopify_markets_us_localization"
HOST = {SKIN: "skintific.com", NOUR: "nourwish.com", QAR: "qar-store.example",
        ORPHAN: "orphan-store.example", UPPER: "upper-store.example"}

# (listing, seed id, status, price_currency column, seed_data)
SEEDS = [
    # skintific.com as prod read it on 10-19: payload and variants still say USD 23.56.
    (SKIN, "eps_e49ba92834d24bac922e80cf", "active", "AUD", {
        "price_amount": "23.56", "price_currency": "USD",
        "snapshot": {"price_amount": "57.0", "price_currency": "AUD"},
        "variants": [{"price": "23.56", "currency": "USD"}]}),
    # nourwish.com: SGD column and page read; the mirror stamped its offer `or "USD"`.
    (NOUR, "eps_nourwish_sgd", "active", "SGD", {
        "price_currency": "USD", "snapshot": {"price_amount": "27.90", "price_currency": "SGD"}}),
    # the column holds the writers' old USD default; the page was read in QAR.
    (QAR, "eps_qar_page", "active", "USD", {"snapshot": {"price_amount": "99", "price_currency": "QAR"}}),
    # the only seed behind this listing was retired: nothing reads it any more.
    (ORPHAN, "eps_retired", "retired", "USD", {"snapshot": {"price_currency": "USD"}}),
    # status spelled the way some writers spell it: still active.
    (UPPER, "eps_upper", " ACTIVE ", "USD", {"snapshot": {"price_currency": "usd"}}),
]

# (offer id, listing, source_system, currency, price)
OFFERS = [
    ("skin_mirror_aud", SKIN, MIRROR, "AUD", 56),
    ("skin_variant_1", SKIN, BACKFILL, "USD", 23.56),
    ("skin_variant_2", SKIN, BACKFILL, "USD", 23.56),
    ("skin_markets_usd", SKIN, MARKETS, "USD", 39),
    ("nour_mirror_usd", NOUR, MIRROR, "USD", 20),
    ("qar_mirror_usd", QAR, MIRROR, "USD", 28),
    ("orphan_variant_usd", ORPHAN, BACKFILL, "USD", 15),
    ("upper_variant_usd", UPPER, BACKFILL, "USD", 31),
]
# What the rule keeps: the AUD mirror offer (its seed's currency), the Markets read (not a seed
# copy), and the upper-case-status listing's USD copy. Every other projected offer is refused.
KEPT = {"skin_mirror_aud", "skin_markets_usd", "upper_variant_usd"}


def _offer_values(offer_id, product_key, source_system, currency, price, *, sku_key=None):
    return {
        "oid": offer_id, "sku": sku_key or f"{product_key}::canonical", "pk": product_key,
        "m": f"merch_obs_{HOST[product_key].split('.')[0]}", "cur": currency, "price": price,
        "src": source_system, "dest": f"https://{HOST[product_key]}/products/{offer_id}",
    }


_OFFER_INSERT = (
    "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,"
    " readiness_tier, offer_mode, channel, availability, currency, list_price, merchant_effective_price,"
    " market, source_system, offer_type, is_first_party, source_ref, offer_payload, updated_at)"
    " VALUES (:oid, :sku, :pk, :m, 'external_referral', 'observed', 'referral_only', 'redirect',"
    " 'external_referral', 'in_stock', :cur, :price, :price, 'US', :src, 'retailer', false, :dest,"
    " CAST(:payload AS jsonb), NOW())"
)
_SEED_INSERT = (
    "INSERT INTO external_product_seeds (id, attached_product_key, status, price_currency, seed_data,"
    " title, updated_at, market, destination_url)"
    " VALUES (:id, :pk, :st, :col, CAST(:sd AS jsonb), 'Seed', NOW(), 'US', 'https://seed.example/p')"
)
_PRODUCT_INSERT = (
    "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
    " content_key, pivota_signature_id, catalog_track, truth_tier, readiness_tier, pdp_scope,"
    " pdp_lifecycle_stage, source_system, sync_status, updated_at)"
    " VALUES (:pk, :m, 'external_seed', :pk, 'Product', 'Brand', :ck, :sig, 'citation', 'primary',"
    " 'referral_only', 'multi_merchant_canonical', 'published', :src, 'live', NOW())"
)


def _ck(product_key: str) -> str:
    return "ck_ppccy_" + product_key.rsplit("::", 1)[-1]


def _sig(product_key: str) -> str:
    return "sig_ppccy_" + product_key.rsplit("::", 1)[-1]


async def _plant(db) -> None:
    for pk in LISTINGS:
        await db.execute(_PRODUCT_INSERT, {"pk": pk, "m": f"merch_obs_{HOST[pk].split('.')[0]}",
                                           "ck": _ck(pk), "sig": _sig(pk), "src": MIRROR})
    for pk, seed_id, status, column, seed_data in SEEDS:
        await db.execute(_SEED_INSERT, {"id": seed_id, "pk": pk, "st": status, "col": column,
                                        "sd": json.dumps(seed_data)})
    for offer in OFFERS:
        values = _offer_values(*offer)
        values["payload"] = json.dumps({"destination_url": values["dest"]})
        await db.execute(_OFFER_INSERT, values)


# ── private schema: the predicate, its Python twin, the Reap reads, index_pipeline_state ──────────

_EXTRA_DDL = (
    "CREATE TABLE external_product_seeds (id text PRIMARY KEY, attached_product_key text, status text,"
    " price_currency text, seed_data jsonb, title text, updated_at timestamptz, market text,"
    " destination_url text)",
    "CREATE INDEX ON external_product_seeds (attached_product_key)",
    "CREATE TABLE product_group_members (merchant_id text, platform text, platform_product_id text,"
    " product_group_id text)",
    "CREATE TABLE merchant_stores (merchant_id text, status text)",
)
_MODEL_TABLES = (
    "catalog_products", "catalog_offers", "catalog_merchants", "agent_pdp_view", "beauty_product_profiles",
    "beauty_sku_ingredients", "external_offer_snapshots", "product_quality_snapshot",
)


@pytest.fixture
async def private_db():
    import asyncpg
    import databases
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    import db.catalog  # noqa: F401  (registers the catalog tables)
    import db.external_offers  # noqa: F401  (external_offer_snapshots)
    import db.product_quality  # noqa: F401  (product_quality_snapshot)
    import db.database as dbmod

    schema = "ppccy_" + uuid.uuid4().hex
    admin = await asyncpg.connect(DATABASE_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    database = None
    try:
        for name in _MODEL_TABLES:
            await admin.execute(str(sa.schema.CreateTable(dbmod.metadata.tables[name]).compile(
                dialect=postgresql.dialect())))
        for ddl in _EXTRA_DDL:
            await admin.execute(ddl)
        database = databases.Database(DATABASE_URL, server_settings={"search_path": schema})
        await database.connect()
        await _plant(database)
        yield database
    finally:
        if database is not None:
            await database.disconnect()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _sql_verdicts(db) -> dict:
    from services.seed_offer_currency import seed_projected_offer_in_seed_currency_sql

    rows = await db.fetch_all(
        f"SELECT o.offer_id, {seed_projected_offer_in_seed_currency_sql('o', 'o.product_key')} AS kept"
        " FROM catalog_offers o"
    )
    return {row["offer_id"]: row["kept"] for row in rows}


async def _twin_verdicts(db) -> dict:
    """The Python twin over the rows exactly as the Reap route reads them."""
    from routes.agent_commerce_reap import _LISTING_SEEDS_SQL
    from services.seed_offer_currency import offer_priced_in_seed_currency

    verdicts = {}
    for row in await db.fetch_all("SELECT offer_id, product_key, source_system, currency FROM catalog_offers"):
        seeds = [dict(s) for s in await db.fetch_all(_LISTING_SEEDS_SQL, {"product_key": row["product_key"]})]
        verdicts[row["offer_id"]] = offer_priced_in_seed_currency(dict(row), seeds)
    return verdicts


async def test_the_rule_keeps_exactly_the_offers_priced_in_their_seeds_currency(private_db):
    sql = await _sql_verdicts(private_db)
    assert {oid for oid, kept in sql.items() if kept} == KEPT
    assert await _twin_verdicts(private_db) == sql


# Shapes beyond the five listings, each its own listing: (seed status, column, seed_data as stored
# SQL text or None, offer source_system, offer currency).
_EDGES = [
    ("active", "USD", None, MIRROR, "USD"),                                    # no seed_data: column
    ("active", "USD", "null", MIRROR, "USD"),                                  # JSON null: column
    ("active", "USD", '"a string"', MIRROR, "USD"),                            # a string states none
    ("active", "USD", '{"snapshot": "USD"}', MIRROR, "USD"),                   # a string snapshot: none
    ("active", "USD", '{"snapshot": null}', MIRROR, "USD"),                    # null snapshot: column
    ("active", "USD", '{"snapshot": {}}', MIRROR, "USD"),                      # no snapshot currency
    ("active", "USD", '{"snapshot": {"price_currency": " "}}', MIRROR, "USD"),  # blank: column
    ("active", "USD", '{"snapshot": {"price_currency": 5}}', MIRROR, "USD"),   # a number states "5"
    ("active", " usd ", '{"price_currency": "SGD"}', MIRROR, "USD"),           # seed_data's copy ignored
    ("active", None, '{}', MIRROR, "USD"),                                     # states nothing
    ("active", None, '{}', MIRROR, None),                                      # nor does the offer
    ("Active", "SGD", '{}', "variant_offer_projection_v1", "SGD"),
    ("inactive", "USD", '{}', MIRROR, "USD"),
    ("active", "EUR", '{}', MARKETS, "USD"),                                   # not a seed copy
    ("active", "EUR", '{}', None, "USD"),                                      # no source_system
    ("active", "EUR", '{}', "", "USD"),
]


async def test_the_python_twin_agrees_with_the_sql_predicate_on_every_stored_shape(private_db):
    for i, (status, column, stored, source_system, currency) in enumerate(_EDGES):
        pk = f"prod::merch_obs_edge::external_seed::edge_{i}"
        await private_db.execute(
            "INSERT INTO external_product_seeds (id, attached_product_key, status, price_currency, seed_data)"
            " VALUES (:id, :pk, :st, :col, CAST(:sd AS jsonb))",
            {"id": f"eps_edge_{i}", "pk": pk, "st": status, "col": column, "sd": stored},
        )
        await private_db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track,"
            " truth_tier, readiness_tier, offer_mode, channel, availability, currency, list_price, market,"
            " source_system, offer_type, is_first_party) VALUES (:oid, :sku, :pk, 'm', 'external_referral',"
            " 'observed', 'referral_only', 'redirect', 'external_referral', 'in_stock', :cur, 1, 'US', :src,"
            " 'retailer', false)",
            {"oid": f"edge_{i}", "sku": f"{pk}::canonical", "pk": pk, "cur": currency, "src": source_system},
        )
    # Two active seeds on one listing that disagree (USD and AUD): the USD copy matches one of them
    # and is still no price. A rule that asked whether ANY active seed agrees would keep it.
    pk = "prod::merch_obs_two::external_seed::two_seeds"
    for seed_id, column in (("eps_two_a", "USD"), ("eps_two_b", "AUD")):
        await private_db.execute(
            "INSERT INTO external_product_seeds (id, attached_product_key, status, price_currency, seed_data)"
            " VALUES (:id, :pk, 'active', :col, NULL)", {"id": seed_id, "pk": pk, "col": column})
    await private_db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,"
        " readiness_tier, offer_mode, channel, availability, currency, list_price, market, source_system,"
        " offer_type, is_first_party) VALUES ('two_seeds', :sku, :pk, 'm', 'external_referral', 'observed',"
        " 'referral_only', 'redirect', 'external_referral', 'in_stock', 'USD', 1, 'US', :src, 'retailer',"
        " false)", {"sku": f"{pk}::canonical", "pk": pk, "src": MIRROR})

    sql = await _sql_verdicts(private_db)
    twin = await _twin_verdicts(private_db)
    assert twin == sql
    edges = {oid: kept for oid, kept in sql.items() if oid.startswith("edge_")}
    assert edges == {
        "edge_0": True, "edge_1": True, "edge_2": False, "edge_3": False, "edge_4": True, "edge_5": True,
        "edge_6": True, "edge_7": False, "edge_8": True, "edge_9": False, "edge_10": False, "edge_11": True,
        "edge_12": False, "edge_13": True, "edge_14": True, "edge_15": True,
    }
    assert sql["two_seeds"] is False


async def test_the_reap_reads_price_the_store_read_not_the_cheaper_seed_copy(private_db, monkeypatch):
    """`_OFFER_SQL` / `_CART_OFFER_SQL` read the seller's offers cheapest first; the USD 23.56 copies
    are cheaper than the Markets read and must not be the price."""
    import routes.agent_commerce_reap as reap

    monkeypatch.setattr(reap, "database", private_db)
    # One sku, as the cart reads it: the copies and the Markets read on the skintific variant.
    sku = f"{SKIN}::sku_variant"
    await private_db.execute(
        "UPDATE catalog_offers SET sku_key = :sku, merchant_id = 'merch_obs_skintific'"
        " WHERE offer_id IN ('skin_variant_1', 'skin_variant_2', 'skin_markets_usd')", {"sku": sku})
    params = {"product_key": SKIN, "sku_key": sku, "merchant_id": "merch_obs_skintific"}

    for statement, extra in ((reap._OFFER_SQL, {}), (reap._CART_OFFER_SQL, {"market_currency": "USD"}),
                             (reap._CART_ALL_OFFERS_SQL, {"market_currency": "USD"})):
        read = [dict(r) for r in await private_db.fetch_all(statement, {**params, **extra})]
        assert Decimal(read[0]["price"]) == Decimal("23.56"), "the copies are the cheapest rows read"
        kept = await reap._offers_priced_in_seed_currency(SKIN, read)
        assert [(r["currency"], Decimal(r["price"])) for r in kept] == [("USD", Decimal("39"))]


@pytest.mark.parametrize("listing", [NOUR, QAR, ORPHAN])
async def test_the_reap_reads_leave_no_price_on_a_listing_with_only_refused_copies(private_db, monkeypatch, listing):
    import routes.agent_commerce_reap as reap

    monkeypatch.setattr(reap, "database", private_db)
    params = {"product_key": listing, "sku_key": f"{listing}::canonical",
              "merchant_id": f"merch_obs_{HOST[listing].split('.')[0]}", "market_currency": "USD"}
    read = await private_db.fetch_all(reap._CART_ALL_OFFERS_SQL, params)
    assert len(read) == 1 and read[0]["currency"] == "USD"
    assert await reap._offers_priced_in_seed_currency(listing, read) == []


async def test_a_listing_with_no_seed_copy_never_reads_its_seeds(private_db, monkeypatch):
    import routes.agent_commerce_reap as reap

    calls = []

    class _Counting:
        async def fetch_all(self, query, values=None):
            calls.append(query)
            return await private_db.fetch_all(query, values)

    monkeypatch.setattr(reap, "database", _Counting())
    rows = [{"currency": "USD", "price": "39", "source_system": MARKETS}]
    assert await reap._offers_priced_in_seed_currency(SKIN, rows) == rows
    assert calls == []


# ── index_pipeline_state: has_price / has_serving_region_offer under the flag ─────────────────────

_IPS_EXPECTED = {
    # listing: (flag off, flag on), each (has_price, has_serving_region_offer)
    SKIN: ((True, True), (True, True)),        # the Markets USD read stands; the AUD mirror offer prices it
    NOUR: ((True, True), (False, False)),      # only the USD stamp on an SGD seed
    QAR: ((True, True), (False, False)),       # only the USD copy of a QAR page read
    ORPHAN: ((True, True), (False, False)),    # only a copy whose seed is retired
    UPPER: ((True, True), (True, True)),       # ' ACTIVE ' is active
}


async def test_the_flag_narrows_has_price_and_has_serving_region_offer_to_seed_currency_prices(
    private_db, monkeypatch
):
    import services.index_pipeline_state_service as ips

    for flag, column in (("", 0), ("1", 1)):
        monkeypatch.setenv(ips.SERVING_PRICE_SEED_CURRENCY_ENV, flag)
        for listing, expected in _IPS_EXPECTED.items():
            rows = await ips._fetch_eligibility_inputs(_ck(listing), db=private_db)
            assert len(rows) == 1, listing
            got = (bool(rows[0]["has_price"]), bool(rows[0]["has_serving_region_offer"]))
            assert got == expected[column], (listing, flag)


async def test_skintific_without_its_markets_read_keeps_a_price_but_no_serving_region_offer(
    private_db, monkeypatch
):
    """The AUD mirror offer is a real price in its own currency, so has_price stays TRUE. AUD is no
    served region's currency, so once the USD copies stop counting nothing serves it."""
    import services.index_pipeline_state_service as ips

    await private_db.execute("DELETE FROM catalog_offers WHERE offer_id = 'skin_markets_usd'")
    monkeypatch.setenv(ips.SERVING_PRICE_SEED_CURRENCY_ENV, "on")
    rows = await ips._fetch_eligibility_inputs(_ck(SKIN), db=private_db)
    assert (rows[0]["has_price"], rows[0]["has_serving_region_offer"]) == (True, False)
    monkeypatch.setenv(ips.SERVING_PRICE_SEED_CURRENCY_ENV, "off")
    rows = await ips._fetch_eligibility_inputs(_ck(SKIN), db=private_db)
    assert (rows[0]["has_price"], rows[0]["has_serving_region_offer"]) == (True, True)


async def test_the_batch_and_audit_queries_run_gated_on_postgres(private_db, monkeypatch):
    import services.index_pipeline_state_service as ips

    monkeypatch.setenv(ips.SERVING_PRICE_SEED_CURRENCY_ENV, "true")
    batch = await private_db.fetch_all(ips.eligibility_query(ips._BATCH_QUERY), {"cursor": "", "batch_size": 50})
    multi = await private_db.fetch_all(ips.eligibility_query(ips._MULTI_QUERY),
                                       {"content_keys": [_ck(pk) for pk in LISTINGS]})
    for rows in (batch, multi):
        got = {r["content_key"]: (r["has_price"], r["has_serving_region_offer"]) for r in rows}
        assert got == {_ck(pk): expected[1] for pk, expected in _IPS_EXPECTED.items()}


def test_with_the_flag_off_every_eligibility_query_is_the_one_main_sends(monkeypatch):
    import services.index_pipeline_state_service as ips

    monkeypatch.delenv(ips.SERVING_PRICE_SEED_CURRENCY_ENV, raising=False)
    for query in (ips._BATCH_QUERY, ips._SINGLE_QUERY, ips._MULTI_QUERY):
        assert ips.eligibility_query(query) is query
    monkeypatch.setenv(ips.SERVING_PRICE_SEED_CURRENCY_ENV, "1")
    for query in (ips._BATCH_QUERY, ips._SINGLE_QUERY, ips._MULTI_QUERY):
        gated = ips.eligibility_query(query)
        assert gated != query
        # Only has_price and has_serving_region_offer change: has_us_offer is left as it was.
        assert gated.count("eps_own") > 0 and gated.count(ips._HAS_US_OFFER_EXISTS + "      AS has_us_offer") == 1


# ── offers.resolve's catalog-offers arm, through the real handler (shared gate database) ─────────

@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    import db.commerce_attribution  # noqa: F401  (surface_click_events: the issued links)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    from tests.seed_offer_currency_tables import ensure_seed_currency_columns

    with engine.begin() as conn:
        ensure_seed_currency_columns(conn)
    yield engine
    with engine.begin() as conn:
        _clear(conn, text)
    engine.dispose()


def _clear(conn, text):
    conn.execute(text("DELETE FROM catalog_offers WHERE product_key = ANY(:pks)"), {"pks": list(LISTINGS)})
    conn.execute(text("DELETE FROM catalog_products WHERE product_key = ANY(:pks)"), {"pks": list(LISTINGS)})
    conn.execute(text("DELETE FROM external_product_seeds WHERE attached_product_key = ANY(:pks)"),
                 {"pks": list(LISTINGS)})


def _plant_shared(engine):
    import databases
    from sqlalchemy import text

    with engine.begin() as conn:
        _clear(conn, text)

    async def go():
        db = databases.Database(DATABASE_URL)
        await db.connect()
        try:
            await _plant(db)
        finally:
            await db.disconnect()

    asyncio.run(go())


def _resolve(product_id):
    from routes.agent_shop_gateway import OffersResolvePayload, _handle_offers_resolve

    class _BT:
        def add_task(self, *a, **k):
            pass

    async def go():
        from db.database import database
        await database.connect()
        try:
            return await _handle_offers_resolve(
                OffersResolvePayload(product={"product_id": product_id}, limit=10,
                                     market="US", tool="*", commerce_surface="agent_api"),
                None, _BT(),
            )
        finally:
            await database.disconnect()

    return asyncio.run(go())


def _catalog_offers(res):
    offers = [o for o in (res.get("offers") or [])
              if str(o.get("offer_id") or "").startswith("of:catalog_offer:")]
    return [(o["source"]["offer_id"], o.get("price"), str(o.get("currency") or "").upper()) for o in offers]


def test_the_arm_links_the_markets_read_not_the_cheaper_usd_copy(pg_engine):
    """Without the rule the arm's one-per-seller window picked skintific.com's USD 23.56 copy (cheapest
    in the buyer's currency) and minted its link."""
    _plant_shared(pg_engine)
    res = _resolve(_sig(SKIN))
    assert _catalog_offers(res) == [("skin_markets_usd", 39.0, "USD")]
    breakdown = (res.get("metadata") or {}).get("failure_breakdown") or {}
    assert "catalog_offers" not in breakdown, breakdown


@pytest.mark.parametrize("listing", [NOUR, QAR, ORPHAN])
def test_the_arm_links_nothing_for_a_listing_with_only_refused_copies(pg_engine, listing):
    _plant_shared(pg_engine)
    res = _resolve(_sig(listing))
    assert _catalog_offers(res) == []
    breakdown = (res.get("metadata") or {}).get("failure_breakdown") or {}
    assert "catalog_offers" not in breakdown, "an empty arm must be the rule, not a failed statement"


def test_the_arm_still_links_a_copy_priced_in_its_seeds_currency(pg_engine):
    _plant_shared(pg_engine)
    res = _resolve(_sig(UPPER))
    assert _catalog_offers(res) == [("upper_variant_usd", 31.0, "USD")]
