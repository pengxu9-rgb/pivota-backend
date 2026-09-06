"""Shoplazza refunds through the REAL route and the REAL ledger, on SQLite.

The mapper tests prove the arithmetic given a "previously recorded" figure.
This file proves the figure itself: the receiver reads it back out of the
ledger it wrote, so two partial refunds of one order — reported only as a
CUMULATIVE `total_refund_price` — become two rows of 1000 and 1500 that the
funnel sums to 2500.

Everything below the HMAC is real: the signature check, the source-domain
check, the mapper, `ingest_merchant_event_batch`, `record_commerce_event`, the
`commerce_interactions` unique indexes, and the funnel aggregation. Only the
`merchant_stores` lookup is a double, because that table is not part of what is
under test here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest


APP_SECRET = "app-secret"
DOMAIN = "demo.myshoplaza.com"
MERCHANT_ID = "merchant-sz"
STORE_ID = "store-sz"
ORDER_ID = "sz-order-1"
PATH = f"/webhooks/shoplazza/{STORE_ID}"

# The funnel reads a bounded recent window, so the fixture instants have to be
# recent. The FORMAT stays what Shoplazza documents: RFC-3339 with a Z.
_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


CREATED_AT = _iso(_NOW - timedelta(days=2))
PLACED_AT = _iso(_NOW - timedelta(days=2) + timedelta(minutes=1))
REFUND_ONE_AT = _iso(_NOW - timedelta(hours=6))
REFUND_TWO_AT = _iso(_NOW - timedelta(hours=3))


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "created_at": CREATED_AT,
        "placed_at": PLACED_AT,
        "updated_at": PLACED_AT,
        "currency": "USD",
        "total_price": "40.00",
        "real_total_paid": "40.00",
        "financial_status": "paid",
        "landing_site": "https://demo.myshoplaza.com/?pivota_click_id=clk_12345678",
        "customer": {"id": "buyer-2", "email": "private@example.com"},
        "line_items": [
            {"id": "line-2", "product_id": "product-2", "sku": "SKU-2", "quantity": 1}
        ],
        "payment_line": {"id": "payment-line-2", "transaction_no": "transaction-2"},
    }
    order.update(overrides)
    return order


def _refund_order(cumulative, updated_at, financial_status="partially_refunded"):
    return _order(
        updated_at=updated_at,
        financial_status=financial_status,
        total_refund_price=cumulative,
    )


class _AwareDatetimeRows:
    """SQLite drops the UTC offset on write; re-attach it on read.

    Production is Postgres `timestamptz`, which hands aware datetimes back, and
    the ledger compares an incoming `occurred_at` against the stored one. The
    sibling ledger fixtures dodge this by stripping tzinfo off the mapper's
    events before ingesting — impossible here, because the mapper runs INSIDE
    the route under test. So the offset is restored at the boundary where the
    SQLite dialect lost it, and nothing in the code under test changes.
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

    raw_database = databases.Database(f"sqlite+aiosqlite:///{db_path}")
    await raw_database.connect()
    test_database = _AwareDatetimeRows(raw_database)
    # The ledger read the receiver does BEFORE mapping, the advisory-lock
    # helper around it, and the write itself all resolve `database` from this
    # one module, so one patch covers the whole read-modify-write.
    monkeypatch.setattr(interaction_service, "database", test_database)
    monkeypatch.setattr(interaction_service, "IS_POSTGRES", False)
    monkeypatch.setattr(funnel_module, "database", test_database)
    return test_database


