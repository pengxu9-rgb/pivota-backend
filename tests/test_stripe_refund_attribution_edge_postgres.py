"""Production-dialect gate: a Stripe refund must reach the attribution edge ONCE.

    DATABASE_URL=postgresql://localhost/refund_edge_overcount_test \
        .venv/bin/python -m pytest tests/test_stripe_refund_attribution_edge_postgres.py

WHAT IS UNDER TEST. `commerce_attribution_edges.refund_amount_cents` feeds the
stored generated column `net_attributed_gmv_cents` (migration 109), which is
what the merchant's monthly statement bills. Stripe reports one refund under
several ids:

* `charge.refunded` carries the charge id (ch_…) and `amount_refunded`, the
  charge's CUMULATIVE total, so a second partial refund reuses the same ch_.
* `refund.updated` (status=succeeded) carries the refund id (re_…) and that ONE
  refund's amount.
* `RefundService.create_refund` records a merchant refund under its own REF_… id.

These writers used to ADD each id's amount through `_ATTRIBUTE_REFUND_QUERY`,
which dedupes on the id alone. Measured here before the fix, on a $500 order:
one $300 refund landed at 600, $300 + $200 at 800 (or 300 when only
charge.refunded arrived), and a merchant $300 refund at 900. The order's
`total_refunded` was right every time (tests/test_stripe_refund_partial_accounting.py),
so the edge now takes that total as a CEILING
(`apply_refund_total_to_attribution_edge`). Chargebacks are not in
`total_refunded`, so they stay additive and are kept apart in
`dispute_amount_cents` (migration 236).

HOW IT IS DRIVEN. The real `/webhooks/stripe` route over `httpx.ASGITransport`
(TestClient hangs against the asyncpg pool), a real `orders` row, a real edge
row, and the real edge SQL. Only the signature check and the
best-effort side ledgers (webhook-event dedupe, canonical event, order_events,
the interaction-ledger emit) are doubled; none of them feeds the edge.

TABLE HYGIENE. `orders` and `commerce_attribution_edges` are shared with other
gate modules, so they are created `checkfirst` and never dropped; this module
deletes only its own rows. The migration-109 refund columns the model does not
declare are added with `ADD COLUMN IF NOT EXISTS` exactly as the migration does,
and migration 236 is applied from its own file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
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

SECRET = "whsec_edge_overcount"
MERCHANT_ID = "m_edge_overcount"
ORDER_ID = "ORD_EDGE_OVERCOUNT"
PAYMENT_INTENT = "pi_edge_overcount"
CHARGE_ID = "ch_edge_overcount"
EDGE_ID = "edge_edge_overcount"
ORDER_TOTAL = Decimal("500.00")
GROSS_CENTS = 50000


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to write orders/edges in database {dbname!r}; "
            "this gate must only run against a throwaway such as pivota_dialect_check"
        )


# Migration 109's refund columns. The model does not declare them, so
# `create_all` alone builds an edge table `_ATTRIBUTE_REFUND_QUERY` cannot write.
_EDGE_MIGRATION_109_COLUMNS = """
ALTER TABLE commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS gross_attributed_gmv_cents BIGINT,
  ADD COLUMN IF NOT EXISTS refund_amount_cents BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ
"""
_EDGE_MIGRATION_109_NET = """
ALTER TABLE commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS net_attributed_gmv_cents BIGINT GENERATED ALWAYS AS (
    CASE
      WHEN gross_attributed_gmv_cents IS NULL THEN NULL
      ELSE GREATEST(gross_attributed_gmv_cents - refund_amount_cents, 0)
    END
  ) STORED
