"""A refresh that did not read the product we serve must not write its price or stock.

`_refresh_external_seed_by_id` decided the price, currency and availability BEFORE it knew whether the
fetch read the served product, and the UPDATE wrote them unconditionally: only `last_crawled_at`, the
variant reconcile and the projection waited for `_read_the_served_product`. So a 200 that was not a
reading (another product's page, a challenge page, a cached copy, a page we cannot tell is the
product) overwrote the columns the serving lanes read first, while the freshness clock said "never
read". The six cases below are every way the read gate says no on the success path; the control is
a real read, which must still apply.

The fetch is stubbed the way `services.external_offers_service._fetch_html` reports one: it fills
`observed` with the status, the FINAL url and the challenge flag (or `from_cache` on a fallback),
and returns the snapshot. Everything after it is the real function.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

DEST = "https://shop.example/products/toner"
HANDLELESS = "https://shop.example/shop/toner.html"

STORED_PRICE = 10.0
STORED_CURRENCY = "USD"
STORED_AVAILABILITY = "in_stock"
PAGE_PRICE = 99.0
PAGE_AVAILABILITY = "out_of_stock"


def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_not_read_1",
        "external_product_id": "ext_not_read_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": DEST,
        "canonical_url": DEST,
        "domain": "shop.example",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": STORED_PRICE,
        "price_currency": STORED_CURRENCY,
        "availability": STORED_AVAILABILITY,
        # The stored snapshot carries the same claim as the columns; a non-read must leave it too.
        "seed_data": {
            "title": "Toner",
            "snapshot": {
                "price_amount": STORED_PRICE,
                "price_currency": STORED_CURRENCY,
                "availability": STORED_AVAILABILITY,
            },
        },
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
        "destination_checked_at": None,
        "destination_verdict": None,
        "destination_failure_streak": 0,
        "last_crawled_at": "2026-10-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _snapshot(**overrides: Any) -> SimpleNamespace:
    base = {
        "canonical_url": DEST,
        "domain": "shop.example",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": PAGE_PRICE,
        "price_currency": "USD",
        "availability": PAGE_AVAILABILITY,
        "last_checked_at": None,
        "evidence": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _refresh(
    monkeypatch: pytest.MonkeyPatch,
    row: Dict[str, Any],
    observed_fill: Dict[str, Any],
    snapshot: SimpleNamespace,
    **call_kwargs: Any,
) -> Dict[str, Any]:
    import routes.employee_products as mod

    writes: List[Dict[str, Any]] = []
    projections: List[str] = []

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == row["id"]:
            return copy.deepcopy(row)
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        writes.append(dict(values))

    async def fake_resolve(*, observed=None, **_kwargs):
        if observed is not None:
            observed.update(observed_fill)
        return snapshot

    async def fake_project(seed_id, **_kwargs):
        projections.append(seed_id)
        return {"projected": 1}

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", fake_resolve)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation",
                        AsyncMock(return_value={"status": "recorded"}))
    monkeypatch.setattr(mod, "_project_refreshed_seed_to_serving_surfaces", fake_project)

    result = asyncio.run(mod._refresh_external_seed_by_id(row["id"], **call_kwargs))
    assert len(writes) == 1, "one UPDATE per refresh"
    written = writes[0]
    return {
        "result": result,
        "written": written,
        "seed_data": mod._ensure_json_obj(written["seed_data"]) or {},
        "snapshot": (mod._ensure_json_obj(written["seed_data"]) or {}).get("snapshot") or {},
        "projected": bool(projections),
    }


# Every way the read gate says no on the success path. (row overrides, what the fetch observed,
# snapshot overrides)
NOT_READ_CASES = {
    # The served URL (`canonical_url`) is another product's path: the fetch of `destination_url` is
    # not the page we serve, so nothing is classified.
    "canonical_names_another_product": (
        {"canonical_url": "https://shop.example/products/toner-2024"},
        {"status_code": 200, "final_url": DEST, "bot_challenged": False},
        {},
    ),
    # A 301 onto ANOTHER handle, answering 200: that page's price is another product's.
    "redirect_onto_another_handle": (
        {},
        {"status_code": 200, "final_url": "https://shop.example/products/serum", "bot_challenged": False},
        {"canonical_url": "https://shop.example/products/serum"},
    ),
    # A challenge page that answers 200 (`cf-mitigated`).
    "bot_challenge_200": (
        {},
        {"status_code": 200, "final_url": DEST, "bot_challenged": True},
        {},
    ),
    # The response was stamped, then a failure after it handed back the CACHED snapshot.
    "cached_after_the_response": (
        {},
        {"status_code": 200, "final_url": DEST, "bot_challenged": False, "from_cache": True},
        {},
    ),
    # No product handle in the URL: `classify_destination` cannot tell the page is the product.
    "url_with_no_product_handle": (
        {"destination_url": HANDLELESS, "canonical_url": HANDLELESS},
        {"status_code": 200, "final_url": HANDLELESS, "bot_challenged": False},
        {"canonical_url": HANDLELESS},
    ),
    # Nothing left the process (timeout, TLS, robots): the cached snapshot, no status at all.
    "cache_fallback_no_status": (
        {},
        {},
        {},
    ),
}


@pytest.mark.parametrize("case", list(NOT_READ_CASES))
def test_a_refresh_that_did_not_read_the_served_product_keeps_the_stored_price_and_stock(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    row_over, observed_fill, snap_over = NOT_READ_CASES[case]
    out = _refresh(monkeypatch, _seed_row(**row_over), observed_fill, _snapshot(**snap_over))
    result, written, snapshot = out["result"], out["written"], out["snapshot"]

    assert result["status"] == "success"
    assert written["read_the_served_product"] is False
    # The columns the serving lanes read first.
    assert written["price_amount"] == STORED_PRICE
    assert written["price_currency"] == STORED_CURRENCY
    assert written["availability"] == STORED_AVAILABILITY
    # Their copies in the stored snapshot.
    assert snapshot.get("price_amount") == STORED_PRICE
    assert snapshot.get("price_currency") == STORED_CURRENCY
    assert snapshot.get("availability") == STORED_AVAILABILITY
    # Reported as its own outcome, not as a reading that happened to change nothing.
    assert result["price_refresh"]["status"] == "not_read"
    assert result["price_refresh"]["changed"] is False
    assert result["price_refresh"]["amount"] == STORED_PRICE
    assert result["availability_refresh"]["status"] == "not_read"
    assert result["availability_refresh"]["changed"] is False
    assert result["availability_refresh"]["availability"] == STORED_AVAILABILITY
    # And nothing downstream treats it as fresh.
    assert out["projected"] is False
    assert "extracted_at" not in snapshot


def test_the_control_a_read_of_the_served_product_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the file is satisfied by a refresh that never writes a price at all."""
    out = _refresh(
        monkeypatch,
        _seed_row(),
        {"status_code": 200, "final_url": DEST, "bot_challenged": False},
        _snapshot(),
    )
    result, written, snapshot = out["result"], out["written"], out["snapshot"]

    assert written["read_the_served_product"] is True
    assert written["price_amount"] == PAGE_PRICE
    assert written["price_currency"] == "USD"
    assert written["availability"] == PAGE_AVAILABILITY
    assert snapshot.get("price_amount") == PAGE_PRICE
    assert snapshot.get("availability") == PAGE_AVAILABILITY
    assert result["price_refresh"]["status"] == "applied"
    assert result["availability_refresh"]["status"] == "applied"
    assert out["projected"] is True
    assert "extracted_at" in snapshot


