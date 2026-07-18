"""
Watermark-safety for the non-custodial conversion pollers (#1480).

Both pollers advance a per-merchant watermark so each tick only fetches new orders.
The bug: on a transient *page-fetch* failure (Shopify/Woo 429/timeout/5xx) they
used to advance the watermark to `run_started` anyway — marking the window
`[old_watermark, run_started]` as polled though it was never fully fetched, so the
conversions in it were dropped forever (idempotency dedups replays but cannot
recover an unscanned window). This is what made the pollers unsafe to enable.

The fix holds the watermark on a *fetch* failure (re-poll the window next tick) but
NOT on a per-*order* close failure (the window WAS scanned; idempotency retries the
bad order). These tests pin both halves for the Shopify and WooCommerce lanes.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
_PRIOR_WATERMARK = datetime(2026, 1, 1, tzinfo=timezone.utc)
_SHOPIFY_CREDS = {"shop_domain": "s.myshopify.com", "access_token": "t"}
_WOO_CREDS = {"store_url": "https://store.example.com", "consumer_key": "ck", "consumer_secret": "cs"}


def _patch_watermark(monkeypatch, module):
    """Stub the watermark read/write on `module`; return the captured writes list."""
    writes = []

    async def fake_read(*_a, **_k):
        return _PRIOR_WATERMARK

    async def fake_write(*args, **_k):
        writes.append(args)

    monkeypatch.setattr(module, "_read_poll_watermark", fake_read)
    monkeypatch.setattr(module, "_write_poll_watermark", fake_write)
    return writes


# --- Shopify lane -------------------------------------------------------------

@pytest.mark.asyncio
async def test_shopify_holds_watermark_on_fetch_failure(monkeypatch):
    from services import external_conversion_poller as ecp

    writes = _patch_watermark(monkeypatch, ecp)

    async def raising_fetch(**_kw):
        raise RuntimeError("shopify 429 Too Many Requests")

    monkeypatch.setattr(ecp, "_fetch_orders_page", raising_fetch)

    summary = await ecp.poll_external_conversions_for_merchant(
        merchant_id="m1", credentials=_SHOPIFY_CREDS, now=_NOW,
    )
    assert summary["errors"] == 1
    assert summary.get("watermark_held") is True
    assert writes == [], "a FETCH failure must NOT advance the watermark (window unscanned)"


@pytest.mark.asyncio
async def test_shopify_advances_watermark_on_per_order_failure(monkeypatch):
    from services import external_conversion_poller as ecp

    writes = _patch_watermark(monkeypatch, ecp)

    async def one_order_fetch(**_kw):
        return ([{"id": 1}], None)  # one order, no next page

    async def raising_process(**_kw):
        raise RuntimeError("close failed for this order")

    monkeypatch.setattr(ecp, "_fetch_orders_page", one_order_fetch)
    monkeypatch.setattr(ecp, "_process_order", raising_process)

    summary = await ecp.poll_external_conversions_for_merchant(
        merchant_id="m1", credentials=_SHOPIFY_CREDS, now=_NOW,
    )
    assert summary["errors"] == 1                 # the per-order close failure
    assert summary.get("watermark_held") is not True
    assert len(writes) == 1, "a per-ORDER failure must NOT hold the watermark (window was scanned)"


# --- WooCommerce lane ---------------------------------------------------------

@pytest.mark.asyncio
async def test_woo_holds_watermark_on_fetch_failure(monkeypatch):
    from services import woocommerce_conversion_poller as woo

    writes = _patch_watermark(monkeypatch, woo)

    async def raising_fetch(**_kw):
        raise RuntimeError("woo 503 Service Unavailable")

    monkeypatch.setattr(woo, "_fetch_wc_orders_page", raising_fetch)

    summary = await woo.poll_wc_conversions_for_merchant(
        merchant_id="m1", credentials=_WOO_CREDS, now=_NOW,
    )
    assert summary["errors"] == 1
    assert summary.get("watermark_held") is True
    assert writes == [], "a FETCH failure must NOT advance the woo watermark (window unscanned)"


@pytest.mark.asyncio
async def test_woo_advances_watermark_on_per_order_failure(monkeypatch):
    from services import woocommerce_conversion_poller as woo

    writes = _patch_watermark(monkeypatch, woo)

    async def one_order_fetch(**_kw):
        return [{"id": 1}]  # one order (< page limit → loop ends after processing)

    async def raising_process(**_kw):
        raise RuntimeError("close failed for this order")

    monkeypatch.setattr(woo, "_fetch_wc_orders_page", one_order_fetch)
    monkeypatch.setattr(woo, "_process_wc_order", raising_process)

    summary = await woo.poll_wc_conversions_for_merchant(
        merchant_id="m1", credentials=_WOO_CREDS, now=_NOW,
    )
    assert summary["errors"] == 1
    assert summary.get("watermark_held") is not True
    assert len(writes) == 1, "a per-ORDER failure must NOT hold the woo watermark"


# --- F1 (#1485): MAX_PAGES cap is an INCOMPLETE scan → hold, don't drop the tail --

@pytest.mark.asyncio
async def test_shopify_holds_watermark_on_page_cap(monkeypatch):
    from services import external_conversion_poller as ecp

    writes = _patch_watermark(monkeypatch, ecp)  # prior watermark = _PRIOR_WATERMARK (1 day stale)

    async def always_more(**_kw):
        # every page reports a pending cursor → the loop can only exit at MAX_PAGES,
        # never because the window ended (the >MAX_PAGES×LIMIT-orders window shape).
        return ([], "next_page_info_xyz")

    monkeypatch.setattr(ecp, "_fetch_orders_page", always_more)

    summary = await ecp.poll_external_conversions_for_merchant(
        merchant_id="m1", credentials=_SHOPIFY_CREDS, now=_NOW,
    )
    assert summary["pages"] == ecp.MAX_PAGES
    assert summary.get("watermark_held") is True
    assert summary.get("page_cap_hit") is True
    assert summary.get("watermark_stuck") is not True   # only 1 day stale → warn, not escalate
    assert writes == [], "a page-cap (incomplete-scan) exit must NOT advance the watermark"


@pytest.mark.asyncio
async def test_shopify_page_cap_first_run_escalates_stuck(monkeypatch):
    from services import external_conversion_poller as ecp

    writes = []

    async def no_watermark(*_a, **_k):
        return None  # first run: no prior watermark

    async def fake_write(*args, **_k):
        writes.append(args)

    monkeypatch.setattr(ecp, "_read_poll_watermark", no_watermark)
    monkeypatch.setattr(ecp, "_write_poll_watermark", fake_write)

    async def always_more(**_kw):
        return ([], "cursor")

    monkeypatch.setattr(ecp, "_fetch_orders_page", always_more)

    summary = await ecp.poll_external_conversions_for_merchant(
        merchant_id="m1", credentials=_SHOPIFY_CREDS, now=_NOW,
    )
    assert summary.get("watermark_held") is True
    assert summary.get("page_cap_hit") is True
    assert summary.get("watermark_stuck") is True, "a first-run window over the page cap is stuck"
    assert writes == []


@pytest.mark.asyncio
async def test_shopify_advances_on_complete_multipage_scan(monkeypatch):
    # A window that spans multiple pages but genuinely ends (final page has no
    # cursor) is a COMPLETE scan → advance (regression guard for the new branch).
    from services import external_conversion_poller as ecp

    writes = _patch_watermark(monkeypatch, ecp)
    pages = [([], "c1"), ([], None)]

    async def seq_fetch(**_kw):
        return pages.pop(0)

    monkeypatch.setattr(ecp, "_fetch_orders_page", seq_fetch)

    summary = await ecp.poll_external_conversions_for_merchant(
        merchant_id="m1", credentials=_SHOPIFY_CREDS, now=_NOW,
    )
    assert summary["pages"] == 2
    assert summary.get("watermark_held") is not True
    assert summary.get("page_cap_hit") is not True
    assert len(writes) == 1, "a fully-scanned window (final page, no cursor) must advance"


@pytest.mark.asyncio
async def test_woo_holds_watermark_on_page_cap(monkeypatch):
    from services import woocommerce_conversion_poller as woo

    writes = _patch_watermark(monkeypatch, woo)
    full_page = [{} for _ in range(woo.ORDERS_PAGE_LIMIT)]  # a FULL page ⇒ more pending

    async def always_full(**_kw):
        return list(full_page)

    async def noop_process(**_kw):
        return "no_click"

    monkeypatch.setattr(woo, "_fetch_wc_orders_page", always_full)
    monkeypatch.setattr(woo, "_process_wc_order", noop_process)

    summary = await woo.poll_wc_conversions_for_merchant(
        merchant_id="m1", credentials=_WOO_CREDS, now=_NOW,
    )
    assert summary["pages"] == woo.MAX_PAGES
    assert summary.get("watermark_held") is True
    assert summary.get("page_cap_hit") is True
    assert writes == [], "a full final page at MAX_PAGES is an incomplete scan → hold"


# --- F2 (#1485): batch surfaces held / stuck counts across both lanes ---------

@pytest.mark.asyncio
async def test_batch_aggregates_held_and_stuck(monkeypatch):
    from services import external_conversion_poller as ecp
    from services import woocommerce_conversion_poller as woo

    monkeypatch.setattr(ecp, "_poller_enabled", lambda: True)

    async def fake_candidates(*_a, **_k):
        return [{"merchant_id": "m1"}, {"merchant_id": "m2"}, {"merchant_id": "m3"}]

    monkeypatch.setattr(ecp.database, "fetch_all", fake_candidates)

    summaries = {
        "m1": {"merchant_id": "m1", "closed": 2, "errors": 0},
        "m2": {"merchant_id": "m2", "closed": 0, "errors": 1, "watermark_held": True},
        "m3": {"merchant_id": "m3", "closed": 0, "errors": 0,
                "watermark_held": True, "watermark_stuck": True},
    }

    async def fake_poll(*, merchant_id, now):
        return summaries[merchant_id]

    async def no_woo(*_a, **_k):
        return []

    monkeypatch.setattr(ecp, "poll_external_conversions_for_merchant", fake_poll)
    monkeypatch.setattr(woo, "poll_wc_conversions_batch_lane", no_woo)

    result = await ecp.poll_external_conversions_batch(now=_NOW)
    assert result["merchants_polled"] == 3
    assert result["total_closed"] == 2
    assert result["merchants_held"] == 2
    assert result["merchants_stuck"] == 1
    assert result["total_errors"] == 1
