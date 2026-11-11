"""services/deal_code_capture_writer.py: captured codes become UNVERIFIED merchant_published rows.

Pinned: one row per (seller, market, code); market only with the page's own currency as evidence; no row without a
stated kind; amounts keep a currency only when the symbol agrees; eligibility stays unknown; a later sighting refreshes
observation fields only; an opted-out seller is never written; the code never reaches a log; nothing ever raises."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services import deal_code_capture as dcc  # noqa: E402
from services import deal_code_capture_writer as w  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db():
    from db import deal_lane
    from db.database import database, engine, metadata

    from db.catalog import catalog_products

    tables = [deal_lane.observed_store_offers, deal_lane.observed_store_offer_verifications, deal_lane.deal_lane_optout,
              catalog_products]
    metadata.create_all(engine, tables=tables)
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    async def _clean():
        for table in reversed(tables):
            await database.execute(table.delete())

    await _clean()
    dcc.reset_stats()
    try:
        yield database
    finally:
        await _clean()
        dcc.reset_stats()
        if not was_connected:
            await database.disconnect()


def captured(code="SAVE15", kind="percent", value="15", symbol=None, spend=None, spend_symbol=None, hints=None,
             snippet="Get 15% off with code SAVE15", form="with_code"):
    return dcc.CapturedCode(code=code, kind=kind, value=value, currency_symbol=symbol, min_subtotal=spend,
                            hints=list(hints or []), snippet=snippet, shape="alnum", form=form,
                            min_subtotal_symbol=spend_symbol)


async def rows(db):
    from db.deal_lane import observed_store_offers as o

    return [dict(r) for r in await db.fetch_all(o.select())]


async def test_a_captured_code_becomes_one_unverified_merchant_published_row(db):
    await w.record([captured(spend="50", spend_symbol="$", hints=["new_customer_only"])], url="https://www.Glow.example/products/toner", final_url="https://www.Glow.example/products/toner",
                   market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["seller_id"] == "glow.example"
    assert r["market"] == "US"
    assert r["code"] == "SAVE15"
    assert (r["kind"], float(r["value"]), r["currency"]) == ("percent", 15.0, "USD")  # the minimum spend's currency
    assert float(r["min_subtotal"]) == 50.0
    assert r["new_customer_only"] is True or r["new_customer_only"] == 1
    assert not r["eligibility_known"]
    assert r["source"] == "merchant_published"
    assert r["source_url"] == "https://www.Glow.example/products/toner"
    assert r["terms_text"] == "Get 15% off with code SAVE15"
    assert r["deal_id"].startswith("dlc_"), "the crawl writer's own provenance prefix"
    assert dcc.stats()["writes"] == {"inserted": 1}


async def test_market_only_with_the_pages_own_currency_as_evidence(db):
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="SG", page_currency="USD")
    (r,) = await rows(db)
    assert r["market"] is None
    assert dcc.stats()["writes"] == {"no_market_evidence": 1, "inserted": 1}
    assert w.market_evidence("SG", "SGD") == "SG"
    assert w.market_evidence("GB", "GBP") is None  # not a served market
    assert w.market_evidence("US", None) is None


async def test_a_second_sighting_refreshes_the_observation_never_identity_or_verification(db):
    from db.deal_lane import observed_store_offer_verifications as v

    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    (first,) = await rows(db)
    await db.execute(v.insert().values(deal_id=first["deal_id"], variant_id="44012345678901", state="unverified"))
    await w.record([captured(code="save15", value="20", snippet="Now 20% off with code save15")],
                   url="https://glow.example/q", final_url="https://glow.example/q", market="US", page_currency="USD")
    (second,) = await rows(db)
    assert second["deal_id"] == first["deal_id"]
    assert second["code"] == "SAVE15"  # identity column untouched (Shopify matches codes case-insensitively)
    assert float(second["value"]) == 15.0, "kind and value are set once, at insert"
    assert second["source_url"] == "https://glow.example/q"
    assert second["observed_at"] >= first["observed_at"]
    assert second["created_at"] == first["created_at"]
    assert len(await db.fetch_all(v.select())) == 1
    # The bar text follows the row's own discount: a sighting stating another value does not replace it.
    assert second["terms_text"] == "Get 15% off with code SAVE15"
    assert dcc.stats()["writes"] == {"inserted": 1, "updated": 1, "terms_differ": 1}


async def test_no_row_without_a_stated_kind_or_with_an_unreadable_value(db):
    await w.record([captured(kind=None, value=None), captured(code="BIG200", value="200"), captured(code="NAN1", value="abc")],
                   url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_no_kind": 1, "skipped_unreadable": 2}


async def test_an_amount_keeps_a_currency_only_when_the_symbol_agrees(db):
    await w.record([captured(code="TEN10", kind="amount", value="10", symbol="$")], url="https://a.example/p", final_url="https://a.example/p",
                   market="US", page_currency="USD")
    await w.record([captured(code="FIVE5", kind="amount", value="5", symbol="£")], url="https://b.example/p", final_url="https://b.example/p",
                   market="US", page_currency="USD")
    got = {r["code"]: r for r in await rows(db)}
    assert got["TEN10"]["currency"] == "USD"
    assert "FIVE5" not in got, "an amount stated in another currency than the page's writes no row"
    assert dcc.stats()["writes"]["skipped_unreadable"] == 1


async def test_free_shipping_is_written_without_a_value(db):
    await w.record([captured(code="SHIP25", kind="free_shipping", value=None)], url="https://a.example/p", final_url="https://a.example/p",
                   market="US", page_currency="USD")
    (r,) = await rows(db)
    assert (r["kind"], r["value"]) == ("free_shipping", None)


async def test_an_opted_out_seller_is_never_written(db):
    from db.deal_lane import deal_lane_optout

    await db.execute(deal_lane_optout.insert().values(seller_id="glow.example", requested_at=datetime.now(timezone.utc),
                                                       recorded_by="test"))
    await w.record([captured()], url="https://www.glow.example/p", final_url="https://www.glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout": 1}


async def test_a_database_failure_is_counted_and_never_raised(db, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(w.database, "fetch_one", boom)
    monkeypatch.setattr(w.database, "fetch_all", boom)
    caplog.set_level(logging.WARNING)
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    # The first read is the opt-out check, which fails closed.
    assert dcc.stats()["writes"] == {"skipped_optout_unreadable": 1}
    assert "SAVE15" not in caplog.text


async def test_one_codes_failure_never_costs_the_others(db, monkeypatch):
    real = w.database.execute
    calls = {"n": 0}

    async def flaky(query, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("one bad write")
        return await real(query, *a, **k)

    monkeypatch.setattr(w.database, "execute", flaky)
    await w.record([captured(code="FIRST1"), captured(code="SECOND2")], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert [r["code"] for r in await rows(db)] == ["SECOND2"]
    assert dcc.stats()["writes"] == {"errors": 1, "inserted": 1}


async def test_nothing_to_write_touches_nothing(db, monkeypatch):
    async def must_not_run(*a, **k):
        raise AssertionError("no database call for an empty page")

    monkeypatch.setattr(w.database, "fetch_one", must_not_run)
    await w.record([], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert dcc.stats()["writes"] == {}



# ---- review of be#2732 ------------------------------------------------------------------------------------------


async def insert_product(db, *, merchant_id, source_domain):
    from db.catalog import catalog_products

    await db.execute(catalog_products.insert().values(
        product_key=f"prod::{merchant_id}::{source_domain}", merchant_id=merchant_id, platform="shopify",
        source_product_id="1", source_domain=source_domain, title="t",
    ))


async def test_the_loose_bare_code_form_is_never_written(db):
    await w.record([captured(form="after_code"), captured(code="REAL10", form="use_code")], url="https://glow.example/p", final_url="https://glow.example/p",
                   market="US", page_currency="USD")
    assert [r["code"] for r in await rows(db)] == ["REAL10"]
    assert dcc.stats()["writes"] == {"skipped_loose_form": 1, "inserted": 1}


async def test_a_page_another_host_served_is_not_written_under_the_requested_seller(db):
    await w.record([captured()], url="https://glow.example/p", final_url="https://other-brand.example/p",
                   market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_host_changed": 1}
    await w.record([captured()], url="https://glow.example/p", final_url="https://www.glow.example/products/p",
                   market="US", page_currency="USD")
    assert len(await rows(db)) == 1, "the same seller's www. form is the same host"


@pytest.mark.parametrize("optout_id", ["glow.example", "www.glow.example", "merch_obs_glow"])
async def test_an_opt_out_matches_the_host_its_www_form_or_the_catalogs_merchant_id(db, optout_id):
    from db.deal_lane import deal_lane_optout

    await insert_product(db, merchant_id="merch_obs_glow", source_domain="www.glow.example")
    await db.execute(deal_lane_optout.insert().values(seller_id=optout_id, requested_at=datetime.now(timezone.utc),
                                                       recorded_by="test"))
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout": 1}


async def test_an_opt_out_check_that_cannot_be_read_writes_nothing(db, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("catalog unreadable SAVE15")

    monkeypatch.setattr(w.database, "fetch_all", boom)  # the opt-out check is the first read
    caplog.set_level(logging.WARNING)
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    monkeypatch.undo()
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout_unreadable": 1}
    assert "SAVE15" not in caplog.text and "catalog unreadable" not in caplog.text


async def test_the_per_host_and_per_run_write_budgets(db, monkeypatch):
    monkeypatch.setattr(dcc, "MAX_WRITES_PER_HOST_PER_RUN", 2)
    monkeypatch.setattr(dcc, "MAX_WRITES_PER_RUN", 3)
    await w.record([captured(code=f"HOST{i}A") for i in range(3)], url="https://a.example/p", final_url="https://a.example/p", market="US", page_currency="USD")
    await w.record([captured(code=f"HOST{i}B") for i in range(3)], url="https://b.example/p", final_url="https://b.example/p", market="US", page_currency="USD")
    assert len(await rows(db)) == 3
    assert dcc.stats()["writes"] == {"inserted": 3, "skipped_host_cap": 1, "skipped_run_cap": 2}
    dcc.reset_stats()
    await w.record([captured(code="AFTER9")], url="https://a.example/p", final_url="https://a.example/p", market="US", page_currency="USD")
    assert dcc.stats()["writes"] == {"inserted": 1}, "a new run starts with a fresh budget"


async def test_an_insert_that_loses_the_race_becomes_an_update(db, monkeypatch):
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    real = w.database.fetch_one
    misses = {"n": 0}

    async def stale_first_look(query, *a, **k):
        text = str(query)
        if "observed_store_offers" in text and misses["n"] == 0:
            misses["n"] += 1
            return None  # as if another writer had not committed yet
        return await real(query, *a, **k)

    monkeypatch.setattr(w.database, "fetch_one", stale_first_look)
    monkeypatch.setattr(dcc, "MAX_WRITES_PER_HOST_PER_RUN", 1)
    dcc.reset_stats()
    await w.record([captured()], url="https://glow.example/q", final_url="https://glow.example/q", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["source_url"] == "https://glow.example/q"
    assert dcc.stats()["writes"] == {"updated": 1}
    # The lost race wrote no new row, so it gave its slot back (be#2732 P3-E): the host's one new row still fits.
    await w.record([captured(code="NEW22")], url="https://glow.example/q", final_url="https://glow.example/q", market="US", page_currency="USD")
    assert dcc.stats()["writes"] == {"updated": 1, "inserted": 1}


async def test_a_row_another_source_owns_is_never_overwritten(db):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o

    now = datetime.now(timezone.utc)
    key = observed_store_offer_key(seller_id="glow.example", market="US", code="SAVE15", platform_rule_ref=None, program_id=None)
    await db.execute(o.insert().values(deal_id="dl_other", deal_key=key, seller_id="glow.example", market="US", code="SAVE15",
                                       kind="amount", value=5, source="channel_exclusive", observed_at=now))
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert (r["source"], r["kind"]) == ("channel_exclusive", "amount")
    assert dcc.stats()["writes"] == {"skipped_other_source": 1}


async def test_the_identity_includes_the_market(db):
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="SG", page_currency="SGD")
    got = sorted((r["market"], r["code"]) for r in await rows(db))
    assert got == [("SG", "SAVE15"), ("US", "SAVE15")]


async def test_a_minimum_spend_is_kept_only_in_the_pages_currency(db):
    await w.record([captured(code="GBP50", spend="50", spend_symbol="£")], url="https://a.example/p", final_url="https://a.example/p", market="US", page_currency="USD")
    await w.record([captured(code="USD50", spend="50", spend_symbol="$")], url="https://b.example/p", final_url="https://b.example/p", market="US", page_currency="USD")
    got = {r["code"]: r for r in await rows(db)}
    assert (got["GBP50"]["min_subtotal"], got["GBP50"]["currency"]) == (None, None)
    assert (float(got["USD50"]["min_subtotal"]), got["USD50"]["currency"]) == (50.0, "USD")


async def test_a_write_failure_logs_the_error_type_only(db, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("insert failed for SAVE15 at glow")

    monkeypatch.setattr(w.database, "execute", boom)
    caplog.set_level(logging.WARNING)
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert dcc.stats()["writes"] == {"errors": 1}
    assert "RuntimeError" in caplog.text
    assert "SAVE15" not in caplog.text and "insert failed" not in caplog.text



async def test_a_merchant_published_row_this_writer_did_not_create_is_never_refreshed(db):
    from db.deal_lane import observed_store_offer_key, observed_store_offers as o

    now = datetime.now(timezone.utc)
    key = observed_store_offer_key(seller_id="glow.example", market="US", code="SAVE15", platform_rule_ref=None, program_id=None)
    await db.execute(o.insert().values(deal_id="dl_handentered", deal_key=key, seller_id="glow.example", market="US",
                                       code="SAVE15", kind="amount", value=5, source="merchant_published", observed_at=now))
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert (r["deal_id"], r["kind"]) == ("dl_handentered", "amount")
    assert dcc.stats()["writes"] == {"skipped_other_source": 1}


# ---- delta review of be#2732 ------------------------------------------------------------------------------------


async def test_a_code_without_a_digit_stays_in_the_shadow_counts(db):
    await w.record([captured(code="THROUGH"), captured(code="NEWSLETTER"), captured(code="SAVE15")],
                   url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert [r["code"] for r in await rows(db)] == ["SAVE15"]
    assert dcc.stats()["writes"] == {"skipped_no_digit": 2, "inserted": 1}


async def test_no_final_url_writes_nothing(db):
    await w.record([captured()], url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_no_final_url": 1}


async def test_the_opt_out_finds_a_merchant_past_twenty_ids_mixed_case_and_beside_null_domains(db):
    from db.catalog import catalog_products
    from db.deal_lane import deal_lane_optout

    for i in range(25):
        await db.execute(catalog_products.insert().values(product_key=f"p{i}", merchant_id=f"merch_obs_{i:02d}", platform="shopify",
                                                          source_product_id=str(i), source_domain="WWW.Glow.Example", title="t"))
    await db.execute(catalog_products.insert().values(product_key="pnull", merchant_id="merch_obs_24", platform="shopify",
                                                      source_product_id="n", source_domain=None, title="t"))
    await db.execute(deal_lane_optout.insert().values(seller_id="MERCH_OBS_24", requested_at=datetime.now(timezone.utc), recorded_by="test"))
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout": 1}


async def test_the_opt_out_matches_a_mixed_case_host_id(db):
    from db.deal_lane import deal_lane_optout

    await db.execute(deal_lane_optout.insert().values(seller_id="WWW.GLOW.EXAMPLE", requested_at=datetime.now(timezone.utc), recorded_by="test"))
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []


async def test_a_repeat_sighting_does_not_spend_the_budget(db, monkeypatch):
    monkeypatch.setattr(dcc, "MAX_WRITES_PER_HOST_PER_RUN", 1)
    for _ in range(3):
        await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    await w.record([captured(code="NEW22")], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert dcc.stats()["writes"] == {"inserted": 1, "updated": 2, "skipped_host_cap": 1}


async def test_a_later_sighting_never_changes_the_restriction_flags(db):
    await w.record([captured(hints=["new_customer_only"])], url="https://glow.example/p", final_url="https://glow.example/p",
                   market="US", page_currency="USD")
    await w.record([captured(hints=[])], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["new_customer_only"] in (True, 1)


# ── digit tokens that are not codes (review of be#2732, P3-K) ──────────────────────────────────────────────────────
@pytest.mark.parametrize("token", ["48HRS", "24HR", "48H", "1ST", "2ND", "3RD", "4TH", "3PM", "10AM", "2-DAY", "2DAY",
                                   "3DAYS", "3X", "1PER", "XXXX-1234", "SPF50", "B2B", "D2C",
                                   "48HOURS", "2-IN-1", "3PCS", "50ML", "8OZ", "2PK", "KN95", "N95", "30MIN", "2WEEKS",
                                   "12MONTHS", "1080P", "10AM-2PM", "6FT",
                                   "4X4", "3XL", "2XS", "100PCT", "360-DEGREE", "360DEG", "5STAR", "5-STARS", "2026SS",
                                   "2025FW"])
async def test_a_digit_token_that_names_something_else_is_not_written(db, token):
    await w.record([captured(code=token, snippet=f"Get 15% off with code {token}")], url="https://glow.example/p",
                   final_url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_not_a_code": 1}


@pytest.mark.parametrize("snippet", ["Get 15% off with code ABC123 (example)", "Get 15% off with code ABC123, e.g. at checkout",
                                     "Get 15% off with code ABC123 — for example only",
                                     "Example: use code ABC123 at checkout for 15% off",
                                     "Enter a code such as ABC123 for 15% off",
                                     "For instance, use code ABC123 for 15% off",
                                     "Get 15% off with code ABC123, for instance on boots"])
async def test_a_code_the_page_calls_an_example_is_not_written(db, snippet):
    await w.record([captured(code="ABC123", snippet=snippet)], url="https://glow.example/p",
                   final_url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_not_a_code": 1}


@pytest.mark.parametrize("token", ["50OFF", "20OFF", "4EVER", "COVID19", "W10", "SAVE15", "B2B10X1", "10PCT", "20PCT"])
async def test_ambiguous_digit_codes_stores_really_use_are_written(db, token):
    await w.record([captured(code=token, snippet=f"Get 15% off with code {token} today")], url="https://glow.example/p",
                   final_url="https://glow.example/p", market="US", page_currency="USD")
    assert [r["code"] for r in await rows(db)] == [token]


async def test_a_letters_and_dash_token_is_not_written(db):
    await w.record([captured(code="IN-APP", snippet="Get 15% off with code IN-APP")], url="https://glow.example/p",
                   final_url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_no_digit": 1}


# ── the opt-out normalises every side (review of be#2732, P3-I) ────────────────────────────────────────────────────
async def _optout(db, seller_id):
    from db.deal_lane import deal_lane_optout

    await db.execute(deal_lane_optout.insert().values(seller_id=seller_id, requested_at=datetime.now(timezone.utc), recorded_by="test"))


async def _catalog(db, i, merchant, domain=None, url=None):
    from db.catalog import catalog_products

    await db.execute(catalog_products.insert().values(product_key=f"p{i}", merchant_id=merchant, platform="shopify",
                                                      source_product_id=str(i), source_domain=domain, canonical_url=url, title="t"))


@pytest.mark.parametrize("optout,catalog", [
    ("  glow.example  ", None),
    ("GLOW.EXAMPLE.", None),
    ("https://www.glow.example/collections/all?x=1", None),
    ("glow.example:443", None),
    ("merch_obs_glow", ("merch_obs_glow", "  WWW.Glow.Example.  ", None)),
    ("merch_obs_glow", ("merch_obs_glow", "glow.example:8443", None)),
    (" merch_obs_glow ", ("merch_obs_glow", "glow.example", None)),
    ("merch_obs_glow", ("merch_obs_glow", None, "https://www.glow.example/products/toner")),
    ("www.*.glow.example", None),
    ("*.www.glow.example", None),
    ("www.\u2003glow.example", None),  # an em space UTS-46 maps to a space after "www." (be#2742 delta 3, P3-1)
    ("merch_obs_glow", ("merch_obs_glow", None, "https://www.\u2003glow.example/p")),
])
async def test_the_opt_out_matches_whatever_form_each_side_was_entered_in(db, optout, catalog):
    if catalog:
        await _catalog(db, 1, *catalog)
    await _optout(db, optout)
    await w.record([captured()], url="https://glow.example./p", final_url="https://glow.example./p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout": 1}


async def test_an_opted_out_host_covers_its_subdomains_but_not_a_shared_platform_or_a_look_alike(db):
    await _optout(db, "glow.example")
    await _optout(db, "myshopify.com")
    await w.record([captured()], url="https://shop.glow.example/p", final_url="https://shop.glow.example/p", market="US", page_currency="USD")
    await w.record([captured()], url="https://notglow.example/p", final_url="https://notglow.example/p", market="US", page_currency="USD")
    await w.record([captured()], url="https://brand.myshopify.com/p", final_url="https://brand.myshopify.com/p", market="US", page_currency="USD")
    assert sorted(r["seller_id"] for r in await rows(db)) == ["brand.myshopify.com", "notglow.example"]


async def test_negative_controls_the_merchant_and_the_host_must_both_match(db):
    # The opted-out merchant's catalog names another host; another merchant's catalog names this host.
    await _catalog(db, 1, "merch_obs_glow", "other.example")
    await _catalog(db, 2, "merch_obs_someone", "glow.example")
    await _optout(db, "merch_obs_glow")
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert [r["seller_id"] for r in await rows(db)] == ["glow.example"]


async def test_the_placeholder_merchant_id_never_blocks_a_host(db):
    await _catalog(db, 1, "external_seed", "glow.example")
    await _optout(db, "external_seed")
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    assert [r["seller_id"] for r in await rows(db)] == ["glow.example"]


async def test_a_merchant_id_over_the_host_limit_fails_closed_for_writes(db):
    # Review of be#2735, P2-N1: not one seller's hosts, so retention deletes nothing through it, but the writer
    # blocks every host it names (costs recall only).
    for i, host in enumerate(["a.example", "b.example", "c.example", "glow.example"]):
        await _catalog(db, 10 + i, "merch_wide", host)
    await _optout(db, "merch_wide")
    for host in ("a.example", "glow.example", "shop.glow.example", "other.example"):
        await w.record([captured()], url=f"https://{host}/p", final_url=f"https://{host}/p", market="US", page_currency="USD")
    assert [r["seller_id"] for r in await rows(db)] == ["other.example"]
    assert dcc.stats()["writes"] == {"skipped_optout": 3, "inserted": 1}


# ── a later sighting adds a restriction, never clears one; the bar text is never emptied (be#2732, P3-F) ───────────
async def test_a_later_sighting_adds_a_restriction_and_never_clears_one(db):
    await w.record([captured(hints=["new_customer_only"], snippet="15% off your first order with code SAVE15")],
                   url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    await w.record([captured(snippet="Get 15% off with code SAVE15")], url="https://glow.example/q",
                   final_url="https://glow.example/q", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["new_customer_only"] is True or r["new_customer_only"] == 1
    await w.record([captured(hints=["once_per_customer"])], url="https://glow.example/r",
                   final_url="https://glow.example/r", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert bool(r["once_per_customer"]) and bool(r["new_customer_only"])


async def test_an_empty_snippet_never_empties_the_bar_text(db):
    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    await w.record([captured(snippet="")], url="https://glow.example/q", final_url="https://glow.example/q", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["terms_text"] == "Get 15% off with code SAVE15"
    assert r["source_url"] == "https://glow.example/q"


async def test_a_re_sighting_does_not_restore_bar_text_retention_cleared(db):
    from db.deal_lane import observed_store_offer_verifications as v, observed_store_offers as o

    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    (first,) = await rows(db)
    await db.execute(v.insert().values(deal_id=first["deal_id"], variant_id="1", state="failed", failure_reason="x"))
    await db.execute(o.update().values(terms_text=None))
    await w.record([captured()], url="https://glow.example/q", final_url="https://glow.example/q", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["terms_text"] is None and r["source_url"] == "https://glow.example/q"
    # A queued, unchecked verification does not stop the refresh.
    await db.execute(v.delete())
    await db.execute(v.insert().values(deal_id=first["deal_id"], variant_id="1", state="unverified"))
    await w.record([captured()], url="https://glow.example/r", final_url="https://glow.example/r", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["terms_text"] == "Get 15% off with code SAVE15"


async def test_a_re_sighting_does_not_restore_bar_text_after_14_days(db):
    from datetime import timedelta

    from db.deal_lane import observed_store_offers as o

    await w.record([captured()], url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    await db.execute(o.update().values(terms_text=None, created_at=datetime.now(timezone.utc) - timedelta(days=15)))
    await w.record([captured()], url="https://glow.example/q", final_url="https://glow.example/q", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["terms_text"] is None



# ── a canonical-URL host blocks writes; a marketplace host never does; each id reads its own URLs (P3-N2) ──────────
async def test_canonical_url_hosts_block_writes_per_id_and_skip_marketplaces(db, monkeypatch):
    monkeypatch.setattr(w, "CANONICAL_HOSTS_PER_ID", 2)
    for i in range(5):  # the first id has more URLs than the per-id read
        await _catalog(db, i, "merch_big", url=f"https://big.example/p/{i}")
    await _catalog(db, 20, "merch_small", url="https://small.example/p/1")
    await _catalog(db, 21, "merch_market", url="https://www.amazon.com/dp/1")
    for mid in ("merch_big", "merch_small", "merch_market"):
        await _optout(db, mid)
    for host in ("big.example", "small.example", "amazon.com", "deals.amazon.com"):
        await w.record([captured()], url=f"https://{host}/p", final_url=f"https://{host}/p", market="US", page_currency="USD")
    assert sorted(r["seller_id"] for r in await rows(db)) == ["amazon.com", "deals.amazon.com"]


async def test_a_public_suffix_or_platform_opt_out_never_blocks_the_stores_under_it(db):
    for optout in ("co.uk", "wixsite.com", "shop.app"):
        await _optout(db, optout)
    for host in ("brand9.co.uk", "s4.wixsite.com", "store.shop.app"):
        await w.record([captured()], url=f"https://{host}/p", final_url=f"https://{host}/p", market="US", page_currency="USD")
    assert sorted(r["seller_id"] for r in await rows(db)) == ["brand9.co.uk", "s4.wixsite.com", "store.shop.app"]


# ── the denylist is read once per run (P3-N6) ────────────────────────────────────────────────────────────────────────
async def test_the_denylist_is_read_once_per_run(db, monkeypatch):
    await _optout(db, "blocked.example")
    reads = []
    real = w.read_denyset

    async def counting():
        reads.append(1)
        return await real()

    monkeypatch.setattr(w, "read_denyset", counting)
    for host in ("a.example", "b.example", "blocked.example"):
        await w.record([captured()], url=f"https://{host}/p", final_url=f"https://{host}/p", market="US", page_currency="USD")
    assert len(reads) == 1
    assert dcc.stats()["writes"] == {"inserted": 2, "skipped_optout": 1}
    dcc.reset_stats()  # a new run reads again
    await w.record([captured(code="NEW22")], url="https://c.example/p", final_url="https://c.example/p", market="US", page_currency="USD")
    assert len(reads) == 2


async def test_a_sighting_stating_another_currency_does_not_replace_the_bar_text(db):
    # The first sighting states a $50 minimum (the row's currency is USD); the second states none (no currency).
    await w.record([captured(spend="50", spend_symbol="$", snippet="15% off $50+ with code SAVE15")],
                   url="https://glow.example/p", final_url="https://glow.example/p", market="US", page_currency="USD")
    await w.record([captured(snippet="15% off everything with code SAVE15")],
                   url="https://glow.example/q", final_url="https://glow.example/q", market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["currency"] == "USD"
    assert r["terms_text"] == "15% off $50+ with code SAVE15"
    assert dcc.stats()["writes"] == {"inserted": 1, "updated": 1, "terms_differ": 1}



async def test_the_canonical_url_read_sees_past_the_first_url_of_an_id(db):
    # Ordered by URL, this id's first is a marketplace listing; its own host comes after it.
    await _catalog(db, 1, "merch_mix", url="https://amazon.com/dp/1")
    await _catalog(db, 2, "merch_mix", url="https://mix.example/p/1")
    await _optout(db, "merch_mix")
    await w.record([captured()], url="https://mix.example/p", final_url="https://mix.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout": 1}


# ── review of be#2735 delta 3 ───────────────────────────────────────────────────────────────────────────────────────
async def test_a_store_on_a_shared_host_known_only_from_canonical_urls_is_blocked(db):
    """P3-R1(b): a tenant of square.site, mybigcommerce.com or company.site is the store's own host, not a marketplace."""
    for i, (mid, host) in enumerate([("merch_sq", "brand1.square.site"), ("merch_bc", "brand2.mybigcommerce.com"),
                                     ("merch_co", "brand3.company.site")]):
        await _catalog(db, i, mid, url=f"https://{host}/p/1")
        await _optout(db, mid)
    for host in ("brand1.square.site", "brand2.mybigcommerce.com", "brand3.company.site", "other.square.site"):
        await w.record([captured()], url=f"https://{host}/p", final_url=f"https://{host}/p", market="US", page_currency="USD")
    assert [r["seller_id"] for r in await rows(db)] == ["other.square.site"]
    deny = await w.read_denyset()
    assert deny.counts == {"optout_ids_canonical_only": 3}