def test_the_batch_counts_not_read_rows_from_what_the_refresh_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The counters read the refresh's own output, so they are fed it, not a hand-built dict."""
    import json

    import jobs.external_referral_refresh as job
    import services.external_referral_readiness as err

    produced = {}
    for case in ("redirect_onto_another_handle", "url_with_no_product_handle"):
        row_over, observed_fill, snap_over = NOT_READ_CASES[case]
        produced[case] = _refresh(monkeypatch, _seed_row(**row_over), observed_fill, _snapshot(**snap_over))["result"]
    produced["control"] = _refresh(
        monkeypatch, _seed_row(), {"status_code": 200, "final_url": DEST, "bot_challenged": False}, _snapshot()
    )["result"]

    ids = list(produced)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids", lambda *a, **k: asyncio.sleep(0, result=ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda _ids: asyncio.sleep(0, result={i: "shop.example" for i in ids}))

    async def scripted_refresh(seed_id, **_kwargs):
        return produced[seed_id]

    summary = asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=scripted_refresh, limit=10))
    assert summary["price_not_read"] == 2
    assert summary["availability_not_read"] == 2
    assert summary["price_changed"] == 1
    assert summary["availability_changed"] == 1

    line = job.price_refresh_line(summary)
    assert line["price_not_read"] == 2 and line["availability_not_read"] == 2
    json.dumps(line)


