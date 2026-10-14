"""A seed never takes its price from a page read in a currency it may not store.

THE DEFECT. `create_external_seed` took the page read's `price_amount` / `price_currency` into the
row columns (INSERT for a new seed, UPDATE on re-create) with no check. A store can geo-serve
another market's page to the crawler, so a seed filed under market 'US' created from a CAD read
stored a CAD row price. The nightly refresh refused a currency CHANGE but filled an empty row
currency from any read.

THE RULE is `region_pricing.foreign_seed_currencies`, shared with variant adoption: a served or
acquisition market's currency only, and a seed keeps the storable currency it is already priced in
(row plus stored variants). It never reads the seed's `market`, a read partition that ADR-026
drops: SGD/AUD/JPY seeds are filed under 'US' and keep their prices. The refresh still never
redenominates a stored price; a seed stuck in an unstorable currency heals on re-create.

Every page here goes through the real JSON-LD extractor (`_extract_from_html`, via the harness in
tests/test_external_seed_refresh_writes_variants.py); only the network and the DB are stubbed.
Each refusal has a twin that still writes, so the guard cannot pass by refusing everything.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest

from tests.test_external_seed_refresh_writes_variants import DEST, IN, _offer, _page, _refresh, _seed_row


def _create(monkeypatch, page, existing_rows: List[Dict[str, Any]], **body: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Drive the real endpoint; return (response, the values of the one seed-row write)."""
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
    response = asyncio.run(
        ep.create_external_seed(
            ep.CreateExternalSeedRequest(**{"destination_url": DEST, "market": "US", **body}),
            request=SimpleNamespace(),
            current_user={"employee_id": "emp_test"},
        )
    )
    (write,) = [w for w in writes if "seed_data" in w]
    return response, write


def _read(currency: str):
    return _page([_offer(110, IN, sku="EX-30", priceCurrency=currency)])


def _row_price(write: Dict[str, Any]) -> Tuple[Any, Any]:
    return write["price_amount"], write["price_currency"]


def test_the_extractor_reads_the_pages_currency_onto_the_snapshot():
    # The premise: the guard reads what the real extractor emits.
    page = _read("CAD")
    assert (page.price_amount, page.price_currency) == (110.0, "CAD")


# ---- a new seed ----------------------------------------------------------------------------------


def test_creating_a_us_seed_from_a_cad_page_leaves_the_row_price_empty(monkeypatch):
    response, write = _create(monkeypatch, _read("CAD"), [])
    assert _row_price(write) == (None, None)
    assert response["seed"]["price"] == {"amount": None, "currency": None}
    refused = response["snapshot_price_refused"]
    assert refused["reason"] == "currency_not_storable_for_this_seed"
    assert refused["currencies"] == ["CAD"]
    assert refused["snapshot_price"] == {"amount": 110.0, "currency": "CAD"}


def test_creating_a_us_seed_from_a_usd_page_writes_its_price(monkeypatch):
    response, write = _create(monkeypatch, _read("USD"), [])
    assert _row_price(write) == (110.0, "USD")
    assert response["seed"]["price"] == {"amount": 110.0, "currency": "USD"}
    assert response["snapshot_price_refused"] is None


def test_the_cad_read_is_still_recorded_as_snapshot_evidence(monkeypatch):
    _, write = _create(monkeypatch, _read("CAD"), [])
    seed_data = write["seed_data"]
    seed_data = seed_data if isinstance(seed_data, dict) else json.loads(seed_data)
    assert (seed_data["snapshot"]["price_amount"], seed_data["snapshot"]["price_currency"]) == (110.0, "CAD")


# ---- re-creating an existing seed ----------------------------------------------------------------


def test_re_creating_a_usd_seed_keeps_its_row_price_when_the_page_reads_cad(monkeypatch):
    existing = _seed_row(variants=None, price=80.0, availability="in_stock")
    response, write = _create(monkeypatch, _read("CAD"), [existing])
    assert _row_price(write) == (80.0, "USD")
    assert response["snapshot_price_refused"]["snapshot_price"]["currency"] == "CAD"


