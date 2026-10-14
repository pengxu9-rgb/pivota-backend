"""A page read's variants are adopted into a seed only in the seed's own currency.

THE DEFECT. The seed refresh and the employee create path adopt the page's JSON-LD variants into
`seed_data.variants` (`_should_overwrite_seed_variants` / `_should_replace_seed_variant_content`
-> `_adopt_seed_variants`). A store can geo-serve another market's page to the crawler, and the
read's variants carry that page's currency. Probed 2026-10-13: a US/USD seed that read a CAD-priced
page stored CAD variant prices. The offer dual-write refused the offer, but the seed broke
"price currency = market currency", and the builders price offers from its variants.

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
    _foreign_variant_currencies,
    _should_overwrite_seed_variants,
    _should_replace_seed_variant_content,
)
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

    monkeypatch.setattr(mod, "_foreign_variant_currencies", lambda **_: [])
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
    # The market decides, not the stored list: otherwise the seeds base main already wrote in CAD
    # could never take their own market's price again.
    row = _seed_row(variants=[_stored_variant("CAD")], price=80.0, availability="in_stock")
    _, stored = _refresh(monkeypatch, row, _page(_sized_offers("USD")))
    assert _stated(stored["seed_data"]["variants"]) == {"USD"}


# ---- the rule itself ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("market", "stored", "incoming", "foreign"),
    [
        ("US", ["USD"], ["CAD"], ["CAD"]),
        ("US", ["USD"], ["USD", "CAD"], ["CAD"]),  # any foreign variant refuses the whole read
        ("US", [], ["usd"], []),  # case does not matter
        ("US", ["CAD"], ["USD"], []),  # the market decides
        ("US", ["CAD"], ["CAD"], ["CAD"]),  # a polluted stored list does not license more of it
        ("ZZ", ["EUR"], ["GBP"], ["GBP"]),  # an unmapped market falls back to the stored currency
        ("ZZ", ["EUR"], ["EUR"], []),
        ("ZZ", [], ["GBP"], []),  # nothing to hold it to
        ("US", ["USD"], [None], []),  # an unstated currency is filled from the market downstream
    ],
)
def test_the_rule(market, stored, incoming, foreign):
    existing = [{"variant_id": f"s{i}", "price_currency": c} for i, c in enumerate(stored)]
    read = [{"variant_id": f"r{i}", **({"price_currency": c} if c else {})} for i, c in enumerate(incoming)]
    assert _foreign_variant_currencies(existing=existing, incoming=read, market=market) == foreign


def test_the_rule_reads_the_currency_key_too():
    assert _foreign_variant_currencies(existing=[], incoming=[{"variant_id": "a", "currency": "CAD"}],
                                       market="US") == ["CAD"]


# ---- the employee create path, through the real endpoint ----------------------------------------


def _create(monkeypatch, page, existing_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
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
    asyncio.run(ep.create_external_seed(ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
                                        request=SimpleNamespace(), current_user={"employee_id": "emp_test"}))
    (write,) = [w for w in writes if "seed_data" in w]
    return write["seed_data"] if isinstance(write["seed_data"], dict) else json.loads(write["seed_data"])


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
