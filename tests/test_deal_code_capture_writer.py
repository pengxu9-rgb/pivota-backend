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

    tables = [deal_lane.observed_store_offers, deal_lane.observed_store_offer_verifications, deal_lane.deal_lane_optout]
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


def captured(code="SAVE15", kind="percent", value="15", symbol=None, spend=None, hints=None, snippet="Get 15% off with code SAVE15"):
    return dcc.CapturedCode(code=code, kind=kind, value=value, currency_symbol=symbol, min_subtotal=spend,
                            hints=list(hints or []), snippet=snippet, shape="alnum", form="with_code")


async def rows(db):
    from db.deal_lane import observed_store_offers as o

    return [dict(r) for r in await db.fetch_all(o.select())]


async def test_a_captured_code_becomes_one_unverified_merchant_published_row(db):
    await w.record([captured(spend="50", hints=["new_customer_only"])], url="https://www.Glow.example/products/toner",
                   market="US", page_currency="USD")
    (r,) = await rows(db)
    assert r["seller_id"] == "glow.example"
    assert r["market"] == "US"
    assert r["code"] == "SAVE15"
    assert (r["kind"], float(r["value"]), r["currency"]) == ("percent", 15.0, None)
    assert float(r["min_subtotal"]) == 50.0
    assert r["new_customer_only"] is True or r["new_customer_only"] == 1
    assert not r["eligibility_known"]
    assert r["source"] == "merchant_published"
    assert r["source_url"] == "https://www.Glow.example/products/toner"
    assert r["terms_text"] == "Get 15% off with code SAVE15"
    assert r["deal_id"].startswith("dl_")
    assert dcc.stats()["writes"] == {"inserted": 1}


async def test_market_only_with_the_pages_own_currency_as_evidence(db):
    await w.record([captured()], url="https://glow.example/p", market="SG", page_currency="USD")
    (r,) = await rows(db)
    assert r["market"] is None
    assert dcc.stats()["writes"] == {"no_market_evidence": 1, "inserted": 1}
    assert w.market_evidence("SG", "SGD") == "SG"
    assert w.market_evidence("GB", "GBP") is None  # not a served market
    assert w.market_evidence("US", None) is None


async def test_a_second_sighting_refreshes_the_observation_never_identity_or_verification(db):
    from db.deal_lane import observed_store_offer_verifications as v

    await w.record([captured()], url="https://glow.example/p", market="US", page_currency="USD")
    (first,) = await rows(db)
    await db.execute(v.insert().values(deal_id=first["deal_id"], variant_id="44012345678901", state="unverified"))
    await w.record([captured(code="save15", value="20", snippet="Now 20% off with code save15")],
                   url="https://glow.example/q", market="US", page_currency="USD")
    (second,) = await rows(db)
    assert second["deal_id"] == first["deal_id"]
    assert second["code"] == "SAVE15"  # identity column untouched (Shopify matches codes case-insensitively)
    assert float(second["value"]) == 20.0
    assert second["source_url"] == "https://glow.example/q"
    assert second["created_at"] == first["created_at"]
    assert len(await db.fetch_all(v.select())) == 1
    assert dcc.stats()["writes"] == {"inserted": 1, "updated": 1}


async def test_no_row_without_a_stated_kind_or_with_an_unreadable_value(db):
    await w.record([captured(kind=None, value=None), captured(code="BIG200", value="200"), captured(code="NAN1", value="abc")],
                   url="https://glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_no_kind": 1, "skipped_unreadable": 2}


async def test_an_amount_keeps_a_currency_only_when_the_symbol_agrees(db):
    await w.record([captured(code="TENOFF", kind="amount", value="10", symbol="$")], url="https://a.example/p",
                   market="US", page_currency="USD")
    await w.record([captured(code="FIVEOFF", kind="amount", value="5", symbol="£")], url="https://b.example/p",
                   market="US", page_currency="USD")
    got = {r["code"]: r for r in await rows(db)}
    assert got["TENOFF"]["currency"] == "USD"
    assert got["FIVEOFF"]["currency"] is None


async def test_free_shipping_is_written_without_a_value(db):
    await w.record([captured(code="SHIPFREE", kind="free_shipping", value=None)], url="https://a.example/p",
                   market="US", page_currency="USD")
    (r,) = await rows(db)
    assert (r["kind"], r["value"]) == ("free_shipping", None)


async def test_an_opted_out_seller_is_never_written(db):
    from db.deal_lane import deal_lane_optout

    await db.execute(deal_lane_optout.insert().values(seller_id="glow.example", requested_at=datetime.now(timezone.utc),
                                                       recorded_by="test"))
    await w.record([captured()], url="https://www.glow.example/p", market="US", page_currency="USD")
    assert await rows(db) == []
    assert dcc.stats()["writes"] == {"skipped_optout": 1}


async def test_a_database_failure_is_counted_and_never_raised(db, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(w.database, "fetch_one", boom)
    caplog.set_level(logging.WARNING)
    await w.record([captured()], url="https://glow.example/p", market="US", page_currency="USD")
    assert dcc.stats()["writes"] == {"errors": 1}
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
    await w.record([captured(code="FIRST1"), captured(code="SECOND2")], url="https://glow.example/p", market="US", page_currency="USD")
    assert [r["code"] for r in await rows(db)] == ["SECOND2"]
    assert dcc.stats()["writes"] == {"errors": 1, "inserted": 1}


async def test_nothing_to_write_touches_nothing(db, monkeypatch):
    async def must_not_run(*a, **k):
        raise AssertionError("no database call for an empty page")

    monkeypatch.setattr(w.database, "fetch_one", must_not_run)
    await w.record([], url="https://glow.example/p", market="US", page_currency="USD")
    assert dcc.stats()["writes"] == {}
