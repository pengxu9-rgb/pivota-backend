"""Every canonical funnel read is bounded by a declared time window.

THE DEFECT. `_fetch_event_rows` selected a merchant's whole
`commerce_interaction_events` history newest-first with `LIMIT limit+1` and
aggregated it in Python. There was no `since`/`until`, so the LIMIT alone
decided which purchases were counted — and it cuts on recency, not on
purchases. A `refund.succeeded` inside the newest N can belong to an
`order.paid` that fell outside it, at which point `paid_amount_cents_by_currency`
and `refunded_amount_cents_by_currency` no longer describe the same population.
The only signal was `truncated: true`, which says a bound was hit but not
WHICH slice of time was aggregated.

Fixtures are written the way the WRITER writes: `order_id` is not a column on
`commerce_interaction_events` — it lives in `payload`, which is what
`_analytics_row` merges over the row. On SQLite `occurred_at` is stored naive
(the sibling ledger tests strip tzinfo the same way).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import databases
import pytest
from sqlalchemy import create_engine

from db.commerce_interactions import commerce_interaction_events
from db.database import metadata


MERCHANT_ID = "merch_window"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


async def _sqlite_ledger(tmp_path, monkeypatch, name: str, rows: List[Dict[str, Any]]):
    db_path = tmp_path / f"{name}.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine, tables=[commerce_interaction_events], checkfirst=True
    )
    if rows:
        with sync_engine.begin() as connection:
            connection.execute(commerce_interaction_events.insert(), rows)
    sync_engine.dispose()
    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    import services.merchant_commerce_event_funnel_service as funnel_module

    monkeypatch.setattr(funnel_module, "database", test_database)
    return test_database


def _row(
    event_id: str,
    interaction_id: str,
    event_type: str,
    *,
    occurred_at: datetime,
    payload: Dict[str, Any],
    surface: str = "merchant_storefront",
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "interaction_id": interaction_id,
        "merchant_id": MERCHANT_ID,
        "platform": "shopify",
        "store_id": "store_1",
        "surface": surface,
        "event_type": event_type,
        # SQLite's DATETIME binding drops the offset, so the value stored has
        # to already be UTC wall clock.
        "occurred_at": occurred_at.astimezone(timezone.utc).replace(tzinfo=None),
        "payload": payload,
    }


# ---- 1. the window contract -------------------------------------------------


def test_resolve_window_defaults_to_the_configured_window(monkeypatch):
    from services.merchant_commerce_event_funnel_service import resolve_funnel_window

    monkeypatch.delenv("COMMERCE_FUNNEL_DEFAULT_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("COMMERCE_FUNNEL_MAX_WINDOW_DAYS", raising=False)
    window = resolve_funnel_window(now=NOW)

    assert window.until == NOW
    assert window.since == NOW - timedelta(days=90)
    assert window.days == 90
    assert window.clamped is False
    assert window.as_payload() == {
        "since": (NOW - timedelta(days=90)).isoformat(),
        "until": NOW.isoformat(),
        "days": 90,
        "clamped": False,
    }


def test_the_default_window_is_configurable_and_capped_by_the_maximum(monkeypatch):
    from services.merchant_commerce_event_funnel_service import resolve_funnel_window

    monkeypatch.setenv("COMMERCE_FUNNEL_DEFAULT_WINDOW_DAYS", "30")
    assert resolve_funnel_window(now=NOW).days == 30

    # A default misconfigured wider than the hard maximum must not become a
    # way to read all-time history without asking for it.
    monkeypatch.setenv("COMMERCE_FUNNEL_DEFAULT_WINDOW_DAYS", "5000")
    monkeypatch.setenv("COMMERCE_FUNNEL_MAX_WINDOW_DAYS", "400")
    assert resolve_funnel_window(now=NOW).days == 400


def test_a_window_wider_than_the_maximum_is_clamped_and_says_so(monkeypatch):
    from services.merchant_commerce_event_funnel_service import resolve_funnel_window

    monkeypatch.delenv("COMMERCE_FUNNEL_MAX_WINDOW_DAYS", raising=False)
    window = resolve_funnel_window(NOW - timedelta(days=900), NOW, now=NOW)

    assert window.clamped is True
    assert window.days == 400
    assert window.since == NOW - timedelta(days=400)

    # Exactly at the maximum is not clamped.
    at_max = resolve_funnel_window(NOW - timedelta(days=400), NOW, now=NOW)
    assert at_max.clamped is False
    assert at_max.days == 400


def test_the_maximum_window_is_configurable(monkeypatch):
    from services.merchant_commerce_event_funnel_service import resolve_funnel_window

    monkeypatch.setenv("COMMERCE_FUNNEL_MAX_WINDOW_DAYS", "10")
    window = resolve_funnel_window(NOW - timedelta(days=900), NOW, now=NOW)
    assert window.clamped is True
    assert window.days == 10


def test_a_naive_bound_is_read_as_utc_not_as_process_local():
    from services.merchant_commerce_event_funnel_service import resolve_funnel_window

    window = resolve_funnel_window(datetime(2026, 6, 1), datetime(2026, 6, 11), now=NOW)
    assert window.since == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert window.until == datetime(2026, 6, 11, tzinfo=timezone.utc)


def test_since_after_until_is_refused():
    from services.merchant_commerce_event_funnel_service import resolve_funnel_window

    with pytest.raises(ValueError):
        resolve_funnel_window(NOW, NOW - timedelta(days=1), now=NOW)


# ---- 2. the window is enforced in SQL ---------------------------------------


def _three_ages() -> List[Dict[str, Any]]:
    return [
        _row(
            "evt_recent",
            "int_recent",
            "order.paid",
            occurred_at=NOW - timedelta(days=1),
            payload={"order_id": "ORDER_RECENT", "amount_cents": 100, "currency": "USD"},
        ),
        _row(
            "evt_mid",
            "int_mid",
            "order.paid",
            occurred_at=NOW - timedelta(days=100),
            payload={"order_id": "ORDER_MID", "amount_cents": 200, "currency": "USD"},
        ),
        _row(
            "evt_old",
            "int_old",
            "order.paid",
            occurred_at=NOW - timedelta(days=500),
            payload={"order_id": "ORDER_OLD", "amount_cents": 400, "currency": "USD"},
        ),
    ]


async def _funnel(monkeypatch, tmp_path, name, **kwargs):
    import services.merchant_commerce_event_funnel_service as module

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, name, _three_ages())
    try:
        return await module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID, group_by="store", **kwargs
        )
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_default_window_reads_only_the_last_ninety_days(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMERCE_FUNNEL_DEFAULT_WINDOW_DAYS", raising=False)
    result = await _funnel(monkeypatch, tmp_path, "window-default")

    assert result.payload["summary"]["events_total"] == 1
    assert result.payload["summary"]["paid_amount_cents_by_currency"] == {"USD": 100}
    assert result.payload["window"]["days"] == 90
    assert result.payload["window"]["clamped"] is False


@pytest.mark.asyncio
async def test_a_wider_since_reads_further_back(tmp_path, monkeypatch):
    result = await _funnel(
        monkeypatch, tmp_path, "window-since", since=NOW - timedelta(days=200)
    )

    assert result.payload["summary"]["events_total"] == 2
    assert result.payload["summary"]["paid_amount_cents_by_currency"] == {"USD": 300}


@pytest.mark.asyncio
async def test_until_excludes_everything_newer_than_it(tmp_path, monkeypatch):
    result = await _funnel(
        monkeypatch,
        tmp_path,
        "window-until",
        since=NOW - timedelta(days=200),
        until=NOW - timedelta(days=50),
    )

    # Only the 100-day-old row sits inside [now-200d, now-50d].
    assert result.payload["summary"]["events_total"] == 1
    assert result.payload["summary"]["paid_amount_cents_by_currency"] == {"USD": 200}
    assert result.payload["window"]["until"] == (NOW - timedelta(days=50)).isoformat()


@pytest.mark.asyncio
async def test_a_request_beyond_the_maximum_is_clamped_and_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMERCE_FUNNEL_MAX_WINDOW_DAYS", raising=False)
    result = await _funnel(
        monkeypatch, tmp_path, "window-clamp", since=NOW - timedelta(days=900), until=NOW
    )

    assert result.payload["window"]["clamped"] is True
    assert result.payload["window"]["days"] == 400
    # The 500-day-old row is outside the clamped window, so it is NOT counted —
    # the clamp is a real bound, not a label on an unbounded read.
    assert result.payload["summary"]["events_total"] == 2
    assert result.payload["summary"]["paid_amount_cents_by_currency"] == {"USD": 300}


@pytest.mark.asyncio
async def test_an_unavailable_event_store_still_reports_the_window(tmp_path, monkeypatch):
    import services.merchant_commerce_event_funnel_service as module

    async def unavailable(**_kwargs):
        raise RuntimeError("schema rollout in progress")

    monkeypatch.setattr(module, "_fetch_event_rows", unavailable)
    result = await module.get_merchant_commerce_event_funnel(
        merchant_id=MERCHANT_ID, group_by="store", since=NOW - timedelta(days=7), until=NOW
    )

    assert result.payload["available"] is False
    assert result.payload["window"]["days"] == 7


@pytest.mark.asyncio
async def test_a_bad_window_is_not_swallowed_as_an_unavailable_event_store(monkeypatch):
    """A caller error must not come back as a plausible-looking empty funnel."""
    import services.merchant_commerce_event_funnel_service as module

    async def never_called(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("the fetch must not run for an unusable window")

    monkeypatch.setattr(module, "_fetch_event_rows", never_called)
    with pytest.raises(ValueError):
        await module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
            since=NOW,
            until=NOW - timedelta(days=1),
        )


# ---- 3. the inconsistency the window fixes ----------------------------------
#
# COMMERCE_FUNNEL_LEDGER_EVENT_LIMIT=100 (the floor `_event_limit` enforces).
# 101 rows for one merchant:
#
#   now-1d   99 refund.succeeded for 99 OTHER orders, EUR
#   now-2d    1 refund.succeeded for ORDER_A, USD 5000
#   now-3d    1 order.paid       for ORDER_A, USD 5000
#
# Newest-first with LIMIT 100 keeps the 99 unrelated refunds and ORDER_A's
# refund, and drops ORDER_A's order.paid — so the ledger reports USD 5000
# refunded against USD 0 paid.


def _split_purchase_rows() -> List[Dict[str, Any]]:
    rows = [
        _row(
            "evt_a_paid",
            "int_a",
            "order.paid",
            occurred_at=NOW - timedelta(days=3),
            payload={"order_id": "ORDER_A", "amount_cents": 5000, "currency": "USD"},
        ),
        _row(
            "evt_a_refund",
            "int_a",
            "refund.succeeded",
            occurred_at=NOW - timedelta(days=2),
            payload={
                "order_id": "ORDER_A",
                "refund_id": "REFUND_A",
                "amount_cents": 5000,
                "currency": "USD",
            },
        ),
    ]
    for index in range(99):
        rows.append(
            _row(
                f"evt_other_{index:03d}",
                f"int_other_{index:03d}",
                "refund.succeeded",
                occurred_at=NOW - timedelta(days=1),
                payload={
                    "order_id": f"ORDER_OTHER_{index:03d}",
                    "refund_id": f"REFUND_OTHER_{index:03d}",
                    "amount_cents": 1,
                    "currency": "EUR",
                },
            )
        )
    return rows


@pytest.mark.asyncio
async def test_an_unwindowed_read_counts_a_refund_without_its_order(tmp_path, monkeypatch):
    """The defect, and the field that now makes it legible.

    The event LIMIT still cuts on recency inside the window, so a wide enough
    window can still split a purchase. What changes is that the response now
    NAMES the slice it aggregated, so an operator can narrow to a window the
    limit does not bite — which the next test does.
    """
    import services.merchant_commerce_event_funnel_service as module

    monkeypatch.setenv("COMMERCE_FUNNEL_LEDGER_EVENT_LIMIT", "100")
    test_database = await _sqlite_ledger(
        tmp_path, monkeypatch, "split-default", _split_purchase_rows()
    )
    try:
        result = await module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID, group_by="store"
        )
    finally:
        await test_database.disconnect()

    summary = result.payload["summary"]
    assert result.payload["truncated"] is True
    # ORDER_A's refund is counted; the order.paid it belongs to is not.
    assert summary["refunded_amount_cents_by_currency"]["USD"] == 5000
    assert "USD" not in summary["paid_amount_cents_by_currency"]
    # The response now declares the population it aggregated. Before this
    # change there was no such field, and `truncated: true` said a bound was
    # hit without saying which one.
    assert result.payload["window"]["days"] == 90
    assert result.payload["window"]["since"] is not None
    assert result.payload["window"]["until"] is not None


@pytest.mark.asyncio
async def test_a_window_containing_the_purchase_keeps_paid_and_refunded_together(
    tmp_path, monkeypatch
):
    """Both inside, or both outside — never one without the other."""
    import services.merchant_commerce_event_funnel_service as module

    monkeypatch.setenv("COMMERCE_FUNNEL_LEDGER_EVENT_LIMIT", "100")
    test_database = await _sqlite_ledger(
        tmp_path, monkeypatch, "split-windowed", _split_purchase_rows()
    )
    try:
        both_inside = await module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
            since=NOW - timedelta(days=4),
            until=NOW - timedelta(days=2),
        )
        both_outside = await module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
            since=NOW - timedelta(days=1, hours=12),
            until=NOW,
        )
    finally:
        await test_database.disconnect()

    inside = both_inside.payload["summary"]
    assert both_inside.payload["truncated"] is False
    assert inside["paid_amount_cents_by_currency"] == {"USD": 5000}
    assert inside["refunded_amount_cents_by_currency"] == {"USD": 5000}

    outside = both_outside.payload["summary"]
    assert "USD" not in outside["paid_amount_cents_by_currency"]
    assert "USD" not in outside["refunded_amount_cents_by_currency"]
    assert outside["refunded_amount_cents_by_currency"] == {"EUR": 99}


# ---- 4. the ops canary's own read still lands inside the default window -----


@pytest.mark.asyncio
async def test_the_ops_canary_read_lands_inside_the_default_window(tmp_path, monkeypatch):
    """The canary stamps `occurred_at = now` and reads back with surface=ops_canary."""
    import services.merchant_commerce_event_funnel_service as module

    now = datetime.now(timezone.utc)
    rows = [
        _row(
            "evt_canary",
            "int_canary",
            "order.paid",
            occurred_at=now,
            payload={"order_id": "CANARY", "amount_cents": 100, "currency": "USD"},
            surface=module.OPS_CANARY_SURFACE,
        )
    ]
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "canary-window", rows)
    try:
        result = await module.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
            surface=module.OPS_CANARY_SURFACE,
        )
    finally:
        await test_database.disconnect()

    assert result.payload["summary"]["events_total"] == 1
    assert result.payload["summary"]["stages"]["paid"] == 1