"""

_MIGRATION_236 = (
    Path(__file__).resolve().parents[1]
    / "db/migrations/236_commerce_attribution_edges_dispute_amount.sql"
)

# db/migrations/001_add_refund_tables.sql, the only DDL owner RefundService has.
_REFUND_RECORDS_DDL = """
CREATE TABLE IF NOT EXISTS refund_records (
    refund_id VARCHAR(50) PRIMARY KEY,
    order_id VARCHAR(50) NOT NULL,
    merchant_id VARCHAR(50) NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD',
    reason VARCHAR(100),
    source VARCHAR(50),
    status VARCHAR(50) DEFAULT 'pending',
    platform_type VARCHAR(50),
    platform_refund_id VARCHAR(255),
    platform_sync_status VARCHAR(50),
    psp_type VARCHAR(50),
    psp_refund_id VARCHAR(255),
    raw_payload JSONB,
    created_by VARCHAR(255),
    error_message TEXT,
    idempotency_key VARCHAR(255) UNIQUE,
    created_at TIMESTAMP DEFAULT NOW(),
    processed_at TIMESTAMP
)
"""


async def _delete_own_rows() -> None:
    from db.database import database

    await database.execute(
        "DELETE FROM refund_records WHERE order_id = :o", {"o": ORDER_ID}
    )
    await database.execute(
        "DELETE FROM commerce_attribution_edges WHERE order_id = :o", {"o": ORDER_ID}
    )
    await database.execute("DELETE FROM orders WHERE order_id = :o", {"o": ORDER_ID})


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy import create_engine

    from db.commerce_attribution import commerce_attribution_edges
    from db.database import database, metadata
    from db.orders import orders

    _assert_throwaway_database()
    engine = create_engine(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        metadata.create_all(engine, tables=[orders, commerce_attribution_edges], checkfirst=True)
        with engine.begin() as conn:
            conn.exec_driver_sql(_EDGE_MIGRATION_109_COLUMNS)
            conn.exec_driver_sql(_EDGE_MIGRATION_109_NET)
        # The real migration file, run the way db/sql_migrations.py runs it: the
        # whole body on a raw cursor, in one transaction.
        raw = engine.raw_connection()
        try:
            raw.cursor().execute(_MIGRATION_236.read_text())
            raw.commit()
        finally:
            raw.close()
    finally:
        engine.dispose()

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(_REFUND_RECORDS_DDL)
    await _delete_own_rows()

    now = datetime.now(timezone.utc)
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
            metadata={},
            created_at=now,
        )
    )
    await database.execute(
        commerce_attribution_edges.insert().values(
            edge_id=EDGE_ID,
            merchant_id=MERCHANT_ID,
            order_id=ORDER_ID,
            surface="agent",
            refund_count=0,
            refunded_amount=0,
            created_at=now,
            updated_at=now,
        )
    )
    await database.execute(
        "UPDATE commerce_attribution_edges SET gross_attributed_gmv_cents = :g WHERE edge_id = :e",
        {"g": GROSS_CENTS, "e": EDGE_ID},
    )
    yield
    await _delete_own_rows()
    if not was_connected and database.is_connected:
        await database.disconnect()


# -- the real route ---------------------------------------------------------


@pytest.fixture
def ledger_emits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Double the best-effort side ledgers; record the interaction-ledger emits.

    `record_commerce_event_best_effort` is the interaction-ledger write
    `attach_refund_to_attribution_edge` makes once per refund id. It is doubled
    (it writes tables this gate does not own) but RECORDED, because its
    `upstream_idempotency_key=refund:<id>` inherits the edge's keying.
    """
    import routes.webhook_routes as webhook_routes
    import services.commerce_attribution_service as attribution
    import services.dispute_records_service as dispute_records
    import services.pcs_evidence_pack_service as evidence_packs
    import services.refund_service as refund_service

    emits: List[Dict[str, Any]] = []

    async def fake_emit(**kwargs: Any) -> None:
        emits.append(kwargs)
        return None

    async def fake_record_event(**kwargs: Any) -> bool:
        return False  # each event below is a distinct Stripe event

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(webhook_routes.settings, "stripe_webhook_secret", SECRET, raising=False)
    monkeypatch.setattr(webhook_routes, "_record_stripe_webhook_event_best_effort", fake_record_event)
    monkeypatch.setattr(webhook_routes, "_mark_stripe_webhook_event_status_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "_record_stripe_canonical_event_best_effort", noop)
    monkeypatch.setattr(webhook_routes, "log_order_event", noop)
    monkeypatch.setattr(attribution, "record_commerce_event_best_effort", fake_emit)

    # The rollup re-roll after each write is covered on Postgres by
    # tests/test_refund_rollup_recompute_postgres.py. Here it would write
    # gmv_attribution_daily rows into a database other gate modules share.
    async def no_recompute(rows: Any) -> Dict[Any, str]:
        return {}

    monkeypatch.setattr(attribution, "recompute_days_for_edges", no_recompute)
    monkeypatch.setattr(refund_service, "recompute_days_for_edges", no_recompute)
    # The dispute branch's own records (not the edge) live in tables this gate
    # does not build.
    monkeypatch.setattr(dispute_records, "upsert_stripe_dispute_record_best_effort", noop)
    monkeypatch.setattr(evidence_packs, "create_dispute_evidence_pack", noop)
    return emits


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
    event = {"id": f"evt_edge_{_EVENT_SEQ[0]}", "type": event_type, "data": {"object": obj}}

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
            content=b'{"id":"evt_edge"}',
            headers={"stripe-signature": "sig_edge"},
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


def _refund(refund_id: str, amount_minor: int, status: str) -> Dict[str, Any]:
    return {
        "id": refund_id,
        "object": "refund",
        "status": status,
        "payment_intent": PAYMENT_INTENT,
        "charge": CHARGE_ID,
        "amount": amount_minor,
        "currency": "usd",
        "metadata": {},
    }


def _dispute(dispute_id: str, amount_minor: int) -> Dict[str, Any]:
    return {
        "id": dispute_id,
        "object": "dispute",
        "status": "needs_response",
        "charge": CHARGE_ID,
        "payment_intent": PAYMENT_INTENT,
        "amount": amount_minor,
        "currency": "usd",
        # The bare /stripe endpoint resolves a dispute's order from metadata.
        "metadata": {"order_id": ORDER_ID, "merchant_id": MERCHANT_ID},
    }


def _emitted(emits: List[Dict[str, Any]]) -> List[tuple]:
    """(refund id, amount) of each refund.succeeded the edge writers emitted."""
    return [
        (e["metadata"]["refund_id"], Decimal(str(e["metadata"]["refunded_amount"])))
        for e in emits
    ]


async def _edge() -> Dict[str, Any]:
    from db.database import database

    row = await database.fetch_one(
        """
        SELECT refund_amount_cents, refunded_amount, refund_count, refund_ids,
               dispute_amount_cents,
               gross_attributed_gmv_cents, net_attributed_gmv_cents
        FROM commerce_attribution_edges WHERE edge_id = :e
        """,
        {"e": EDGE_ID},
    )
    assert row is not None
    return dict(row)


async def _order_total_refunded() -> Decimal:
    from db.database import database

    row = await database.fetch_one(
        "SELECT total_refunded FROM orders WHERE order_id = :o", {"o": ORDER_ID}
    )
    return Decimal(str(row["total_refunded"]))


def _describe(edge: Dict[str, Any], emits: List[Dict[str, Any]]) -> str:
    keys = [e.get("upstream_idempotency_key") for e in emits]
    return (
        f"edge refund_amount_cents={edge['refund_amount_cents']} "
        f"dispute_amount_cents={edge['dispute_amount_cents']} "
        f"refunded_amount={edge['refunded_amount']} refund_count={edge['refund_count']} "
        f"refund_ids={edge['refund_ids']} net={edge['net_attributed_gmv_cents']} "
        f"ledger_emits={keys}"
    )


# -- 1. one refund, reported twice ------------------------------------------


@pytest.mark.asyncio
async def test_one_refund_reported_by_charge_refunded_and_refund_updated_counts_once(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """ONE $300 refund on a $500 order: refund.created(pending) ->
    charge.refunded(amount_refunded=300) -> refund.updated(succeeded, 300)."""
    await _send(monkeypatch, "refund.created", _refund("re_one", 30000, "pending"))
    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))
    await _send(monkeypatch, "refund.updated", _refund("re_one", 30000, "succeeded"))

    assert await _order_total_refunded() == Decimal("300"), "order level is already right"
    edge = await _edge()
    assert edge["refund_amount_cents"] == 30000, _describe(edge, ledger_emits)
    assert edge["refunded_amount"] == Decimal("300.00"), _describe(edge, ledger_emits)
    assert edge["net_attributed_gmv_cents"] == 20000, _describe(edge, ledger_emits)
    # Both ids stay on the edge for audit; only the first report added money.
    # jsonb comes back from asyncpg as its JSON text.
    assert set(json.loads(edge["refund_ids"])) == {CHARGE_ID, "re_one"}
    assert _emitted(ledger_emits) == [(CHARGE_ID, Decimal("300"))]


