"""
Rank-2 poller hardening (#1489): 429 Retry-After backoff, inter-page /
inter-merchant throttle, and a per-tick merchant cap.

All backoff/throttle waits route through the module ``_sleep`` indirection, so
these tests stub it and never wall-clock. The 429 path is driven with a fake
httpx client (the real fetch functions mock away in the other poller suites).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import external_conversion_poller as ecp  # noqa: E402
from services import woocommerce_conversion_poller as woo  # noqa: E402

_NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
_SHOPIFY_CREDS = {"shop_domain": "s.myshopify.com", "access_token": "t"}


# --- _parse_retry_after (pure) ------------------------------------------------

def test_parse_retry_after_delta_seconds():
    assert ecp._parse_retry_after("3", default=2.0, cap=15.0) == 3.0


def test_parse_retry_after_clamped_to_cap():
    assert ecp._parse_retry_after("999", default=2.0, cap=15.0) == 15.0


def test_parse_retry_after_absent_uses_default():
    assert ecp._parse_retry_after(None, default=2.0, cap=15.0) == 2.0


def test_parse_retry_after_unparseable_uses_default():
    # An HTTP-date form (uncommon for rate limits) is not a delta-seconds int.
    assert ecp._parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", default=2.0, cap=15.0) == 2.0


def test_parse_retry_after_negative_floored_to_zero():
    assert ecp._parse_retry_after("-5", default=2.0, cap=15.0) == 0.0


# --- _get_with_backoff (429 handling) -----------------------------------------

class _FakeClient:
    """Async-context httpx client stub that dispenses queued responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, params=None, headers=None):
        self.calls += 1
        return self._responses.pop(0)


def _install_client(monkeypatch, responses, sleeps):
    monkeypatch.setattr("httpx.AsyncClient", lambda *a, **k: _FakeClient(responses))

    async def rec_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(ecp, "_sleep", rec_sleep)


@pytest.mark.asyncio
async def test_backoff_retries_after_429_then_succeeds(monkeypatch):
    sleeps = []
    _install_client(
        monkeypatch,
        [httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200, json={"orders": []})],
        sleeps,
    )
    resp = await ecp._get_with_backoff("https://x/orders.json", params={}, headers={})
    assert resp.status_code == 200
    assert sleeps == [5.0], "must back off once, honoring Retry-After, then retry"


@pytest.mark.asyncio
async def test_backoff_honors_cap_on_huge_retry_after(monkeypatch):
    sleeps = []
    _install_client(
        monkeypatch,
        [httpx.Response(429, headers={"Retry-After": "999"}), httpx.Response(200, json={"orders": []})],
        sleeps,
    )
    resp = await ecp._get_with_backoff("https://x/orders.json", params={}, headers={})
    assert resp.status_code == 200
    assert sleeps == [ecp._MAX_RETRY_AFTER_S], "a huge Retry-After must be clamped to the cap"


@pytest.mark.asyncio
async def test_backoff_exhausts_and_returns_429(monkeypatch):
    sleeps = []
    # initial GET + _MAX_429_RETRIES retries, all 429 → returns the final 429.
    _install_client(
        monkeypatch,
        [httpx.Response(429) for _ in range(ecp._MAX_429_RETRIES + 1)],
        sleeps,
    )
    resp = await ecp._get_with_backoff("https://x/orders.json", params={}, headers={})
    assert resp.status_code == 429
    assert len(sleeps) == ecp._MAX_429_RETRIES, "backs off once per retry, then gives up"


@pytest.mark.asyncio
async def test_fetch_orders_page_raises_on_exhausted_429(monkeypatch):
    # A persistent 429 (backoff exhausted) surfaces as a fetch failure → the poll
    # loop then HOLDS the watermark (safe fallback, per #1484/#1485).
    sleeps = []
    _install_client(monkeypatch, [httpx.Response(429) for _ in range(ecp._MAX_429_RETRIES + 1)], sleeps)
    with pytest.raises(RuntimeError, match="429"):
        await ecp._fetch_orders_page(
            shop_domain="s.myshopify.com", access_token="t", api_version="2024-01", params={},
        )


