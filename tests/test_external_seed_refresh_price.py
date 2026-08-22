"""A refresh must CORRECT a stale price — without inventing a new one.

`_refresh_external_seed_by_id` previously wrote its perishable fields with
`COALESCE(price_amount, :price_amount)` — "write the fresh value only if the
stored one is NULL". Since a seed almost always has a price, the refresh
re-fetched the live page, wrote the new price into ``seed_data["snapshot"]``, and
then discarded it for the column every consumer reads.

Two things make this file's assertions look the way they do.

**The harness cannot see SQL.** It stubs ``_execute_seed_data_stmt`` and applies
the bound parameters, so a `COALESCE` living in the statement is invisible: with
an earlier version of these tests, a mutant reinstating *every* `COALESCE`
left all 8 green. The statement text is therefore captured and asserted on
directly — see ``test_the_statement_itself_stays_coalesce_free_for_perishable_columns``.

**The producer post-processes both fields.** An earlier version of the fix gated
on ``snap_price_currency is None`` / ``snap_availability is None``, having read
``_extract_from_html``. Neither is ever None by the time it arrives here:
``resolve_external_offer`` fabricates a currency (``"JPY" if market=="JP" else
"USD"``) and stores availability as ``... or "unknown"``. The tests below pin the
*reachable* inputs, not the shapes the extractor alone could produce.
"""

from __future__ import annotations

import asyncio
import math
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
    """Drive the real function, capturing both the bound values and the statement."""
    import routes.employee_products as mod

    captured: Dict[str, Any] = {}

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        captured["query"] = _query
        stored_row.update(values)

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", AsyncMock(return_value=snapshot))

    result = asyncio.run(mod._refresh_external_seed_by_id(stored_row["id"]))
    result["_sql"] = captured.get("query", "")
    return result


# --------------------------------------------------------------------------------------
# The statement itself. Without this the whole file is satisfiable by the original defect.
# --------------------------------------------------------------------------------------

def test_the_statement_itself_stays_coalesce_free_for_perishable_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug lived in SQL, and this harness cannot evaluate SQL.

    Verified: with every `COALESCE` reinstated in the UPDATE — i.e. the shipped defect
    fully restored — the value-level tests in this file all still passed. Only asserting
    the statement text can catch that, so this test is load-bearing for the rest.
    """
    result = _run_refresh(monkeypatch, _seed_row(), _snapshot())
    sql = result["_sql"]

    for column in ("price_amount", "price_currency", "availability"):
        assert f"COALESCE({column}" not in sql, (
            f"{column} is a perishable fact — a COALESCE here means the refresh can "
            f"never correct it, which is the defect this change removes"
        )
        assert f"{column} = :{column}" in sql

    # title/image_url stay in SQL on purpose: they are curated copy, so existing-wins is
    # correct, and evaluating it at write time avoids the lost-update window that
    # resolving in Python would open across the live HTTP fetch.
    assert "title = COALESCE(title, :title)" in sql
    assert "image_url = COALESCE(image_url, :image_url)" in sql


# --------------------------------------------------------------------------------------
# Price
# --------------------------------------------------------------------------------------

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


def test_a_scraped_currency_may_not_redenominate_a_stored_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocker an adversarial review found in the first version of this fix.

    `_detect_currency_from_text` has no `₩`/KRW case, and `resolve_external_offer`
    then defaults a missing currency by MARKET — so a Korean page priced ₩24,000
    arrives here as `24000.0 USD`. Writing that turns a ₩24,000 product into a
    $24,000 one and reports it as a successful correction.

    A disagreement between stored and fresh currency is far likelier to mean we
    failed to READ a currency than that the merchant changed one, so the whole pair
    is refused and the refusal is counted.
    """
    row = _seed_row(price_amount=24000.0, price_currency="KRW")
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=24000.0, price_currency="USD"))

    assert row["price_currency"] == "KRW", "a scraper must never silently redenominate"
    assert row["price_amount"] == pytest.approx(24000.0)

    price = result["price_refresh"]
    assert price["status"] == "skipped_currency_mismatch"
    assert price["changed"] is False