# -- 2. two sequential partial refunds --------------------------------------


@pytest.mark.asyncio
async def test_two_partial_refunds_with_refund_updated_count_their_sum(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """$300 then $200 on a $500 order; each refund goes pending -> succeeded, so
    Stripe reports it twice: charge.refunded (cumulative, same ch_ both times)
    and refund.updated (per refund)."""
    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))
    await _send(monkeypatch, "refund.updated", _refund("re_first", 30000, "succeeded"))
    await _send(monkeypatch, "charge.refunded", _charge_refunded(50000))
    await _send(monkeypatch, "refund.updated", _refund("re_second", 20000, "succeeded"))

    assert await _order_total_refunded() == Decimal("500"), "order level is already right"
    edge = await _edge()
    assert edge["refund_amount_cents"] == 50000, _describe(edge, ledger_emits)
    assert edge["net_attributed_gmv_cents"] == 0, _describe(edge, ledger_emits)
    assert _emitted(ledger_emits) == [(CHARGE_ID, Decimal("300")), (CHARGE_ID, Decimal("200"))]


@pytest.mark.asyncio
async def test_two_partial_refunds_reported_only_by_charge_refunded_count_their_sum(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """$300 then $200, each succeeding immediately, so no refund.updated(succeeded)
    ever arrives — only charge.refunded, whose id is the SAME ch_ both times."""
    await _send(monkeypatch, "refund.created", _refund("re_first", 30000, "succeeded"))
    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))
    await _send(monkeypatch, "refund.created", _refund("re_second", 20000, "succeeded"))
    await _send(monkeypatch, "charge.refunded", _charge_refunded(50000))

    assert await _order_total_refunded() == Decimal("500"), "order level is already right"
    edge = await _edge()
    assert edge["refund_amount_cents"] == 50000, _describe(edge, ledger_emits)


