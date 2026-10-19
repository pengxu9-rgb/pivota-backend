"""agent_pdp_view's offer read refuses offers copied off a seed in another currency (real Postgres).

Peng 2026-10-19: a price's currency is the buyer market's, never defaulted, never relabelled. That
day the view for SKINTIFIC's "Glow Cushion & Peptide Lip Serum Set" (ck_886037a7...) served five
USD 23.56 offers and a row currency of USD. aggregate_offers takes the MODAL currency, and the
variant backfill had copied 20 USD 23.56 offers off seed_data.variants, against one AUD 56 mirror
offer. The seed itself had been re-read in AUD: snapshot AUD 57, column AUD 56. The rows below are
that product's shape (read-only probe skin-probe-1019), plus the other shapes the 10-19 censuses
found (spccy-1019 .. spccy3-1019).

The seed's currency is its snapshot's (the page as read), else its column's. seed_data.price_currency
is a writer-defaulted copy and is not evidence (services/seed_offer_currency). Every refused row has
a kept sibling on the same product, so an empty read can never pass for a refusal.

PRIVATE DATABASE, created and dropped here (as test_agent_pdp_view_market_prices_postgres.py): the
catalog tables come from their db.catalog models. external_product_seeds has no model, so it gets
the columns the predicate reads.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB_NAME = f"apv_seed_offer_ccy_{os.getpid()}"
SKIN = "prod::external_seed::external_seed::ext_d7ce5dea9d90103468e9640d"
NOUR = "prod::external_seed::external_seed::ext_nourwish_sgd"
SGOK = "prod::merch_obs_sg::external_seed::ext_sg_ok"
COLONLY = "prod::merch_obs_col::external_seed::ext_col_only"
NOCCY = "prod::merch_obs_none::external_seed::ext_no_ccy"
INACTIVE = "prod::merch_obs_old::external_seed::ext_inactive_seed"
QAR = "prod::merch_obs_qar::external_seed::ext_qar_page"
UPPER = "prod::merch_obs_up::external_seed::ext_upper_status"
ORPHAN = "prod::merch_obs_orphan::external_seed::ext_no_seed"
STRSNAP = "prod::merch_obs_str::external_seed::ext_string_snapshot"
TWOSEEDS = "prod::merch_obs_two::external_seed::ext_two_seeds"
LOWER = "prod::merch_obs_low::external_seed::ext_lower_ccy"
ARRAY = "prod::merch_obs_arr::external_seed::ext_array_seed_data"
NOSRC = "prod::merch_obs_nosrc::external_seed::ext_no_source"
GBP = "prod::merch_obs_gbp::external_seed::ext_col_usd_page_gbp"
BLANKS = "prod::merch_obs_blank::external_seed::ext_blank_both"
KEYS = [SKIN, NOUR, SGOK, COLONLY, NOCCY, INACTIVE, QAR, UPPER, ORPHAN, STRSNAP, TWOSEEDS, LOWER, ARRAY, NOSRC, GBP, BLANKS]


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


@pytest.fixture(scope="module")
def db_url():
    import psycopg2

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()
    try:
        yield _private_url()
    finally:
        admin = psycopg2.connect(os.environ["DATABASE_URL"])
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (_DB_NAME,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        admin.close()


def _build(url: str) -> None:
    from sqlalchemy import create_engine, text

    from db.catalog import catalog_merchants, catalog_offers

    engine = create_engine(url, future=True)
    try:
        catalog_offers.metadata.create_all(engine, tables=[catalog_merchants, catalog_offers], checkfirst=True)
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE external_product_seeds (id text PRIMARY KEY, attached_product_key text, "
                "status text, market text, price_amount numeric, price_currency text, seed_data jsonb)"
            ))
            conn.execute(text(
                "CREATE INDEX idx_eps_attached ON external_product_seeds(attached_product_key)"
            ))

            def offer(offer_id, product_key, currency, price, source_system, market="US"):
                conn.execute(text(
                    "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, "
                    " truth_tier, readiness_tier, offer_mode, channel, availability, currency, list_price, "
                    " merchant_effective_price, market, source_system, offer_type, is_first_party) "
                    "VALUES (:oid, :sku, :pk, 'merch_obs_9cbce09d1019844b', 'external_referral', 'observed', "
                    " 'referral_only', 'redirect', 'external_referral', 'in_stock', :cur, :price, :price, "
                    " :mkt, :src, 'brand_direct', true)"
                ), {"oid": offer_id, "sku": f"{product_key}::{offer_id}", "pk": product_key, "cur": currency,
                    "price": price, "mkt": market, "src": source_system})

            def seed(seed_id, product_key, column, seed_data, status="active"):
                conn.execute(text(
                    "INSERT INTO external_product_seeds (id, attached_product_key, status, market, price_amount, "
                    " price_currency, seed_data) VALUES (:id, :pk, :st, 'US', 1, :col, CAST(:sd AS jsonb))"
                ), {"id": seed_id, "pk": product_key, "st": status, "col": column, "sd": json.dumps(seed_data)})

            # skintific.com, prod's shape: an AUD re-read whose payload and variants still say USD 23.56.
            offer("mirror_aud", SKIN, "AUD", 56, "external_product_seeds_mirror_v1")
            for vid in ("42706501599337", "42706501632105", "42706501664873"):
                offer(f"variant_{vid}", SKIN, "USD", 23.56, "variant_identity_backfill_v1")
            offer("markets_usd", SKIN, "USD", 39, "shopify_markets_us_localization")  # its own US read: kept
            seed("eps_e49ba92834d24bac922e80cf", SKIN, "AUD", {
                "price_amount": "23.56", "price_currency": "USD",
                "snapshot": {"price_amount": "57.0", "price_currency": "AUD"},
                "variants": [{"variant_id": "42706501599337", "price": "23.56", "currency": "USD"}]})
            # nourwish.com: the writer's old `or "USD"` stamp on an SGD seed; its SGD sibling is kept.
            offer("nour_usd", NOUR, "USD", 38, "external_product_seeds_mirror_v1")
            offer("nour_sgd", NOUR, "SGD", 38, "variant_offer_projection_v1", market="SG")
            seed("eps_nour", NOUR, "SGD", {"price_currency": "USD", "snapshot": {"price_currency": "SGD"}})
            # The 16 good SGD prices: column SGD, snapshot SGD, seed_data's defaulted copy "USD".
            offer("sg_ok", SGOK, "SGD", 25, "external_product_seeds_mirror_v1", market="SG")
            seed("eps_sg_ok", SGOK, "SGD", {"price_currency": "USD", "snapshot": {"price_currency": "SGD"}})
            # A seed that kept no snapshot currency: the column stands in.
            offer("col_only_usd", COLONLY, "USD", 18, "external_product_seeds_mirror_v1")
            offer("col_only_eur", COLONLY, "EUR", 17, "external_product_seeds_mirror_v1")
            seed("eps_col_only", COLONLY, "USD", {"snapshot": {"price_amount": "18"}})
            # A seed that states no currency at all: its projections are not prices.
            offer("no_ccy_usd", NOCCY, "USD", 12, "external_product_seeds_mirror_v1")
            offer("no_ccy_markets", NOCCY, "USD", 13, "shopify_markets_us_localization")
            seed("eps_no_ccy", NOCCY, None, {})
            # 367 seeds on 10-19: the column says USD (the old default) while the page was read in QAR.
            offer("qar_mirror_usd", QAR, "USD", 95, "external_product_seeds_mirror_v1")
            offer("qar_markets_usd", QAR, "USD", 26, "us_market_capture")
            seed("eps_qar", QAR, "USD", {"price_currency": "USD", "snapshot": {"price_amount": "95", "price_currency": "QAR"}})
            # An offer whose only seed is retired is an orphan of a read nobody holds: refused
            # (PR controller review of PIVOTA-Agent#2467), and its seed's currency does not matter.
            offer("inactive_usd", INACTIVE, "USD", 30, "external_product_seeds_mirror_v1")
            offer("inactive_markets", INACTIVE, "USD", 31, "us_market_capture")
            seed("eps_inactive", INACTIVE, "USD", {"snapshot": {"price_currency": "USD"}}, status="inactive")
            # Status is compared case-insensitively: 'ACTIVE ' is active, and agrees.
            offer("upper_usd", UPPER, "USD", 22, "external_product_seeds_mirror_v1")
            seed("eps_upper", UPPER, "USD", {"snapshot": {"price_currency": "USD"}}, status="ACTIVE ")
            # No seed attached at all.
            offer("orphan_usd", ORPHAN, "USD", 21, "external_product_seeds_mirror_v1")
            offer("orphan_markets", ORPHAN, "USD", 20, "shopify_markets_us_localization")
            # Two active seeds on one listing, one re-read in AUD: one disagreeing read is enough to refuse.
            offer("two_usd", TWOSEEDS, "USD", 17, "external_product_seeds_mirror_v1")
            offer("two_markets", TWOSEEDS, "USD", 16, "us_market_capture")
            seed("eps_two_usd", TWOSEEDS, "USD", {"snapshot": {"price_currency": "USD"}})
            seed("eps_two_aud", TWOSEEDS, "AUD", {"snapshot": {"price_currency": "AUD"}})
            # The offer's currency is compared trimmed and upper-cased.
            offer("low_aud", LOWER, "aud ", 33, "external_product_seeds_mirror_v1")
            seed("eps_low", LOWER, "AUD", {"snapshot": {"price_currency": "AUD"}})
            # seed_data stored as an array hides the page read: no currency.
            offer("arr_usd", ARRAY, "USD", 29, "external_product_seeds_mirror_v1")
            offer("arr_markets", ARRAY, "USD", 28, "us_market_capture")
            conn.execute(text(
                "INSERT INTO external_product_seeds (id, attached_product_key, status, market, price_amount, "
                " price_currency, seed_data) VALUES ('eps_arr', :pk, 'active', 'US', 1, 'USD', CAST('[1, 2]' AS jsonb))"
            ), {"pk": ARRAY})
            # An offer no seed-copying writer wrote (source_system NULL) is not judged.
            offer("nosrc_usd", NOSRC, "USD", 27, None)
            seed("eps_nosrc", NOSRC, "AUD", {"snapshot": {"price_currency": "AUD"}})
            # Column USD, page read GBP (483 seeds on 10-19): either may be wrong, so even the GBP row is no price.
            offer("gbp_copied", GBP, "GBP", 26, "external_product_seeds_mirror_v1", market="GB")
            offer("gbp_markets", GBP, "USD", 25, "us_market_capture")
            seed("eps_gbp", GBP, "USD", {"snapshot": {"price_currency": "GBP"}})
            # No currency on the copied row AND none on its seed: two blanks are not an agreement.
            offer("blank_copied", BLANKS, None, 24, "external_product_seeds_mirror_v1")
            seed("eps_blank", BLANKS, None, {"snapshot": {"price_amount": "24"}})
            # A snapshot stored as a JSON string hides the page read: no currency, refused.
            offer("strsnap_usd", STRSNAP, "USD", 19, "external_product_seeds_mirror_v1")
            offer("strsnap_markets", STRSNAP, "USD", 18, "us_market_capture")
            seed("eps_strsnap", STRSNAP, "USD", {"snapshot": json.dumps({"price_currency": "USD"})})
    finally:
        engine.dispose()


async def _read(url: str):
    from databases import Database

    from services.agent_pdp_view_assembler import fetch_offers_for_keys

    db = Database(url)
    await db.connect()
    try:
        return await fetch_offers_for_keys(KEYS, db=db)
    finally:
        await db.disconnect()


def test_the_view_reads_only_offers_priced_in_their_seed_currency(db_url):
    _build(db_url)
    rows = asyncio.run(_read(db_url))
    assert sorted(r["offer_id"] for r in rows) == sorted([
        "mirror_aud", "markets_usd",   # skintific: the AUD re-read and the store's own US read
        "nour_sgd",                    # nourwish: the SGD projection, not the USD stamp
        "sg_ok",                       # seed_data's "USD" copy refuses nothing
        "col_only_usd",                # the column stands in for a missing snapshot currency
        "no_ccy_markets",              # a currency-less seed refuses only what was copied off it
        "inactive_markets",            # a retired seed leaves its copied row an orphan; the store's read stays
        "upper_usd",                   # 'ACTIVE ' is active
        "orphan_markets",              # no seed at all: only the store's own read
        "strsnap_markets",             # a string snapshot states no currency
        "two_markets",                 # one of two active seeds reads AUD: the copied USD row is refused
        "low_aud",                     # 'aud ' is AUD
        "arr_markets",                 # an array seed_data states no currency
        "nosrc_usd",                   # not a seed-copying writer: not judged
        "gbp_markets",                 # column and page read disagree: the GBP copy is no price either
        "qar_markets_usd",             # the page said QAR: the USD copied off the column is refused
    ])


def test_the_view_row_no_longer_quotes_usd_23_56_for_the_aud_set(db_url):
    """aggregate_offers takes the modal currency: 3 copied USD rows outvoted the AUD re-read and
    the Markets read, so the row said USD at 23.56. With them refused it is the store's own reads."""
    from services.agent_pdp_view_assembler import aggregate_offers

    rows = [r for r in asyncio.run(_read(db_url)) if r["product_key"] == SKIN]
    currency, price_min, _max, _count, offers = aggregate_offers(rows, "merch_obs_9cbce09d1019844b", {})
    assert all(float(o["price"]) != 23.56 for o in offers)
    # One AUD and one USD read left: the modal tie goes to the higher code, so the row is the
    # store's own US read (shopify_markets_us_localization), USD 39.
    assert (currency, float(price_min)) == ("USD", 39.0)


