"""A signed settlement-sweep batch, through the REAL route, into the REAL ledger.

The mapper tests prove the shape of each event and the contract test pins what
the cartridge sends; this file proves what actually lands. It posts a batch the
way `PivotaSettlementSweep` + `DrainPivotaTelemetry` would — real HMAC over
`"{timestamp}.{body}"`, real site-id binding, real route — against commerce
ledger tables built on SQLite.

The SFCC wrinkle its siblings do not have: SFCC fires nothing on settlement, so
every one of these events is an OBSERVATION the sweep made on a cursor, keyed
on an id it derived itself. That makes two properties load-bearing here rather
than incidental:

* two credit invoices on one order must stay two refund rows (keyed on the
  invoice, never the order), and so must two partial refunds against ONE
  invoice — `Invoice.refundedAmount` is cumulative per invoice, so the sweep
  sends the DELTA under a key qualified by the cumulative total it reached, and
  the funnel's `max(amount)` per `refund_id` then SUMS to the invoice's
  cumulative figure; and
* a redelivery — which a cursor with an overlap window makes routine, not
  exceptional — must dedupe instead of doubling the money.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


MERCHANT_ID = "merchant-sfcc-e2e"
STORE_ID = "store-sfcc-e2e"
SITE_ID = "RefArchGlobal"
SECRET = "sweep-signing-secret"
ORDER_NO = "00001234"
SIGNING_TIME = 2_000_000_000

# The funnel reads a bounded recent window, so the fixture instants have to be
# recent. The FORMAT stays what the cartridge emits: `Date#toISOString()`.
_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


PAID_AT = _iso(_NOW - timedelta(days=2))
REFUND_ONE_AT = _iso(_NOW - timedelta(hours=6))
REFUND_TWO_AT = _iso(_NOW - timedelta(hours=3))
CANCELLED_AT = _iso(_NOW - timedelta(hours=1))


def _sweep_event(event_type, *, event_id, occurred_at, amount, status, **extra):
    """One event exactly as `SweepPivotaSettlements.js` enqueues it: a
    deterministic `event_id`, no line items, `lastModified` as the time."""
    event = {
        "event_id": event_id,
        "type": event_type,
        "occurred_at": occurred_at,
        "site_id": SITE_ID,
        "basket_id": None,
        "checkout_id": None,
        "order_id": ORDER_NO,
        "payment_id": None,
        "customer_id": "customer-8",
        "amount": amount,
        "currency": "USD",
        "status": status,
        "items": [],
    }
    event.update(extra)
    return event


def _paid_event():
    return _sweep_event(
        "order.paid",
        event_id=f"order.paid:{ORDER_NO}",
        occurred_at=PAID_AT,
        amount="40.56",
        status="PAID",
    )


def _cancelled_event():
    return _sweep_event(
        "order.cancelled",
        event_id=f"order.cancelled:{ORDER_NO}",
        occurred_at=CANCELLED_AT,
        amount=None,
        status="CANCELLED",
    )


def _refund_event(refund_id, amount, occurred_at):
    """One refund observation.

    `refund_id` is what the sweep sends: `<invoiceNumber>:<the cumulative
    refunded amount that invoice had reached>`. `amount` is the DELTA that
    observation added, not the cumulative figure.
    """
    return _sweep_event(
        "refund.succeeded",
        event_id=f"refund.succeeded:{refund_id}",
        occurred_at=occurred_at,
        amount=amount,
        status="PAID",
        refund_id=refund_id,
    )


async def _sqlite_ledger(tmp_path, monkeypatch, name: str):
    """Real commerce ledger tables on SQLite, wired into the write path."""
    import databases
    from sqlalchemy import create_engine

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )
    from db.database import metadata
    from services import commerce_interaction_service as interaction_service
    import services.merchant_commerce_event_funnel_service as funnel_module

    db_path = tmp_path / f"{name}.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    sync_engine.dispose()

    test_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await test_database.connect()
    monkeypatch.setattr(interaction_service, "database", test_database)
    monkeypatch.setattr(interaction_service, "IS_POSTGRES", False)
    monkeypatch.setattr(funnel_module, "database", test_database)
    return test_database


def _client(monkeypatch):
    """The real receiver, with only the store lookup faked.

    Everything the change touches stays real: the signature check, the site
    binding, the mapper, the ingest service and the ledger write.
    """
    from routes import sfcc_events as route
    from services.merchant_event_ingest_service import ingest_merchant_event_batch

    class FakeStoreDB:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "api_key": json.dumps(
                    {"site_id": SITE_ID, "telemetry_signing_secret": SECRET}
                ),
            }

    async def ingest(**kwargs):
        # SQLite's DATETIME binding refuses tz-aware values. Strip AFTER the
        # real mapper has run, so its own normalization is still exercised, and
        # before the real ingest service, which is not faked.
        for event in kwargs["batch"].events:
            event.occurred_at = event.occurred_at.replace(tzinfo=None)
        return await ingest_merchant_event_batch(**kwargs)

    monkeypatch.setattr(route, "database", FakeStoreDB())
    monkeypatch.setattr(route, "ingest_merchant_event_batch", ingest)
    monkeypatch.setattr(route.time, "time", lambda: SIGNING_TIME)
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _post(client, events, *, delivery_id):
    raw = json.dumps({"events": events}, separators=(",", ":")).encode("utf-8")
    timestamp = str(SIGNING_TIME)
    digest = hmac.new(
        SECRET.encode("utf-8"),
        timestamp.encode("ascii") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    return client.post(
        f"/webhooks/salesforce-commerce-cloud/{STORE_ID}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-SFCC-Signature": f"sha256={digest}",
            "X-Pivota-SFCC-Timestamp": timestamp,
            "X-Pivota-SFCC-Delivery-Id": delivery_id,
            "X-Pivota-SFCC-Site-Id": SITE_ID,
        },
    )


@pytest.mark.asyncio
async def test_a_signed_sweep_batch_lands_paid_cancelled_and_two_refunds(
    tmp_path, monkeypatch
):
    from sqlalchemy import select

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sfcc-sweep")
    try:
        # "one interaction, no unique violation" is only worth claiming if the
        # fixture actually built the guard the two refund ids have to pass.
        indexes = {
            dict(row)["name"]
            for row in await test_database.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_commerce_interactions_refund_id_unique" in indexes

        client = _client(monkeypatch)
        response = _post(
            client,
            [
                _paid_event(),
                _refund_event("INV-77:10.50", "10.50", REFUND_ONE_AT),
                _refund_event("INV-78:5.00", "5.00", REFUND_TWO_AT),
                _cancelled_event(),
            ],
            delivery_id="delivery-1",
        )
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == 4
        assert response.json()["rejected"] == 0

        interactions = [
            dict(row)
            for row in await test_database.fetch_all(select(commerce_interactions))
        ]
        assert len(interactions) == 1
        interaction = interactions[0]
        assert interaction["order_id"] == ORDER_NO
        assert interaction["order_ref"] == f"salesforce_commerce_cloud:{ORDER_NO}"

        rows = [
            dict(row)
            for row in await test_database.fetch_all(select(commerce_interaction_events))
        ]
        assert {row["interaction_id"] for row in rows} == {interaction["interaction_id"]}
        by_type = {}
        for row in rows:
            by_type.setdefault(row["event_type"], []).append(row)
        assert sorted(by_type) == [
            "order.cancelled",
            "order.paid",
            "refund.succeeded",
        ]

        paid = by_type["order.paid"][0]
        assert paid["payload"]["amount_cents"] == 4056
        assert paid["payload"]["currency"] == "USD"
        # The amount is the ORDER TOTAL, not a capture, and the event says so.
        # `record_commerce_event` spreads metadata flat into the payload.
        assert paid["payload"]["native_amount_semantics"] == "order_total_gross"
        assert paid["payload"]["native_event_name"] == "order.paid"
        # A cancellation moves no money, so it carries none. The writer drops
        # falsy refs, so the assertion is that the key is simply absent.
        cancelled_payload = by_type["order.cancelled"][0]["payload"]
        assert "amount_cents" not in cancelled_payload
        assert cancelled_payload["native_status"] == "CANCELLED"

        # TWO refund rows: keyed on the credit invoice, never on the order.
        refunds = by_type["refund.succeeded"]
        assert len(refunds) == 2
        assert sorted(row["payload"]["refund_id"] for row in refunds) == [
            "INV-77:10.50",
            "INV-78:5.00",
        ]
        assert sorted(row["payload"]["amount_cents"] for row in refunds) == [500, 1050]
        # A refund's amount is a delta against its invoice's cumulative figure,
        # and the row says so — read as an invoice total it would over-report.
        assert {row["payload"]["native_amount_semantics"] for row in refunds} == {
            "invoice_cumulative_delta"
        }
        # The ingress stamped the provenance, not the payload.
        assert {row["write_path"] for row in rows} == {"sfcc_cartridge"}
        assert {row["authority"] for row in rows} == {"platform"}
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_sweeps_overlap_window_redelivery_dedupes_instead_of_doubling(
    tmp_path, monkeypatch
):
    """The cursor is stored rewound by `OverlapMinutes`, so an order is
    re-examined routinely; a marker that was never written (a failed
    Transaction, a Business Manager clear) makes a re-enqueue routine too. The
    deterministic `event_id` is what keeps either from doubling the money.
    """
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sfcc-replay")
    try:
        client = _client(monkeypatch)
        batch = [
            _paid_event(),
            _refund_event("INV-77:10.50", "10.50", REFUND_ONE_AT),
            _refund_event("INV-78:5.00", "5.00", REFUND_TWO_AT),
        ]
        first = _post(client, batch, delivery_id="delivery-1")
        assert first.json()["accepted"] == 3

        # Same facts, a new batch and a new delivery id — exactly what the next
        # sweep tick sends when a marker did not stick.
        replay = _post(client, batch, delivery_id="delivery-2")
        assert replay.status_code == 200, replay.text
        assert replay.json()["accepted"] == 0
        assert all(result["duplicate"] for result in replay.json()["events"])

        rows = [
            dict(row)
            for row in await test_database.fetch_all(select(commerce_interaction_events))
        ]
        assert len(rows) == 3
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_zero_amount_refund_never_reaches_the_ledger_to_shadow_the_real_one(
    tmp_path, monkeypatch
):
    """The whole point of the positive-amount rule, end to end.

    Dedupe is first-write-wins on the event key, and for a refund that key is
    the credit invoice and the cumulative figure it had reached. A zero-amount
    `refund.succeeded` for INV-77 would therefore make the real 10.50 for that
    same observation unwritable FOREVER. It is rejected at the mapper, so the
    later correct figure still lands.
    """
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sfcc-shadow")
    try:
        client = _client(monkeypatch)
        zero = _post(
            client,
            [_refund_event("INV-77:0.00", "0.00", REFUND_ONE_AT)],
            delivery_id="delivery-1",
        )
        assert zero.status_code == 200, zero.text
        assert zero.json()["status"] == "rejected"
        assert zero.json()["rejected"] == 1
        assert zero.json()["accepted"] == 0
        assert await test_database.fetch_all(select(commerce_interaction_events)) == []

        real = _post(
            client,
            [_refund_event("INV-77:10.50", "10.50", REFUND_ONE_AT)],
            delivery_id="delivery-2",
        )
        assert real.json()["accepted"] == 1
        rows = [
            dict(row)
            for row in await test_database.fetch_all(select(commerce_interaction_events))
        ]
        assert len(rows) == 1
        assert rows[0]["payload"]["amount_cents"] == 1050
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_second_partial_refund_on_one_invoice_lands_and_the_funnel_sums_it(
    tmp_path, monkeypatch
):
    """The bug this scheme exists for.

    SFCC does not create a second invoice for a second partial refund — it
    raises the SAME invoice's cumulative `refundedAmount`. Keyed on the invoice
    number alone, tick 2 was lost twice over: the once-only marker skipped the
    invoice, and even with the marker gone the ledger deduped the event against
    tick 1's key. So the sweep sends the DELTA under a key qualified by the new
    cumulative total, and the funnel — `max(amount)` per `refund_id`, SUMMED
    across distinct `refund_id`s inside one authority — reports the whole
    cumulative figure.
    """
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events
    from services import merchant_commerce_event_funnel_service as funnel_service

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sfcc-partials")
    try:
        client = _client(monkeypatch)
        # Tick 1: the invoice has refunded 10.00 in total, none of it reported.
        first = _post(
            client,
            [_paid_event(), _refund_event("INV-77:10.00", "10.00", REFUND_ONE_AT)],
            delivery_id="delivery-1",
        )
        assert first.status_code == 200, first.text
        assert first.json()["accepted"] == 2

        # Tick 2: a second partial refund took the SAME invoice's cumulative
        # figure to 25.00, so the sweep sends the 15.00 difference.
        second = _post(
            client,
            [_refund_event("INV-77:25.00", "15.00", REFUND_TWO_AT)],
            delivery_id="delivery-2",
        )
        assert second.status_code == 200, second.text
        assert second.json()["accepted"] == 1

        rows = [
            dict(row)
            for row in await test_database.fetch_all(select(commerce_interaction_events))
        ]
        refunds = [row for row in rows if row["event_type"] == "refund.succeeded"]
        assert len(refunds) == 2
        assert sorted(row["payload"]["amount_cents"] for row in refunds) == [1000, 1500]
        assert sorted(row["payload"]["refund_id"] for row in refunds) == [
            "INV-77:10.00",
            "INV-77:25.00",
        ]

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        summary = result.payload["summary"]
        # 10.00 + 15.00 = the invoice's cumulative 25.00, counted once.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 2500}
        assert summary["event_type_breakdown"]["refund.succeeded"] == 2

        # …and a redelivery of EITHER observation still dedupes: the cursor's
        # overlap window makes that routine, not exceptional.
        replay = _post(
            client,
            [
                _refund_event("INV-77:10.00", "10.00", REFUND_ONE_AT),
                _refund_event("INV-77:25.00", "15.00", REFUND_TWO_AT),
            ],
            delivery_id="delivery-3",
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["accepted"] == 0
        assert all(event["duplicate"] for event in replay.json()["events"])
        after = [
            dict(row)
            for row in await test_database.fetch_all(select(commerce_interaction_events))
        ]
        assert len([row for row in after if row["event_type"] == "refund.succeeded"]) == 2
        replayed = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        assert replayed.payload["summary"]["refunded_amount_cents_by_currency"] == {
            "USD": 2500
        }
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_funnel_counts_one_paid_order_and_sums_both_credit_invoices(
    tmp_path, monkeypatch
):
    from services import merchant_commerce_event_funnel_service as funnel_service

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sfcc-funnel")
    try:
        client = _client(monkeypatch)
        _post(
            client,
            [
                _paid_event(),
                _refund_event("INV-77:10.50", "10.50", REFUND_ONE_AT),
                _refund_event("INV-78:5.00", "5.00", REFUND_TWO_AT),
            ],
            delivery_id="delivery-1",
        )

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        assert result.payload["available"] is True
        summary = result.payload["summary"]
        assert summary["event_type_breakdown"]["refund.succeeded"] == 2
        assert summary["event_type_breakdown"]["order.paid"] == 1
        # `order.paid` alone carries the settled money: the sweep deliberately
        # does NOT also emit `payment.succeeded` per capture, because
        # `_PAID_EVENTS` feeds one stage and takes the MAX per order rather
        # than summing captures — a second event would add rows and no money.
        assert summary["paid_amount_cents_by_currency"] == {"USD": 4056}
        # Both refunds carry the same authority, so they SUM.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 1550}
        assert result.payload["slices"][0]["key"] == STORE_ID
    finally:
        await test_database.disconnect()