# The ORDER-level total for this sequence is wrong today, on Postgres only.
# `_resolve_stripe_order_for_refund` reads orders.metadata (a json column) through
# a raw SELECT, which returns a STRING, so `_stripe_refund_level_cumulative` and
# `finalize_refund_success` see {} and never sum the earlier refund. The same
# string also makes the finalizer overwrite every other order metadata key.
# Pinned rather than marked xfail, because postgres-dialect-gate counts an xfail
# as a skip and refuses to go green on one.
_KNOWN_ORDER_TOTAL_FOR_REFUND_UPDATED_ONLY = Decimal("300")


@pytest.mark.asyncio
async def test_two_partial_refunds_reported_only_by_refund_updated_follow_the_order_total(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """No charge.refunded at all: each refund.updated carries ONE refund's amount.

    The edge must equal the order's reconciled total, whatever that is. The correct
    total is 500. Today the order lands at 300 (see the constant above). When
    that is fixed, the pin below fails: change the constant to 500 and the
    expected edge follows. At 500 this test also catches the route passing the
    event's amount instead of the reconciled total, which would leave the edge
    at max(300, 200) = 300.
    """
    await _send(monkeypatch, "refund.updated", _refund("re_first", 30000, "succeeded"))
    await _send(monkeypatch, "refund.updated", _refund("re_second", 20000, "succeeded"))

    order_total = await _order_total_refunded()
    assert order_total == _KNOWN_ORDER_TOTAL_FOR_REFUND_UPDATED_ONLY, (
        f"orders.total_refunded is now {order_total}. If it is 500, the order-level "
        "metadata-as-string bug is fixed: set _KNOWN_ORDER_TOTAL_FOR_REFUND_UPDATED_ONLY "
        "to 500."
    )
    edge = await _edge()
    assert edge["refund_amount_cents"] == int(order_total * 100), _describe(edge, ledger_emits)


# -- 3. merchant-initiated refund, then Stripe's webhooks for it -------------


@pytest.mark.asyncio
async def test_merchant_create_refund_then_its_stripe_webhooks_count_once(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """RefundService.create_refund($300) — the PSP call returns re_merchant — and
    Stripe then delivers the webhooks for that same refund."""
    from db.database import database
    from services.refund_service import RefundService

    service = RefundService(database)

    async def fake_psp_refund(order, refund_id, amount, reason, *, idempotency_key=None):
        return {"success": True, "refund_id": "re_merchant"}

    monkeypatch.setattr(service, "_process_psp_refund", fake_psp_refund)

    result = await service.create_refund(
        order_id=ORDER_ID,
        amount=300.0,
        reason="requested_by_customer",
        idempotency_key="edge_overcount_merchant_refund",
    )
    assert result["status"] == "success", result
    assert result["psp_refund_id"] == "re_merchant"
    after_service = await _edge()
    assert after_service["refund_amount_cents"] == 30000, _describe(after_service, ledger_emits)
    assert await _order_total_refunded() == Decimal("300")

    await _send(monkeypatch, "refund.created", _refund("re_merchant", 30000, "pending"))
    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))
    await _send(monkeypatch, "refund.updated", _refund("re_merchant", 30000, "succeeded"))

    assert await _order_total_refunded() == Decimal("300"), "order level is already right"
    edge = await _edge()
    assert edge["refund_amount_cents"] == 30000, _describe(edge, ledger_emits)
    assert edge["net_attributed_gmv_cents"] == 20000, _describe(edge, ledger_emits)
    assert len(ledger_emits) == 1 and ledger_emits[0]["metadata"]["refund_id"] == result["refund_id"]
    assert _emitted(ledger_emits) == [(result["refund_id"], Decimal("300"))]


