"""Production-dialect gate: Stripe refund events keep the order's metadata, and its refund book.

    createdb pivota_refund_order_metadata_test
    DATABASE_URL=postgresql://localhost/pivota_refund_order_metadata_test \\
        .venv/bin/python -m pytest tests/test_stripe_refund_order_metadata_postgres.py

WHAT WAS WRONG. `_resolve_stripe_order_for_refund` loads the order with a raw
`SELECT * FROM orders WHERE payment_intent_id = ...`, and `orders.metadata` is a
`json` column: through a raw query (and through `_db_row_to_dict`, which reads
`row._mapping`) asyncpg hands it back as its JSON TEXT. Every refund consumer
treats a non-dict as `{}`:

* `finalize_refund_success` rebuilt `psp_refund_refs` / `psp_refund_records` from
  nothing on every event;
* `_stripe_refund_level_cumulative` never summed an earlier `refund.updated` row, so
  $300 then $200 reported only by `refund.updated` landed at total_refunded = 300;
* `_persist_stripe_refund_observability` and `_finalize_stripe_refund_failure` write
  through `update_order`, which REPLACES the column, so a `refund.created`, a pending
  `refund.updated` or a `refund.failed` erased every other metadata key;
* the `refund.failed` rollback never found the refund it was rolling back.

The `metadata.order_id` fallback goes through `get_order`, which returns a decoded
dict; only the `payment_intent_id` lookup (every event Stripe sends with one) was hit.

WHY POSTGRES. The str-vs-dict shape is the asyncpg driver's.
tests/test_stripe_refund_partial_accounting.py fakes `fetch_one` with a dict and
is green on the broken code; so is SQLite.

HOW IT IS DRIVEN. The real `/webhooks/stripe` route over `httpx.ASGITransport`
(TestClient hangs against the asyncpg pool) and a real `orders` row. Only the
signature check and the best-effort side ledgers (webhook-event dedupe, canonical
event, order_events) are doubled, and the attribution edge is switched off with
its own env flag (tests/test_stripe_refund_attribution_edge_postgres.py owns it).
`orders` is shared with other gate modules: created `checkfirst`, never dropped,
and only this module's rows are deleted.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs the Postgres DATABASE_URL supplied by postgres-dialect-gate",
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

SECRET = "whsec_refund_order_meta"
MERCHANT_ID = "m_refund_order_meta"
ORDER_ID = "ORD_REFUND_ORDER_META"
PAYMENT_INTENT = "pi_refund_order_meta"
CHARGE_ID = "ch_refund_order_meta"
ORDER_TOTAL = Decimal("500.00")

# Keys other writers own. Every refund event must leave them exactly as they were.
FOREIGN_METADATA = {
    "merchant_order": {"id": "mo_refund_order_meta", "platform": "shopify"},
    "payment_recovery": {"attempts": 2},
    "agent_id": "agt_refund_order_meta",
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


@pytest.fixture(autouse=True)
async def _db(monkeypatch: pytest.MonkeyPatch):
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
            status="paid",
            payment_status="paid",
            payment_intent_id=PAYMENT_INTENT,
            psp_used="stripe",
            metadata=FOREIGN_METADATA,
            # As create_order does: the model's default is Python-side, which `databases`
            # binds as NULL, and update_order only writes rows where is_deleted IS false.
            is_deleted=False,
            created_at=datetime.now(timezone.utc),
        )
    )
    yield
    await _delete_own_rows()
    if not was_connected and database.is_connected:
        await database.disconnect()


@pytest.fixture(autouse=True)
def _doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.webhook_routes as webhook_routes

    async def fake_record_event(**kwargs: Any) -> bool:
        return False  # each event below is a distinct Stripe event

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setenv("ATTRIBUTION_REVERSE_ON_REFUND", "false")
    monkeypatch.setattr(webhook_routes.settings, "stripe_webhook_secret", SECRET, raising=False)
    monkeypatch.setattr(webhook_routes, "_record_stripe_webhook_event_best_effort", fake_record_event)
    monkeypatch.setattr(webhook_routes, "_mark_stripe_webhook_event_status_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "_record_stripe_canonical_event_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "log_order_event", noop)


def _app():
    from fastapi import FastAPI

    import routes.webhook_routes as webhook_routes

    app = FastAPI()
    app.include_router(webhook_routes.router)
    return app


_EVENT_SEQ = [0]


async def _send(monkeypatch: pytest.MonkeyPatch, event_type: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    import httpx

    import routes.webhook_routes as webhook_routes

    _EVENT_SEQ[0] += 1
    event = {"id": f"evt_refund_meta_{_EVENT_SEQ[0]}", "type": event_type, "data": {"object": obj}}

    def fake_construct_event(payload: bytes, signature: Optional[str], secret: str) -> Dict[str, Any]:
        assert secret == SECRET
        return event

    monkeypatch.setattr(
        webhook_routes.stripe.Webhook, "construct_event", staticmethod(fake_construct_event)
    )
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/stripe",
            content=b'{"id":"evt_refund_meta"}',
            headers={"stripe-signature": "sig_refund_meta"},
        )
    assert resp.status_code == 200, (resp.status_code, resp.text)
    body = resp.json()
    assert body.get("status") != "unmatched", body
    return body


def _charge_refunded(amount_refunded_minor: int) -> Dict[str, Any]:
    return {
        "id": CHARGE_ID,
        "object": "charge",
        "payment_intent": PAYMENT_INTENT,
        "amount": int(ORDER_TOTAL * 100),
        "amount_refunded": amount_refunded_minor,
        "currency": "usd",
    }


def _refund(refund_id: str, amount_minor: int, status: str, **extra: Any) -> Dict[str, Any]:
    return {
        "id": refund_id,
        "object": "refund",
        "status": status,
        "payment_intent": PAYMENT_INTENT,
        "charge": CHARGE_ID,
        "amount": amount_minor,
        "currency": "usd",
        "metadata": {},
        **extra,
    }


async def _order() -> Dict[str, Any]:
    """The stored row, metadata decoded HERE (the test's own read, not the code under test)."""
    from db.database import database

    row = await database.fetch_one(
        "SELECT total_refunded, payment_status, metadata FROM orders WHERE order_id = :o",
        {"o": ORDER_ID},
    )
    assert row is not None
    raw = row["metadata"]
    return {
        "total_refunded": Decimal(str(row["total_refunded"])),
        "payment_status": row["payment_status"],
        "metadata": json.loads(raw) if isinstance(raw, str) else raw,
    }


def _assert_foreign_keys_survive(meta: Dict[str, Any]) -> None:
    for key, value in FOREIGN_METADATA.items():
        assert meta.get(key) == value, f"{key} was lost or changed: {sorted(meta)}"


# -- precondition: the shape the bug needs ------------------------------------


@pytest.mark.asyncio
async def test_the_raw_refund_lookup_hands_back_metadata_as_text() -> None:
    """If this stops holding (e.g. someone registers an asyncpg JSON codec), the
    tests below no longer exercise the defect they guard, and must be revisited."""
    from db.database import database

    row = await database.fetch_one(
        "SELECT * FROM orders WHERE payment_intent_id = :p", {"p": PAYMENT_INTENT}
    )
    assert isinstance(row["metadata"], str), type(row["metadata"])


# -- success: the refund book accumulates -------------------------------------


@pytest.mark.asyncio
async def test_two_refund_updated_partials_accumulate_and_keep_other_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """$300 then $200, reported ONLY by refund.updated (each carries one refund's
    amount). Before the fix the second event saw {} and landed at max(300, 200)."""
    await _send(monkeypatch, "refund.updated", _refund("re_first", 30000, "succeeded"))
    first = await _order()
    assert first["total_refunded"] == Decimal("300")
    assert first["payment_status"] == "partially_refunded"

    await _send(monkeypatch, "refund.updated", _refund("re_second", 20000, "succeeded"))
    order = await _order()
    meta = order["metadata"]
    assert order["total_refunded"] == Decimal("500"), meta.get("psp_refund_records")
    assert order["payment_status"] == "refunded"
    assert meta["psp_refund_refs"] == ["stripe:re_first", "stripe:re_second"]
    assert set(meta["psp_refund_records"]) == {"stripe:re_first", "stripe:re_second"}
    assert int(meta["psp_refund_records"]["stripe:re_first"]["amount_minor"]) == 30000
    assert set(meta["stripe_refund_statuses"]) == {"re_first", "re_second"}
    _assert_foreign_keys_survive(meta)


@pytest.mark.asyncio
async def test_charge_refunded_then_its_refund_updated_keep_both_rows_and_count_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))
    await _send(monkeypatch, "refund.updated", _refund("re_one", 30000, "succeeded"))

    order = await _order()
    meta = order["metadata"]
    assert order["total_refunded"] == Decimal("300")
    assert meta["psp_refund_refs"] == [f"stripe:{CHARGE_ID}", "stripe:re_one"]
    assert set(meta["psp_refund_records"]) == {f"stripe:{CHARGE_ID}", "stripe:re_one"}
    _assert_foreign_keys_survive(meta)


