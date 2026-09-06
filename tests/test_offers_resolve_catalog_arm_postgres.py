"""The catalog-offers arm's SQL must run on POSTGRES, not merely on SQLite.

WHY THIS FILE EXISTS. The unit suite for `offers.resolve`
(`tests/test_offers_resolve.py`) fakes `database.fetch_all` and routes on SQL
substrings, so the statement is never sent to any engine — a dialect error in it
is invisible there. And none of the 64 `test_*_postgres.py` files touches
`agent_shop_gateway` at all, so the "execute real routes on real Postgres" job
went green on this change without executing one character of its SQL. That is
the green-that-did-not-run this repo keeps producing.

The statement uses four things that differ between the two engines:
  * `= ANY(:aliases)` with a LIST bind — SQLite has no ANY(array); a driver that
    silently accepts it here would fail in prod,
  * `offer_payload->>'destination_url'` — jsonb text extraction,
  * `coalesce(...) > 0` over three NUMERIC columns,
  * `ORDER BY price_amount ASC` on a COMPUTED alias.

The test drives the REAL handler rather than a copy of the SQL: a copied
statement cannot catch a change to the one the route actually sends.
"""

from __future__ import annotations

import asyncio
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

_SIG = "sig_catalog_arm_pg"
_PK = "prod::m_arm::external_seed::snail-essence"


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    yield engine
    engine.dispose()


def _seed(engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        for t in ("catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants"):
            conn.execute(text(f"DELETE FROM {t}"))
        for mid, name in (("m_arm", "Arm Merchant"), ("stylekorean_global", "StyleKorean")):
            conn.execute(text(
                "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status)"
                " VALUES (:m, :n, 'external_seed', 'active')"
            ), {"m": mid, "n": name})
        conn.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,"
            " title, brand, content_key, pivota_signature_id, catalog_track, truth_tier,"
            " readiness_tier, pdp_scope, pdp_lifecycle_stage, source_system, updated_at)"
            " VALUES (:pk,'m_arm','external_seed',:pk,'Snail Essence','COSRX','ck_arm',:sig,"
            "         'citation','primary','referral_only','multi_merchant_canonical',"
            "         'published','test',NOW())"
        ), {"pk": _PK, "sig": _SIG})

        # `catalog_offers.sku_key` is NOT NULL — a real offer always hangs off a SKU, so the
        # fixture must too. Found by running this gate rather than by reading the model.
        sku_key = f"{_PK}::canonical"
        conn.execute(text(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
            " source_product_id, source_variant_id, title, currency, updated_at)"
            " VALUES (:sk,:pk,'m_arm','external_seed',:pk,'v1','Snail Essence','USD',NOW())"
        ), {"sk": sku_key, "pk": _PK})

        def offer(oid, merchant, price, offer_type, mode="redirect", suppressed=False):
            conn.execute(text(
                "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
                " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
                " currency, list_price, merchant_effective_price, offer_type, is_first_party,"
                " source_ref, offer_payload, suppressed_at, updated_at)"
                " VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only',:mode,"
                "         'external_referral','in_stock','USD',:p,:p,:ot,false,"
                "         'https://www.stylekorean.com/p/1',"
                "         cast(:payload AS jsonb), :sup, NOW())"
            ), {"oid": oid, "sk": sku_key, "pk": _PK, "m": merchant, "p": price,
                "ot": offer_type, "mode": mode,
                "payload": '{"destination_url": "https://www.stylekorean.com/p/1"}',
                "sup": "2026-01-01T00:00:00" if suppressed else None})

        offer("of_retailer_cheap", "stylekorean_global", 17.50, "retailer")
        offer("of_retailer_dear", "stylekorean_global", 29.00, "retailer")
        offer("of_brand_direct", "m_arm", 9.00, "brand_direct")          # must not be sourced
        offer("of_suppressed", "stylekorean_global", 1.00, "retailer", suppressed=True)
        offer("of_not_redirect", "stylekorean_global", 2.00, "retailer", mode="checkout")
        offer("of_zero_price", "stylekorean_global", 0, "retailer")
        conn.execute(text(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
            " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
            " currency, list_price, merchant_effective_price, offer_type, is_first_party,"
            " updated_at)"
            " VALUES ('of_no_destination',:sk,:pk,'stylekorean_global','external_referral',"
            "         'observed','referral_only','redirect','external_referral','in_stock',"
            "         'USD',:p,:p,'retailer',false,NOW())"
        ), {"sk": sku_key, "pk": _PK, "p": 12.00})


