"""The portal sync endpoints must not run the sync inside the HTTP request.

The inline form's duration is dominated by per-product writes against a database
~140ms of RTT away, so any store big enough to matter exceeded the edge's idle
timeout: the edge closed the connection with NO response, the portal showed
"API Error: undefined … ERR_CONNECTION_CLOSED", and the sync — which the server
finished after the client vanished — looked failed while succeeding. Both syncs
of the 2026-07-29 Wix pilot hit exactly this (20/20 rows landed behind a scary
error, verified in the DB each time). A merchant's first interaction with the
product was a fake failure.

The Shopify endpoint (routes/merchant_api_extensions.py) learned this lesson
long ago — "schedule an async import task (do not block request)" — and the
wix/woocommerce/bigcommerce endpoints never did. This brings them to parity,
with `merchant_stores.last_sync` as the completion signal (already written by
universal_product_sync on completion; deliberately no new job table).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STORE_ROW = {
    "store_id": "store_1", "merchant_id": "merch_p", "platform": "wix",
    "name": "Pilot Store", "status": "active", "last_sync": None, "product_count": 0,
}
USER = {"role": "merchant", "merchant_id": "merch_p"}


class _FakeDb:
    async def fetch_one(self, *_a, **_k):
        return dict(STORE_ROW)


@pytest.fixture()
def patched(monkeypatch):
    import routes.wix_sync as mod

    monkeypatch.setattr(mod, "database", _FakeDb())
    calls = {"sync": 0}

    class _Result:
        status = "success"; message = "ok"; products_synced = 20
        platform = "wix"; sync_time = "t"

    async def fake_sync_products(**kw):
        calls["sync"] += 1
        return _Result()

    import routes.product_sync as ps
    monkeypatch.setattr(ps, "sync_products", fake_sync_products)
    return mod, calls


@pytest.mark.asyncio
async def test_default_returns_started_immediately_and_syncs_in_background(patched):
    mod, calls = patched
    out = await mod._sync_connected_platform_products(
        platform="wix", store_id=None, current_user=dict(USER))
    # The response must exist BEFORE the sync runs — that is the entire fix.
    assert out["status"] == "started"
    assert out["started_at"]
    assert "product_count" not in out, (
        "a started response cannot carry a count — claiming one would be the "
        "fabricated-success mirror image of the old fabricated failure"
    )
    # Let the scheduled task run; the background sync must actually fire.
    for _ in range(10):
        if calls["sync"]:
            break
        await asyncio.sleep(0.01)
    assert calls["sync"] == 1, "background task never executed the sync"


@pytest.mark.asyncio
async def test_wait_true_preserves_the_inline_contract(patched):
    mod, calls = patched
    out = await mod._sync_connected_platform_products(
        platform="wix", store_id=None, current_user=dict(USER), wait=True)
    assert out["status"] == "success"
    assert out["product_count"] == 20
    assert calls["sync"] == 1


@pytest.mark.asyncio
async def test_background_failure_never_raises_out_of_the_task(patched, monkeypatch):
    mod, calls = patched
    import routes.product_sync as ps

    async def boom(**kw):
        calls["sync"] += 1
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(ps, "sync_products", boom)
    out = await mod._sync_connected_platform_products(
        platform="wix", store_id=None, current_user=dict(USER))
    assert out["status"] == "started"
    for _ in range(10):
        if calls["sync"]:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.01)  # let the exception path complete; must not propagate
    assert calls["sync"] == 1


@pytest.mark.asyncio
async def test_sync_status_reads_the_store_row(patched):
    mod, _ = patched
    out = await mod.merchant_sync_status(platform="wix", store_id="store_1",
                                         current_user=dict(USER))
    assert out["store_id"] == "store_1"
    assert out["product_count"] == 0
    assert out["last_sync"] is None  # never synced -> portal keeps polling


def test_all_three_endpoints_thread_the_wait_param():
    import ast

    src = (Path(__file__).resolve().parents[1] / "routes" / "wix_sync.py").read_text()
    tree = ast.parse(src)
    threaded = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_sync_connected_platform_products":
            assert any(k.arg == "wait" for k in node.keywords), (
                "an endpoint calls the helper without threading `wait` — that "
                "endpoint silently loses the inline escape hatch"
            )
            threaded += 1
    assert threaded == 3
