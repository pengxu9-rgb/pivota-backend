"""The attached lane carries the refresh's stock statement onto the listing's product-level row
(EXTERNAL_OFFER_ATTACHED_AVAILABILITY_WRITE_ENABLED, default off).

An enrichment-agent listing ingested with availability `unknown` kept it forever: the refresh re-read its price
every run and wrote the page's stock to the SEED only, while the gateway's own-offer money needs the OFFER row
`in_stock`. These tests pin what may move, and what never does: a variant row, a row whose price this run does
not write, an employee edit, a statement the page did not make, and anything at all with the flag off.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest

from services import external_offer_dual_write as mod

PK = "ext:sample-body-cream::0a1b2c3d"
DEST = "https://brand.example/products/body-cream"
SELLER = "agent_seed::brand-example"
SEED_ID = "seed:catalog_enrichment_agent_v1:0a1b2c3d4e5f6071"


def _seed(**over) -> Dict[str, Any]:
    seed = {
        "id": SEED_ID,
        "status": "active",
        "external_product_id": "brand:0a1b2c3d4e5f6071",
        "attached_product_key": PK,
        "destination_url": DEST,
        "price_amount": 29.2,
        "price_currency": "USD",
        "availability": "in_stock",
        "market": "US",
        "seed_variants": json.dumps([
            {"variant_id": "40000000000001", "price_amount": 29.2},
            {"variant_id": "40000000000002", "price_amount": 41.0},
        ]),
        "snapshot_variants": None,
        "variant_refresh_status": "all_re_read",
    }
    seed.update(over)
    return seed


def _offer(offer_id, *, sku=None, vid=None, merchant=SELLER, dest=DEST, price=29.0) -> Dict[str, Any]:
    return {
        "list_price": price, "merchant_effective_price": price,
        "offer_id": offer_id, "sku_key": sku or f"{PK}::canonical",
        "merchant_id": merchant, "currency": "USD",
        "source_ref": dest, "payload_destination_url": dest, "payload_seed_id": None,
        "suppressed": False, "source_variant_id": vid,
        "read_offer": "{}", "read_sku": "{}", "read_product": "{}",
    }


def _offers():
    return [
        _offer("of_canon"),
        _offer("of_v1", sku=f"{PK}::v:40000000000001", vid="40000000000001"),
        _offer("of_v2", sku=f"{PK}::v:40000000000002", vid="40000000000002", price=40.0),
        _offer("of_other", merchant="agent_seed::retailer::shop.example", dest="https://shop.example/p/cream"),
    ]


def _plan(stated, *, source="refresh", seed=None, offers=None):
    return mod.plan_attached_listing_offer_writes(
        seed or _seed(), offers or _offers(), source=source, currency_read=True, max_ratio=3.0,
        availability_stated=stated,
    )


def _by_id(plan):
    return {w["offer_id"]: w for w in plan["writes"]}


# ── the planner ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stated", ["in_stock", "out_of_stock", " IN_STOCK "])
def test_the_product_level_row_takes_the_stated_stock(stated):
    writes = _by_id(_plan(stated))
    assert writes["of_canon"]["availability"] == stated.strip().lower()
    assert writes["of_canon"]["price"] == 29.2, "the stock rides on the price write, it does not replace it"


def test_a_variant_row_never_takes_the_products_stock():
    writes = _by_id(_plan("out_of_stock"))
    assert {"of_canon", "of_v1", "of_v2"} <= set(writes)
    assert "availability" not in writes["of_v1"] and "availability" not in writes["of_v2"]


def test_another_sellers_listing_is_never_written():
    assert "of_other" not in _by_id(_plan("in_stock"))


@pytest.mark.parametrize("stated", [None, "", "unknown", "instock", "preorder", "true", 1])
def test_anything_but_a_stated_in_or_out_of_stock_moves_no_stock(stated):
    writes = _by_id(_plan(stated))
    assert writes["of_canon"]["price"] == 29.2
    assert all("availability" not in w for w in writes.values())


def test_an_employee_edit_never_writes_stock():
    """A person vouching for a PRICE says nothing about the page's stock."""
    writes = _by_id(_plan("in_stock", source="employee_edit"))
    assert writes and all("availability" not in w for w in writes.values())


def test_a_row_whose_price_this_run_does_not_write_takes_no_stock():
    # 100x the stored price: the ratio band refuses the write, and the stock goes with it.
    plan = _plan("in_stock", seed=_seed(price_amount=2920.0), offers=[_offer("of_canon")])
    assert plan["writes"] == [] and plan["skips"] == {"price_ratio_out_of_bounds": 1}


def test_without_a_statement_the_write_is_byte_for_byte_what_it_was():
    plan = _plan(None, offers=[_offer("of_canon")])
    assert plan["writes"] == [{"offer_id": "of_canon", "price": 29.2, "currency": "USD"}]


# ── the flag, through sync_offer_for_seed → the guarded UPDATE ──────────────────────────────────