# --------------------------------------------------------------------------------------------
# What else a non-read must not move (controller review of #2713): the served price is a stored
# variant's before it is the column's (`routes/agent_api._seed_primary_price`).
# --------------------------------------------------------------------------------------------

OTHER = "https://shop.example/products/serum"
REDIRECT_ONTO_ANOTHER_PRODUCT = {"status_code": 200, "final_url": OTHER, "bot_challenged": False}


def _other_products_page(**overrides: Any) -> SimpleNamespace:
    base = {
        "canonical_url": OTHER, "title": "Serum", "image_url": "https://cdn.example.com/s.jpg",
        "evidence": {"variants": [
            {"variant_id": "111", "title": "Serum 30 ml", "price_amount": 99.0, "price_currency": "USD",
             "availability": "out_of_stock"},
            {"variant_id": "112", "title": "Serum 50 ml", "price_amount": 129.0, "price_currency": "USD",
             "availability": "in_stock"},
        ]},
    }
    base.update(overrides)
    return _snapshot(**base)


def _served_price(out: Dict[str, Any]) -> Any:
    import routes.agent_api as agent_api

    written = out["written"]
    served = agent_api._seed_primary_price(
        {"price_amount": written["price_amount"], "price_currency": written["price_currency"]}, out["seed_data"]
    )
    return (served or {}).get("amount")


