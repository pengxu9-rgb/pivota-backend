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

import asyncio
import base64
import hashlib
import hmac
import json
import logging
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


def _app(monkeypatch):
    from fastapi import FastAPI

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
    return app


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    return TestClient(_app(monkeypatch))


def _signed_request(order, *, topic, delivery_id):
    raw = json.dumps({"order": order}, separators=(",", ":")).encode("utf-8")
    signature = base64.b64encode(
        hmac.new(APP_SECRET.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    return raw, {
        "X-Shoplazza-Hmac-Sha256": signature,
        "X-Shoplazza-Topic": topic,
        "X-Shoplazza-Deduplication-ID": delivery_id,
        "X-Shoplazza-Shop-Domain": DOMAIN,
        "Content-Type": "application/json",
    }


def _post(client, order, *, topic, delivery_id):
    raw, headers = _signed_request(order, topic=topic, delivery_id=delivery_id)
    return client.post(PATH, content=raw, headers=headers)


async def _apost(client, order, *, topic, delivery_id):
    """The same signed delivery, awaited in THIS event loop.

    The race below needs two deliveries genuinely in flight at once, which
    `TestClient` cannot express: it drives the app through a portal and blocks.
    """
    raw, headers = _signed_request(order, topic=topic, delivery_id=delivery_id)
    return await client.post(PATH, content=raw, headers=headers)


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


@pytest.mark.asyncio
async def test_a_raced_pair_of_different_totals_inflates_the_refunded_gmv(
    tmp_path, monkeypatch
):
    """The failure mode `order_money_read_modify_write_lock` exists to prevent.

    This test DOCUMENTS a hazard, it does not accept one. The behaviour pinned
    below is wrong, and the Postgres advisory lock is what makes it
    unreachable in production; the assertions exist so that anyone who is
    tempted to call that lock an optimisation, or to run this write path on an
    engine without advisory locks, can see the number it produces.

    The original claim in this PR was that an unserialised pair "collapses to
    one row, understating rather than inflating". That is only true of a pair
    carrying the SAME cumulative total, which lands on one deterministic key.
    Two partial refunds moments apart carry DIFFERENT totals: below, 10.00 and
    25.00 both read a baseline of 0 and emit `<order>:1000` for 1000 and
    `<order>:2500` for 2500. Those are two distinct keys, nothing dedupes them,
    and the funnel sums them to 3500 — against a true cumulative of 2500, a 40%
    INFLATION of refunded GMV.

    The lock is a no-op here because `_sqlite_ledger` sets `IS_POSTGRES` False,
    which is exactly what the helper does on any engine without advisory locks.
    """
    import httpx

    from services import merchant_commerce_event_funnel_service as funnel_service
    from routes import shopline_family_webhooks as route

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-race")
    try:
        app = _app(monkeypatch)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # The order (and its interaction row) exists before the race, so
            # what races is only the refund read-modify-write.
            paid = await _apost(
                client, _order(), topic="orders/paid", delivery_id="delivery-paid"
            )
            assert paid.status_code == 200, paid.text

            baselines = []
            both_have_read = asyncio.Event()
            real_read = route.recorded_refund_amount_cents

            async def racing_read(**kwargs):
                """Hold each delivery at the point the real lock would block it."""
                value = await real_read(**kwargs)
                baselines.append(value)
                if len(baselines) == 2:
                    both_have_read.set()
                await asyncio.wait_for(both_have_read.wait(), timeout=10)
                return value

            monkeypatch.setattr(route, "recorded_refund_amount_cents", racing_read)

            first, second = await asyncio.gather(
                _apost(
                    client,
                    _refund_order("10.00", REFUND_ONE_AT),
                    topic="orders/partially_refunded",
                    delivery_id="delivery-refund-1",
                ),
                _apost(
                    client,
                    _refund_order("25.00", REFUND_TWO_AT),
                    topic="orders/partially_refunded",
                    delivery_id="delivery-refund-2",
                ),
            )

        # Both deliveries really did read the same stale baseline; without this
        # the rest of the test would pass for the ordinary sequential reason.
        assert baselines == [0, 0]
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert {first.json()["status"], second.json()["status"]} == {"recorded"}

        rows = await _refund_rows(test_database)
        # Two DISTINCT keys, so first-write-wins has nothing to collapse.
        assert [row["payload"]["amount_cents"] for row in rows] == [1000, 2500]
        assert sorted(row["payload"]["refund_id"] for row in rows) == [
            f"{ORDER_ID}:1000",
            f"{ORDER_ID}:2500",
        ]

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        summary = result.payload["summary"]
        # The platform's true cumulative refund is 2500. This is 3500.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 3500}
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_synthetic_refund_row_does_not_reduce_the_next_delta(
    tmp_path, monkeypatch
):
    """A probe must never be able to suppress real money.

    `recorded_refund_amount_cents` excludes `synthetic` rows. Without that
    filter, one canary refund written under this write path for a real order
    becomes a baseline the platform's next cumulative total has to exceed — and
    a canary large enough makes every subsequent real refund of that order
    `refund_not_new`, i.e. silently uncounted for good.
    """
    from services.merchant_event_ingest_service import ingest_merchant_event_batch
    from services.shopline_family_event_adapter import map_shoplazza_webhook

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-synthetic")
    try:
        client = _client(monkeypatch)
        _post(client, _order(), topic="orders/paid", delivery_id="delivery-paid")

        # A canary refund of 50.00 for the SAME order, same write path.
        canary = map_shoplazza_webhook(
            {"order": _refund_order("50.00", REFUND_ONE_AT)},
            topic="orders/partially_refunded",
            delivery_id="delivery-canary",
            store_id=STORE_ID,
            previously_recorded_refund_cents=0,
        )
        canary_result = await ingest_merchant_event_batch(
            merchant_id=MERCHANT_ID,
            batch=canary,
            agent_identity_confidence="platform_asserted",
            write_path="shoplazza_webhook",
            synthetic=True,
        )
        assert canary_result["accepted"] == 1

        # The row is really MARKED synthetic — otherwise the exclusion below
        # would be trivially satisfied and this test would prove nothing.
        rows = await _refund_rows(test_database)
        assert len(rows) == 1
        assert bool(rows[0]["synthetic"]) is True
        assert rows[0]["payload"]["amount_cents"] == 5000
        assert rows[0]["write_path"] == "shoplazza_webhook"
        assert rows[0]["order_ref"] == f"shoplazza:{ORDER_ID}"

        # A real 10.00 refund now. If the canary counted, previously would be
        # 5000 and this delivery would be ignored as `refund_not_new`.
        real = _post(
            client,
            _refund_order("10.00", REFUND_TWO_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-1",
        )
        assert real.status_code == 200, real.text
        assert real.json()["status"] == "recorded"
        assert real.json()["accepted"] == 1

        recorded = [row for row in await _refund_rows(test_database) if not row["synthetic"]]
        assert [row["payload"]["amount_cents"] for row in recorded] == [1000]
        assert [row["payload"]["refund_id"] for row in recorded] == [f"{ORDER_ID}:1000"]
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_prior_refund_in_another_currency_does_not_reduce_the_delta(
    tmp_path, monkeypatch
):
    """Subtraction is only meaningful inside one currency.

    3000 minor units of EUR are not 3000 minor units of USD. If a row in
    another currency were allowed into the baseline, this order's first USD
    refund would be ignored as `refund_not_new` and never counted at all.
    """
    from services.commerce_interaction_service import recorded_refund_amount_cents

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-currency")
    try:
        client = _client(monkeypatch)
        _post(client, _order(), topic="orders/paid", delivery_id="delivery-paid")

        eur = _post(
            client,
            _order(
                updated_at=REFUND_ONE_AT,
                currency="EUR",
                financial_status="partially_refunded",
                total_refund_price="30.00",
            ),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-eur",
        )
        assert eur.status_code == 200, eur.text
        assert eur.json()["status"] == "recorded"

        usd = _post(
            client,
            _refund_order("10.00", REFUND_TWO_AT),
            topic="orders/partially_refunded",
            delivery_id="delivery-refund-usd",
        )
        assert usd.status_code == 200, usd.text
        assert usd.json()["status"] == "recorded", usd.text
        assert usd.json()["accepted"] == 1

        rows = await _refund_rows(test_database)
        assert [(row["payload"]["currency"], row["payload"]["amount_cents"]) for row in rows] == [
            ("USD", 1000),
            ("EUR", 3000),
        ]

        scope = dict(
            merchant_id=MERCHANT_ID,
            store_id=STORE_ID,
            order_ref=f"shoplazza:{ORDER_ID}",
            write_path="shoplazza_webhook",
        )
        # The read itself, directly: each currency sees only its own rows, and
        # the match is case-insensitive because the caller passes whatever the
        # delivery said.
        assert await recorded_refund_amount_cents(**scope, currency="USD") == 1000
        assert await recorded_refund_amount_cents(**scope, currency="usd") == 1000
        assert await recorded_refund_amount_cents(**scope, currency="EUR") == 3000
        assert await recorded_refund_amount_cents(**scope, currency="JPY") == 0
        # No currency asked for is still every row, which is what makes the
        # filter above a real narrowing rather than a no-op.
        assert await recorded_refund_amount_cents(**scope) == 4000
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_a_refund_delivery_with_no_total_logs_but_a_redelivery_stays_quiet(
    tmp_path, monkeypatch, caplog
):
    """`refund_total_absent` is a platform contract break and must be audible.

    Both ignore reasons answer 2xx with `{"status": "ignored"}`, and the
    ingress metric labels every ignore identically (`outcome="ignored"`, no
    reason dimension). So a merchant whose deliveries stopped carrying
    `total_refund_price` would show zero refunded GMV with nothing at all to
    alert on. `refund_not_new` is ordinary expected traffic — every redelivery
    and every downward correction — and must stay quiet or it would drown the
    real signal.
    """
    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "sz-refund-log")
    try:
        client = _client(monkeypatch)
        _post(client, _order(), topic="orders/paid", delivery_id="delivery-paid")

        with caplog.at_level(logging.WARNING, logger="shopline_family_webhooks"):
            caplog.clear()
            absent = _post(
                client,
                _order(updated_at=REFUND_ONE_AT, financial_status="refunding"),
                topic="orders/partially_refunded",
                delivery_id="delivery-refund-no-total",
            )
            assert absent.status_code == 200, absent.text
            assert absent.json()["status"] == "ignored"
            assert "refund_total_absent" in absent.json()["reason"]

            warnings = [
                record
                for record in caplog.records
                if record.name == "shopline_family_webhooks"
                and record.levelno == logging.WARNING
            ]
            assert len(warnings) == 1
            message = warnings[0].getMessage()
            assert "total_refund_price" in message
            assert f"store_id={STORE_ID}" in message
            assert f"order_ref=shoplazza:{ORDER_ID}" in message
            assert "topic=orders/partially_refunded" in message

            # And the quiet case: a real refund, then the same total again.
            caplog.clear()
            recorded = _post(
                client,
                _refund_order("10.00", REFUND_ONE_AT),
                topic="orders/partially_refunded",
                delivery_id="delivery-refund-1",
            )
            assert recorded.json()["status"] == "recorded"
            replay = _post(
                client,
                _refund_order("10.00", REFUND_ONE_AT),
                topic="orders/partially_refunded",
                delivery_id="delivery-refund-1-retry",
            )
            assert replay.json()["status"] == "ignored"
            assert "refund_not_new" in replay.json()["reason"]
            assert [
                record
                for record in caplog.records
                if record.name == "shopline_family_webhooks"
            ] == []
    finally:
        await test_database.disconnect()
