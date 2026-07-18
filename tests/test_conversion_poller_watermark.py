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