def test_refresh_refuses_an_amount_that_arrives_without_a_currency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth for the same invariant, one layer earlier.

    Unreachable through `resolve_external_offer` today (it always fabricates a
    currency), but `_refresh_external_seed_by_id` must not depend on that: the
    producer's defaulting is the bug, not the contract.
    """
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=2400.0, price_currency=None))

    assert row["price_amount"] == pytest.approx(28.0)
    assert row["price_currency"] == "USD"
    assert result["price_refresh"]["status"] == "skipped_incomplete_pair"


def test_refresh_keeps_the_stored_price_when_the_fetch_found_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial fetch must not destroy a price we already know."""
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=None, price_currency=None))

    assert row["price_amount"] == pytest.approx(28.0)
    assert row["price_currency"] == "USD"
    assert result["price_refresh"]["status"] == "unavailable"


def test_a_first_fill_is_not_counted_as_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """`filled` vs `applied` keeps the sweep's staleness rate honest.

    The one case the old COALESCE did handle — it must keep working, but it is not
    evidence the index was stale.
    """
    row = _seed_row(price_amount=None, price_currency=None)
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=22.4))

    assert row["price_amount"] == pytest.approx(22.4)
    assert row["price_currency"] == "USD"
    assert result["price_refresh"]["status"] == "filled"
    assert result["price_refresh"]["changed"] is False


def test_currency_case_normalization_is_written_but_not_counted_as_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lowercase currency is reachable (bulk import and the PATCH route both write raw).

    Normalizing the stored value is desirable; reporting it as a price change would
    inflate the one metric the sweep is judged on.
    """
    row = _seed_row(price_currency="usd")
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=28.0, price_currency="USD"))

    assert row["price_currency"] == "USD"
    assert result["price_refresh"]["status"] == "unchanged"
    assert result["price_refresh"]["changed"] is False


def test_a_nan_price_is_treated_as_no_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """NaN survives float() and then defeats every comparison: `abs(nan - prev) >= x`
    is False, so it would be written to a DOUBLE PRECISION column and reported
    "unchanged"."""
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(price_amount=float("nan")))

    assert not math.isnan(row["price_amount"])
    assert row["price_amount"] == pytest.approx(28.0)
    assert result["price_refresh"]["status"] == "unavailable"


# --------------------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------------------

def test_refresh_corrects_availability_to_out_of_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """11.1% of the live sample was listed in-stock while actually out of stock."""
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(availability="out_of_stock"))

    assert row["availability"] == "out_of_stock"
    availability = result["availability_refresh"]
    assert availability["status"] == "applied"
    assert availability["changed"] is True
    assert availability["previous"] == "in_stock"


def test_unknown_availability_must_not_erase_a_known_out_of_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second blocker the review found.

    `resolve_external_offer` stores `extracted.get("availability") or "unknown"`, so
    "saw nothing" arrives as the literal string `"unknown"` — it is essentially never
    None. And `services/beauty_external_ranking` maps anything outside
    {out_of_stock, outofstock, sold_out} to inventory 999, i.e. IN STOCK. So writing
    "unknown" over a known `out_of_stock` serves a sold-out product as purchasable —
    precisely the defect this change exists to remove.
    """
    row = _seed_row(availability="out_of_stock")
    result = _run_refresh(monkeypatch, row, _snapshot(availability="unknown"))

    assert row["availability"] == "out_of_stock"
    assert result["availability_refresh"]["status"] == "unavailable"
    assert result["availability_refresh"]["changed"] is False


def test_refresh_does_not_report_a_stock_flip_when_nothing_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _seed_row()
    result = _run_refresh(monkeypatch, row, _snapshot(availability="in_stock"))

    assert result["availability_refresh"]["status"] == "unchanged"
    assert result["availability_refresh"]["changed"] is False
