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
  * `ORDER BY` a boolean over `= ANY(:unavailable)`, then the COMPUTED `price_amount` alias.

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
        for mid, name in (("m_arm", "Arm Merchant"), ("stylekorean_global", "StyleKorean"),
                          ("oliveyoung_global", "Olive Young")):
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

        def offer(oid, merchant, price, offer_type, mode="redirect", suppressed=False,
                  dest="https://www.stylekorean.com/p/1"):
            conn.execute(text(
                "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
                " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
                " currency, list_price, merchant_effective_price, offer_type, is_first_party,"
                " source_ref, offer_payload, suppressed_at, updated_at)"
                " VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only',:mode,"
                "         'external_referral','in_stock','USD',:p,:p,:ot,false,"
                "         :dest,"
                "         cast(:payload AS jsonb), :sup, NOW())"
            ), {"oid": oid, "sk": sku_key, "pk": _PK, "m": merchant, "p": price,
                "ot": offer_type, "mode": mode,
                "dest": dest,
                "payload": '{"destination_url": "%s"}' % dest,
                "sup": "2026-01-01T00:00:00" if suppressed else None})

        offer("of_retailer_cheap", "stylekorean_global", 17.50, "retailer")
        offer("of_retailer_dear", "oliveyoung_global", 29.00, "retailer",
              dest="https://global.oliveyoung.com/p/2")
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


def _resolve(product_id, limit=10):
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
                OffersResolvePayload(product={"product_id": product_id}, limit=limit,
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


def test_two_offers_to_the_same_destination_are_offered_once(pg_engine):
    """Dedupe is on the DESTINATION HOST, so it collapses a repeat within this source as well as
    one shared with the seed lane — two links to the same page are one place to send the buyer,
    whatever the price says. Found by this fixture: the first version gave both retailer rows the
    same URL and the second row correctly vanished."""
    from sqlalchemy import text

    _seed(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text(
            "UPDATE catalog_offers"
            "   SET source_ref = 'https://www.stylekorean.com/p/1',"
            "       offer_payload = cast('{\"destination_url\": \"https://www.stylekorean.com/p/1\"}' AS jsonb)"
            " WHERE offer_id = 'of_retailer_dear'"))

    res = _resolve(_SIG)
    ids = {str(o.get("offer_id") or "").rsplit(":", 1)[-1] for o in (res.get("offers") or [])}
    assert ids == {"of_retailer_cheap"}, "the same destination must be offered once"
    assert any(
        str(s.get("source")) == "catalog_offers" and s.get("deduped") == 1
        for s in ((res.get("metadata") or {}).get("sources") or [])
    )


_SIB_PK = "ext:retailer:sibling-listing"
_SIB_SIG = "sig_catalog_arm_sibling"
_OTHER_PK = "ext:retailer:other-product"


def _seed_listing(engine, pk, sig, content_key, merchant, offer_id, price, dest, suppressed=False,
                  availability="in_stock"):
    """A second retailer LISTING: its own product row, SKU and offer, like the curated retailer
    lane writes one per seller host."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status)"
            " VALUES (:m, :m, 'external_seed', 'active') ON CONFLICT DO NOTHING"
        ), {"m": merchant})
        conn.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,"
            " title, brand, content_key, pivota_signature_id, catalog_track, truth_tier,"
            " readiness_tier, pdp_scope, pdp_lifecycle_stage, source_system, updated_at,"
            " suppressed_at, suppression_reason)"
            " VALUES (:pk,:m,'external_seed',:pk,'Snail Essence','COSRX',:ck,:sig,"
            "         'citation','primary','referral_only','multi_merchant_canonical',"
            "         'published','test',NOW(), :sup, :why)"
        ), {"pk": pk, "m": merchant, "ck": content_key, "sig": sig,
            "sup": "2026-01-01T00:00:00" if suppressed else None,
            "why": "test_takedown" if suppressed else None})
        sku_key = f"{pk}::canonical"
        conn.execute(text(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
            " source_product_id, source_variant_id, title, currency, updated_at)"
            " VALUES (:sk,:pk,:m,'external_seed',:pk,'v1','Snail Essence','USD',NOW())"
        ), {"sk": sku_key, "pk": pk, "m": merchant})
        conn.execute(text(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
            " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
            " currency, list_price, merchant_effective_price, offer_type, is_first_party,"
            " source_ref, offer_payload, updated_at)"
            " VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only','redirect',"
            "         'external_referral',:avail,'USD',:p,:p,'retailer',false,:dest,"
            "         cast(:payload AS jsonb), NOW())"
        ), {"oid": offer_id, "sk": sku_key, "pk": pk, "m": merchant, "p": price, "dest": dest,
            "avail": availability, "payload": '{"destination_url": "%s"}' % dest})


def _sourced(res):
    return {str(o.get("offer_id") or "").rsplit(":", 1)[-1] for o in (res.get("offers") or [])}


def test_a_listing_id_returns_every_seller_that_shares_its_content_key(pg_engine):
    """The door fix. Two retailers' listings of one product converge on a content_key, and a
    buyer's agent holds a LISTING id (search and the PDP hand out product_key / signature, never
    the content_key). Asking by EITHER listing must return BOTH sellers — and each offer keeps its
    own listing product_key, so the seller tuple is not rewritten. An unrelated product's offer
    must not ride along."""
    _seed(pg_engine)
    _seed_listing(pg_engine, _SIB_PK, _SIB_SIG, "ck_arm", "agent_seed::retailer::ohlolly.com",
                  "of_sibling", 19.99, "https://ohlolly.com/products/snail")
    _seed_listing(pg_engine, _OTHER_PK, "sig_other_product", "ck_other",
                  "agent_seed::retailer::eyurs.com", "of_other", 5.00,
                  "https://eyurs.com/products/other")

    want = {"of_retailer_cheap", "of_retailer_dear", "of_sibling"}
    for ident in (_SIG, _PK, _SIB_SIG, _SIB_PK, "ck_arm"):
        res = _resolve(ident)
        assert _sourced(res) == want, (ident, _sourced(res))
        by_offer = {o["source"]["offer_id"]: o for o in res["offers"]}
        assert by_offer["of_sibling"]["source"]["product_key"] == _SIB_PK
        assert by_offer["of_retailer_cheap"]["source"]["product_key"] == _PK
        assert by_offer["of_sibling"]["merchant_id"] == "agent_seed::retailer::ohlolly.com"


def test_a_withdrawn_sibling_is_not_offered_through_a_live_listing(pg_engine):
    _seed(pg_engine)
    _seed_listing(pg_engine, _SIB_PK, _SIB_SIG, "ck_arm", "agent_seed::retailer::ohlolly.com",
                  "of_sibling", 19.99, "https://ohlolly.com/products/snail", suppressed=True)
    assert _sourced(_resolve(_SIG)) == {"of_retailer_cheap", "of_retailer_dear"}


def test_a_withdrawn_listing_id_does_not_widen_to_its_live_siblings(pg_engine):
    """The takedown contract for a listing id is unchanged: a withdrawn listing answered nothing
    before the widening, and it must not start answering with its siblings' offers."""
    _seed(pg_engine)
    _seed_listing(pg_engine, _SIB_PK, _SIB_SIG, "ck_arm", "agent_seed::retailer::ohlolly.com",
                  "of_sibling", 19.99, "https://ohlolly.com/products/snail", suppressed=True)
    assert _sourced(_resolve(_SIB_SIG)) == set()
    assert _sourced(_resolve(_SIB_PK)) == set()


