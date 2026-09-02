"""The paid-orders-missing-a-merchant-order metric is now an actual signal.

It carried `page_if_greater_than_zero_for_live_merchants` since #1967 while
being served only by an admin pull that nothing scrapes, and the durable-queue
work leaned on it three times as the standing trace for a create that cannot be
retried. This tick runs it and emits a structured ERROR, which is what every
other money-path incident here uses.
"""

import pytest

import services.merchant_order_gap_alert as gap


class _RecordingLogger:
    """Capture on the module's own logger: `utils.logger` does not propagate to
    root, so caplog sees nothing."""

    def __init__(self):
        self.errors = []
        self.infos = []
        self.warnings = []

    def error(self, msg, *a, **k):
        self.errors.append(msg % a if a else msg)

    def info(self, msg, *a, **k):
        self.infos.append(msg % a if a else msg)

    def warning(self, msg, *a, **k):
        self.warnings.append(msg % a if a else msg)


def _wire(monkeypatch, *, count, rows):
    import routes.order_routes as order_routes

    async def fake_count(*, merchant_id):
        return {"count": count, "available": True}

    async def fake_fetch(*, merchant_id, limit):
        return rows

    monkeypatch.setattr(
        order_routes, "_count_paid_orders_missing_merchant_order_best_effort", fake_count
    )
    monkeypatch.setattr(
        order_routes, "_fetch_paid_orders_missing_merchant_order", fake_fetch
    )
    rec = _RecordingLogger()
    monkeypatch.setattr(gap, "logger", rec)
    return rec


def _order(order_id, merchant_id, *, linked=False):
    md = {"merchant_order": {"platform_order_id": "woo-1"}} if linked else {}
    return {"order_id": order_id, "merchant_id": merchant_id, "metadata": md,
            "shopify_order_id": None}


@pytest.mark.asyncio
async def test_pages_when_a_live_merchant_has_an_unfulfilled_paid_order(monkeypatch):
    monkeypatch.delenv("TEST_PSP_PROBE_MERCHANTS", raising=False)
    rec = _wire(monkeypatch, count=1, rows=[_order("ORD_LIVE", "merch_live")])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary["live"] == 1
    assert len(rec.errors) == 1
    assert "ORD_LIVE" in rec.errors[0] and "merch_live" in rec.errors[0]


@pytest.mark.asyncio
async def test_does_not_page_for_test_probe_merchants(monkeypatch):
    """The measured backlog was 33 orders across two merchants, both in
    TEST_PSP_PROBE_MERCHANTS. Paging on those trains whoever carries this to
    ignore it, which is worse than not alerting."""
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", "merch_probe_a,merch_probe_b")
    rec = _wire(monkeypatch, count=2, rows=[
        _order("ORD_A", "merch_probe_a"), _order("ORD_B", "merch_probe_b"),
    ])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary["total"] == 2
    assert summary["live"] == 0
    assert rec.errors == []


@pytest.mark.asyncio
async def test_a_delivered_non_shopify_order_does_not_page(monkeypatch):
    """A Woo/Wix/BigCommerce order records its id in metadata and leaves
    shopify_order_id empty — the merchant HAS it."""
    monkeypatch.delenv("TEST_PSP_PROBE_MERCHANTS", raising=False)
    rec = _wire(monkeypatch, count=1, rows=[_order("ORD_WOO", "merch_live", linked=True)])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary["live"] == 0
    assert rec.errors == []


@pytest.mark.asyncio
async def test_quiet_when_there_is_no_gap(monkeypatch):
    monkeypatch.delenv("TEST_PSP_PROBE_MERCHANTS", raising=False)
    rec = _wire(monkeypatch, count=0, rows=[])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary == {"total": 0, "live": 0, "excluded": 0}
    assert rec.errors == []


@pytest.mark.asyncio
async def test_reconcile_tick_is_off_by_default(monkeypatch):
    """It enqueues on the money path and its first prod run picks up the
    pre-queue backlog, so it must be armed deliberately."""
    import jobs.agentic_commerce_reconciliation as job

    monkeypatch.delenv("MERCHANT_ORDER_CREATE_RECONCILE_ENABLED", raising=False)

    async def fail(**kwargs):
        raise AssertionError("a disabled reconcile tick must not query")

    monkeypatch.setattr(job, "reconcile_paid_orders_missing_merchant_order", fail)

    assert await job.run_merchant_order_create_reconcile_tick() == {"status": "disabled"}