def test_re_creating_a_usd_seed_takes_a_usd_read(monkeypatch):
    existing = _seed_row(variants=None, price=80.0, availability="in_stock")
    response, write = _create(monkeypatch, _read("USD"), [existing])
    assert _row_price(write) == (110.0, "USD")
    assert response["snapshot_price_refused"] is None


# ---- the employee's own price --------------------------------------------------------------------


def test_an_employee_stated_price_is_left_alone(monkeypatch):
    _, write = _create(monkeypatch, _read("CAD"), [], price_amount=95.0, price_currency="USD")
    assert _row_price(write) == (95.0, "USD")


def test_an_employee_stated_amount_is_not_paired_with_the_reads_currency(monkeypatch):
    # The amount is the employee's; the CAD beside it would be the read's.
    _, write = _create(monkeypatch, _read("CAD"), [], price_amount=95.0)
    assert _row_price(write) == (95.0, None)


def test_an_employee_stated_currency_is_not_paired_with_the_reads_amount(monkeypatch):
    existing = _seed_row(variants=None, price=80.0, availability="in_stock")
    _, write = _create(monkeypatch, _read("CAD"), [existing], price_currency="USD")
    assert _row_price(write) == (80.0, "USD")


# ---- the rule is the registry's, not the partition's (ADR-026 Q2: the seed partition is dropped) --
#
# Seeds for stores in other served/acquisition markets are filed under market 'US' in their own
# currency. A read in a served or acquisition currency is a price; an off-registry one (CAD) is not.
# A seed already priced in an allowed currency keeps it: a read in another allowed currency is
# another market's page, not this seed's price change.


def _row_in(currency: str, amount: float = 80.0) -> Dict[str, Any]:
    row = _seed_row(variants=None, price=amount, availability="in_stock")
    row["price_currency"] = currency
    return row


@pytest.mark.parametrize("currency", ["SGD", "AUD", "JPY"])
def test_a_us_filed_seed_is_created_in_a_served_or_acquisition_currency(monkeypatch, currency):
    response, write = _create(monkeypatch, _read(currency), [])
    assert _row_price(write) == (110.0, currency)
    assert response["snapshot_price_refused"] is None


@pytest.mark.parametrize("currency", ["SGD", "AUD", "JPY"])
def test_re_creating_a_seed_in_its_own_allowed_currency_takes_the_read(monkeypatch, currency):
    _, write = _create(monkeypatch, _read(currency), [_row_in(currency)])
    assert _row_price(write) == (110.0, currency)


@pytest.mark.parametrize("currency", ["SGD", "AUD", "JPY"])
def test_re_creating_a_usd_seed_refuses_another_allowed_currency(monkeypatch, currency):
    response, write = _create(monkeypatch, _read(currency), [_row_in("USD")])
    assert _row_price(write) == (80.0, "USD")
    assert response["snapshot_price_refused"]["snapshot_price"]["currency"] == currency


def test_re_creating_an_sgd_seed_refuses_a_usd_read(monkeypatch):
    _, write = _create(monkeypatch, _read("USD"), [_row_in("SGD")])
    assert _row_price(write) == (80.0, "SGD")


def test_re_creating_an_unpriced_seed_follows_its_stored_variants_currency(monkeypatch):
    row = _seed_row(variants=[{"variant_id": "EX-30", "title": "30 mL", "price_amount": 80.0,
                               "price_currency": "USD", "availability": "in_stock"}],
                    price=80.0, availability="in_stock")
    row["price_amount"], row["price_currency"] = None, None
    response, write = _create(monkeypatch, _read("SGD"), [row])
    assert _row_price(write) == (None, None)
    assert response["snapshot_price_refused"]["currencies"] == ["SGD"]


def test_re_creating_a_cad_polluted_seed_heals_from_a_usd_read(monkeypatch):
    # An established currency off the registry does not hold.
    response, write = _create(monkeypatch, _read("USD"), [_row_in("CAD")])
    assert _row_price(write) == (110.0, "USD")
    assert response["snapshot_price_refused"] is None


