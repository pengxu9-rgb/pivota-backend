"""Webflow through the REAL route, the REAL sweep, and the REAL ledger.

Both Webflow ingresses see the same resource — an order fetched from the Data
API — so one purchase must produce ONE paid row and one refund must produce ONE
refund row, whichever ingress saw it first and however many times either sees it
again.

Testing only webhook-then-sweep would prove the sweep defers to an existing row
and say nothing about the receiver deferring to the sweep. And sweep-first is the
COMMON ordering after any outage: Webflow gives up retrying, the sweep picks the
order up, and a later `ecomm_order_changed` arrives for the same order. So both
directions are run.

Everything below the `merchant_stores` lookup and the Data API is production
code: the URL-secret check, the site binding, the mapper, `ingest_merchant_event_batch`,
and the ledger's unique indexes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

URL_SECRET = "wf-url-secret-value"
MERCHANT_ID = "merchant-wf"
STORE_ID = "store-wf"
SITE_ID = "5f1a0000000000000000aaaa"
ORDER_ID = "0000-0001"
ORDER_REF = f"webflow:{ORDER_ID}"
PATH = f"/webhooks/webflow/{STORE_ID}/{URL_SECRET}"

_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


ACCEPTED_AT = _iso(_NOW - timedelta(days=2))

_CREDENTIALS = {
    "api_token": "wf-token",
    "site_id": SITE_ID,
    "url_secret": URL_SECRET,
}


def _order(*, status="unfulfilled", refunded_on=None, **overrides):
    order = {
        "orderId": ORDER_ID,
        "status": status,
        "acceptedOn": ACCEPTED_AT,
        # 5898 minor units == $58.98. The whole point of this integration's
        # money handling, carried end to end so a 100x conversion would show up
        # in a real ledger row rather than only in a unit test.
        "customerPaid": {"unit": "USD", "value": 5898, "string": "$58.98"},
    }
    if refunded_on is not None:
        order["refundedOn"] = refunded_on
    order.update(overrides)
    return order


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
    monkeypatch.setattr(interaction_service, "database", test_database)
    monkeypatch.setattr(interaction_service, "IS_POSTGRES", False)
    return test_database


def _webhook_client(monkeypatch, order):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import webflow_webhooks as route

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": "shop.webflow.io",
                "api_key": json.dumps(_CREDENTIALS),
            }

    async def fake_fetch(*, api_token, site_id, order_id, **kwargs):
        return order() if callable(order) else order

    monkeypatch.setattr(route, "database", FakeStores())
    monkeypatch.setattr(route, "fetch_webflow_order", fake_fetch)
    route._SEEN_DELIVERIES.clear()

    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _deliver(client, *, trigger="ecomm_order_changed", nonce="1"):
    """One delivery. `nonce` varies the body so the per-process delivery cache
    (which is keyed on the body digest) does not short-circuit a deliberate
    re-observation before it can reach the ledger."""
    body = json.dumps(
        {
            "triggerType": trigger,
            "siteId": SITE_ID,
            "deliveryNonce": nonce,
            "payload": {"orderId": ORDER_ID},
        },
        separators=(",", ":"),
    ).encode()
    return client.post(
        PATH, content=body, headers={"Content-Type": "application/json"}
    )


async def _sweep(monkeypatch, orders, *, lanes=None):
    """The real sweep over a scripted single page per lane."""
    from services import webflow_order_sweep as sweep

    class _Response:
        status_code = 200
        content = b"{}"
        headers: dict = {}

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        async def get(self, url, headers=None, params=None):
            # The sweep proves the credential's SITE before it lists anything;
            # answering with this store's own site id is what makes this an
            # end-to-end run of a correctly-connected store.
            if "/orders" not in str(url):
                return _Response({"id": SITE_ID, "displayName": "Shop"})
            status = (params or {}).get("status")
            rows = orders if status is None else [
                row for row in orders if row.get("status") == status
            ]
            return _Response({"orders": rows, "pagination": {}})

        async def aclose(self):
            return None

    persisted = {}

    async def fake_find(store_id):
        return {
            "store_id": STORE_ID,
            "merchant_id": MERCHANT_ID,
            "domain": "shop.webflow.io",
            "api_key": json.dumps(_CREDENTIALS),
        }

    async def fake_merge(*, store_id, updates=None, **kwargs):
        persisted.update(updates or {})
        return {**_CREDENTIALS, **(updates or {})}

    monkeypatch.setattr(sweep, "find_webflow_store", fake_find)
    monkeypatch.setattr(sweep, "merge_webflow_credentials", fake_merge)
    return await sweep.sweep_webflow_store(
        store_id=STORE_ID, client=_Client(), lanes=lanes
    )


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
async def test_a_webhook_then_a_sweep_of_one_order_produce_one_paid_row(
    tmp_path, monkeypatch
):
    """The single most expensive regression this integration could have.

    A provisioned store has BOTH paths armed, and the sweep re-reads every order
    the webhook already delivered. If the event ids carried the delivery or the
    write path, every purchase would be counted twice.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wf-one-paid")
    try:
        order = _order()
        client = _webhook_client(monkeypatch, order)

        delivered = _deliver(client)
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["accepted"] == 2  # order.created + order.paid

        swept = await _sweep(monkeypatch, [order], lanes=["orders"])
        assert swept["seen"] == 1
        assert swept["accepted"] == 0
        assert swept["duplicates"] == 2

        paid = await _events(test_database, "order.paid")
        assert len(paid) == 1
        # 5898 minor units, unconverted, all the way into a real ledger row.
        assert paid[0]["payload"]["amount_cents"] == 5898
        assert paid[0]["payload"]["currency"] == "USD"
        assert paid[0]["order_ref"] == ORDER_REF
        # The row keeps the FIRST observer's provenance; that is what
        # first-write-wins means, and it is the honest answer to "who told us".
        assert paid[0]["write_path"] == "webflow_webhook"
        assert paid[0]["authority"] == "platform"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_sweep_then_a_LATER_webhook_of_one_order_produce_one_paid_row(
    tmp_path, monkeypatch
):
    """The SAME order, the OTHER way round — the common ordering after an outage."""
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wf-sweep-first")
    try:
        order = _order()

        swept = await _sweep(monkeypatch, [order], lanes=["orders"])
        assert swept["accepted"] == 2

        client = _webhook_client(monkeypatch, order)
        delivered = _deliver(client)
        assert delivered.status_code == 200, delivered.text
        assert delivered.json()["accepted"] == 0
        assert delivered.json()["duplicates"] == 2

        paid = await _events(test_database, "order.paid")
        assert len(paid) == 1
        assert paid[0]["payload"]["amount_cents"] == 5898
        assert paid[0]["write_path"] == "webflow_reconciliation"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_webhook_then_a_sweep_of_a_refund_produce_one_refund_row(
    tmp_path, monkeypatch
):
    """Webflow refunds are FULL-ORDER, so both observations carry the identical
    amount under the identical key and the ledger collapses them.

    This is what makes the cumulative-delta machinery (and its lock) unnecessary
    here: there is no baseline to read, so there is nothing to race on.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wf-refund")
    try:
        refunded = _order(
            status="refunded", refunded_on=_iso(_NOW - timedelta(days=1))
        )
        client = _webhook_client(monkeypatch, refunded)

        delivered = _deliver(client)
        assert delivered.status_code == 200, delivered.text
        # order.created + order.paid + refund.succeeded
        assert delivered.json()["accepted"] == 3

        swept = await _sweep(monkeypatch, [refunded], lanes=["refunded"])
        assert swept["accepted"] == 0
        assert swept["duplicates"] == 3

        refunds = await _events(test_database, "refund.succeeded")
        assert len(refunds) == 1
        assert refunds[0]["payload"]["amount_cents"] == 5898
        assert refunds[0]["payload"]["refund_id"] == f"{ORDER_ID}:refund"
        assert refunds[0]["order_ref"] == ORDER_REF
        # And the purchase is still counted exactly once beside it.
        assert len(await _events(test_database, "order.paid")) == 1
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_sweep_then_a_LATER_webhook_of_a_refund_produce_one_refund_row(
    tmp_path, monkeypatch
):
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wf-refund-sweep-first")
    try:
        refunded = _order(
            status="refunded", refunded_on=_iso(_NOW - timedelta(days=1))
        )

        swept = await _sweep(monkeypatch, [refunded], lanes=["refunded"])
        assert swept["accepted"] == 3

        client = _webhook_client(monkeypatch, refunded)
        assert _deliver(client).json()["duplicates"] == 3

        refunds = await _events(test_database, "refund.succeeded")
        assert len(refunds) == 1
        assert refunds[0]["payload"]["amount_cents"] == 5898
        assert refunds[0]["write_path"] == "webflow_reconciliation"
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_an_order_that_moves_pending_to_paid_to_refunded_records_each_once(
    tmp_path, monkeypatch
):
    """The whole lifecycle over three deliveries of one order.

    `pending` is not paid, so the first delivery records only `order.created`.
    The second adds `order.paid` beside the SAME created row. The third adds the
    refund. Nothing is recorded twice, and nothing is missed because a delivery
    for an order already seen was mistaken for a redelivery.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wf-lifecycle")
    try:
        state = {"order": _order(status="pending", acceptedOn=None)}
        client = _webhook_client(monkeypatch, lambda: state["order"])

        first = _deliver(client, trigger="ecomm_new_order", nonce="1")
        assert first.json()["accepted"] == 1

        state["order"] = _order(status="unfulfilled")
        second = _deliver(client, nonce="2")
        assert second.json()["accepted"] == 1  # order.paid; created deduped
        assert second.json()["duplicates"] == 1

        state["order"] = _order(
            status="refunded", refunded_on=_iso(_NOW - timedelta(hours=1))
        )
        third = _deliver(client, nonce="3")
        assert third.json()["accepted"] == 1  # refund.succeeded
        assert third.json()["duplicates"] == 2

        assert len(await _events(test_database, "order.created")) == 1
        assert len(await _events(test_database, "order.paid")) == 1
        assert len(await _events(test_database, "refund.succeeded")) == 1
        # And no cancellation was invented from the refund.
        assert await _events(test_database, "order.cancelled") == []
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_dispute_lost_after_a_refund_does_not_double_count_the_money_out(
    tmp_path, monkeypatch
):
    """The reason `dispute-lost` shares the refund's key.

    The two statuses are mutually exclusive at any instant, but an order can MOVE
    between them across observations. Under two keys this sequence would record
    the same money leaving twice, and the funnel sums refund rows.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wf-dispute")
    try:
        state = {
            "order": _order(
                status="refunded", refunded_on=_iso(_NOW - timedelta(hours=2))
            )
        }
        client = _webhook_client(monkeypatch, lambda: state["order"])
        assert _deliver(client, nonce="1").json()["accepted"] == 3

        state["order"] = _order(
            status="dispute-lost", disputedOn=_iso(_NOW - timedelta(hours=1))
        )
        assert _deliver(client, nonce="2").json()["accepted"] == 0

        refunds = await _events(test_database, "refund.succeeded")
        assert len(refunds) == 1
        assert refunds[0]["payload"]["amount_cents"] == 5898
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_one_interaction_carries_the_whole_order(tmp_path, monkeypatch):
    """Both ingresses resolve to ONE `commerce_interactions` row via `order_ref`,
    which is what lets the funnel see a purchase and its refund as one thing."""
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interactions

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "wf-interaction")
    try:
        refunded = _order(
            status="refunded", refunded_on=_iso(_NOW - timedelta(days=1))
        )
        _deliver(_webhook_client(monkeypatch, refunded))
        await _sweep(monkeypatch, [refunded], lanes=["refunded"])

        rows = [dict(r) for r in await test_database.fetch_all(select(commerce_interactions))]
        assert len(rows) == 1
        assert rows[0]["order_ref"] == ORDER_REF
    finally:
        await test_database.disconnect()