async def test_marketplace_listings_never_crowd_out_the_stores_own_host(db, monkeypatch):
    """P3-R1(c): the per-id read takes distinct HOSTS, so many marketplace URLs that sort first never hide the store's
    own host, nor its second host behind many URLs on its first."""
    monkeypatch.setattr(w, "CANONICAL_HOSTS_PER_ID", 5)
    for i in range(30):
        await _catalog(db, i, "merch_mix", url=f"https://www.amazon.com/dp/{i:03d}")
        await _catalog(db, 100 + i, "merch_mix", url=f"https://a-own.example/p/{i:03d}")
    await _catalog(db, 200, "merch_mix", url="https://shop.z-own.example/p/1")
    await _optout(db, "merch_mix")
    deny = await w.read_denyset()
    assert deny.blocks("a-own.example") and deny.blocks("shop.z-own.example") and not deny.blocks("amazon.com")


async def test_the_per_id_host_read_keeps_the_first_hosts_in_host_order(db, monkeypatch):
    monkeypatch.setattr(w, "CANONICAL_HOSTS_PER_ID", 2)
    for i, host in enumerate(["d.example", "c.example", "b.example", "a.example"]):  # inserted out of order
        await _catalog(db, i, "merch_many", url=f"https://{host}/p")
    await _optout(db, "merch_many")
    deny = await w.read_denyset()
    assert sorted(h for h in deny.block if not h.startswith("www.")) == ["a.example", "b.example"]
    assert deny.counts == {"optout_ids_canonical_only": 1}