class FakeDB:
    """sync_offer_for_seed's reads (no mirror: the attached lane), plus the guarded UPDATE's binds."""

    def __init__(self, refuse=False):
        self.updates: List[Dict[str, Any]] = []
        self.refuse = refuse

    async def fetch_one(self, sql, params=None):
        s = str(sql)
        if "FROM external_product_seeds" in s:
            return _seed()
        if "FROM catalog_products" in s:
            return None
        if s.strip().startswith("UPDATE catalog_offers"):
            assert ":availability" in s and "availability" in params, "the statement binds it on every write"
            self.updates.append(dict(params))
            if self.refuse:
                return None
            # The real statement's RETURNING: the row took the stock (an object payload, as every row here).
            return {"offer_id": params["offer_id"], "stock_written": params["availability"] is not None}
        if "information_schema" in s:
            return {"present": True}
        raise AssertionError(f"unexpected fetch_one: {s[:80]}")

    async def fetch_all(self, sql, params=None):
        assert "FROM catalog_offers" in str(sql) and params == {"product_key": PK}
        return [dict(o) for o in _offers()]

    async def execute(self, sql, params=None):
        raise AssertionError("the attached lane only prices existing rows")


def _sync(monkeypatch, *, flag, stated, source="refresh", refuse=False):
    fake = FakeDB(refuse=refuse)
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    if flag is None:
        monkeypatch.delenv(mod.ATTACHED_AVAILABILITY_WRITE_ENV, raising=False)
    else:
        monkeypatch.setenv(mod.ATTACHED_AVAILABILITY_WRITE_ENV, flag)
    monkeypatch.setattr(mod, "database", fake)
    monkeypatch.setattr(mod, "_price_check_column", {"present": True, "checked": None})
    result = asyncio.run(mod.sync_offer_for_seed(
        SEED_ID, attached_price_source=source, currency_read=True, availability_stated=stated))
    return result, {u["offer_id"]: u for u in fake.updates}


def _sync_result(monkeypatch, **kw):
    return _sync(monkeypatch, **kw)[0]


@pytest.mark.parametrize("flag", [None, "", "0", "false", "off", "no"])
def test_with_the_flag_off_no_row_is_told_a_stock(monkeypatch, flag):
    result, updates = _sync(monkeypatch, flag=flag, stated="in_stock")
    assert result["status"] == "synced" and updates
    assert all(u["availability"] is None for u in updates.values()), "NULL keeps every row's availability"


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", " yes ", "on"])
def test_with_the_flag_on_the_product_level_row_is_told_the_stated_stock(monkeypatch, flag):
    _, updates = _sync(monkeypatch, flag=flag, stated="in_stock")
    assert updates["of_canon"]["availability"] == "in_stock"
    assert updates["of_v1"]["availability"] is None and updates["of_v2"]["availability"] is None


def test_with_the_flag_on_an_employee_edit_still_writes_no_stock(monkeypatch):
    _, updates = _sync(monkeypatch, flag="1", stated="in_stock", source="employee_edit")
    assert updates and all(u["availability"] is None for u in updates.values())


def test_the_update_statement_keeps_the_rows_stock_when_told_none():
    for sql in (mod.ATTACHED_LISTING_OFFER_UPDATE_SQL, mod.ATTACHED_LISTING_OFFER_UPDATE_SQL_WITHOUT_PRICE_CHECK):
        assert "THEN CAST(:availability AS VARCHAR)\n        ELSE availability" in sql


# ── the per-run count (P2-2) ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stated", ["in_stock", "out_of_stock"])
def test_the_result_counts_the_rows_given_a_stock(monkeypatch, stated):
    result = _sync_result(monkeypatch, flag="1", stated=stated)
    assert result["availability_written"] == {stated: 1}, "the product-level row only"
    assert "availability_skipped" not in result


def test_a_stated_stock_that_no_row_took_is_counted_as_skipped(monkeypatch):
    # Every guarded UPDATE matches nothing (the rows changed since the read).
    result = _sync_result(monkeypatch, flag="1", stated="in_stock", refuse=True)
    assert "availability_written" not in result and result["availability_skipped"] == 1


@pytest.mark.parametrize("flag, stated, source", [
    (None, "in_stock", "refresh"), ("1", None, "refresh"), ("1", "unknown", "refresh"),
    ("1", "in_stock", "employee_edit"),
])
def test_no_stock_means_no_count_at_all(monkeypatch, flag, stated, source):
    result = _sync_result(monkeypatch, flag=flag, stated=stated, source=source)
    assert "availability_written" not in result and "availability_skipped" not in result


def test_the_statement_marks_every_row_it_gives_a_stock():
    for sql in (mod.ATTACHED_LISTING_OFFER_UPDATE_SQL, mod.ATTACHED_LISTING_OFFER_UPDATE_SQL_WITHOUT_PRICE_CHECK):
        assert "jsonb_build_object('availability_read'" in sql and "AS stock_written" in sql