def test_a_non_read_adopts_no_variants_when_the_seed_had_none(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _refresh(monkeypatch, _seed_row(), REDIRECT_ONTO_ANOTHER_PRODUCT, _other_products_page())
    assert out["written"]["read_the_served_product"] is False
    assert not out["seed_data"].get("variants")
    assert _served_price(out) == STORED_PRICE


def test_a_non_read_keeps_a_weakly_titled_stored_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A "Default Title" variant is the shape a page read is allowed to overwrite; a non-read is not."""
    stored = [{"variant_id": "222", "title": "Default Title", "price_amount": STORED_PRICE,
               "price_currency": STORED_CURRENCY, "availability": STORED_AVAILABILITY}]
    row = _seed_row()
    row["seed_data"] = {**row["seed_data"], "variants": copy.deepcopy(stored)}
    out = _refresh(monkeypatch, row, REDIRECT_ONTO_ANOTHER_PRODUCT, _other_products_page())
    assert out["seed_data"]["variants"] == stored
    assert _served_price(out) == STORED_PRICE


def test_the_control_a_read_still_adopts_the_pages_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _snapshot(evidence={"variants": [
        {"variant_id": "111", "title": "Toner 150 ml", "price_amount": PAGE_PRICE, "price_currency": "USD",
         "availability": PAGE_AVAILABILITY}]})
    out = _refresh(monkeypatch, _seed_row(), {"status_code": 200, "final_url": DEST, "bot_challenged": False}, page)
    assert [v.get("variant_id") for v in out["seed_data"].get("variants") or []] == ["111"]


def test_a_non_read_does_not_fill_an_empty_seed_data_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _seed_row(availability=None)
    row["seed_data"] = {"title": "Toner", "snapshot": {}}
    out = _refresh(monkeypatch, row, REDIRECT_ONTO_ANOTHER_PRODUCT, _other_products_page(evidence={}))
    assert out["written"]["availability"] is None
    assert "availability" not in out["seed_data"]


def test_a_non_read_with_another_page_currency_keeps_the_stored_pair_as_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _refresh(monkeypatch, _seed_row(), REDIRECT_ONTO_ANOTHER_PRODUCT,
                   _other_products_page(price_currency="EUR", evidence={}))
    assert (out["written"]["price_amount"], out["written"]["price_currency"]) == (STORED_PRICE, STORED_CURRENCY)
    assert out["result"]["price_refresh"]["status"] == "not_read"


def test_a_non_read_with_an_unreadable_price_is_not_read_not_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _refresh(monkeypatch, _seed_row(), REDIRECT_ONTO_ANOTHER_PRODUCT,
                   _other_products_page(price_amount=None, evidence={"price_read": {"status": "currency_unread"}}))
    assert out["result"]["price_refresh"]["status"] == "not_read"
    assert "reason" not in out["result"]["price_refresh"]


# The catalogue price lane: a catalogue entry is a read only for the URL we serve.

def _catalogue_read(price: float, availability: str) -> Any:
    from services import external_seed_destination_liveness as liveness
    from services.external_seed_catalogue_refresh import SeedCatalogueRead

    return SeedCatalogueRead(
        snapshot=_snapshot(price_amount=price, availability=availability,
                           evidence={"read_source": "shopify_catalogue"}),
        observation=liveness.DestinationObservation(liveness.VERDICT_LIVE, None, None, "listed in products.json"),
        host="shop.example",
        handle="toner",
    )


def _catalogue_row(**overrides: Any) -> Dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    row = _seed_row(last_page_read_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                    last_crawled_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    row.update(overrides)
    return row


def test_a_catalogue_read_for_another_served_url_is_not_a_read(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _refresh(monkeypatch, _catalogue_row(canonical_url="https://shop.example/products/toner-2024"), {},
                   _snapshot(), catalogue_read=_catalogue_read(11.0, PAGE_AVAILABILITY), page_read_column=True)
    # The catalogue lane priced this row (no fallback to a page read, which would also be a non-read here).
    assert out["result"]["read_source"] != "page" and out["result"]["catalogue_fallback"] is None
    assert out["written"]["read_the_served_product"] is False
    assert (out["written"]["price_amount"], out["written"]["availability"]) == (STORED_PRICE, STORED_AVAILABILITY)
    assert out["result"]["price_refresh"]["status"] == "not_read"
    assert out["snapshot"].get("price_amount") == STORED_PRICE
    assert "read_source" not in out["snapshot"]


def test_the_control_a_catalogue_read_of_the_served_url_applies_price_and_stock(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _refresh(monkeypatch, _catalogue_row(), {}, _snapshot(),
                   catalogue_read=_catalogue_read(11.0, PAGE_AVAILABILITY), page_read_column=True)
    assert out["written"]["read_the_served_product"] is True
    assert out["written"]["price_amount"] == 11.0
    assert out["written"]["availability"] == PAGE_AVAILABILITY
    assert out["result"]["availability_refresh"]["status"] == "applied"
    assert out["snapshot"].get("read_source") == "shopify_catalogue"


def test_a_non_read_reports_no_variant_adoption_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a read adopts variants, so a non-read whose page lists another currency has nothing refused."""
    page = _other_products_page(evidence={"variants": [
        {"variant_id": "111", "title": "Serum 30 ml", "price_amount": 99.0, "price_currency": "EUR",
         "availability": "in_stock"}]})
    out = _refresh(monkeypatch, _seed_row(), REDIRECT_ONTO_ANOTHER_PRODUCT, page)
    assert out["result"].get("variant_adoption_refused") is None
