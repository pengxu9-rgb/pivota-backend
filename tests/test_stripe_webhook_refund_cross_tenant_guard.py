"""Cross-tenant + amount integrity guards on the Stripe refund branches.

Background: the payment branches resolve their order through
`_resolve_stripe_order_for_payment_event`, which scopes the row to the merchant
that owns the webhook endpoint's `psp_id` (`_order_belongs_to_psp_owner`) and
verifies the signed amount against the order. The refund branches did neither —
`charge.refunded` ran a bare
`SELECT * FROM orders WHERE payment_intent_id = :payment_intent_id` and
`_resolve_stripe_order_for_refund` was never handed a `psp_id` — so a merchant
holding their OWN endpoint secret could drive refund state on another merchant's
order, for whatever amount the event carried.

Every test here drives the real ASGI route. The fake `fetch_one` answers BOTH
`merchant_psps` reads: the `provider_config` one that supplies the endpoint
secret, and the `merchant_id` one that ARMS the guard. Each test asserts the
owner lookup actually ran — without it `_stripe_psp_owner_merchant_id` returns
None, `_order_belongs_to_psp_owner` short-circuits to True, and a "blocked"
assertion would pass for the wrong reason.
"""

from __future__ import annotations

import logging
import os
import sys
from decimal import Decimal
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


MERCHANT_A_PSP = "psp_stripe_merchant_a"
MERCHANT_A = "m_merchant_a"
MERCHANT_B = "m_merchant_b"
ENDPOINT_SECRET = "whsec_merchant_a"


def _stripe_event(event_type: str, obj: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": event_type, "data": {"object": obj}}


def _victim_order() -> Dict[str, Any]:
    """Merchant B's order — the one merchant A must not be able to touch."""
    return {
        "order_id": "ORD_MERCHANT_B",
        "merchant_id": MERCHANT_B,
        "payment_intent_id": "pi_merchant_b",
        "total": "500.00",
        "total_refunded": "0.00",
        "currency": "USD",
        "metadata": {},
    }


def _own_order() -> Dict[str, Any]:
    """Merchant A's own order — the positive control."""
    return {
        "order_id": "ORD_MERCHANT_A",
        "merchant_id": MERCHANT_A,
        "payment_intent_id": "pi_merchant_a",
        "total": "500.00",
        "total_refunded": "0.00",
        "currency": "USD",
        "metadata": {},
    }


class _Harness:
    """Records every state mutation the refund branches can make."""

    def __init__(self) -> None:
        self.owner_lookups: List[str] = []
        self.order_lookups: List[str] = []
        self.status_updates: List[Dict[str, Any]] = []
        self.order_updates: List[Dict[str, Any]] = []
        self.order_events: List[Dict[str, Any]] = []
        self.attribution_calls: List[Dict[str, Any]] = []
        self.event_status: List[Tuple[Optional[str], str, Optional[str]]] = []

    @property
    def mutations(self) -> List[Any]:
        return [*self.status_updates, *self.order_updates, *self.order_events]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event: Dict[str, Any],
    order_row: Optional[Dict[str, Any]],
) -> _Harness:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module
    import services.commerce_attribution_service as attribution_module

    h = _Harness()

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Any:
        if "FROM merchant_psps" in query and "provider_config" in query:
            assert values["psp_id"] == MERCHANT_A_PSP
            return {"provider_config": {"webhook_endpoint_secret": ENDPOINT_SECRET}}
        # This is the read that ARMS the cross-tenant guard.
        if "FROM merchant_psps" in query and "merchant_id" in query:
            h.owner_lookups.append(values["psp_id"])
            return {"merchant_id": MERCHANT_A}
        # The orders table is read three ways: by payment_intent, by order_id,
        # and (under a merchant scope) with a merchant_id predicate. Model all
        # three, or a scoped lookup silently returns the unscoped row.
        if "FROM orders" in query:
            if "payment_intent_id" in values:
                h.order_lookups.append(values["payment_intent_id"])
            if order_row is None:
                return None
            row = dict(order_row)
            for column in ("payment_intent_id", "order_id", "merchant_id"):
                if column in values and str(row.get(column)) != str(values[column]):
                    return None
            return row
        raise AssertionError(f"Unexpected query: {query}")

    def fake_construct_event(payload: bytes, signature: Optional[str], secret: str) -> Dict[str, Any]:
        assert secret == ENDPOINT_SECRET
        return event

    async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
        if order_row is not None and str(order_row.get("order_id")) == order_id:
            return dict(order_row)
        return None

    async def fake_update_order_status(order_id: str, status: str, **kwargs: Any) -> None:
        h.status_updates.append({"order_id": order_id, "status": status, **kwargs})

    async def fake_update_order(order_id: str, fields: Dict[str, Any]) -> None:
        h.order_updates.append({"order_id": order_id, "fields": fields})

    async def fake_log_order_event(**kwargs: Any) -> None:
        h.order_events.append(kwargs)

    async def fake_attach_refund_to_attribution_edge(**kwargs: Any) -> None:
        h.attribution_calls.append(kwargs)

    async def fake_record_event(**kwargs: Any) -> bool:
        return False

    async def fake_mark_status(
        event_id: Optional[str], status: str, error_message: Optional[str] = None
    ) -> None:
        h.event_status.append((event_id, status, error_message))

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook, "construct_event", staticmethod(fake_construct_event)
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_update_order_status)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_update_order)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_log_order_event)
    monkeypatch.setattr(
        webhook_routes_module, "_record_stripe_webhook_event_best_effort", fake_record_event
    )
    monkeypatch.setattr(
        webhook_routes_module, "_mark_stripe_webhook_event_status_best_effort", fake_mark_status
    )
    monkeypatch.setattr(
        attribution_module,
        "attach_refund_to_attribution_edge",
        fake_attach_refund_to_attribution_edge,
    )
    return h


