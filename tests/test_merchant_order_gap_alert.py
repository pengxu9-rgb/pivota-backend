"""The paid-orders-missing-a-merchant-order metric is now an actual signal.

It carried `page_if_greater_than_zero_for_live_merchants` since #1967 while
being served only by an admin pull that nothing scrapes, and the durable-queue
work leaned on it three times as the standing trace for a create that cannot be
retried.
"""

import pytest

import services.merchant_order_gap_alert as gap


class _RecordingLogger:
    """Capture on the module's own logger: `utils.logger` sets propagate=False,
    so caplog genuinely sees nothing and a caplog assertion would be vacuous."""

    def __init__(self):
        self.errors, self.infos, self.warnings = [], [], []

    def error(self, msg, *a, **k):
        self.errors.append(msg % a if a else msg)

    def info(self, msg, *a, **k):
        self.infos.append(msg % a if a else msg)

    def warning(self, msg, *a, **k):
        self.warnings.append(msg % a if a else msg)


def _wire(monkeypatch, *, counts, excluded=()):
    """`counts` maps merchant_id (None for the total) to a count result."""
    import routes.order_routes as order_routes

    seen = []

    async def fake_count(*, merchant_id):
        seen.append(merchant_id)
        value = counts.get(merchant_id, {"count": 0, "available": True})
        return value

    monkeypatch.setattr(
        order_routes, "_count_paid_orders_missing_merchant_order_best_effort", fake_count
    )
    monkeypatch.setattr(order_routes, "_test_psp_probe_merchants", lambda: set(excluded))
    rec = _RecordingLogger()
    monkeypatch.setattr(gap, "logger", rec)
    return rec, seen


def _ok(n):
    return {"count": n, "available": True}


@pytest.mark.asyncio
async def test_pages_when_a_live_merchant_has_an_unfulfilled_paid_order(monkeypatch):
    rec, _ = _wire(monkeypatch, counts={None: _ok(4), "merch_probe": _ok(3)},
                   excluded=["merch_probe"])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary == {"total": 4, "live": 1, "excluded": 3}
    assert len(rec.errors) == 1
    # The resolved exclusion set is in the page, so a stale list reads as the
    # cause rather than as a real incident.
    assert "merch_probe" in rec.errors[0]


@pytest.mark.asyncio
async def test_does_not_page_when_every_gap_is_test_traffic(monkeypatch):
    """The measured backlog was 33 orders across two merchants, both probes."""
    rec, _ = _wire(monkeypatch, counts={None: _ok(33), "merch_a": _ok(30), "merch_b": _ok(3)},
                   excluded=["merch_a", "merch_b"])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary == {"total": 33, "live": 0, "excluded": 33}
    assert rec.errors == []


@pytest.mark.asyncio
async def test_every_number_comes_from_the_same_counter(monkeypatch):
    """An earlier cut took the total from the count and the split from
    `_fetch_paid_orders_missing_merchant_order`, which carries neither the 300s
    age floor nor the platform_order_id conjunct — so it paged on healthy
    in-flight orders and on already-delivered non-Shopify ones."""
    _, seen = _wire(monkeypatch, counts={None: _ok(5), "merch_probe": _ok(2)},
                    excluded=["merch_probe"])

    await gap.run_merchant_order_gap_alert_tick()

    # Total, then one scoped count per excluded merchant. No listing.
    assert seen == [None, "merch_probe"]


@pytest.mark.asyncio
async def test_skips_the_tick_rather_than_guessing_the_split(monkeypatch):
    """If an excluded merchant's count is unavailable we cannot say how much of
    the total is test traffic. Under-subtracting would page falsely."""
    rec, _ = _wire(
        monkeypatch,
        counts={None: _ok(33), "merch_probe": {"count": None, "available": False,
                                               "error": "statement timeout"}},
        excluded=["merch_probe"],
    )

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary["live"] is None
    assert rec.errors == []
    assert len(rec.warnings) == 1


@pytest.mark.asyncio
async def test_quiet_when_there_is_no_gap(monkeypatch):
    rec, seen = _wire(monkeypatch, counts={None: _ok(0)}, excluded=["merch_probe"])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary == {"total": 0, "live": 0, "excluded": 0}
    assert rec.errors == []
    assert seen == [None], "no per-merchant work when there is nothing to split"


@pytest.mark.asyncio
async def test_does_not_page_when_the_total_is_unavailable(monkeypatch):
    rec, _ = _wire(monkeypatch,
                   counts={None: {"count": None, "available": False, "error": "boom"}})

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary["total"] is None
    assert rec.errors == []
    assert len(rec.warnings) == 1


@pytest.mark.asyncio
async def test_exclusion_matching_is_case_insensitive(monkeypatch):
    rec, seen = _wire(monkeypatch, counts={None: _ok(3), "merch_probe": _ok(3)},
                      excluded=["MERCH_PROBE"])

    summary = await gap.run_merchant_order_gap_alert_tick()

    assert summary["live"] == 0
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


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
async def test_reconcile_tick_runs_when_the_flag_is_set(monkeypatch, value):
    """Positive counterpart to the off-by-default test. Without this, a typo'd
    env-var name or an inverted gate ships a repair lane that never runs, with
    a green suite."""
    import jobs.agentic_commerce_reconciliation as job

    monkeypatch.setenv("MERCHANT_ORDER_CREATE_RECONCILE_ENABLED", value)
    calls = []

    async def fake_reconcile(**kwargs):
        calls.append(kwargs)
        return {"dry_run": False, "attempted": 0, "queued": 0, "failed": 0}

    monkeypatch.setattr(job, "reconcile_paid_orders_missing_merchant_order", fake_reconcile)

    result = await job.run_merchant_order_create_reconcile_tick()

    assert result["queued"] == 0
    assert len(calls) == 1
    # The scheduled path is the unfiltered one, and it must not be a dry run.
    assert calls[0]["merchant_id"] is None
    assert calls[0]["dry_run"] is False
