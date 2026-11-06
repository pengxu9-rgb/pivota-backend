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

    result = asyncio.run(mod._refresh_external_seed_by_id(row["id"]))
    assert len(writes) == 1, "one UPDATE per refresh"
    written = writes[0]
    return {
        "result": result,
        "written": written,
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