# -- 4. chargebacks are their own total -------------------------------------


@pytest.mark.asyncio
async def test_a_chargeback_before_a_refund_is_not_absorbed_by_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """A $100 dispute, then a $300 refund. orders.total_refunded never sees the
    dispute, so a plain ceiling at 300 would swallow it (max(100, 300) = 300)."""
    await _send(monkeypatch, "charge.dispute.created", _dispute("dp_first", 10000))
    edge = await _edge()
    assert edge["refund_amount_cents"] == 10000, _describe(edge, ledger_emits)
    assert edge["dispute_amount_cents"] == 10000, _describe(edge, ledger_emits)

    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))
    await _send(monkeypatch, "refund.updated", _refund("re_after_dispute", 30000, "succeeded"))
    # Stripe re-sends dispute events as the dispute moves; the id dedupes them.
    await _send(monkeypatch, "charge.dispute.updated", _dispute("dp_first", 10000))

    edge = await _edge()
    assert edge["dispute_amount_cents"] == 10000, _describe(edge, ledger_emits)
    assert edge["refund_amount_cents"] == 40000, _describe(edge, ledger_emits)
    assert edge["refunded_amount"] == Decimal("400.00"), _describe(edge, ledger_emits)
    assert edge["net_attributed_gmv_cents"] == 10000, _describe(edge, ledger_emits)


@pytest.mark.asyncio
async def test_a_chargeback_after_a_refund_adds_on_top_and_survives_a_refund_replay(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))
    await _send(monkeypatch, "charge.dispute.created", _dispute("dp_second", 10000))
    # A late refund.updated for the same $300 must not re-add it, and must not
    # fold the dispute into the refund part either.
    await _send(monkeypatch, "refund.updated", _refund("re_before_dispute", 30000, "succeeded"))

    edge = await _edge()
    assert edge["dispute_amount_cents"] == 10000, _describe(edge, ledger_emits)
    assert edge["refund_amount_cents"] == 40000, _describe(edge, ledger_emits)
    assert edge["refunded_amount"] == Decimal("400.00"), _describe(edge, ledger_emits)
    assert edge["net_attributed_gmv_cents"] == 10000, _describe(edge, ledger_emits)


# -- 5. the ceiling never lowers a value --------------------------------------


@pytest.mark.asyncio
async def test_the_ceiling_leaves_an_edge_already_above_the_order_total_alone(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """An edge over-counted before this fix is NOT corrected by the next event.
    That is a deliberate property of a ceiling (it cannot go down); correcting old
    rows is a separate, explicit recompute, not a side effect of a webhook."""
    from db.database import database

    await database.execute(
        "UPDATE commerce_attribution_edges SET refund_amount_cents = 60000, "
        "refunded_amount = 600 WHERE edge_id = :e",
        {"e": EDGE_ID},
    )
    await _send(monkeypatch, "charge.refunded", _charge_refunded(30000))

    edge = await _edge()
    assert edge["refund_amount_cents"] == 60000, _describe(edge, ledger_emits)
    assert edge["refunded_amount"] == Decimal("600.00"), _describe(edge, ledger_emits)
    assert ledger_emits == [], "nothing was added, so nothing is emitted"


@pytest.mark.asyncio
async def test_two_merchant_refunds_raise_the_edge_to_the_order_total(
    monkeypatch: pytest.MonkeyPatch, ledger_emits: List[Dict[str, Any]]
) -> None:
    """create_refund($300) then create_refund($200): the second must raise the
    edge to 500, which it only does if it passes the ORDER's total, not its own
    $200."""
    from db.database import database
    from services.refund_service import RefundService

    service = RefundService(database)
    psp_ids = iter(["re_merchant_a", "re_merchant_b"])

    async def fake_psp_refund(order, refund_id, amount, reason, *, idempotency_key=None):
        return {"success": True, "refund_id": next(psp_ids)}

    monkeypatch.setattr(service, "_process_psp_refund", fake_psp_refund)

    for amount, key in ((300.0, "edge_overcount_a"), (200.0, "edge_overcount_b")):
        result = await service.create_refund(
            order_id=ORDER_ID, amount=amount, reason="requested_by_customer", idempotency_key=key
        )
        assert result["status"] == "success", result

    assert await _order_total_refunded() == Decimal("500")
    edge = await _edge()
    assert edge["refund_amount_cents"] == 50000, _describe(edge, ledger_emits)