class _CapturedAlerts(logging.Handler):
    """The app logs through the shared `pivota` logger, which sets
    propagate=False — so `caplog` (a root handler) sees nothing. Attach to the
    real logger instead."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(str(record.getMessage()))


@contextmanager
def _capture_alerts():
    from utils.logger import logger as pivota_logger

    handler = _CapturedAlerts()
    pivota_logger.addHandler(handler)
    try:
        yield handler
    finally:
        pivota_logger.removeHandler(handler)


async def _post(psp_id: Optional[str] = MERCHANT_A_PSP) -> httpx.Response:
    path = f"/webhooks/stripe/{psp_id}" if psp_id else "/webhooks/stripe"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            path,
            content=b'{"id":"evt_refund_guard"}',
            headers={"stripe-signature": "sig_refund_guard"},
        )


# ---------------------------------------------------------------------------
# Cross-tenant guard: merchant A's endpoint must not move merchant B's order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_refunded_on_foreign_psp_endpoint_does_not_mutate_other_merchants_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_cross_tenant",
            "payment_intent": "pi_merchant_b",
            "amount_refunded": 50000,
            "currency": "usd",
        },
    )
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    with _capture_alerts() as alerts:
        resp = await _post()

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unmatched",
        "event": "charge.refunded",
        "reason": "cross_tenant_blocked",
    }
    # The guard was armed by a real owner lookup, and the victim row WAS read —
    # so the block is the guard's doing, not a lookup miss.
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.order_lookups == ["pi_merchant_b"]
    assert h.mutations == []
    assert h.attribution_calls == []
    # The event is recorded 'unmatched', never 'processed' — a real refund we
    # refused must stay visible to reconcile. (The event id is a payload hash,
    # so pin the status + reason, not the id.)
    assert [(s, r) for _e, s, r in h.event_status] == [
        ("unmatched", "cross_tenant_blocked")
    ]
    # The block came from the guard specifically, not from some unrelated miss.
    assert any("stripe_webhook_cross_tenant_blocked" in m for m in alerts.messages)
    assert any("stripe_refund_event_unmatched" in m for m in alerts.messages)


@pytest.mark.asyncio
async def test_refund_updated_succeeded_on_foreign_psp_endpoint_does_not_mutate_other_merchants_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _stripe_event(
        "refund.updated",
        {
            "id": "re_cross_tenant",
            "status": "succeeded",
            "payment_intent": "pi_merchant_b",
            "amount": 50000,
            "currency": "usd",
            "metadata": {},
        },
    )
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    resp = await _post()

    assert resp.json() == {
        "status": "unmatched",
        "event": "refund.updated",
        "reason": "cross_tenant_blocked",
    }
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.mutations == []


@pytest.mark.asyncio
async def test_refund_created_on_foreign_psp_endpoint_does_not_touch_other_merchants_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _stripe_event(
        "refund.created",
        {
            "id": "re_cross_tenant_created",
            "status": "pending",
            "payment_intent": "pi_merchant_b",
            "amount": 50000,
            "currency": "usd",
            "metadata": {},
        },
    )
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    resp = await _post()

    assert resp.json() == {
        "status": "unmatched",
        "event": "refund.created",
        "reason": "cross_tenant_blocked",
    }
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.mutations == []


@pytest.mark.asyncio
async def test_refund_failed_on_foreign_psp_endpoint_does_not_roll_back_other_merchants_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _stripe_event(
        "refund.failed",
        {
            "id": "re_cross_tenant_failed",
            "status": "failed",
            "payment_intent": "pi_merchant_b",
            "amount": 50000,
            "currency": "usd",
            "failure_reason": "expired_or_canceled_card",
            "metadata": {},
        },
    )
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    resp = await _post()

    assert resp.json() == {
        "status": "unmatched",
        "event": "refund.failed",
        "reason": "cross_tenant_blocked",
    }
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.mutations == []


@pytest.mark.asyncio
async def test_refund_metadata_order_hint_cannot_reach_across_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata.order_id fallback is the second reachable path into
    `_resolve_stripe_order_for_refund`; it must be scoped too. No stored PI
    matches, so resolution falls through to the forged hint."""
    event = _stripe_event(
        "refund.created",
        {
            "id": "re_forged_hint",
            "status": "pending",
            "payment_intent": "pi_not_stored_anywhere",
            "amount": 50000,
            "currency": "usd",
            "metadata": {"order_id": "ORD_MERCHANT_B"},
        },
    )
    h = _install(monkeypatch, event=event, order_row=None)

    import routes.webhook_routes as webhook_routes_module

    async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
        assert order_id == "ORD_MERCHANT_B"
        return _victim_order()

    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)

    resp = await _post()

    assert resp.json() == {
        "status": "unmatched",
        "event": "refund.created",
        "reason": "cross_tenant_blocked",
    }
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.mutations == []


