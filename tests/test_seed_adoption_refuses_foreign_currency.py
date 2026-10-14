"""A page read's variants are adopted into a seed only in a currency the seed may store.

THE DEFECT. The seed refresh and the employee create path adopt the page's JSON-LD variants into
`seed_data.variants` (`_should_overwrite_seed_variants` / `_should_replace_seed_variant_content`
-> `_adopt_seed_variants`). A store can geo-serve another market's page to the crawler, and the
read's variants carry that page's currency. Probed 2026-10-13: a US/USD seed that read a CAD-priced
page stored CAD variant prices. The offer dual-write refused the offer, but the seed kept them,
and the builders price offers from its variants.

The rule is `services.region_pricing.foreign_seed_currencies` (Peng, 2026-10-14). A read may state
only the currency of a served or acquisition market in config/market_registry.json. A seed already
priced in such a currency keeps it (sticky). There is no comparison against the seed's market
partition: SG seeds sit in 'US' priced in SGD (ADR-026 drops the partition).

Every page here goes through the real JSON-LD extractor (`_extract_from_html`, via the harness in
tests/test_external_seed_refresh_writes_variants.py); only the network and the DB are stubbed.
Each refusal has a same-currency twin that still adopts, so the guard cannot pass by refusing all.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from routes.employee_products import (
    _foreign_refresh_variant_currencies,
    _foreign_variant_currencies,
    _should_overwrite_seed_variants,
    _should_replace_seed_variant_content,
)
from services.region_pricing import SEED_CURRENCY_STATUSES, foreign_seed_currencies, seed_storable_currencies
from tests.test_external_seed_refresh_writes_variants import DEST, IN, TITLE, _offer, _page, _refresh, _seed_row


def _sized_offers(currency: str) -> List[Dict[str, Any]]:
    """Two sizes, each named and priced in `currency` -- a richer read than the one stored variant."""
    return [
        _offer(110, IN, sku="EX-30", name=f"{TITLE} - 30 mL", priceCurrency=currency),
        _offer(150, IN, sku="EX-50", name=f"{TITLE} - 50 mL", priceCurrency=currency),
    ]


def _stored_variant(currency: str = "USD", **extra: Any) -> Dict[str, Any]:
    return {"variant_id": "EX-BASE", "title": TITLE, "price_amount": 80.0, "price_currency": currency,
            "availability": "in_stock", **extra}


def _stated(variants: Optional[List[Dict[str, Any]]]) -> set:
    return {str(v.get("price_currency") or v.get("currency")) for v in variants or []}


def test_the_extractor_reads_the_pages_currency_onto_every_variant():
    # The premise: the guard reads what the real extractor emits.
    read = _page(_sized_offers("CAD")).evidence["variants"]
    assert [(v["variant_id"], v["price_currency"]) for v in read] == [("EX-30", "CAD"), ("EX-50", "CAD")]


def test_without_the_guard_the_refresh_stores_cad_variants_in_a_usd_seed(monkeypatch):
    # The premise, through the real refresh: the adoption rules alone take this read.
    import routes.employee_products as mod

    monkeypatch.setattr(mod, "_foreign_refresh_variant_currencies", lambda **_: [])
    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("CAD")))
    assert _stated(stored["seed_data"]["variants"]) == {"CAD"}


# ---- the nightly refresh ------------------------------------------------------------------------


def test_the_refresh_keeps_the_usd_variants_when_the_page_reads_cad(monkeypatch):
    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(_sized_offers("CAD")))
    variants = stored["seed_data"]["variants"]
    assert [(v["variant_id"], v["price_amount"], v["price_currency"]) for v in variants] == [("EX-BASE", 80.0, "USD")]
    assert result["variant_adoption_refused"] == {"reason": "currency_not_the_seeds", "currencies": ["CAD"]}


def test_the_refresh_still_adopts_a_richer_usd_read(monkeypatch):
    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(_sized_offers("USD")))
    assert [v["variant_id"] for v in stored["seed_data"]["variants"]] == ["EX-30", "EX-50"]
    assert _stated(stored["seed_data"]["variants"]) == {"USD"}
    assert result["variant_adoption_refused"] is None


def test_a_seed_with_no_variants_does_not_take_cad_ones(monkeypatch):
    # No stored variants: the market's currency alone decides.
    row = _seed_row(variants=None, price=80.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(_sized_offers("CAD")))
    assert "CAD" not in _stated(stored["seed_data"].get("variants"))
    assert "CAD" not in _stated(stored["seed_data"]["snapshot"].get("variants"))
    assert result["variant_adoption_refused"]["currencies"] == ["CAD"]


def test_a_seed_with_no_variants_takes_usd_ones(monkeypatch):
    row = _seed_row(variants=None, price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("USD")))
    assert [v["variant_id"] for v in stored["seed_data"]["variants"]] == ["EX-30", "EX-50"]


GERMAN, ENGLISH = "Ein frischer Duft für jeden Tag.", "A fresh fragrance for every day."


def _covering_page_with_descriptions(currency: str):
    page = _page([_offer(110, IN, sku="EX-30", name="30 mL", priceCurrency=currency),
                  _offer(150, IN, sku="EX-50", name="50 mL", priceCurrency=currency)])
    for v in page.evidence["variants"]:
        v["description"] = ENGLISH
    return page


def _german_stored() -> List[Dict[str, Any]]:
    return [{"variant_id": "EX-30", "title": "30 mL", "price_amount": 80.0, "price_currency": "USD",
             "availability": "in_stock", "description": GERMAN},
            {"variant_id": "EX-50", "title": "50 mL", "price_amount": 110.0, "price_currency": "USD",
             "availability": "in_stock", "description": GERMAN}]


def test_the_localized_copy_branch_is_the_one_that_would_replace_here():
    # The premise for the next two: the overwrite rule declines this read, the copy rule takes it.
    read = _covering_page_with_descriptions("CAD").evidence["variants"]
    assert not _should_overwrite_seed_variants(existing=_german_stored(), incoming=read, product_title=TITLE)
    assert _should_replace_seed_variant_content(existing=_german_stored(), incoming=read, market="US",
                                                previous_canonical_url=None, refreshed_canonical_url=None)


def test_the_localized_copy_branch_refuses_a_cad_read(monkeypatch):
    row = _seed_row(variants=_german_stored(), price=80.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _covering_page_with_descriptions("CAD"))
    variants = stored["seed_data"]["variants"]
    assert [(v["description"], v["price_currency"]) for v in variants] == [(GERMAN, "USD"), (GERMAN, "USD")]
    assert result["variant_adoption_refused"]["currencies"] == ["CAD"]


def test_the_localized_copy_branch_takes_a_usd_read(monkeypatch):
    row = _seed_row(variants=_german_stored(), price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _covering_page_with_descriptions("USD"))
    assert [v["description"] for v in stored["seed_data"]["variants"]] == [ENGLISH, ENGLISH]


def test_a_usd_read_heals_a_us_seed_an_earlier_read_left_in_cad(monkeypatch):
    # A stored currency that is not storable holds nothing: otherwise the seeds base main already
    # wrote in CAD could never take a storable price again. The row stayed USD (the refresh refuses
    # a row currency change), so the seed heals back to it.
    row = _seed_row(variants=[_stored_variant("CAD")], price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("USD")))
    assert _stated(stored["seed_data"]["variants"]) == {"USD"}


# ---- SG seeds sit in the 'US' partition priced in SGD: they must stay priced ---------------------


def _sgd_seed_row(**kw: Any) -> Dict[str, Any]:
    row = _seed_row(variants=[_stored_variant("SGD")], price=80.0, availability="in_stock", **kw)
    row["price_currency"] = "SGD"
    return row


def test_an_sgd_seed_in_the_us_partition_adopts_an_sgd_read(monkeypatch):
    assert _sgd_seed_row()["market"] == "US", "the premise: the partition says US"
    result, stored = _refresh(monkeypatch, _sgd_seed_row(), _page(_sized_offers("SGD")))
    assert [(v["variant_id"], v["price_currency"]) for v in stored["seed_data"]["variants"]] == [
        ("EX-30", "SGD"), ("EX-50", "SGD")]
    assert result["variant_adoption_refused"] is None


def test_an_sgd_seed_refuses_a_usd_read(monkeypatch):
    # Sticky: both are storable, but the seed is established in SGD.
    result, stored = _refresh(monkeypatch, _sgd_seed_row(), _page(_sized_offers("USD")))
    assert _stated(stored["seed_data"]["variants"]) == {"SGD"}
    assert result["variant_adoption_refused"]["currencies"] == ["USD"]


def test_a_usd_seed_refuses_an_sgd_read(monkeypatch):
    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    result, stored = _refresh(monkeypatch, row, _page(_sized_offers("SGD")))
    assert _stated(stored["seed_data"]["variants"]) == {"USD"}
    assert result["variant_adoption_refused"]["currencies"] == ["SGD"]


def test_the_row_currency_alone_establishes_the_seed(monkeypatch):
    # No stored variants: the row's price_currency is what the seed is priced in.
    row = _seed_row(variants=None, price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("SGD")))
    assert "SGD" not in _stated(stored["seed_data"].get("variants"))


# ---- the refresh never changes the row's currency; an employee's re-create may -------------------


def _cad_seed_row(variants=True) -> Dict[str, Any]:
    row = _seed_row(variants=[_stored_variant("CAD")] if variants else None, price=80.0, availability="in_stock")
    row["price_currency"] = "CAD"
    return row


def test_the_refresh_does_not_move_a_cad_seeds_variants_off_its_row_currency(monkeypatch):
    # The row guard keeps the row CAD (a misread currency is likelier than a merchant changing one);
    # adopting USD variants would leave row CAD, variants USD.
    result, stored = _refresh(monkeypatch, _cad_seed_row(), _page(_sized_offers("USD")))
    assert stored["price_currency"] == "CAD", "the premise: the row guard keeps the row currency"
    assert _stated(stored["seed_data"]["variants"]) == {"CAD"}
    assert result["variant_adoption_refused"]["currencies"] == ["USD"]


def test_a_re_create_heals_a_cad_seed_to_a_storable_read(monkeypatch):
    seed_data = _create(monkeypatch, _page(_sized_offers("USD")), [_cad_seed_row()])
    assert _stated(seed_data["variants"]) == {"USD"}


def test_an_unstorable_row_currency_takes_nothing_on_refresh(monkeypatch):
    # The review repro: a US seed created from a CAD page got a CAD row and no variants, and its
    # first refresh stored CAD variants 110/150, undoing the create gate. Its own currency is still
    # not storable: the refresh adopts nothing and the reconcile re-prices nothing.
    row = _cad_seed_row(variants=False)
    result, stored = _refresh(monkeypatch, row, _page(_sized_offers("CAD")))
    assert "CAD" not in _stated(stored["seed_data"].get("variants"))
    assert result["variant_adoption_refused"]["currencies"] == ["CAD"]
    _, stored = _refresh(monkeypatch, _cad_seed_row(), _page(_sized_offers("CAD")))
    assert [(v["variant_id"], v["price_amount"]) for v in stored["seed_data"]["variants"]] == [("EX-BASE", 80.0)]


def test_a_refresh_with_no_row_currency_falls_to_the_shared_rule(monkeypatch):
    for currency, adopted in (("CAD", False), ("SGD", True)):
        row = _seed_row(variants=None, price=80.0, availability="in_stock")
        row["price_currency"] = None
        _, stored = _refresh(monkeypatch, row, _page(_sized_offers(currency)))
        assert (currency in _stated(stored["seed_data"].get("variants"))) is adopted, currency


def test_a_refresh_with_no_row_currency_is_held_to_its_stored_variants(monkeypatch):
    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    row["price_currency"] = None
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("SGD")))
    assert _stated(stored["seed_data"]["variants"]) == {"USD"}


# ---- a read no richer than the stored list still heals a currency an earlier read left -------------


def _cad_sizes() -> List[Dict[str, Any]]:
    return [{"variant_id": "EX-30", "title": f"{TITLE} - 30 mL", "price_amount": 110.0, "price_currency": "CAD",
             "availability": "in_stock"},
            {"variant_id": "EX-50", "title": f"{TITLE} - 50 mL", "price_amount": 150.0, "price_currency": "CAD",
             "availability": "in_stock"}]


def test_a_same_sizes_usd_read_is_not_richer(monkeypatch):
    # The premise: without the heal, nothing replaces these (same ids and titles), and the reconcile
    # never re-prices across currencies.
    read = _page(_sized_offers("USD")).evidence["variants"]
    assert not _should_overwrite_seed_variants(existing=_cad_sizes(), incoming=read, product_title=TITLE)


def test_the_refresh_heals_same_sizes_left_in_cad(monkeypatch):
    row = _seed_row(variants=_cad_sizes(), price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("USD")))
    assert [(v["variant_id"], v["price_currency"]) for v in stored["seed_data"]["variants"]] == [
        ("EX-30", "USD"), ("EX-50", "USD")]


def test_a_re_create_heals_same_sizes_left_in_cad(monkeypatch):
    existing = _seed_row(variants=_cad_sizes(), price=80.0, availability="in_stock")
    seed_data = _create(monkeypatch, _page(_sized_offers("USD")), [existing])
    assert _stated(seed_data["variants"]) == {"USD"}


def test_the_heal_never_drops_a_size_the_read_does_not_name(monkeypatch):
    stored_variants = _cad_sizes() + [{"variant_id": "EX-100", "title": f"{TITLE} - 100 mL", "price_amount": 200.0,
                                       "price_currency": "CAD", "availability": "in_stock"}]
    row = _seed_row(variants=stored_variants, price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("USD")))
    assert [v["variant_id"] for v in stored["seed_data"]["variants"]] == ["EX-30", "EX-50", "EX-100"]


# ---- a read in two currencies is refused whole ------------------------------------------------------


def _mixed_offers() -> List[Dict[str, Any]]:
    return [_offer(110, IN, sku="EX-30", name=f"{TITLE} - 30 mL", priceCurrency="USD"),
            _offer(150, IN, sku="EX-50", name=f"{TITLE} - 50 mL", priceCurrency="SGD")]


def test_creating_from_a_mixed_currency_read_stores_no_variants(monkeypatch):
    seed_data, response = _create(monkeypatch, _page(_mixed_offers()), [], with_response=True)
    assert seed_data["variants"] == []
    assert response["variant_adoption_refused"] == {"reason": "currency_not_the_seeds", "currencies": ["SGD", "USD"]}


def test_a_refresh_with_nothing_held_refuses_a_mixed_currency_read(monkeypatch):
    row = _seed_row(variants=None, price=80.0, availability="in_stock")
    row["price_currency"] = None
    result, stored = _refresh(monkeypatch, row, _page(_mixed_offers()))
    assert not stored["seed_data"].get("variants")
    assert result["variant_adoption_refused"]["currencies"] == ["SGD", "USD"]


# ---- the reconcile is held to the same gate ----------------------------------------------------------


@pytest.mark.parametrize(("row_currency", "read_currency"), [(None, "CAD"), ("USD", "SGD")])
def test_the_reconcile_does_not_price_a_currencyless_variant_in_a_refused_currency(monkeypatch, row_currency, read_currency):
    # The review repro: the gate refused the read, then the reconcile wrote its 110 CAD into a stored
    # variant that stated no currency, and the builders serve variants first.
    row = _seed_row(variants=[{"variant_id": "EX-30", "title": f"{TITLE} - 30 mL", "price_amount": 80.0,
                               "availability": "in_stock"}], price=80.0, availability="in_stock")
    row["price_currency"] = row_currency
    if row_currency is None:
        row["price_amount"] = None
    result, stored = _refresh(monkeypatch, row, _page([_offer(110, IN, sku="EX-30", name=f"{TITLE} - 30 mL",
                                                              priceCurrency=read_currency)]))
    (v,) = stored["seed_data"]["variants"]
    assert (v["price_amount"], v.get("price_currency")) == (80.0, None)
    assert result["variant_adoption_refused"]["currencies"] == [read_currency]


def test_the_reconcile_still_prices_a_currencyless_variant_in_the_rows_currency(monkeypatch):
    row = _seed_row(variants=[{"variant_id": "EX-30", "title": f"{TITLE} - 30 mL", "price_amount": 80.0,
                               "availability": "in_stock"}], price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page([_offer(110, IN, sku="EX-30", name=f"{TITLE} - 30 mL")]))
    (v,) = stored["seed_data"]["variants"]
    assert (v["price_amount"], v.get("price_currency")) == (110.0, "USD")


# ---- a refused read does not leave the seed reading fresh ----------------------------------------


def test_a_refused_read_removes_the_fresh_stamp(monkeypatch):
    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    assert row["seed_data"]["snapshot"].get("extracted_at"), "the premise: an earlier refresh stamped it"
    result, stored = _refresh(monkeypatch, row, _page(_sized_offers("CAD")))
    assert result["price_refresh"]["status"] == "skipped_currency_mismatch"
    assert "extracted_at" not in stored["seed_data"]["snapshot"]


def test_an_adopted_read_keeps_a_fresh_stamp(monkeypatch):
    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("USD")))
    assert stored["seed_data"]["snapshot"].get("extracted_at")


# ---- the rule itself ----------------------------------------------------------------------------


def test_storable_currencies_are_the_registrys_served_and_acquisition_markets():
    from services import market_registry as reg

    assert SEED_CURRENCY_STATUSES == ("served", "acquisition")
    assert seed_storable_currencies() == {reg.currency_for(m) for m in reg.markets_with_status("served", "acquisition")}
    # The premises the end-to-end tests above rest on.
    assert {"USD", "SGD"} <= seed_storable_currencies() and "CAD" not in seed_storable_currencies()


@pytest.mark.parametrize(
    ("row", "stored", "stated", "foreign"),
    [
        (None, [], ["CAD"], ["CAD"]),  # not storable
        (None, [], ["usd"], []),  # case does not matter
        (None, [], ["SGD"], []),  # a new seed may take any storable currency
        (None, [], ["USD", "SGD"], ["SGD", "USD"]),  # two currencies: refused whole
        (None, [], ["USD", "CAD"], ["CAD", "USD"]),
        ("USD", [], ["SGD"], ["SGD"]),  # sticky to the row
        ("SGD", [], ["USD"], ["USD"]),
        ("USD", ["SGD"], ["USD"], []),  # the row wins over the stored variants
        ("USD", ["SGD"], ["SGD"], ["SGD"]),
        (None, ["USD"], ["SGD"], ["SGD"]),  # no row: the stored variants hold
        ("CAD", ["USD"], ["SGD"], ["SGD"]),  # an unstorable row: the stored variants hold
        ("CAD", [], ["USD"], []),  # an unstorable currency holds nothing
        (None, ["CAD"], ["USD"], []),
        (None, ["CAD", "USD"], ["SGD"], ["SGD"]),  # ...and does not hide the storable one beside it
        ("CAD", ["CAD"], ["CAD"], ["CAD"]),  # nor licenses more of itself
        (" ", [None, " "], ["SGD"], []),  # blank currencies are ignored
        ("USD", [], [None, ""], []),  # an unstated currency does not count
    ],
)
def test_the_rule(row, stored, stated, foreign):
    assert foreign_seed_currencies(stated, row=row, stored=stored) == foreign


def test_the_variant_gate_establishes_the_seed_from_its_row_and_stored_variants():
    read = [{"variant_id": "r", "price_currency": "SGD"}]
    assert _foreign_variant_currencies(existing=[], incoming=read, row_currency=None) == []
    assert _foreign_variant_currencies(existing=[], incoming=read, row_currency="usd") == ["SGD"]
    assert _foreign_variant_currencies(existing=[{"price_currency": "USD"}], incoming=read, row_currency=None) == ["SGD"]
    assert _foreign_variant_currencies(existing=[{"currency": "USD"}], incoming=read, row_currency=None) == ["SGD"]
    assert _foreign_variant_currencies(existing=[], incoming=[{"variant_id": "a", "currency": "CAD"}],
                                       row_currency=None) == ["CAD"]


# ---- the employee create path, through the real endpoint ----------------------------------------


def _create(monkeypatch, page, existing_rows: List[Dict[str, Any]], *, variants=None, with_response=False):
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append(values)

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=page))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=existing_rows))
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.pivota.cc/r?token=t"))
    response = asyncio.run(ep.create_external_seed(
        ep.CreateExternalSeedRequest(destination_url=DEST, market="US", variants=variants),
        request=SimpleNamespace(), current_user={"employee_id": "emp_test"}))
    (write,) = [w for w in writes if "seed_data" in w]
    seed_data = write["seed_data"] if isinstance(write["seed_data"], dict) else json.loads(write["seed_data"])
    return (seed_data, response) if with_response else seed_data


def test_re_creating_a_usd_seed_keeps_its_variants_when_the_page_reads_cad(monkeypatch):
    existing = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    seed_data = _create(monkeypatch, _page(_sized_offers("CAD")), [existing])
    assert [(v["variant_id"], v["price_currency"]) for v in seed_data["variants"]] == [("EX-BASE", "USD")]


def test_re_creating_a_usd_seed_adopts_a_richer_usd_read(monkeypatch):
    existing = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    seed_data = _create(monkeypatch, _page(_sized_offers("USD")), [existing])
    assert [v["variant_id"] for v in seed_data["variants"]] == ["EX-30", "EX-50"]


def test_creating_a_us_seed_from_a_cad_page_stores_no_variants(monkeypatch):
    seed_data = _create(monkeypatch, _page(_sized_offers("CAD")), [])
    assert seed_data["variants"] == []


def test_creating_a_us_seed_from_a_usd_page_stores_its_variants(monkeypatch):
    seed_data = _create(monkeypatch, _page(_sized_offers("USD")), [])
    assert [(v["variant_id"], v["price_currency"]) for v in seed_data["variants"]] == [("EX-30", "USD"), ("EX-50", "USD")]


def test_creating_a_seed_from_an_sgd_page_stores_its_sgd_variants(monkeypatch):
    # SG supply is created in the 'US' partition too; a new seed has no currency to hold it to.
    seed_data = _create(monkeypatch, _page(_sized_offers("SGD")), [])
    assert _stated(seed_data["variants"]) == {"SGD"}


def test_re_creating_a_usd_seed_refuses_an_sgd_read(monkeypatch):
    existing = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    seed_data = _create(monkeypatch, _page(_sized_offers("SGD")), [existing])
    assert [(v["variant_id"], v["price_currency"]) for v in seed_data["variants"]] == [("EX-BASE", "USD")]


def test_re_creating_a_seed_with_no_variants_is_held_to_its_row_currency(monkeypatch):
    existing = _seed_row(variants=None, price=80.0, availability="in_stock")
    seed_data = _create(monkeypatch, _page(_sized_offers("SGD")), [existing])
    assert "SGD" not in _stated(seed_data.get("variants"))


def test_re_creating_reports_the_refusal(monkeypatch):
    existing = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    _, response = _create(monkeypatch, _page(_sized_offers("CAD")), [existing], with_response=True)
    assert response["variant_adoption_refused"] == {"reason": "currency_not_the_seeds", "currencies": ["CAD"]}
    _, response = _create(monkeypatch, _page(_sized_offers("USD")), [existing], with_response=True)
    assert response["variant_adoption_refused"] is None


# ---- variants an employee sends ----------------------------------------------------------------------


def _employee_variants(*currencies: str) -> List[Dict[str, Any]]:
    return [{"variant_id": f"EX-{i}", "title": f"{TITLE} - {i}", "price_amount": 10.0 + i, "price_currency": c}
            for i, c in enumerate(currencies)]


@pytest.mark.parametrize("currencies", [("CAD",), ("USD", "SGD")])
def test_employee_variants_in_an_unstorable_or_second_currency_are_refused(monkeypatch, currencies):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        _create(monkeypatch, _page(_sized_offers("USD")), [], variants=_employee_variants(*currencies))
    assert err.value.status_code == 422 and "VARIANT_CURRENCY_NOT_STORABLE" in err.value.detail


def test_an_employee_may_move_a_seed_to_another_storable_currency(monkeypatch):
    # The sticky rule binds a scraped read, not an employee: re-creating a USD seed with SGD variants.
    existing = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    seed_data = _create(monkeypatch, _page(_sized_offers("USD")), [existing], variants=_employee_variants("SGD"))
    assert _stated(seed_data["variants"]) == {"SGD"}


def test_the_refresh_row_rule_folds_case_on_both_sides():
    lower = [{"variant_id": "a", "price_currency": "usd"}]
    assert _foreign_refresh_variant_currencies(existing=[], incoming=lower, row_currency="USD") == []
    assert _foreign_refresh_variant_currencies(existing=[], incoming=lower, row_currency=" usd ") == []


class _WriteReached(Exception):
    pass


def _patch(monkeypatch, variants):
    import routes.employee_products as ep

    async def write(*_a, **_k):
        raise _WriteReached

    row = _seed_row(variants=[_stored_variant()], price=80.0, availability="in_stock")
    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value=row))
    monkeypatch.setattr(ep.database, "execute", write)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", write)
    asyncio.run(ep.update_external_seed(row["id"], ep.UpdateExternalSeedRequest(variants=variants),
                                        current_user={"employee_id": "emp_test"}))


@pytest.mark.parametrize("currencies", [("CAD",), ("USD", "SGD")])
def test_patching_variants_in_an_unstorable_or_second_currency_is_refused_before_any_write(monkeypatch, currencies):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        _patch(monkeypatch, _employee_variants(*currencies))
    assert err.value.status_code == 422


def test_patching_variants_in_a_storable_currency_reaches_the_write(monkeypatch):
    with pytest.raises(_WriteReached):
        _patch(monkeypatch, _employee_variants("SGD"))