def test_a_refused_write_leaves_the_stored_offer_untouched(db_url, monkeypatch):
    """The writer refuses (column USD, page read AUD), writes nothing, and the offer already stored
    (skintific's AUD 56 mirror row) keeps every value it had: refusal never relabels."""
    from databases import Database

    import services.external_offer_dual_write as dual_write
    from services.seed_offer_currency import SeedOfferCurrencyRefused

    async def go():
        db = Database(db_url)
        await db.connect()
        monkeypatch.setattr(dual_write, "database", db)
        try:
            before = dict(await db.fetch_one(
                "SELECT currency, list_price, market, source_system FROM catalog_offers WHERE offer_id = 'mirror_aud'"))
            stored_id = dual_write.derive_mirror_offer_id(SKIN)
            await db.execute("UPDATE catalog_offers SET offer_id = :oid WHERE offer_id = 'mirror_aud'", {"oid": stored_id})
            with pytest.raises(SeedOfferCurrencyRefused, match="page read AUD"):
                await dual_write.upsert_catalog_offer_from_seed_row(SKIN, {
                    "id": "eps_e49ba92834d24bac922e80cf", "price_amount": 23.56, "price_currency": "USD",
                    "market": "US", "domain": "skintific.com", "snapshot_price_currency": "AUD",
                }, merchant_id="merch_obs_9cbce09d1019844b")
            after = dict(await db.fetch_one(
                "SELECT currency, list_price, market, source_system FROM catalog_offers WHERE offer_id = :oid", {"oid": stored_id}))
            await db.execute("UPDATE catalog_offers SET offer_id = 'mirror_aud' WHERE offer_id = :oid", {"oid": stored_id})
            return before, after
        finally:
            await db.disconnect()

    before, after = asyncio.run(go())
    assert after == before and after["currency"] == "AUD" and float(after["list_price"]) == 56.0
