"""PrestaShop telemetry through the REAL ledger tables on SQLite.

The mapper tests prove the shape of each event; this file proves what the
ledger and the funnel do with them. The PrestaShop wrinkle its siblings do not
have: a refund is a *credit slip*, and PrestaShop also has an order STATE
called refund. Two partial refunds of one order are two slips, and the state
transition that usually accompanies them must add nothing — otherwise every
refunded order is counted twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# The funnel reads a bounded recent window, so the fixture instants have to be
# recent. The FORMAT stays what the module emits: `gmdate('c')`.
_NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


CREATED_AT = _iso(_NOW - timedelta(days=2))
REFUND_ONE_AT = _iso(_NOW - timedelta(hours=6))
REFUND_TWO_AT = _iso(_NOW - timedelta(hours=3))

MERCHANT_ID = "merchant-prestashop"
STORE_ID = "store-prestashop"
ORDER_ID = 1042


def _order(**overrides):
    order = {
        "id": ORDER_ID,
        "reference": "XKBKNABJK",
        "id_cart": 55,
        "id_customer": 9,
        "currency": "USD",
        "current_state": 2,
        "state_key": "payment",
        "state_flags": {"paid": True, "shipped": False, "delivery": False, "logable": True},
        "valid": True,
        "total_paid_tax_incl": "40.56",
        "total_paid_real": "40.56",
        "payment_module": "ps_checkout",
        "date_add": "2026-09-01 10:00:00",
        "date_upd": "2026-09-02 10:00:00",
    }
    order.update(overrides)
    return order


def _slip_event(slip_id, products, occurred_at):
    return {
        "event_id": f"actionOrderSlipAdd:{ORDER_ID}:{slip_id}",
        "hook": "actionOrderSlipAdd",
        "occurred_at": occurred_at,
        "order": _order(),
        "order_slip": {
            "id": slip_id,
            "amount": "0.00",
            "shipping_cost_amount": "0.00",
            "total_products_tax_incl": products,
            "total_shipping_tax_incl": "0.00",
            "date_add": "2026-09-03 10:00:00",
        },
    }


def _created_event():
    return {
        "event_id": f"actionValidateOrder:{ORDER_ID}:2",
        "hook": "actionValidateOrder",
        "occurred_at": CREATED_AT,
        "order": _order(),
        "order_slip": None,
    }


def _refund_state_event():
    """The state flip that usually accompanies a refund. It must add nothing."""
    return {
        "event_id": f"actionOrderStatusPostUpdate:{ORDER_ID}:7",
        "hook": "actionOrderStatusPostUpdate",
        "occurred_at": REFUND_TWO_AT,
        "order": _order(
            current_state=7,
            state_key="refund",
            state_flags={"paid": False, "shipped": False, "delivery": False, "logable": True},
        ),
        "order_slip": None,
    }


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


async def _ingest(module_events, *, delivery_id):
    from services.merchant_event_ingest_service import (
        MerchantEventBatch,
        ingest_merchant_event_batch,
    )
    from services.prestashop_event_adapter import (
        UnsupportedPrestaShopEvent,
        map_prestashop_module_event,
    )

    mapped = []
    for event in module_events:
        try:
            mapped.extend(
                map_prestashop_module_event(
                    event, store_id=STORE_ID, delivery_id=delivery_id
                )
            )
        except UnsupportedPrestaShopEvent:
            continue
    if not mapped:
        return {"accepted": 0, "duplicates": 0, "events": []}
    # SQLite's DATETIME binding refuses tz-aware values; strip AFTER the real
    # mapper has run so its own normalization is still exercised.
    for event in mapped:
        event.occurred_at = event.occurred_at.replace(tzinfo=None)
    return await ingest_merchant_event_batch(
        merchant_id=MERCHANT_ID,
        batch=MerchantEventBatch(events=mapped),
        agent_identity_confidence="platform_asserted",
        write_path="prestashop_module",
    )


@pytest.mark.asyncio
async def test_two_credit_slips_land_on_one_interaction_and_replay_dedupes(
    tmp_path, monkeypatch
):
    from sqlalchemy import select

    from db.commerce_interactions import (
        commerce_interaction_events,
        commerce_interactions,
    )

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "prestashop-slips")
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

        await _ingest([_created_event()], delivery_id="delivery-1")
        await _ingest(
            [_slip_event(77, "10.50", REFUND_ONE_AT)],
            delivery_id="delivery-2",
        )
        await _ingest(
            [_slip_event(78, "5.00", REFUND_TWO_AT), _refund_state_event()],
            delivery_id="delivery-3",
        )

        interactions = [
            dict(row) for row in await test_database.fetch_all(select(commerce_interactions))
        ]
        assert len(interactions) == 1
        interaction = interactions[0]
        assert interaction["order_id"] == str(ORDER_ID)
        assert interaction["order_ref"] == f"prestashop:{ORDER_ID}"

        refund_rows = [
            dict(row)
            for row in await test_database.fetch_all(
                select(commerce_interaction_events).where(
                    commerce_interaction_events.c.event_type == "refund.succeeded"
                )
            )
        ]
        # TWO, not three: the `refund` STATE transition delivered alongside the
        # second slip contributed nothing.
        assert len(refund_rows) == 2
        assert {row["interaction_id"] for row in refund_rows} == {
            interaction["interaction_id"]
        }
        assert sorted(row["payload"]["refund_id"] for row in refund_rows) == ["77", "78"]
        assert sorted(row["payload"]["amount_cents"] for row in refund_rows) == [500, 1050]
        # The ingress stamped the provenance, not the payload.
        assert {row["write_path"] for row in refund_rows} == {"prestashop_module"}
        assert {row["authority"] for row in refund_rows} == {"platform"}

        # A redelivered batch (the module retries anything it could not confirm)
        # must report duplicates, even though its delivery id is new.
        replay = await _ingest(
            [
                _created_event(),
                _slip_event(77, "10.50", REFUND_ONE_AT),
                _slip_event(78, "5.00", REFUND_TWO_AT),
            ],
            delivery_id="delivery-4",
        )
        assert replay["accepted"] == 0
        assert all(result["duplicate"] for result in replay["events"])

        after_replay = await test_database.fetch_all(
            select(commerce_interaction_events).where(
                commerce_interaction_events.c.event_type == "refund.succeeded"
            )
        )
        assert len(after_replay) == 2
    finally:
        await test_database.disconnect()


@pytest.mark.asyncio
async def test_funnel_sums_both_credit_slips_under_one_order_ref(tmp_path, monkeypatch):
    from services import merchant_commerce_event_funnel_service as funnel_service

    test_database = await _sqlite_ledger(tmp_path, monkeypatch, "prestashop-funnel")
    try:
        await _ingest([_created_event()], delivery_id="delivery-1")
        await _ingest([_slip_event(77, "10.50", REFUND_ONE_AT)], delivery_id="delivery-2")
        await _ingest(
            [_slip_event(78, "5.00", REFUND_TWO_AT), _refund_state_event()],
            delivery_id="delivery-3",
        )

        result = await funnel_service.get_merchant_commerce_event_funnel(
            merchant_id=MERCHANT_ID,
            group_by="store",
        )
        assert result.payload["available"] is True
        summary = result.payload["summary"]
        assert summary["event_type_breakdown"]["refund.succeeded"] == 2
        # Both refunds carry the same authority, so they SUM rather than take
        # the max a PSP mirror would.
        assert summary["refunded_amount_cents_by_currency"] == {"USD": 1550}
        assert summary["paid_amount_cents_by_currency"] == {"USD": 4056}
        assert result.payload["slices"][0]["key"] == STORE_ID
    finally:
        await test_database.disconnect()