# ---------------------------------------------------------------------------
# Positive control: the guard must not block the endpoint owner's OWN order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_refunded_still_applies_for_the_endpoint_owners_own_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kills the "always return None" mutant: the same psp endpoint, the same
    owner lookup, an order that DOES belong to merchant A — refund applies."""
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_own_order",
            "payment_intent": "pi_merchant_a",
            "amount_refunded": 50000,
            "currency": "usd",
        },
    )
    h = _install(monkeypatch, event=event, order_row=_own_order())

    resp = await _post()

    assert resp.json() == {"status": "success", "event": "charge.refunded"}
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert len(h.status_updates) == 1
    assert h.status_updates[0]["order_id"] == "ORD_MERCHANT_A"
    assert h.status_updates[0]["status"] == "refunded"
    assert str(h.status_updates[0]["total_refunded"]) == "500"
    assert [e["event_type"] for e in h.order_events] == ["refund_processed_webhook"]


@pytest.mark.asyncio
async def test_bare_stripe_endpoint_keeps_platform_wide_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No psp_id in the path means the platform-wide secret authenticated the
    call; there is no endpoint owner to scope to, so the guard stays open."""
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_bare_endpoint",
            "payment_intent": "pi_merchant_b",
            "amount_refunded": 50000,
            "currency": "usd",
        },
    )
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    import routes.webhook_routes as webhook_routes_module

    monkeypatch.setattr(
        webhook_routes_module.settings, "stripe_webhook_secret", ENDPOINT_SECRET, raising=False
    )

    resp = await _post(psp_id=None)

    assert resp.json() == {"status": "success", "event": "charge.refunded"}
    assert h.owner_lookups == []
    assert len(h.status_updates) == 1
    assert h.status_updates[0]["order_id"] == "ORD_MERCHANT_B"