def _client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import shopline_family_webhooks as route

    class FakeStores:
        async def fetch_one(self, *args, **kwargs):
            return {
                "store_id": STORE_ID,
                "merchant_id": MERCHANT_ID,
                "domain": DOMAIN,
                "api_key": json.dumps({"app_secret": APP_SECRET}),
            }

    monkeypatch.setattr(route, "database", FakeStores())
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _post(client, order, *, topic, delivery_id):
    raw = json.dumps({"order": order}, separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(APP_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    return client.post(
        PATH,
        content=raw,
        headers={
            "X-Shoplazza-Hmac-Sha256": signature,
            "X-Shoplazza-Topic": topic,
            "X-Shoplazza-Deduplication-ID": delivery_id,
            "X-Shoplazza-Shop-Domain": DOMAIN,
            "Content-Type": "application/json",
        },
    )


async def _refund_rows(test_database):
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interaction_events

    rows = [
        dict(row)
        for row in await test_database.fetch_all(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.event_type == "refund.succeeded"
            )
        )
    ]
    return sorted(rows, key=lambda row: row["payload"]["amount_cents"])


@pytest.mark.asyncio
async def test_two_cumulative_refund_deliveries_record_their_deltas(tmp_path, monkeypatch):
    from sqlalchemy import select

    from db.commerce_interactions import commerce_interactions

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-deltas")
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

        paid = _post(client, _order(), topic="orders/paid", delivery_id="delivery-paid")
        assert paid.status_code == 200, paid.text
        assert paid.json()["accepted"] == 1

        first = _post(
            client,
            _refund_order("10.00", REFUND_ONE_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-1",
        )
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "recorded"
        assert first.json()["accepted"] == 1

        second = _post(
            client,
            _refund_order("25.00", REFUND_TWO_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-2",
        )
        assert second.status_code == 200, second.text
        assert second.json()["accepted"] == 1

        rows = await _refund_rows(test_database)
        assert [row["payload"]["amount_cents"] for row in rows] == [1000, 1500]
        assert [row["payload"]["refund_id"] for row in rows] == [
            f"{ORDER_ID}:1000",
            f"{ORDER_ID}:2500",
        ]
        assert {row["payload"]["currency"] for row in rows} == {"USD"}
        assert {row["order_ref"] for row in rows} == {f"shoplazza:{ORDER_ID}"}
        # The ingress stamped the provenance, not the payload.
        assert {row["write_path"] for row in rows} == {"shoplazza_webhook"}
        assert {row["authority"] for row in rows} == {"platform"}
        assert {
            row["payload"]["native_amount_semantics"] for row in rows
        } == {"cumulative_refund_total_delta"}

        interactions = [
            dict(row) for row in await test_database.fetch_all(select(commerce_interactions))
        ]
        assert len(interactions) == 1
        assert interactions[0]["order_ref"] == f"shoplazza:{ORDER_ID}"
        assert {row["interaction_id"] for row in rows} == {
            interactions[0]["interaction_id"]
        }
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_redelivery_of_the_same_cumulative_total_records_nothing(
    tmp_path, monkeypatch
):
    """Two ways the same total can come back, both of which must add no money.

    1. After the first write committed, the ledger read sees it and the mapper
       refuses: `refund_not_new`.
    2. If a redelivery RACED the read (the fallback on any engine without
       advisory locks), it still computes the same delta under the same
       deterministic key, and the ledger's first-write-wins collapses it.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-replay")
    try:
        client = _client(monkeypatch)
        _post(client, _order(), topic="orders/paid", delivery_id="delivery-paid")
        _post(
            client,
            _refund_order("10.00", REFUND_ONE_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-1",
        )

        # (1) A new delivery id, the same cumulative total, read after commit.
        replay = _post(
            client,
            _refund_order("10.00", REFUND_ONE_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-1-retry",
        )
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert body["status"] == "ignored"
        assert "refund_not_new" in body["reason"]
        assert len(await _refund_rows(test_database)) == 1

        # (2) The raced case: the same delivery mapped against a STALE
        # "previously recorded" figure of 0 still lands on `<order>:1000`.
        from services.merchant_event_ingest_service import ingest_merchant_event_batch
        from services.shopline_family_event_adapter import map_shoplazza_webhook

        raced = map_shoplazza_webhook(
            {"order": _refund_order("10.00", REFUND_ONE_AT)},
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-1-raced",
            store_id=STORE_ID,
            previously_recorded_refund_cents=0,
        )
        result = await ingest_merchant_event_batch(
            merchant_id=MERCHANT_ID,
            batch=raced,
            agent_identity_confidence="platform_asserted",
            write_path="shoplazza_webhook",
        )
        assert result["accepted"] == 0
        assert result["duplicates"] == 1
        assert len(await _refund_rows(test_database)) == 1
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_funnel_reports_the_full_refunded_gmv(tmp_path, monkeypatch):
    from services import merchant_commerce_event_funnel_service as funnel_service

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-funnel")
    try:
        client = _client(monkeypatch)
        _post(client, _order(), topic="orders/paid", delivery_id="delivery-paid")
        _post(
            client,
            _refund_order("10.00", REFUND_ONE_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-1",
        )
        _post(
            client,
            _refund_order("25.00", REFUND_TWO_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-2",
        )

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        assert result.payload["available"] is True
        summary = result.payload["summary"]
        assert summary["event_type_breakdown"]["refund.succeeded"] == 2
        # Two deltas under one authority SUM to the cumulative total the
        # platform last reported. Before this change the figure was absent
        # entirely: both rows carried amount_cents=None.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 2500}
        assert summary["paid_amount_cents_by_currency"] == {"USD": 4000}
        assert summary["stages"]["refunded"] >= 1
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_the_ledger_read_is_scoped_to_this_store_and_write_path(
    tmp_path, monkeypatch
):
    """A neighbouring store's refunds must not suppress this store's.

    `recorded_refund_amount_cents` is the only input to the subtraction, so a
    filter it gets wrong turns straight into money the funnel never counts.
    """
    from services.commerce_interaction_service import recorded_refund_amount_cents

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-scope")
    try:
        client = _client(monkeypatch)
        _post(client, _order(), topic="orders/paid", delivery_id="delivery-paid")
        _post(
            client,
            _refund_order("10.00", REFUND_ONE_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-1",
        )

        order_ref = f"shoplazza:{ORDER_ID}"
        exact = dict(
            merchant_id=MERCHANT_ID,
            store_id=STORE_ID,
            order_ref=order_ref,
            write_path="shoplazza_webhook",
        )
        assert await recorded_refund_amount_cents(**exact) == 1000
        assert await recorded_refund_amount_cents(**{**exact, "store_id": "other"}) == 0
        assert await recorded_refund_amount_cents(**{**exact, "merchant_id": "other"}) == 0
        assert (
            await recorded_refund_amount_cents(**{**exact, "write_path": "stripe_webhook"})
            == 0
        )
        assert (
            await recorded_refund_amount_cents(
                **{**exact, "order_ref": "shoplazza:some-other-order"}
            )
            == 0
        )
    finally:
        await test_database.disconnect()