def _resolve(product_id):
    """Drive the REAL handler, so the SQL under test is the one the route sends."""
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


def test_the_catalog_arm_sql_executes_on_postgres_and_returns_the_retailer(pg_engine):
    _seed(pg_engine)
    res = _resolve(_SIG)

    offers = res.get("offers") or []
    assert offers, "the arm's SQL must run on Postgres and match by pivota_signature_id"
    ids = [str(o.get("offer_id") or "") for o in offers]
    assert all(i.startswith("of:catalog_offer:") for i in ids), ids

    # the filters, each asserted against a row planted to violate exactly one of them
    sourced = {i.rsplit(":", 1)[-1] for i in ids}
    assert "of_brand_direct" not in sourced, "brand_direct must not be sourced (the seed lane emits it)"
    assert "of_suppressed" not in sourced, "suppressed_at must be honoured"
    assert "of_not_redirect" not in sourced, "only redirect-mode offers are referral offers"
    assert "of_zero_price" not in sourced, "an unpriced offer cannot be shown to a buyer"
    # TWO guards cover this, deliberately: the SQL `destination IS NOT NULL` conjunct and the
    # Python `startswith(("http://", "https://"))` skip. Removing EITHER alone leaves the other,
    # so neither mutant dies on its own — removing BOTH fails this assertion. Recorded because a
    # future reader will otherwise see one of them as dead code and delete it.
    assert "of_no_destination" not in sourced, "an offer with nowhere to send the buyer is not an offer"
    assert sourced == {"of_retailer_cheap", "of_retailer_dear"}

    # ORDER BY on the computed alias — cheapest first
    assert offers[0]["source"]["offer_id"] == "of_retailer_cheap"
    assert offers[0]["price"] == 17.5
    assert offers[0]["merchant_id"] == "stylekorean_global"
    assert offers[0]["merchant_name"] == "StyleKorean", "the catalog_merchants join must resolve"
    # jsonb ->> extraction
    assert offers[0]["url"] == "https://www.stylekorean.com/p/1"


def test_the_arm_matches_by_content_key_and_product_key_too(pg_engine):
    """One `= ANY(:aliases)` bind carries all three identity shapes; SQLite cannot
    express it, so only this file can prove the OR-chain binds correctly."""
    _seed(pg_engine)
    for ident in ("ck_arm", _PK):
        res = _resolve(ident)
        assert (res.get("offers") or []), f"{ident} must resolve through the same arm"


def test_an_unknown_identity_returns_no_offers_and_says_so(pg_engine):
    _seed(pg_engine)
    res = _resolve("sig_does_not_exist")
    assert (res.get("offers") or []) == []
    sources = (res.get("metadata") or {}).get("sources") or []
    assert any(
        str(s.get("source")) == "catalog_offers" and str(s.get("status")) == "empty"
        for s in sources
    ), "an empty answer must be recorded, not silent"


def test_a_withdrawn_product_ships_no_offers(pg_engine):
    """`scripts/withdraw_catalog_rows.py` takes a product down by setting `suppressed_at` +
    `suppression_reason` on the PRODUCT. Every serving read in pivot_query_service applies that
    pair; without it here a withdrawn product keeps selling through this lane and the takedown
    silently misses it."""
    from sqlalchemy import text

    _seed(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text(
            "UPDATE catalog_products SET suppressed_at = NOW(), suppression_reason = 'test_takedown'"
            " WHERE product_key = :pk"
        ), {"pk": _PK})

    res = _resolve(_SIG)
    assert (res.get("offers") or []) == [], "a withdrawn product must not ship a retailer offer"


def test_a_foreign_market_offer_does_not_answer_a_us_request(pg_engine):
    """The seed lane filters on market; this arm must too. Latent today — the gateway sends no
    market and the retailer ingest writes 'US' — and live the moment a caller passes one."""
    from sqlalchemy import text

    _seed(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE catalog_offers SET market = 'KR' WHERE offer_id LIKE 'of_retailer%'"))

    res = _resolve(_SIG)
    assert (res.get("offers") or []) == [], "a KR offer must not be sold into a US request"