# ---------------------------------------------------------------------------
# Amount / currency integrity on the refund path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_refunded_larger_than_order_total_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refund branches applied whatever amount the event carried. A $5,000
    refund against a $500 order must not write total_refunded."""
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_over_refund",
            "payment_intent": "pi_merchant_a",
            "amount_refunded": 500000,
            "currency": "usd",
        },
    )
    h = _install(monkeypatch, event=event, order_row=_own_order())

    resp = await _post()

    body = resp.json()
    assert body["status"] == "unmatched"
    assert body["reason"].startswith("refund_exceeds_order_total:")
    assert h.mutations == []
    assert h.attribution_calls == []
    assert h.event_status[-1][1] == "unmatched"


@pytest.mark.asyncio
async def test_charge_refunded_in_a_different_currency_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_wrong_currency",
            "payment_intent": "pi_merchant_a",
            "amount_refunded": 40000,
            "currency": "eur",
        },
    )
    h = _install(monkeypatch, event=event, order_row=_own_order())

    resp = await _post()

    body = resp.json()
    assert body["status"] == "unmatched"
    assert body["reason"] == "refund_currency_mismatch:order=usd,event=eur"
    assert h.mutations == []


@pytest.mark.asyncio
async def test_refund_updated_succeeded_larger_than_order_total_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _stripe_event(
        "refund.updated",
        {
            "id": "re_over_refund",
            "status": "succeeded",
            "payment_intent": "pi_merchant_a",
            "amount": 500000,
            "currency": "usd",
            "metadata": {},
        },
    )
    h = _install(monkeypatch, event=event, order_row=_own_order())

    resp = await _post()

    body = resp.json()
    assert body["status"] == "unmatched"
    assert body["reason"].startswith("refund_exceeds_order_total:")
    assert h.mutations == []


@pytest.mark.asyncio
async def test_partial_refund_within_order_total_still_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling is the order total, not the exact total — partial refunds
    must keep working."""
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_partial",
            "payment_intent": "pi_merchant_a",
            "amount_refunded": 12000,
            "currency": "usd",
        },
    )
    h = _install(monkeypatch, event=event, order_row=_own_order())

    resp = await _post()

    assert resp.json() == {"status": "success", "event": "charge.refunded"}
    assert len(h.status_updates) == 1
    assert h.status_updates[0]["status"] == "partially_refunded"
    assert str(h.status_updates[0]["total_refunded"]) == "120"


# ---------------------------------------------------------------------------
# charge.dispute.* — the same attack, via a branch that took its identity
# straight from event metadata and never resolved an order at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispute_on_foreign_psp_endpoint_does_not_touch_other_merchants_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispute branch reversed the attribution edge — the same financial
    primitive the refund branches write — keyed on an attacker-supplied
    metadata.order_id, with no tenant predicate on the UPDATE."""
    event = _stripe_event(
        "charge.dispute.created",
        {
            "id": "dp_cross_tenant",
            "amount": 50000,
            "currency": "usd",
            "payment_intent": "pi_merchant_b",
            "metadata": {"order_id": "ORD_MERCHANT_B", "merchant_id": MERCHANT_B},
        },
    )
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    resp = await _post()

    assert resp.json() == {
        "status": "unmatched",
        "event": "charge.dispute.created",
        "reason": "cross_tenant_blocked",
    }
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.attribution_calls == []
    assert h.mutations == []


@pytest.mark.asyncio
async def test_dispute_metadata_cannot_name_a_foreign_merchant_when_no_order_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with no order to resolve, the dispute record must be attributed to
    the endpoint owner — never to the merchant_id the event asked for."""
    event = _stripe_event(
        "charge.dispute.created",
        {
            "id": "dp_forged_merchant",
            "amount": 50000,
            "currency": "usd",
            "payment_intent": "pi_not_stored_anywhere",
            "metadata": {"merchant_id": MERCHANT_B},
        },
    )
    h = _install(monkeypatch, event=event, order_row=None)

    import routes.webhook_routes as webhook_routes_module

    upserts: List[Dict[str, Any]] = []

    async def fake_get_order(order_id: str) -> None:
        return None

    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)

    import services.dispute_records_service as dispute_module

    async def fake_upsert(dispute: Dict[str, Any], **kwargs: Any) -> None:
        upserts.append(kwargs)

    monkeypatch.setattr(
        dispute_module, "upsert_stripe_dispute_record_best_effort", fake_upsert
    )

    resp = await _post()

    assert resp.json() == {"status": "success", "event": "charge.dispute.created"}
    assert h.owner_lookups == [MERCHANT_A_PSP]
    # Identity came from the endpoint owner, not from the event's metadata.
    assert len(upserts) == 1
    assert upserts[0]["merchant_id_hint"] == MERCHANT_A
    assert upserts[0]["merchant_scope"] == MERCHANT_A
    assert upserts[0]["order_id_hint"] is None
    # No order resolved under this tenant => nothing order-keyed may fire.
    assert h.attribution_calls == []