# --- inter-page throttle ------------------------------------------------------

@pytest.mark.asyncio
async def test_inter_page_throttle_sleeps_between_pages(monkeypatch):
    monkeypatch.setenv(ecp._INTER_PAGE_SLEEP_ENV, "0.05")
    sleeps = []

    async def rec_sleep(seconds):
        sleeps.append(seconds)

    async def read_wm(*_a, **_k):
        return datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def write_wm(*_a, **_k):
        return None

    pages = [([], "c1"), ([], None)]  # 2 pages → one gap between them

    async def seq_fetch(**_kw):
        return pages.pop(0)

    monkeypatch.setattr(ecp, "_sleep", rec_sleep)
    monkeypatch.setattr(ecp, "_read_poll_watermark", read_wm)
    monkeypatch.setattr(ecp, "_write_poll_watermark", write_wm)
    monkeypatch.setattr(ecp, "_fetch_orders_page", seq_fetch)

    summary = await ecp.poll_external_conversions_for_merchant(
        merchant_id="m1", credentials=_SHOPIFY_CREDS, now=_NOW,
    )
    assert summary["pages"] == 2
    assert sleeps == [0.05], "one inter-page sleep between two pages, at the configured value"


# --- per-tick merchant cap + inter-merchant throttle --------------------------

@pytest.mark.asyncio
async def test_batch_per_tick_cap_and_inter_merchant_throttle(monkeypatch):
    monkeypatch.setattr(ecp, "_poller_enabled", lambda: True)
    monkeypatch.setenv(ecp._MAX_MERCHANTS_PER_TICK_ENV, "2")
    monkeypatch.setenv(ecp._INTER_MERCHANT_SLEEP_ENV, "0.01")

    # candidate SQL orders least-recently-polled first; the stub returns them in
    # that order and the cap takes the first 2.
    async def fake_candidates(*_a, **_k):
        return [{"merchant_id": f"m{i}"} for i in range(5)]

    polled = []

    async def fake_poll(*, merchant_id, now):
        polled.append(merchant_id)
        return {"merchant_id": merchant_id, "closed": 0}

    sleeps = []

    async def rec_sleep(seconds):
        sleeps.append(seconds)

    async def no_woo(*_a, **_k):
        return []

    monkeypatch.setattr(ecp.database, "fetch_all", fake_candidates)
    monkeypatch.setattr(ecp, "poll_external_conversions_for_merchant", fake_poll)
    monkeypatch.setattr(ecp, "_sleep", rec_sleep)
    monkeypatch.setattr(woo, "poll_wc_conversions_batch_lane", no_woo)

    result = await ecp.poll_external_conversions_batch(now=_NOW)
    assert polled == ["m0", "m1"], "only the cap's worth of (most-stale-first) merchants are polled"
    assert result["merchants_polled"] == 2
    assert result["shopify_merchants_deferred"] == 3, "the remainder is deferred to the next tick"
    assert sleeps == [0.01, 0.01], "one inter-merchant sleep after each polled merchant"


@pytest.mark.asyncio
async def test_woo_lane_per_tick_cap(monkeypatch):
    monkeypatch.setenv(ecp._MAX_MERCHANTS_PER_TICK_ENV, "1")

    async def fake_candidates(*_a, **_k):
        return [{"merchant_id": f"w{i}"} for i in range(3)]

    polled = []

    async def fake_poll(*, merchant_id, now):
        polled.append(merchant_id)
        return {"merchant_id": merchant_id, "platform": "woocommerce", "closed": 0}

    async def rec_sleep(_seconds):
        return None

    monkeypatch.setattr(woo.database, "fetch_all", fake_candidates)
    monkeypatch.setattr(woo, "poll_wc_conversions_for_merchant", fake_poll)
    monkeypatch.setattr(woo, "_sleep", rec_sleep)

    results = await woo.poll_wc_conversions_batch_lane(now=_NOW)
    assert polled == ["w0"], "woo lane also caps per tick (most-stale-first)"
    assert len(results) == 1