async def test_an_over_broad_canonical_only_id_is_counted_too_broad(db):
    for i in range(4):
        await _catalog(db, i, "merch_wide", url=f"https://brand{i}.example/p")
    await _optout(db, "merch_wide")
    deny = await w.read_denyset()
    assert deny.counts == {"optout_ids_canonical_only": 1, "optout_ids_too_broad": 1}
    assert all(deny.blocks(f"brand{i}.example") for i in range(4)) and not deny.delete


async def test_a_normal_multi_domain_store_adds_no_note(db):
    for i, host in enumerate(["glow.example", "shop.glow.example", "glow.co.uk", "glow-store.myshopify.com"]):
        await _catalog(db, i, "merch_glow", host)
    await _optout(db, "merch_glow")
    deny = await w.read_denyset()
    assert deny.counts == {}
    assert deny.blocks("glow.co.uk") and deny.blocks("glow-store.myshopify.com")


@pytest.mark.parametrize("optout", ["github.io", "vercel.app", "herokuapp.com", "netlify.app", "com.vn", "com.ar",
                                    "company.site", "square.site", "co.uk"])
async def test_a_hosting_platform_or_registry_suffix_opt_out_never_blocks_the_stores_under_it(db, optout):
    await _optout(db, optout)
    host = f"brand.{optout}"
    await w.record([captured()], url=f"https://{host}/p", final_url=f"https://{host}/p", market="US", page_currency="USD")
    assert [r["seller_id"] for r in await rows(db)] == [host]
    deny = await w.read_denyset()
    assert deny.counts == {"optout_ids_shared_host": 1}  # reported: the opt-out probably meant one store (be#2742 P3-4)
    assert deny.blocks(optout)  # the exact host itself is still covered