@pytest.mark.asyncio
async def test_dispute_for_the_endpoint_owners_own_order_still_reverses_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control — the dispute lane must still work for its own tenant."""
    event = _stripe_event(
        "charge.dispute.created",
        {
            "id": "dp_own_order",
            "amount": 50000,
            "currency": "usd",
            "payment_intent": "pi_merchant_a",
            "metadata": {"order_id": "ORD_MERCHANT_A", "merchant_id": MERCHANT_A},
        },
    )
    h = _install(monkeypatch, event=event, order_row=_own_order())

    resp = await _post()

    assert resp.json() == {"status": "success", "event": "charge.dispute.created"}
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.attribution_calls == [
        {"order_id": "ORD_MERCHANT_A", "refund_id": "dp_own_order", "amount": Decimal("500")}
    ]


# ---------------------------------------------------------------------------
# Fail-closed: an unresolvable psp_id owner must refuse, not fall open
# ---------------------------------------------------------------------------


def _install_with_owner_lookup_failure(
    monkeypatch: pytest.MonkeyPatch, *, event: Dict[str, Any], failure: BaseException | None
) -> _Harness:
    """Same harness, but the OWNER query fails (or returns nothing) while the
    secret and orders queries succeed — the partial-failure shape, since these
    are three independent round-trips."""
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    import db.database as database_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Any:
        if "FROM merchant_psps" in query and "provider_config" in query:
            return {"provider_config": {"webhook_endpoint_secret": ENDPOINT_SECRET}}
        if "FROM merchant_psps" in query and "merchant_id" in query:
            h.owner_lookups.append(values["psp_id"])
            if failure is not None:
                raise failure
            return None
        if "FROM orders" in query:
            h.order_lookups.append(values["payment_intent_id"])
            return _victim_order()
        raise AssertionError(f"Unexpected query: {query}")

    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    return h


@pytest.mark.asyncio
async def test_owner_lookup_timeout_refuses_instead_of_restoring_the_exploit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single failed owner query used to make `_order_belongs_to_psp_owner`
    return True for every order — the full pre-fix exploit, restored by a DB
    blip. It must refuse instead."""
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_owner_timeout",
            "payment_intent": "pi_merchant_b",
            "amount_refunded": 50000,
            "currency": "usd",
        },
    )
    h = _install_with_owner_lookup_failure(
        monkeypatch,
        event=event,
        failure=RuntimeError("canceling statement due to statement timeout"),
    )

    resp = await _post()

    # 503, NOT 200: Stripe redelivers, so a transient failure does not discard a
    # real refund. A 200 would be a silent drop — 'unmatched' has no consumer.
    assert resp.status_code == 503
    assert resp.json()["detail"] == "psp_owner_unresolved"
    assert h.owner_lookups == [MERCHANT_A_PSP]
    assert h.mutations == []
    assert h.attribution_calls == []


@pytest.mark.asyncio
async def test_psp_id_with_no_owner_row_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`merchant_psps.psp_id` is the PRIMARY KEY and `merchant_id` is NOT NULL,
    so a psp_id in the path with no owner row is always an error state — never a
    legitimate platform-wide call."""
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_no_owner_row",
            "payment_intent": "pi_merchant_b",
            "amount_refunded": 50000,
            "currency": "usd",
        },
    )
    h = _install_with_owner_lookup_failure(monkeypatch, event=event, failure=None)

    resp = await _post()

    assert resp.status_code == 503
    assert h.mutations == []


@pytest.mark.asyncio
async def test_owner_lookup_failure_on_the_bare_endpoint_is_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare endpoint never resolves an owner, so the fail-closed path cannot
    fire there — the platform-wide secret keeps working during a DB blip."""
    event = _stripe_event(
        "charge.refunded",
        {
            "id": "ch_bare_no_owner",
            "payment_intent": "pi_merchant_b",
            "amount_refunded": 50000,
            "currency": "usd",
        },
    )
    h = _install(monkeypatch, event=event, order_row=_victim_order())

    import routes.webhook_routes as webhook_routes_module

    monkeypatch.setattr(
        webhook_routes_module.settings, "stripe_webhook_secret", ENDPOINT_SECRET, raising=False
    )

    resp = await _post(psp_id=None)

    assert resp.status_code == 200
    assert h.owner_lookups == []
    assert len(h.status_updates) == 1


