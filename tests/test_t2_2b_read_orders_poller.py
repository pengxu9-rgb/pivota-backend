"""T2-2b — read_orders polling floor for external conversions.

Proves the poller: for a merchant we hold `read_orders` for but that has NO
`orders/paid` webhook, it fetches recent Shopify orders, recovers the T2-1
`pivota_click_id` from each order's note_attributes, and closes the conversion
through the SAME idempotent primitive the webhook uses
(`close_external_order_conversion`). The Shopify client is mocked — no live API.
Closure + idempotency run through the REAL function against a fake Postgres that
emulates the (merchant_id, external_order_id) ON CONFLICT guard (same FakeDB
shape as test_t2_2_external_conversion_closure).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import commerce_attribution_service as svc  # noqa: E402
from services import external_conversion_poller as poller  # noqa: E402


# --- fake DB: emulates the ON CONFLICT guard AND the watermark table ----------


class FakeDB:
    """Serves three query shapes used by a poll run:

    - the click lookup (SQLAlchemy select on surface_click_events) → click_row
    - the edge INSERT ... ON CONFLICT DO NOTHING (str) → dedup on (merchant,
      external_order); a replay returns None and does not overwrite.
    - external_conversion_poll_state read/write (str) → per-merchant watermark.
    - the candidate-merchants enumeration (fetch_all) → configured rows.
    """

    def __init__(
        self,
        *,
        click_row: Optional[Dict[str, Any]] = None,
        candidate_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.click_row = click_row
        self.candidate_rows = candidate_rows or []
        self.edges: Dict[tuple, Dict[str, Any]] = {}
        self.insert_attempts = 0
        self.watermarks: Dict[str, Any] = {}

    async def fetch_one(self, query: Any, values: Any = None) -> Optional[Dict[str, Any]]:
        if isinstance(query, str) and "INSERT INTO commerce_attribution_edges" in query:
            self.insert_attempts += 1
            params = dict(values or {})
            key = (params["merchant_id"], params["external_order_id"])
            if key in self.edges:
                return None  # ON CONFLICT DO NOTHING
            self.edges[key] = params
            return {"edge_id": params["edge_id"]}
        if isinstance(query, str) and "external_conversion_poll_state" in query:
            mid = dict(values or {}).get("merchant_id")
            stored = self.watermarks.get(mid)
            return {"last_polled_at": stored} if stored is not None else None
        # SQLAlchemy select() on surface_click_events (the click lookup)
        return self.click_row

    async def fetch_all(self, query: Any, values: Any = None) -> List[Dict[str, Any]]:
        if isinstance(query, str) and "surface_click_events" in query and "merchant_stores" in query:
            return list(self.candidate_rows)
        return []

    async def execute(self, query: Any, values: Any = None) -> int:
        if isinstance(query, str) and "external_conversion_poll_state" in query:
            params = dict(values or {})
            self.watermarks[params["merchant_id"]] = params["last_polled_at"]
        return 0


@pytest.fixture(autouse=True)
def _silence_commerce_events(monkeypatch: pytest.MonkeyPatch):
    async def _noop(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr(svc, "record_commerce_event_best_effort", _noop)


def _click_row(**over: Any) -> Dict[str, Any]:
    base = {
        "click_id": "clk_known",
        "merchant_id": "merch_test",
        "interaction_id": "int_click",
        "surface": "offers.resolve",
        "commerce_surface": "offers.resolve",
        "canonical_product_id": "merch_test|shopify|999",
        "canonical_variant_id": "46123456789",
        "prompt_cluster": "best-serum",
        "source_channel": "chatgpt",
        "source_family": "llm",
        "query_source": "mcp",
        "agent_id": "agent_x",
        "protocol_name": "mcp",
        "llm_provider": "openai",
        "llm_model": "gpt-x",
        "caller_id": "caller_x",
    }
    base.update(over)
    return base


def _order(**over: Any) -> Dict[str, Any]:
    base = {
        "id": 7001,
        "name": "#1001",
        "financial_status": "paid",
        "note_attributes": [
            {"name": "gift_note", "value": "hi"},
            {"name": "pivota_click_id", "value": "clk_known"},
        ],
        "total_price": "49.99",
        "currency": "USD",
        "created_at": "2026-07-04T10:00:00Z",
        "processed_at": "2026-07-04T10:00:05Z",
        "updated_at": "2026-07-04T10:00:05Z",
    }
    base.update(over)
    return base


def _install_fetch(monkeypatch: pytest.MonkeyPatch, pages: List[List[Dict[str, Any]]]):
    """Mock the Shopify order fetch. Records the params of every call; serves
    `pages` (list of (orders) lists) in order, last page repeating for re-polls."""
    calls: List[Dict[str, Any]] = []
    served = {"i": 0}

    async def fake_fetch(*, shop_domain, access_token, api_version, params):
        calls.append(dict(params))
        idx = min(served["i"], len(pages) - 1)
        served["i"] += 1
        orders = pages[idx] if pages else []
        return orders, None  # single page (no cursor) per call

    monkeypatch.setattr(poller, "_fetch_orders_page", fake_fetch)
    return calls


CREDS = {"shop_domain": "test-shop.myshopify.com", "access_token": "shpat_test"}


# --- (a) recent click + paid attributed order → converted edge -----------------


@pytest.mark.asyncio
async def test_paid_attributed_order_closes_conversion(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)
    _install_fetch(monkeypatch, [[_order()]])

    summary = await poller.poll_external_conversions_for_merchant(
        merchant_id="merch_test", credentials=CREDS
    )

    assert summary["closed"] == 1
    assert summary["scanned"] == 1
    assert len(fake.edges) == 1
    stored = next(iter(fake.edges.values()))
    assert stored["merchant_id"] == "merch_test"
    assert stored["external_order_id"] == "7001"
    assert stored["click_id"] == "clk_known"
    assert stored["state"] == "converted"
    assert stored["source"] == "external_redirect"
    assert stored["gross_attributed_gmv_cents"] == 4999
    assert stored["currency"] == "USD"
    assert stored["canonical_product_id"] == "merch_test|shopify|999"


# --- (a2) ADR-009 §D3 wiring: the polled store is forwarded to the closure ------


@pytest.mark.asyncio
async def test_poller_forwards_converting_shop_domain(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)
    _install_fetch(monkeypatch, [[_order()]])

    calls: List[Dict[str, Any]] = []

    async def close_spy(**kwargs: Any):
        calls.append(kwargs)
        return {"replayed": False}

    monkeypatch.setattr(poller, "close_external_order_conversion", close_spy)

    await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS)

    assert len(calls) == 1
    # the store we polled (CREDS shop_domain) is the converting store-of-record
    assert calls[0]["converting_shop_domain"] == CREDS["shop_domain"]


# --- (b) re-poll same order → idempotent, no duplicate edge --------------------


@pytest.mark.asyncio
async def test_repoll_same_order_is_idempotent(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)
    _install_fetch(monkeypatch, [[_order()]])

    first = await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS)
    second = await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS)

    assert first["closed"] == 1
    assert second["closed"] == 1  # attempted again...
    assert fake.insert_attempts == 2  # DB asked twice...
    assert len(fake.edges) == 1  # ...but the guard held → one edge, GMV not doubled
    stored = next(iter(fake.edges.values()))
    assert stored["gross_attributed_gmv_cents"] == 4999


# --- (c) order without pivota_click_id → skipped, no closure -------------------


@pytest.mark.asyncio
async def test_order_without_click_id_is_skipped(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)
    unattributed = _order(id=7002, note_attributes=[{"name": "gift_note", "value": "hi"}])
    _install_fetch(monkeypatch, [[unattributed]])

    summary = await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS)

    assert summary["closed"] == 0
    assert summary["skipped_no_click"] == 1
    assert fake.insert_attempts == 0
    assert len(fake.edges) == 0


# --- (d) unpaid order carrying the attribute → NOT closed ----------------------


@pytest.mark.asyncio
async def test_unpaid_attributed_order_is_not_closed(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)
    pending = _order(id=7003, financial_status="pending")
    _install_fetch(monkeypatch, [[pending]])

    summary = await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS)

    assert summary["closed"] == 0
    assert summary["skipped_unpaid"] == 1
    assert fake.insert_attempts == 0
    assert len(fake.edges) == 0


# --- (e) watermark advances so a second run doesn't re-fetch the same window ---


@pytest.mark.asyncio
async def test_watermark_advances_between_runs(monkeypatch):
    from datetime import datetime, timezone, timedelta

    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)
    calls = _install_fetch(monkeypatch, [[]])  # no orders; only care about the window param

    t1 = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
    first = await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS, now=t1)
    # first run has no watermark → window opens FIRST_RUN_LOOKBACK_DAYS back
    expected_first = (t1 - timedelta(days=poller.FIRST_RUN_LOOKBACK_DAYS)).isoformat()
    assert first["updated_at_min"] == expected_first
    # watermark persisted to the run start
    assert fake.watermarks["merch_test"] == t1

    t2 = t1 + timedelta(days=1)
    second = await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS, now=t2)
    # second run resumes from the watermark (t1), NOT t2 - lookback → no re-scan
    assert second["updated_at_min"] == t1.isoformat()
    assert fake.watermarks["merch_test"] == t2
    # the two fetches used different (advancing) windows
    assert calls[0]["updated_at_min"] == expected_first
    assert calls[1]["updated_at_min"] == t1.isoformat()


# --- (f) batch scoping: disabled by default; only enumerated merchants polled --


@pytest.mark.asyncio
async def test_batch_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EXTERNAL_CONVERSION_POLLER_ENABLED", raising=False)
    fake = FakeDB(candidate_rows=[{"merchant_id": "merch_test"}])
    monkeypatch.setattr(poller, "database", fake)

    result = await poller.poll_external_conversions_batch()
    assert result["skipped"] == "disabled"
    assert result["merchants_polled"] == 0


@pytest.mark.asyncio
async def test_batch_no_recent_click_merchants_polls_nothing(monkeypatch):
    monkeypatch.setenv("EXTERNAL_CONVERSION_POLLER_ENABLED", "1")
    fake = FakeDB(candidate_rows=[])  # no merchant has a recent click
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)

    called: List[str] = []

    async def _spy(*, merchant_id, now=None):
        called.append(merchant_id)
        return {"merchant_id": merchant_id, "ok": True, "closed": 0}

    monkeypatch.setattr(poller, "poll_external_conversions_for_merchant", _spy)

    result = await poller.poll_external_conversions_batch()
    assert result["ok"] is True
    assert result["merchants_polled"] == 0
    assert result["total_closed"] == 0
    assert called == []  # scoping kept us from polling anyone


@pytest.mark.asyncio
async def test_batch_polls_only_enumerated_merchants(monkeypatch):
    monkeypatch.setenv("EXTERNAL_CONVERSION_POLLER_ENABLED", "1")
    fake = FakeDB(candidate_rows=[{"merchant_id": "merch_test"}])
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)

    called: List[str] = []

    async def _spy(*, merchant_id, now=None):
        called.append(merchant_id)
        return {"merchant_id": merchant_id, "ok": True, "closed": 2}

    monkeypatch.setattr(poller, "poll_external_conversions_for_merchant", _spy)

    result = await poller.poll_external_conversions_batch()
    assert called == ["merch_test"]
    assert result["merchants_polled"] == 1
    assert result["total_closed"] == 2


# --- (g) isolation: a bad order never aborts the run; batch never raises -------


@pytest.mark.asyncio
async def test_one_bad_order_does_not_abort_run(monkeypatch):
    fake = FakeDB(click_row=_click_row())
    monkeypatch.setattr(poller, "database", fake)
    monkeypatch.setattr(svc, "database", fake)
    good = _order(id=7010)
    bad = _order(id=7011)
    _install_fetch(monkeypatch, [[bad, good]])

    orig_close = svc.close_external_order_conversion
    seen = {"n": 0}

    async def flaky_close(**kwargs):
        seen["n"] += 1
        if kwargs.get("external_order_id") == "7011":
            raise RuntimeError("boom")
        return await orig_close(**kwargs)

    monkeypatch.setattr(poller, "close_external_order_conversion", flaky_close)

    summary = await poller.poll_external_conversions_for_merchant(merchant_id="merch_test", credentials=CREDS)
    assert summary["errors"] == 1
    assert summary["closed"] == 1  # the good order still closed
    assert len(fake.edges) == 1


@pytest.mark.asyncio
async def test_batch_never_raises_on_candidate_query_failure(monkeypatch):
    monkeypatch.setenv("EXTERNAL_CONVERSION_POLLER_ENABLED", "1")

    class BoomDB:
        async def fetch_all(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(poller, "database", BoomDB())
    result = await poller.poll_external_conversions_batch()
    assert result["ok"] is False
    assert result["reason"] == "candidate_query_failed"