# --- stock before price ---------------------------------------------------------------------
#
# Measured in prod 2026-09-18, right after the Wave 1 retailer ingest: get_offers on the Purito
# Oat-in Calming Gel Cream (ext:retailer:0465db3774ad9906d3d91664fcd3ab1a, three retailers)
# returned eyurs.com $13 out_of_stock, then sokoglam.com $19.50 in_stock, then ohlolly.com $21
# out_of_stock. The same census found 7 content_keys with a cheaper out-of-stock offer ranked
# above an in-stock one, 5 of them at rank 1, and 6 where limit 1 or 2 cut every in-stock seller.

_PURITO_CK = "ck_purito_oat"


def _seed_purito(engine, eyurs="out_of_stock", sokoglam="in_stock", ohlolly="out_of_stock"):
    _seed(engine)
    for n, (merchant, oid, price, dest, avail) in enumerate((
        ("agent_seed::retailer::eyurs.com", "of_eyurs", 13.00,
         "https://eyurs.com/products/purito-oat", eyurs),
        ("agent_seed::retailer::sokoglam.com", "of_sokoglam", 19.50,
         "https://sokoglam.com/products/purito-oat", sokoglam),
        ("agent_seed::retailer::ohlolly.com", "of_ohlolly", 21.00,
         "https://ohlolly.com/products/purito-oat", ohlolly),
    )):
        _seed_listing(engine, f"ext:retailer:purito-{n}", f"sig_purito_{n}", _PURITO_CK,
                      merchant, oid, price, dest, availability=avail)


def _order(res):
    return [o["source"]["offer_id"] for o in (res.get("offers") or [])]