# -- the update_order full-replace paths --------------------------------------


@pytest.mark.asyncio
async def test_refund_created_and_pending_refund_updated_keep_the_refund_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_persist_stripe_refund_observability` writes through update_order, which
    replaces the whole column: before the fix it wrote the patch over {}."""
    await _send(monkeypatch, "refund.updated", _refund("re_done", 10000, "succeeded"))
    await _send(monkeypatch, "refund.created", _refund("re_next", 5000, "pending"))
    await _send(
        monkeypatch,
        "refund.updated",
        _refund("re_next", 5000, "pending", pending_reason="processing"),
    )

    order = await _order()
    meta = order["metadata"]
    assert order["total_refunded"] == Decimal("100")
    assert meta["psp_refund_refs"] == ["stripe:re_done"]
    assert set(meta["psp_refund_records"]) == {"stripe:re_done"}
    assert set(meta["stripe_refund_statuses"]) == {"re_done", "re_next"}
    assert meta["stripe_refund_statuses"]["re_next"]["status"] == "pending"
    _assert_foreign_keys_survive(meta)


@pytest.mark.asyncio
async def test_refund_failed_for_an_unrecorded_refund_keeps_the_refund_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refund.failed for a refund that never succeeded takes the update_order
    branch of `_finalize_stripe_refund_failure`: nothing to roll back."""
    await _send(monkeypatch, "refund.updated", _refund("re_kept", 20000, "succeeded"))
    await _send(
        monkeypatch,
        "refund.failed",
        _refund("re_never", 5000, "failed", failure_reason="expired_or_canceled_card"),
    )

    order = await _order()
    meta = order["metadata"]
    assert order["total_refunded"] == Decimal("200")
    assert order["payment_status"] == "partially_refunded"
    assert meta["psp_refund_refs"] == ["stripe:re_kept"]
    assert set(meta["psp_refund_records"]) == {"stripe:re_kept"}
    assert meta["stripe_last_refund_failure"]["refund_id"] == "re_never"
    assert meta["stripe_last_refund_failure"]["failure_reason"] == "expired_or_canceled_card"
    _assert_foreign_keys_survive(meta)


# -- the refund.failed rollback ----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["refund.failed", "refund.updated"])
async def test_a_failed_refund_that_had_succeeded_is_rolled_back(
    monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    """Stripe can fail a refund after reporting it succeeded (the money comes back
    to the merchant). The rollback keys on psp_refund_records, so before the fix it
    never found the refund on Postgres and total_refunded stayed overstated.

    NOTE: the attribution edge is a ceiling that never lowers; it does NOT follow
    this rollback (switched off here, see the module docstring)."""
    await _send(monkeypatch, "refund.updated", _refund("re_stays", 10000, "succeeded"))
    await _send(monkeypatch, "refund.updated", _refund("re_bounces", 30000, "succeeded"))
    assert (await _order())["total_refunded"] == Decimal("400")

    await _send(
        monkeypatch,
        event_type,
        _refund("re_bounces", 30000, "failed", failure_reason="lost_or_stolen_card"),
    )

    order = await _order()
    meta = order["metadata"]
    assert order["total_refunded"] == Decimal("100")
    assert order["payment_status"] == "partially_refunded"
    assert meta["psp_refund_refs"] == ["stripe:re_stays"]
    assert set(meta["psp_refund_records"]) == {"stripe:re_stays"}
    assert meta["last_refund_failure"]["rollback_reference"] == "re_bounces"
    _assert_foreign_keys_survive(meta)

    # A later partial must sum only what is still refunded: 100 + 50, not 450.
    await _send(monkeypatch, "refund.updated", _refund("re_after", 5000, "succeeded"))
    assert (await _order())["total_refunded"] == Decimal("150")


@pytest.mark.asyncio
async def test_an_order_resolved_through_the_metadata_order_id_hint_accumulates_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refund whose payment_intent we do not track falls back to
    metadata.order_id -> get_order. Measured: get_order returns `dict(Record)`,
    which IS decoded, so this path was never broken; it is pinned so the two
    lookups cannot drift apart (e.g. if get_order moves to a raw query)."""

    def hinted(refund_id: str, amount_minor: int) -> Dict[str, Any]:
        return _refund(
            refund_id,
            amount_minor,
            "succeeded",
            payment_intent="pi_refund_order_meta_untracked",
            metadata={"order_id": ORDER_ID},
        )

    await _send(monkeypatch, "refund.updated", hinted("re_hint_a", 30000))
    await _send(monkeypatch, "refund.updated", hinted("re_hint_b", 20000))

    order = await _order()
    meta = order["metadata"]
    assert order["total_refunded"] == Decimal("500"), meta.get("psp_refund_records")
    assert meta["psp_refund_refs"] == ["stripe:re_hint_a", "stripe:re_hint_b"]
    _assert_foreign_keys_survive(meta)
