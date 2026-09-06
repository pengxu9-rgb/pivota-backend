"""Squarespace through the REAL route, the REAL sweep, and the REAL ledger.

The two Squarespace ingresses see the SAME resource. This file proves the two
consequences of that:

* **One purchase, one paid row.** A webhook observation and a later sweep
  observation of the same order must collapse. The event ids are derived from
  the order, not from the notification, so they do — and a regression here
  would double every GMV figure for a store that has both paths armed.
* **One cumulative refund total, read across both write paths.** The baseline
  the delta is computed against must span `squarespace_webhook` AND
  `squarespace_reconciliation`. Reading only the caller's own path makes the
  sweep re-record money the webhook already counted, under a second
  `<order>:<cumulative>` key that the funnel then SUMS.

Everything below the `merchant_stores` lookup and the Orders API is production
code: the HMAC check, the website binding, the mapper, the money lock, the
ledger read, `ingest_merchant_event_batch`, and the ledger's unique indexes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest

SECRET = "sq-subscription-secret"
MERCHANT_ID = "merchant-sq"
STORE_ID = "store-sq"
WEBSITE_ID = "site-aaaa"
ORDER_ID = "sq-order-1"
ORDER_REF = f"squarespace:{ORDER_ID}"
PATH = f"/webhooks/squarespace/{STORE_ID}"

_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


CREATED_AT = _iso(_NOW - timedelta(days=2))


def _order(*, modified=None, **overrides):
    order = {
        "id": ORDER_ID,
        "orderNumber": "00042",
        "createdOn": CREATED_AT,
        "modifiedOn": modified or CREATED_AT,
        "testmode": False,
        "fulfillmentStatus": "PENDING",
        "grandTotal": {"value": "40.00", "currency": "USD"},
    }
    order.update(overrides)
    return order


def _refunded(cumulative: str, modified: datetime):
    return _order(
        modified=_iso(modified),
        refundedTotal={"value": cumulative, "currency": "USD"},
    )


class _AwareDatetimeRows:
    """SQLite drops the UTC offset on write; re-attach it on read.

    Production is Postgres `timestamptz`, which hands aware datetimes back, and
    the ledger compares an incoming `occurred_at` against the stored one. The
    mappers run INSIDE the code under test here, so the offset is restored at
    the boundary where the SQLite dialect lost it and nothing in the production
    path changes.
    """

    def __init__(self, database):
        self._database = database

    def __getattr__(self, name):
        return getattr(self._database, name)

    async def fetch_all(self, *args, **kwargs):
        rows = await self._database.fetch_all(*args, **kwargs)
        return [self._aware(dict(row)) for row in rows]

    async def fetch_one(self, *args, **kwargs):
        row = await self._database.fetch_one(*args, **kwargs)
        return None if row is None else self._aware(dict(row))

    @staticmethod
    def _aware(row):
        for key, value in row.items():
            if isinstance(value, datetime) and value.tzinfo is None:
                row[key] = value.replace(tzinfo=timezone.utc)
        return row


async def _sqlite_ledger(tmp_path, monkeypatch, name: str):
    import databases
    from sqlalchemy import create_engine

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )
    from db.database import metadata
    from services import commerce_interaction_service as interaction_service

    db_path = tmp_path / f"{name}.sqlite3"
    sync_engine = create_engine(f"sqlite:///{db_path}")
    metadata.create_all(
        sync_engine,
        tables=[commerce_interactions, commerce_interaction_events],
        checkfirst=True,
    )
    sync_engine.dispose()

    raw_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await raw_database.connect()
    test_database = _AwareDatetimeRows(raw_database)
    # The baseline read, the advisory-lock helper around it, and the write all
    # resolve `database` from this one module, so one patch covers the whole
    # read-modify-write for BOTH ingresses.
    monkeypatch.setattr(interaction_service, "database", test_database)
    monkeypatch.setattr(interaction_service, "IS_POSTGRES", False)
    return test_database


_CREDENTIALS = {
    "api_key": "sq-api-key",
    "website_id": WEBSITE_ID,
    "webhook_secret": SECRET,
}


def _webhook_client(monkeypatch, order):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import squarespace_webhooks as route

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": "shop.example",
                "api_key": json.dumps(_CREDENTIALS),
            }

    async def fake_fetch(*, access_token, order_id, **kwargs):
        return order() if callable(order) else order

    monkeypatch.setattr(route, "database", FakeStores())
    monkeypatch.setattr(route, "fetch_squarespace_order", fake_fetch)
    route._SEEN_NOTIFICATIONS.clear()

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _deliver(client, *, topic="order.update", notification_id="n-1"):
    body = json.dumps(
        {
            "id": notification_id,
            "topic": topic,
            "websiteId": WEBSITE_ID,
            "subscriptionId": "sub-1",
            "data": {"orderId": ORDER_ID},
        },
        separators=(",", ":"),
    ).encode()
    return client.post(
        PATH,
        content=body,
        headers={
            "Squarespace-Signature": hmac.new(
                SECRET.encode(), body, hashlib.sha256
            ).hexdigest(),
            "Content-Type": "application/json",
        },
    )


async def _sweep(monkeypatch, orders):
    """The real sweep over a scripted single page."""
    from services import squarespace_order_sweep as sweep

    class _Response:
        status_code = 200
        content = b"{}"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        async def get(self, url, headers=None, params=None):
            # The sweep proves the credential's SITE before it lists anything;
            # answering the lookup with this store's own `website_id` is what
            # makes this an end-to-end run of a correctly-connected store
            # rather than of a store whose token points somewhere else.
            if "/authorization/website" in str(url):
                return _Response({"id": WEBSITE_ID})
            return _Response({"result": orders, "pagination": {}})

        async def aclose(self):
            return None

    persisted = {}

    async def fake_find(store_id):
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.example",
            "api_key": json.dumps(_CREDENTIALS),
        }

    async def fake_merge(*, store_id, updates=None, **kwargs):
        persisted.update(updates or {})
        return {**_CREDENTIALS, **(updates or {})}

    monkeypatch.setattr(sweep, "find_squarespace_store", fake_find)
    monkeypatch.setattr(sweep, "merge_squarespace_credentials", fake_merge)
    return await sweep.sweep_squarespace_store(store_id=STORE_ID, client=_Client())


async def _events(test_database, event_type=None):
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events

    query = select(commerce_interaction_events)
    if event_type:
        query = query.where(commerce_interaction_events.c.event_type == event_type)
    rows = [dict(row) for row in await test_database.fetch_all(query)]
    return sorted(rows, key=lambda row: str(row["event_type"]))


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_webhook_and_a_later_sweep_of_one_order_produce_one_paid_row(
    tmp_path, monkeypatch
):
    """The single most expensive regression this integration could have.

    An OAuth-connected store has BOTH paths armed, and the sweep re-reads every
    order the webhook already delivered. If the event ids carried the
    notification id (or the write path), every purchase would be counted twice.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-one-paid")
    try:
        order = _order()
        client = _webhook_client(monkeypatch, order)

        delivered = _deliver(client)
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["accepted"] == 2  # order.created + order.paid

        swept = await _sweep(monkeypatch, [order])
        assert swept["seen"] == 1
        # Both events were already there; the sweep added no money.
        assert swept["accepted"] == 0
        assert swept["duplicates"] == 2

        paid = await _events(test_database, "order.paid")
        assert len(paid) == 1
        assert paid[0]["payload"]["amount_cents"] == 4000
        assert paid[0]["order_ref"] == ORDER_REF
        # The row keeps the FIRST observer's provenance; that is what
        # first-write-wins means, and it is the honest answer to "who told us".
        assert paid[0]["write_path"] == "squarespace_webhook"
        assert paid[0]["authority"] == "platform"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_sweep_and_a_LATER_webhook_of_one_order_produce_one_paid_row(
    tmp_path, monkeypatch
):
    """The SAME order, the OTHER way round.

    Testing only webhook-then-sweep proves the sweep defers to an existing row;
    it says nothing about the receiver defering to the sweep. And this ordering
    is the COMMON one after any outage: Squarespace gives up retrying, the
    sweep picks the order up, and then a later `order.update` delivery arrives
    for the same order. If the dedupe were asymmetric — if the event ids or the
    money guard depended on which path wrote first — that sequence would double
    every recovered purchase, and only this direction would show it.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-sweep-first")
    try:
        order = _order()

        swept = await _sweep(monkeypatch, [order])
        assert swept["accepted"] == 2  # order.created + order.paid

        client = _webhook_client(monkeypatch, order)
        delivered = _deliver(client)
        assert delivered.status_code == 200, delivered.text
        # Both events were already there; the delivery added no money.
        assert delivered.json()["accepted"] == 0
        assert delivered.json()["duplicates"] == 2

        paid = await _events(test_database, "order.paid")
        assert len(paid) == 1
        assert paid[0]["payload"]["amount_cents"] == 4000
        assert paid[0]["order_ref"] == ORDER_REF
        # First-write-wins, so the provenance is the SWEEP's this time. That
        # asymmetry in the answer is the point: the row records who actually
        # told us first, not whichever path is nominally preferred.
        assert paid[0]["write_path"] == "squarespace_reconciliation"
        assert paid[0]["authority"] == "platform"

        created = await _events(test_database, "order.created")
        assert len(created) == 1
        assert created[0]["write_path"] == "squarespace_reconciliation"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_sweep_only_store_records_the_same_facts(tmp_path, monkeypatch):
    """The API-key case: no webhook ever arrives, and the ledger is identical
    apart from the write path. A sweep that could not stand alone would leave
    every API-key merchant dark."""
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-sweep-only")
    try:
        swept = await _sweep(monkeypatch, [_order()])

        assert swept["accepted"] == 2
        paid = await _events(test_database, "order.paid")
        assert len(paid) == 1
        assert paid[0]["payload"]["amount_cents"] == 4000
        assert paid[0]["write_path"] == "squarespace_reconciliation"
        assert paid[0]["authority"] == "platform"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_refund_baseline_spans_both_write_paths(tmp_path, monkeypatch):
    """A webhook records 10.00 of a cumulative total; the sweep then sees 25.00.

    With the baseline read scoped to the caller's own write path, the sweep
    would read 0 and emit the WHOLE 2500 under `<order>:2500` — a second key
    the funnel sums with the webhook's 1000, for 3500 against a true cumulative
    of 2500. Reading across both paths makes it 1500.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-refund-cross")
    try:
        first = _refunded("10.00", _NOW - timedelta(hours=6))
        client = _webhook_client(monkeypatch, first)
        delivered = _deliver(client)
        assert delivered.status_code == 200, delivered.text

        swept = await _sweep(monkeypatch, [_refunded("25.00", _NOW - timedelta(hours=3))])
        assert swept["accepted"] == 1  # the refund delta only

        refunds = sorted(
            await _events(test_database, "refund.succeeded"),
            key=lambda row: row["payload"]["amount_cents"],
        )
        assert [row["payload"]["amount_cents"] for row in refunds] == [1000, 1500]
        assert [row["payload"]["refund_id"] for row in refunds] == [
            f"{ORDER_ID}:1000",
            f"{ORDER_ID}:2500",
        ]
        assert {row["write_path"] for row in refunds} == {
            "squarespace_webhook",
            "squarespace_reconciliation",
        }
        assert sum(row["payload"]["amount_cents"] for row in refunds) == 2500
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_sweep_re_reading_an_already_recorded_refund_adds_nothing(
    tmp_path, monkeypatch
):
    """The overlap window guarantees this happens on every run."""
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-refund-replay")
    try:
        refunded = _refunded("25.00", _NOW - timedelta(hours=3))
        client = _webhook_client(monkeypatch, refunded)
        assert _deliver(client).status_code == 200

        swept = await _sweep(monkeypatch, [refunded])

        assert swept["accepted"] == 0
        refunds = await _events(test_database, "refund.succeeded")
        assert [row["payload"]["amount_cents"] for row in refunds] == [2500]
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_one_interaction_holds_every_event_for_the_order(tmp_path, monkeypatch):
    """`order_ref` is the cross-authority stitch key. Two ingresses reporting
    one purchase must not fragment it into two interactions that can never
    merge."""
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interactions

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-one-interaction")
    try:
        client = _webhook_client(monkeypatch, _order())
        assert _deliver(client).status_code == 200
        await _sweep(monkeypatch, [_refunded("25.00", _NOW - timedelta(hours=3))])

        interactions = [
            dict(row) for row in await test_database.fetch_all(select(commerce_interactions))
        ]
        assert len(interactions) == 1
        assert interactions[0]["order_ref"] == ORDER_REF
        events = await _events(test_database)
        assert {row["interaction_id"] for row in events} == {
            interactions[0]["interaction_id"]
        }
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_testmode_order_reaches_neither_ingress(tmp_path, monkeypatch):
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-testmode")
    try:
        order = _order(testmode=True)
        client = _webhook_client(monkeypatch, order)

        delivered = _deliver(client)
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["accepted"] == 0
        assert "testmode" in delivered.json()["reason"]

        swept = await _sweep(monkeypatch, [order])
        assert swept["testmode_skipped"] == 1
        assert swept["accepted"] == 0

        assert await _events(test_database) == []
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_refund_row_in_another_currency_does_not_reduce_this_delta(
    tmp_path, monkeypatch
):
    """Subtraction is only meaningful inside ONE unit.

    A refund already recorded for this order in EUR is a different quantity,
    not a smaller one. Without the currency narrowing on the baseline read, a
    3000 EUR row would be subtracted from a 2500 USD cumulative total, the delta
    would go negative, and the USD refund would be silently dropped as
    `refund_not_new` — real money, invisible, with a 2xx on the delivery.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sq-refund-currency")
    try:
        in_euros = _order(
            modified=_iso(_NOW - timedelta(hours=8)),
            grandTotal={"value": "50.00", "currency": "EUR"},
            refundedTotal={"value": "30.00", "currency": "EUR"},
        )
        assert _deliver(_webhook_client(monkeypatch, in_euros)).status_code == 200

        in_dollars = _refunded("25.00", _NOW - timedelta(hours=3))
        delivered = _deliver(
            _webhook_client(monkeypatch, in_dollars), notification_id="n-2"
        )
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["accepted"] == 1, delivered.text

        refunds = await _events(test_database, "refund.succeeded")
        by_currency = {row["payload"]["currency"]: row["payload"] for row in refunds}
        assert by_currency["EUR"]["amount_cents"] == 3000
        # The full USD total, because nothing had been recorded in USD yet.
        assert by_currency["USD"]["amount_cents"] == 2500
    finally:
        await test_database.disconnect()
