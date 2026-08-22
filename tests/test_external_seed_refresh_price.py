"""A refresh must be able to CORRECT a stale price, not just re-fetch one.

`_refresh_external_seed_by_id` previously wrote price/availability with
`COALESCE(price_amount, :price_amount)` — "write the fresh value only if the
stored one is NULL". Because a seed almost always has a price, the refresh could
re-fetch the live page, write the new price into ``seed_data["snapshot"]``, and
then discard it for the column every consumer reads. It was structurally
incapable of fixing drift.

Note on why these tests assert the way they do: the suite's usual harness stubs
``_execute_seed_data_stmt`` and simply applies the bound parameters, so it never
evaluates SQL. A test that only asserted "the new price reached the row" would
therefore have passed against the buggy version too. The resolution now happens
in Python, and the two tests that actually discriminate old from new are the
currency-pairing one (old code half-applied; new code refuses) and the
``price_refresh`` report (absent entirely before).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest


def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_price_1",
        "external_product_id": "ext_price_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://genabelle.com/products/melacare-jelly-touch-dual-pad",
        "canonical_url": "https://genabelle.com/products/melacare-jelly-touch-dual-pad",
        "domain": "genabelle.com",
        "title": "Melacare Jelly Touch Dual Pad",
        "image_url": "https://cdn.example.com/img-1.jpg",
        "price_amount": 28.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {"title": "Melacare Jelly Touch Dual Pad", "snapshot": {}},
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
    }
    row.update(overrides)
    return row


def _snapshot(**overrides: Any) -> SimpleNamespace:
    base = {
        "canonical_url": "https://genabelle.com/products/melacare-jelly-touch-dual-pad",
        "domain": "genabelle.com",
        "title": "Melacare Jelly Touch Dual Pad",
        "image_url": "https://cdn.example.com/img-1.jpg",
        "price_amount": 22.4,
        "price_currency": "USD",
        "availability": "in_stock",
        "fetched_at": None,
        "evidence": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _run_refresh(
    monkeypatch: pytest.MonkeyPatch,
    stored_row: Dict[str, Any],
    snapshot: Optional[SimpleNamespace],
) -> Dict[str, Any]:
    """Drive the real function, capturing what it would have written."""
    import routes.employee_products as mod

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        # Mirrors the suite's existing harness: apply the bound parameters. It
        # deliberately does NOT evaluate SQL, which is exactly why the fix had to
        # move out of SQL to be testable at all.
        stored_row.update(values)

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", AsyncMock(return_value=snapshot))

    return asyncio.run(mod._refresh_external_seed_by_id(stored_row["id"]))


def test_refresh_corrects_a_stale_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 20%-off case measured live on genabelle.com: index 28.00, live 22.40."""
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=22.4))

    assert result["status"] == "success"
    assert row["price_amount"] == pytest.approx(22.4)
    assert row["price_currency"] == "USD"

    price = result["price_refresh"]
    assert price["status"] == "applied"
    assert price["changed"] is True
    assert price["previous_amount"] == pytest.approx(28.0)
    assert price["amount"] == pytest.approx(22.4)


def test_refresh_reports_unchanged_when_the_price_still_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-fetch that confirms the price is not drift, and must not be counted as such."""
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=28.0))

    price = result["price_refresh"]
    assert price["status"] == "unchanged"
    assert price["changed"] is False
    assert row["price_amount"] == pytest.approx(28.0)


def test_refresh_refuses_an_amount_that_arrives_without_a_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Amount and currency move together, or not at all.

    `_extract_from_html` resolves them from independent sources, so a fetch can
    yield an amount with no currency. Applying the amount alone would pair a NEW
    number with the STALE currency and silently redenominate the offer. This is
    the assertion that fails against the pre-fix code, which passed the bare
    amount and a NULL currency straight through.
    """
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=2400.0, price_currency=None))

    assert row["price_amount"] == pytest.approx(28.0), "stale-but-coherent beats fresh-but-redenominated"
    assert row["price_currency"] == "USD"

    price = result["price_refresh"]
    assert price["status"] == "skipped_incomplete_pair"
    assert price["changed"] is False


def test_refresh_keeps_the_stored_price_when_the_fetch_found_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial fetch must not destroy a price we already know."""
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=None, price_currency=None))

    assert row["price_amount"] == pytest.approx(28.0)
    assert row["price_currency"] == "USD"
    assert result["price_refresh"]["status"] == "unavailable"


def test_refresh_corrects_availability_to_out_of_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11.1% of the live sample was listed in-stock while actually out of stock.

    The row assertion alone is vacuous under this harness — the fake applies the
    bound parameter whether or not SQL would have. Verified: against the pre-fix
    code that assertion passed. The `availability_refresh` report is what makes
    this test discriminate, and it is also what lets the sweep count stock flips
    separately from price drift.
    """
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(availability="out_of_stock"))

    assert row["availability"] == "out_of_stock"
    availability = result["availability_refresh"]
    assert availability["status"] == "applied"
    assert availability["changed"] is True
    assert availability["previous"] == "in_stock"


def test_refresh_does_not_report_a_stock_flip_when_nothing_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(availability="in_stock"))

    assert result["availability_refresh"]["status"] == "unchanged"
    assert result["availability_refresh"]["changed"] is False


def test_refresh_populates_a_price_that_was_never_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case the old COALESCE did handle — it must keep working."""
    row = _seed_row(price_amount=None, price_currency=None)
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=22.4))

    assert row["price_amount"] == pytest.approx(22.4)
    assert row["price_currency"] == "USD"
    assert result["price_refresh"]["status"] == "applied"


def test_refresh_does_not_overwrite_a_curated_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """Perishable facts refresh; curated copy does not. This boundary is the point."""
    row = _seed_row(title="Melacare Jelly Touch Dual Pad (60ea)")
    _run_refresh(monkeypatch, row, _snapshot(title="Melacare Jelly Touch Dual Pad"))

    assert row["title"] == "Melacare Jelly Touch Dual Pad (60ea)"
