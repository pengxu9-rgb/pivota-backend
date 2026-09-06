"""A webhook retry worker must notice a stop request BETWEEN deliveries, not only per batch.

`_retry_worker_loop` checked `stop_event` only at the top of its `while`, so a shutdown
arriving mid-batch still had to sit through the rest of it: up to `limit` sequential
deliveries at DELIVERY_TIMEOUT_SECONDS each, ~200s at the defaults. That never showed,
because `database.disconnect()` ran first and the loop's next DB call raised — an accidental
bound, which #2091 removed when it reordered the lifespan so the scheduler could drain
against a live pool. #2091 bounded the STOP (`asyncio.wait_for(task, 1.0)` + cancel); this
makes the common case stop CLEANLY instead of being cancelled mid-flight.

The distinction matters on this path: a cancelled `retry_delivery` is a delivery whose HTTP
call may have reached the destination while its bookkeeping did not. Stopping between rows
never produces that state.

Both services carry an independent copy of this code, so every case runs against both.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

MODULES = ["services.agent_webhook_service", "services.merchant_webhook_service"]


def _rows(n):
    return [{"agent_id": f"a{i}", "merchant_id": f"m{i}", "delivery_id": f"d{i}"} for i in range(n)]


@pytest.fixture(params=MODULES)
def svc(request, monkeypatch):
    mod = importlib.import_module(request.param)
    monkeypatch.setattr(mod, "_db_now", lambda: 0, raising=False)
    ensure = [n for n in ("ensure_agent_webhook_tables", "ensure_merchant_webhook_tables")
              if hasattr(mod, n)]

    async def _noop(*a, **k):
        return None

    for n in ensure:
        monkeypatch.setattr(mod, n, _noop)
    return mod


def _arm(mod, monkeypatch, n, delivered):
    async def fetch_all(*a, **k):
        return _rows(n)

    async def retry_delivery(*a, **k):
        delivered.append(a)

    monkeypatch.setattr(mod.database, "fetch_all", fetch_all)
    monkeypatch.setattr(mod, "retry_delivery", retry_delivery)


@pytest.mark.asyncio
async def test_a_stop_request_ends_the_batch_immediately(svc, monkeypatch):
    """THE POINT. With 20 due rows and a stop raised before the first delivery, the batch must
    end at once rather than working through all twenty."""
    delivered = []
    _arm(svc, monkeypatch, 20, delivered)
    processed = await svc.process_due_retries(limit=20, should_stop=lambda: True)
    assert processed == 0 and delivered == [], (
        f"the worker delivered {len(delivered)} rows after being asked to stop; at "
        f"{svc.DELIVERY_TIMEOUT_SECONDS}s each that is up to "
        f"{20 * svc.DELIVERY_TIMEOUT_SECONDS:.0f}s of shutdown"
    )


@pytest.mark.asyncio
async def test_it_stops_partway_and_reports_what_it_did(svc, monkeypatch):
    """Stopping after N is the realistic case: the event arrives mid-batch. The count returned
    must be the deliveries that ACTUALLY happened — the rest stay due for the next instance."""
    delivered = []
    _arm(svc, monkeypatch, 20, delivered)
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 3          # stop before the 4th

    processed = await svc.process_due_retries(limit=20, should_stop=should_stop)
    assert processed == 3, f"expected 3 deliveries before the stop, got {processed}"
    assert len(delivered) == 3


@pytest.mark.asyncio
async def test_without_a_stop_callback_every_due_row_is_delivered(svc, monkeypatch):
    """The counterpart, and the reason the cases above are not just "it delivers nothing":
    the ordinary path must be unchanged, and other callers pass no callback at all."""
    delivered = []
    _arm(svc, monkeypatch, 5, delivered)
    assert await svc.process_due_retries(limit=20) == 5
    assert len(delivered) == 5


@pytest.mark.asyncio
async def test_the_worker_loop_actually_passes_its_stop_event(svc, monkeypatch):
    """A stop check nothing wires up is decoration. Drive the REAL loop: set the event while a
    delivery is in flight and assert the loop neither delivers the rest nor has to be
    cancelled to stop."""
    delivered = []
    started = asyncio.Event()

    async def fetch_all(*a, **k):
        return _rows(20)

    async def retry_delivery(*a, **k):
        delivered.append(a)
        started.set()
        await asyncio.sleep(0.05)

    monkeypatch.setattr(svc.database, "fetch_all", fetch_all)
    monkeypatch.setattr(svc, "retry_delivery", retry_delivery)
    monkeypatch.setattr(svc.database, "is_connected", True, raising=False)

    stop = asyncio.Event()
    task = asyncio.get_running_loop().create_task(svc._retry_worker_loop(stop))
    await asyncio.wait_for(started.wait(), timeout=5)
    stop.set()
    try:
        await asyncio.wait_for(task, timeout=5)
    except asyncio.TimeoutError:
        task.cancel()
        pytest.fail(
            "the loop did not exit within 5s of the stop event. It only notices between "
            "BATCHES, so a shutdown waits out the remaining deliveries."
        )
    assert not task.cancelled(), "the loop had to be cancelled rather than stopping cleanly"
    assert len(delivered) < 20, (
        f"it worked through all {len(delivered)} rows after the stop was set"
    )
