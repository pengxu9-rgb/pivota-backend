"""Production-dialect gate: a Stripe payment event resolves its order with metadata as a dict.

    createdb pivota_payment_order_metadata_test
    DATABASE_URL=postgresql://localhost/pivota_payment_order_metadata_test \\
        .venv/bin/python -m pytest tests/test_stripe_payment_order_metadata_postgres.py

WHAT WAS WRONG. `_resolve_stripe_order_for_payment_event` loads the order with a raw
`SELECT * FROM orders WHERE payment_intent_id = ...`. `orders.metadata` is a `json` column, and
through a raw query (and `_db_row_to_dict`, which reads `row._mapping`) asyncpg hands it back as
its JSON TEXT. The `metadata.order_id` fallback goes through `get_order`, which decodes it, so
the two lookups disagreed, and the PI lookup is the one every stored-PI event takes:

* `payment_intent.succeeded` read `skip_platform_order_creation` / `ops_canary` off
  `result.get("metadata")`, got a str, and treated it as `{}`: an order flagged only on the
  ORDER (not on the PaymentIntent) still had a Shopify order enqueued for it;
* `finalize_payment_success` / `finalize_payment_failure` built their metadata from `{}`.

WHY POSTGRES. The str-vs-dict shape is the asyncpg driver's; SQLite and dict-faking tests are
green on the broken code.

HOW IT IS DRIVEN. The real `/webhooks/stripe` route over `httpx.ASGITransport` (TestClient hangs
against the asyncpg pool) and a real `orders` row. Only the signature check, the best-effort side
ledgers, the merchant webhook, the evidence pack, and the Shopify store lookup / enqueue are
doubled. `orders` is shared with other gate modules: created `checkfirst`, never dropped, and
only this module's rows are deleted.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

SECRET = "whsec_payment_order_meta"
MERCHANT_ID = "m_payment_order_meta"
ORDER_ID = "ORD_PAYMENT_ORDER_META"
PAYMENT_INTENT = "pi_payment_order_meta"
ORDER_TOTAL = Decimal("42.00")

# Keys other writers own. A payment event must leave them exactly as they were.
FOREIGN_METADATA = {
    "merchant_order": {"id": "mo_payment_order_meta", "platform": "shopify"},
    "payment_recovery": {"attempts": 2},
    "agent_id": "agt_payment_order_meta",
}


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to write orders in database {dbname!r}; "
            "this gate must only run against a throwaway such as pivota_dialect_check"
        )


async def _delete_own_rows() -> None:
    from db.database import database

    await database.execute("DELETE FROM orders WHERE order_id = :o", {"o": ORDER_ID})


async def _insert_order(metadata: Dict[str, Any]) -> None:
    from db.database import database
    from db.orders import orders

    await database.execute(
        orders.insert().values(
            order_id=ORDER_ID,
            merchant_id=MERCHANT_ID,
            customer_email="buyer@example.com",
            shipping_address={},
            items=[],
            subtotal=ORDER_TOTAL,
            total=ORDER_TOTAL,
            total_refunded=Decimal("0"),
            currency="USD",
            status="pending",
            payment_status="unpaid",
            payment_intent_id=PAYMENT_INTENT,
            psp_used="stripe",
            metadata=metadata,
            # The model's default is Python-side, which `databases` binds as NULL.
            is_deleted=False,
            created_at=datetime.now(timezone.utc),
        )
    )


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy import create_engine

    from db.database import database, metadata
    from db.orders import orders

    _assert_throwaway_database()
    engine = create_engine(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        metadata.create_all(engine, tables=[orders], checkfirst=True)
    finally:
        engine.dispose()

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _delete_own_rows()
    yield
    await _delete_own_rows()
    if not was_connected and database.is_connected:
        await database.disconnect()


@pytest.fixture(autouse=True)
def enqueued(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    import routes.webhook_routes as webhook_routes

    calls: List[str] = []

    async def fake_record_event(**kwargs: Any) -> bool:
        return False  # each event below is a distinct Stripe event

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def shopify_store(merchant_id: str) -> Dict[str, Any]:
        return {"platform": "shopify", "merchant_id": merchant_id}

    async def record_enqueue(*, order_id: str, merchant_id: str, **kwargs: Any) -> None:
        calls.append(order_id)

    monkeypatch.setattr(webhook_routes.settings, "stripe_webhook_secret", SECRET, raising=False)
    monkeypatch.setattr(webhook_routes, "_record_stripe_webhook_event_best_effort", fake_record_event)
    monkeypatch.setattr(webhook_routes, "_mark_stripe_webhook_event_status_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "_record_stripe_canonical_event_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "_emit_stripe_merchant_webhook_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "create_order_snapshot_evidence_pack", noop)
    monkeypatch.setattr(webhook_routes, "log_order_event", noop)
    monkeypatch.setattr(webhook_routes, "get_primary_store", shopify_store)
    monkeypatch.setattr(webhook_routes, "enqueue_merchant_order_create", record_enqueue)
    return calls


_EVENT_SEQ = [0]


async def _send(monkeypatch: pytest.MonkeyPatch, event_type: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    import httpx
    from fastapi import FastAPI

    import routes.webhook_routes as webhook_routes

    _EVENT_SEQ[0] += 1
    event = {"id": f"evt_payment_meta_{_EVENT_SEQ[0]}", "type": event_type, "data": {"object": obj}}

    def fake_construct_event(payload: bytes, signature: Optional[str], secret: str) -> Dict[str, Any]:
        assert secret == SECRET
        return event

    monkeypatch.setattr(
        webhook_routes.stripe.Webhook, "construct_event", staticmethod(fake_construct_event)
    )
    app = FastAPI()
    app.include_router(webhook_routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_payment_meta"}',
            headers={"stripe-signature": "sig_payment_meta"},
        )
    assert resp.status_code == 200, (resp.status_code, resp.text)
    body = resp.json()
    assert body.get("status") != "unmatched", body
    return body


def _payment_intent(**extra: Any) -> Dict[str, Any]:
    return {
        "id": PAYMENT_INTENT,
        "object": "payment_intent",
        "amount": int(ORDER_TOTAL * 100),
        "amount_received": int(ORDER_TOTAL * 100),
        "currency": "usd",
        "metadata": {},  # nothing on the PaymentIntent: the ORDER carries the flags
        **extra,
    }


async def _stored() -> Dict[str, Any]:
    """The stored row, metadata decoded HERE (the test's own read, not the code under test)."""
    from db.database import database

    row = await database.fetch_one(
        "SELECT status, payment_status, metadata FROM orders WHERE order_id = :o", {"o": ORDER_ID}
    )
    assert row is not None
    raw = row["metadata"]
    return {
        "status": row["status"],
        "payment_status": row["payment_status"],
        "metadata": json.loads(raw) if isinstance(raw, str) else raw,
    }


# -- precondition: the shape the bug needs ------------------------------------


async def test_the_raw_payment_lookup_hands_back_metadata_as_text() -> None:
    """If this stops holding (e.g. someone registers an asyncpg JSON codec), the tests below no
    longer exercise the defect they guard, and must be revisited."""
    from db.database import database

    await _insert_order(FOREIGN_METADATA)
    row = await database.fetch_one(
        "SELECT * FROM orders WHERE payment_intent_id = :p", {"p": PAYMENT_INTENT}
    )
    assert isinstance(row["metadata"], str), type(row["metadata"])


# -- the resolver ---------------------------------------------------------------


async def test_the_payment_intent_lookup_returns_metadata_as_a_dict() -> None:
    from routes.webhook_routes import _resolve_stripe_order_for_payment_event

    await _insert_order(FOREIGN_METADATA)

    order, reject = await _resolve_stripe_order_for_payment_event(
        payment_intent_id=PAYMENT_INTENT, payment_meta=None, allow_repoint=False
    )

    assert reject is None
    assert order is not None and order["order_id"] == ORDER_ID
    assert order["metadata"] == FOREIGN_METADATA


# -- payment_intent.succeeded: order-level skip flags are honoured ---------------


@pytest.mark.parametrize("flag", ["skip_platform_order_creation", "ops_canary"])
async def test_an_order_level_skip_flag_stops_the_shopify_enqueue(
    monkeypatch: pytest.MonkeyPatch, enqueued: List[str], flag: str
) -> None:
    await _insert_order({**FOREIGN_METADATA, flag: True})

    await _send(monkeypatch, "payment_intent.succeeded", _payment_intent())

    stored = await _stored()
    assert stored["payment_status"] == "paid"  # the success path ran to the skip decision
    assert enqueued == []


async def test_without_a_skip_flag_the_shopify_order_is_still_enqueued(
    monkeypatch: pytest.MonkeyPatch, enqueued: List[str]
) -> None:
    """The control: without it, the test above would pass on a path that never reached the enqueue."""
    await _insert_order(FOREIGN_METADATA)

    await _send(monkeypatch, "payment_intent.succeeded", _payment_intent())

    assert (await _stored())["payment_status"] == "paid"
    assert enqueued == [ORDER_ID]


# -- payment_intent.payment_failed: other writers' keys survive ------------------


async def test_a_payment_failure_records_itself_and_keeps_other_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _insert_order(FOREIGN_METADATA)

    await _send(
        monkeypatch,
        "payment_intent.payment_failed",
        _payment_intent(last_payment_error={"message": "card_declined"}),
    )

    meta = (await _stored())["metadata"]
    assert "last_payment_failure" in meta, sorted(meta)
    for key, value in FOREIGN_METADATA.items():
        assert meta.get(key) == value, f"{key} was lost or changed: {sorted(meta)}"
