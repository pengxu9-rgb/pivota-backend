"""Which ingest offer statement dates a price, and which drain jobs may ask it to (migration 246)."""
import asyncio

import pytest

import services.catalog_enrichment_agent.apply as apply_mod
from services.retailer_ingest import pipeline


def test_default_statements_never_name_the_stamp():
    for sql in (apply_mod._OFFER_UPSERT_SQL, apply_mod._OFFER_UPSERT_MARKET_SQL):
        assert "price_checked_at" not in sql


def test_price_read_statements_add_exactly_the_stamp():
    from services.catalog_enrichment_agent.bulk_writer import split_upsert_sql

    for base, read in ((apply_mod._OFFER_UPSERT_SQL, apply_mod._OFFER_UPSERT_PRICE_READ_SQL),
                       (apply_mod._OFFER_UPSERT_MARKET_SQL, apply_mod._OFFER_UPSERT_MARKET_PRICE_READ_SQL)):
        _, values, tail = split_upsert_sql(read)
        assert values.rstrip().endswith("NOW())")
        assert "price_checked_at = CASE" in tail and "ELSE EXCLUDED.price_checked_at END" in tail
        # No new bind: the bulk path binds VALUES only, and an unknown key fails text().bindparams.
        assert sorted(set(_binds(read))) == sorted(set(_binds(base)))


def _binds(sql):
    import re
    return re.findall(r"(?<!:):([a-z_]+)", sql)


def test_offer_sql_for_picks_by_market_and_vouch():
    plain, market = {"market": None}, {"market": "SG"}
    assert apply_mod._offer_sql_for(plain) is apply_mod._OFFER_UPSERT_SQL
    assert apply_mod._offer_sql_for(market) is apply_mod._OFFER_UPSERT_MARKET_SQL
    assert apply_mod._offer_sql_for(plain, price_read=True) is apply_mod._OFFER_UPSERT_PRICE_READ_SQL
    assert apply_mod._offer_sql_for(market, price_read=True) is apply_mod._OFFER_UPSERT_MARKET_PRICE_READ_SQL


def test_a_derivation_refuses_a_statement_whose_shape_moved():
    with pytest.raises(RuntimeError, match="changed shape"):
        apply_mod._offer_upsert_sql_with_price_read(apply_mod._OFFER_UPSERT_SQL.replace(
            "source_domain, offer_payload)", "source_domain,offer_payload)"))


def test_the_column_check_never_raises():
    class Broken:
        async def fetch_one(self, *_a, **_k):
            raise RuntimeError("no such table: pg_attribute")

    assert asyncio.run(apply_mod._offer_price_check_present(Broken())) is False


@pytest.mark.parametrize("options, vouched", [
    ({}, True),
    ({"source": "storefront"}, True),
    ({"source": "affiliate_feed"}, False),
    ({"source": "shopify_markets"}, False),
])
def test_only_a_storefront_crawl_vouches_for_its_prices(options, vouched):
    assert pipeline.plan_prices_read_live({"options": options}) is vouched


@pytest.mark.parametrize("answer, present", [({"present": False}, False), ({"present": True}, True), (None, False)])
def test_the_column_check_reports_what_the_database_says(answer, present):
    class Answers:
        async def fetch_one(self, *_a, **_k):
            return answer

    assert asyncio.run(apply_mod._offer_price_check_present(Answers())) is present