def test_an_in_stock_seller_outranks_a_cheaper_out_of_stock_one(pg_engine):
    """The measured case. In stock first; price ascending WITHIN each group, so the two sellers
    that cannot sell keep their cheapest-first order behind the one that can."""
    _seed_purito(pg_engine)
    res = _resolve(_PURITO_CK)
    assert _order(res) == ["of_sokoglam", "of_eyurs", "of_ohlolly"]
    # The order must agree with the stock flag printed on each offer, never contradict it.
    assert [o["in_stock"] for o in res["offers"]] == [True, False, False]


def test_the_limit_never_cuts_an_in_stock_seller_for_cheaper_out_of_stock_ones(pg_engine):
    """ORDER BY must apply before LIMIT. At limit 1 the price-only order kept eyurs (cannot sell)
    and dropped sokoglam (can) — the buyer's agent got one offer and it was a dead end."""
    _seed_purito(pg_engine)
    assert _order(_resolve(_PURITO_CK, limit=1)) == ["of_sokoglam"]
    assert _order(_resolve(_PURITO_CK, limit=2)) == ["of_sokoglam", "of_eyurs"]


def test_unknown_availability_ranks_with_in_stock_by_price_not_behind_it(pg_engine):
    """`unknown` is the column's default and says nothing against the seller, so it competes on
    price with in-stock offers and stays ahead of out-of-stock ones. The shipped `in_stock` flag
    reads True for it, which is exactly why the order must not treat it as worse."""
    _seed_purito(pg_engine, eyurs="unknown", sokoglam="in_stock", ohlolly="out_of_stock")
    res = _resolve(_PURITO_CK)
    assert _order(res) == ["of_eyurs", "of_sokoglam", "of_ohlolly"]
    assert [o["in_stock"] for o in res["offers"]] == [True, True, False]


def test_the_sql_normalises_availability_exactly_as_the_stock_flag_does(pg_engine):
    """Case and padding: ` SOLD_OUT ` is unavailable to the Python flag, so it must be to the
    ORDER BY too, or a sloppy feed value would rank an unsellable offer first while labelled so."""
    _seed_purito(pg_engine, eyurs=" SOLD_OUT ", sokoglam="in_stock", ohlolly="Unavailable")
    res = _resolve(_PURITO_CK, limit=1)
    assert _order(res) == ["of_sokoglam"]
    # Tab and newline too: `btrim` alone strips only spaces, `.strip()` strips all of these.
    _seed_purito(pg_engine, eyurs="\tout_of_stock\n", sokoglam="in_stock", ohlolly="sold_out\r")
    res = _resolve(_PURITO_CK, limit=1)
    assert _order(res) == ["of_sokoglam"]


def test_the_host_dedupe_keeps_the_in_stock_listing_of_a_host(pg_engine):
    """The dedupe itself is unchanged — first offer per destination host wins — but it reads the
    rows in the new order, so a host's in-stock listing now survives over its cheaper sold-out
    one instead of being deduped away behind it."""
    _seed(pg_engine)
    _seed_listing(pg_engine, "ext:retailer:dup-a", "sig_dup_a", "ck_dup",
                  "agent_seed::retailer::eyurs.com", "of_dup_sold_out", 9.00,
                  "https://eyurs.com/products/a", availability="out_of_stock")
    _seed_listing(pg_engine, "ext:retailer:dup-b", "sig_dup_b", "ck_dup",
                  "agent_seed::retailer::eyurs.com", "of_dup_in_stock", 11.00,
                  "https://www.eyurs.com/products/b")
    res = _resolve("ck_dup")
    assert _order(res) == ["of_dup_in_stock"]
    assert any(
        str(s.get("source")) == "catalog_offers" and s.get("deduped") == 1
        for s in ((res.get("metadata") or {}).get("sources") or [])
    )


def test_a_price_tie_is_cut_by_offer_id_the_same_way_every_time(pg_engine):
    """Two in-stock sellers at one price: without a final key the LIMIT picks whichever row the
    plan yields first. Seeded in REVERSE id order so insertion order cannot pass this."""
    _seed(pg_engine)
    _seed_listing(pg_engine, "ext:retailer:tie-b", "sig_tie_b", "ck_tie",
                  "agent_seed::retailer::sokoglam.com", "of_tie_b", 15.00,
                  "https://sokoglam.com/products/tie")
    _seed_listing(pg_engine, "ext:retailer:tie-a", "sig_tie_a", "ck_tie",
                  "agent_seed::retailer::ohlolly.com", "of_tie_a", 15.00,
                  "https://ohlolly.com/products/tie")
    assert _order(_resolve("ck_tie", limit=1)) == ["of_tie_a"]