# ---------------------------------------------------------------------------
# dispute_records_service: the scoped identity resolution itself
#
# The webhook test above stubs the upsert out, so these drive the real
# `_resolve_order_and_merchant_from_stripe_payload`. Without them, deleting the
# whole `if scope:` branch leaves every other test green.
# ---------------------------------------------------------------------------


class _FakeDb:
    """Models `orders` for the three lookup shapes the resolver uses."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: List[Tuple[str, Dict[str, Any]]] = []

    async def fetch_one(self, query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.queries.append((query, values))
        for row in self.rows:
            if "order_id" in values and str(row["order_id"]) != str(values["order_id"]):
                continue
            if "pi" in values and str(row["payment_intent_id"]) != str(values["pi"]):
                continue
            if "merchant_id" in values and str(row["merchant_id"]) != str(values["merchant_id"]):
                continue
            return dict(row)
        return None


def _orders_fixture() -> List[Dict[str, Any]]:
    return [
        {"order_id": "ORD_MERCHANT_A", "merchant_id": MERCHANT_A, "payment_intent_id": "pi_merchant_a"},
        {"order_id": "ORD_MERCHANT_B", "merchant_id": MERCHANT_B, "payment_intent_id": "pi_merchant_b"},
    ]


async def _resolve(scope: Optional[str], **kwargs: Any) -> Tuple[Optional[str], Optional[str]]:
    from services.dispute_records_service import (
        _resolve_order_and_merchant_from_stripe_payload,
    )

    db = _FakeDb(_orders_fixture())
    return await _resolve_order_and_merchant_from_stripe_payload(
        kwargs.pop("payload", {}),
        payment_intent_id=kwargs.pop("payment_intent_id", None),
        order_id_hint=kwargs.pop("order_id_hint", None),
        merchant_id_hint=kwargs.pop("merchant_id_hint", None),
        db=db,
        merchant_scope=scope,
    )


@pytest.mark.asyncio
async def test_scoped_resolution_pins_merchant_to_the_scope_not_the_hint() -> None:
    order_id, merchant_id = await _resolve(
        MERCHANT_A,
        order_id_hint="ORD_MERCHANT_B",
        merchant_id_hint=MERCHANT_B,
        payment_intent_id="pi_merchant_b",
    )
    assert merchant_id == MERCHANT_A
    # ORD_MERCHANT_B is not merchant A's, so it must not survive scoping — and
    # the PI fallback must not resurrect it either.
    assert order_id is None


@pytest.mark.asyncio
async def test_scoped_resolution_drops_a_foreign_payment_intent() -> None:
    order_id, merchant_id = await _resolve(MERCHANT_A, payment_intent_id="pi_merchant_b")
    assert (order_id, merchant_id) == (None, MERCHANT_A)


@pytest.mark.asyncio
async def test_scoped_resolution_still_finds_the_scopes_own_order() -> None:
    by_pi = await _resolve(MERCHANT_A, payment_intent_id="pi_merchant_a")
    assert by_pi == ("ORD_MERCHANT_A", MERCHANT_A)

    by_hint = await _resolve(MERCHANT_A, order_id_hint="ORD_MERCHANT_A")
    assert by_hint == ("ORD_MERCHANT_A", MERCHANT_A)


@pytest.mark.asyncio
async def test_unscoped_resolution_keeps_legacy_behaviour() -> None:
    """The bare platform endpoint passes no scope; that path is unchanged."""
    order_id, merchant_id = await _resolve(
        None, order_id_hint="ORD_MERCHANT_B", merchant_id_hint=MERCHANT_B
    )
    assert (order_id, merchant_id) == ("ORD_MERCHANT_B", MERCHANT_B)

    by_pi = await _resolve(None, payment_intent_id="pi_merchant_b")
    assert by_pi == ("ORD_MERCHANT_B", MERCHANT_B)