@pytest.mark.parametrize(("currency", "stored"), [("USD", (110.0, "USD")), ("CAD", (None, None))])
def test_the_rule_does_not_read_the_partition(monkeypatch, currency, stored):
    # A seed filed under a registry market other than the currency's (JP, priced in JPY) takes a USD
    # read and refuses a CAD one: the registry decides, and a seed's market is not where its currency
    # comes from. (Kills a rule that compares the read against the seed's market.)
    response, write = _create(monkeypatch, _read(currency), [], market="JP")
    assert _row_price(write) == stored
    assert (response["snapshot_price_refused"] is None) == (currency == "USD")


# ---- the nightly refresh: the row columns ---------------------------------------------------------


def _refresh_price(monkeypatch, row: Dict[str, Any], page):
    result, stored = _refresh(monkeypatch, row, page)
    return result["price_refresh"], (stored["price_amount"], stored["price_currency"])


def test_the_refresh_does_not_fill_an_empty_row_from_a_cad_read(monkeypatch):
    row = _row_in("USD")
    row["price_amount"], row["price_currency"] = None, None
    report, price = _refresh_price(monkeypatch, row, _read("CAD"))
    assert price == (None, None)
    assert report["status"] == "skipped_currency_mismatch"


def test_the_refresh_fills_an_empty_row_from_a_usd_read(monkeypatch):
    row = _row_in("USD")
    row["price_amount"], row["price_currency"] = None, None
    report, price = _refresh_price(monkeypatch, row, _read("USD"))
    assert price == (110.0, "USD")
    assert report["status"] == "filled"


def test_an_empty_row_follows_its_stored_variants_currency(monkeypatch):
    row = _seed_row(variants=[{"variant_id": "EX-30", "title": "30 mL", "price_amount": 80.0,
                               "price_currency": "USD", "availability": "in_stock"}],
                    price=80.0, availability="in_stock")
    row["price_amount"], row["price_currency"] = None, None
    report, price = _refresh_price(monkeypatch, row, _read("SGD"))
    assert price == (None, None)
    assert report["status"] == "skipped_currency_mismatch"


def test_variants_stating_no_currency_do_not_let_a_cad_read_fill_the_row(monkeypatch):
    # The row half only: a stored variant with no currency establishes nothing, so the rule falls to
    # the registry and CAD is refused. (What the read's reconcile writes into that variant is the
    # variant-adoption guard's to decide.)
    row = _seed_row(variants=[{"variant_id": "EX-30", "title": "30 mL", "price_amount": 80.0,
                               "availability": "in_stock"}],
                    price=80.0, availability="in_stock")
    row["price_amount"], row["price_currency"] = None, None
    report, price = _refresh_price(monkeypatch, row, _page([_offer(110, IN, sku="EX-30", priceCurrency="CAD")]))
    assert price == (None, None)
    assert report["status"] == "skipped_currency_mismatch"


@pytest.mark.parametrize("currency", ["SGD", "AUD", "JPY"])
def test_the_refresh_keeps_a_us_filed_seed_priced_in_its_own_currency(monkeypatch, currency):
    report, price = _refresh_price(monkeypatch, _row_in(currency), _read(currency))
    assert price == (110.0, currency)
    assert report["status"] == "applied"


def test_the_refresh_still_refuses_redenominating_a_usd_seed(monkeypatch):
    report, price = _refresh_price(monkeypatch, _row_in("USD"), _read("SGD"))
    assert price == (80.0, "USD")
    assert report["status"] == "skipped_currency_mismatch"


def test_the_refresh_never_redenominates_even_a_cad_polluted_row(monkeypatch):
    # Healing is the re-create's job, not a scraper's: a misread currency is likelier than a
    # merchant changing one (tests/test_external_seed_refresh_price.py, the KRW page read as USD).
    report, price = _refresh_price(monkeypatch, _row_in("CAD"), _read("USD"))
    assert price == (80.0, "CAD")
    assert report["status"] == "skipped_currency_mismatch"


def test_an_off_registry_row_still_corrects_its_own_amount(monkeypatch):
    # The rule governs which currency a seed may TAKE. A read in the row's own currency takes none.
    report, price = _refresh_price(monkeypatch, _row_in("CAD", amount=80.0), _read("CAD"))
    assert price == (110.0, "CAD")
    assert report["status"] == "applied"