@pytest.mark.parametrize("optout,page", [("bücher.example", "xn--bcher-kva.example"),
                                         ("xn--bcher-kva.example", "bücher.example"),
                                         ("https://www.BÜCHER.example/x", "shop.xn--bcher-kva.example"),
                                         ("ｗｗｗ.bücher.example", "xn--bcher-kva.example"),  # full-width www. (P3-5)
                                         ("bücher.example。", "xn--bcher-kva.example"),  # ideographic full stop
                                         ("straße.example", "xn--strae-oqa.example")])  # UTS-46 nontransitional
async def test_an_internationalised_host_matches_in_either_spelling(db, optout, page):
    await _optout(db, optout)
    await w.record([captured()], url=f"https://{page}/p", final_url=f"https://{page}/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout": 1}


async def test_registrable_domains_under_shared_hosts_and_suffixes():
    assert w.registrable("brand1.square.site") == "brand1.square.site"
    assert w.registrable("x.brand1.square.site") == "brand1.square.site"
    assert w.registrable("shop.brand.com.vn") == "brand.com.vn"
    assert w.registrable("square.site") == "square.site"
    assert not w.is_marketplace("brand1.square.site") and w.is_marketplace("square.site")


async def test_the_denylist_cache_expires_after_its_cap_within_a_run(db, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(w.time, "monotonic", lambda: clock[0])
    reads = []
    real = w.read_denyset

    async def counting():
        reads.append(1)
        return await real()

    monkeypatch.setattr(w, "read_denyset", counting)
    await w.record([captured()], url="https://a.example/p", final_url="https://a.example/p", market="US", page_currency="USD")
    clock[0] += w.DENYLIST_CACHE_SECONDS - 1
    await _optout(db, "b.example")
    await w.record([captured()], url="https://b.example/p", final_url="https://b.example/p", market="US", page_currency="USD")
    assert len(reads) == 1  # still cached: the new opt-out is not seen yet
    clock[0] += 2
    await w.record([captured(code="NEW22")], url="https://b.example/q", final_url="https://b.example/q", market="US", page_currency="USD")
    assert len(reads) == 2
    assert dcc.stats()["writes"] == {"inserted": 2, "skipped_optout": 1}


@pytest.mark.parametrize("value,expected", [
    ("é" * 70 + ".example", "é" * 70 + ".example"),  # a label over 63 characters: kept as it is
    ("\udcff.example", "\udcff.example"),  # a lone surrogate: UTS-46 refuses it, so does the stdlib codec
    ("a..é.example", "a..xn--9ca.example"),  # an empty label does not stop the others
])
async def test_a_host_that_cannot_be_encoded_never_raises_and_keeps_its_spelling(value, expected):
    assert w.normalize_host(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("ｗｗｗ。bücher。example", "xn--bcher-kva.example"),
    ("é" * 70 + "。example", "é" * 70 + ".example"),  # the ideographic stop is a dot even where a label cannot encode
    ("é" * 70 + "．example", "é" * 70 + ".example"),  # so are the full-width and half-width stops (delta 3, P3-3)
    ("é" * 70 + "｡example", "é" * 70 + ".example"),
    ("www.brand．example｡", "brand.example"),
    ("straße.example", "strasse.example"),  # IDNA 2003 without the package (it maps ß; the package keeps it)
])
async def test_without_the_idna_package_the_stdlib_codec_still_gives_one_spelling(monkeypatch, value, expected):
    """Review of be#2742 delta, Q22: the fallback branch maps the full-width and ideographic stops itself."""
    monkeypatch.setitem(sys.modules, "idna", None)  # "import idna" now raises ImportError
    assert w.normalize_host(value) == expected

