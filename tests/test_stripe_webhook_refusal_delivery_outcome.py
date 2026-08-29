"""A refused Stripe event must get the delivery outcome that can recover it.

`webhook_events.status = 'unmatched'` has NO consumer anywhere in this repo, and
the handler answered 200, so a refused event was dropped for good. But the
refusals are not alike:

  - permanent (cross-tenant block, amount mismatch) — a property of the signed
    event itself, so redelivering identical bytes can never succeed. 200.
  - possibly-transient (no order resolved) — usually the charge landing before
    the order is committed. 503, so Stripe redelivers on its own retry schedule.

Both used to be recorded as `no_order_resolved` and answered 200, which is why
they could not be told apart. The distinction matters in both directions: a
permanent refusal answering 503 would let a signed-but-refused event hammer the
endpoint for days.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app


PSP = "psp_owner_a"
OWNER = "m_owner_a"
OTHER = "m_other_b"
SECRET = "whsec_refusal_outcome"


class _H:
    def __init__(self) -> None:
        self.event_status: List[Tuple[Optional[str], str, Optional[str]]] = []
        self.mutations: List[Any] = []


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event: Dict[str, Any],
    order_row: Optional[Dict[str, Any]],
) -> _H:
    import db.database as database_module
    import routes.webhook_routes as webhook_routes_module

    h = _H()

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Any:
        if "FROM merchant_psps" in query and "provider_config" in query:
            return {"provider_config": {"webhook_endpoint_secret": SECRET}}
        if "FROM merchant_psps" in query and "merchant_id" in query:
            return {"merchant_id": OWNER}
        if "FROM orders" in query:
            return dict(order_row) if order_row is not None else None
        raise AssertionError(f"Unexpected query: {query}")

    def fake_construct_event(payload: bytes, sig: Optional[str], secret: str) -> Dict[str, Any]:
        return event

    async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
        if order_row is not None and str(order_row.get("order_id")) == order_id:
            return dict(order_row)
        return None

    async def fake_mutate(*args: Any, **kwargs: Any) -> None:
        h.mutations.append((args, kwargs))

    async def fake_record(**kwargs: Any) -> bool:
        return False

    async def fake_mark(event_id: Optional[str], status: str, err: Optional[str] = None) -> None:
        h.event_status.append((event_id, status, err))

    monkeypatch.setattr(webhook_routes_module.settings, "stripe_webhook_secret", "", raising=False)
    monkeypatch.setattr(
        webhook_routes_module.stripe.Webhook, "construct_event", staticmethod(fake_construct_event)
    )
    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)
    monkeypatch.setattr(webhook_routes_module, "update_order_status", fake_mutate)
    monkeypatch.setattr(webhook_routes_module, "update_order", fake_mutate)
    monkeypatch.setattr(webhook_routes_module, "log_order_event", fake_mutate)
    monkeypatch.setattr(
        webhook_routes_module, "_record_stripe_webhook_event_best_effort", fake_record
    )
    monkeypatch.setattr(
        webhook_routes_module, "_mark_stripe_webhook_event_status_best_effort", fake_mark
    )
    return h


async def _post() -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            f"/webhooks/stripe/{PSP}",
            content=b'{"id":"evt_refusal"}',
            headers={"stripe-signature": "sig_refusal"},
        )


def _succeeded(pi: str, amount: int = 50000) -> Dict[str, Any]:
    return {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": pi,
                "amount": amount,
                "amount_received": amount,
                "currency": "usd",
                "metadata": {"order_id": "ORD_0123456789ABCDEF"},
            }
        },
    }


def _order(merchant_id: str, total: str = "500.00") -> Dict[str, Any]:
    return {
        "order_id": "ORD_0123456789ABCDEF",
        "merchant_id": merchant_id,
        "payment_intent_id": "pi_known",
        "total": total,
        "total_refunded": "0.00",
        "currency": "USD",
        "metadata": {},
        "status": "pending",
    }


# --------------------------------------------------------------------------
# Transient: no order yet -> 503 so Stripe redelivers
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_succeeded_charge_with_no_order_defers_for_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The charge-stuck race: the payment landed before the order was committed.
    A 200 here drops a real charge, because nothing consumes 'unmatched'."""
    h = _install(monkeypatch, event=_succeeded("pi_no_order"), order_row=None)

    resp = await _post()

    assert resp.status_code == 503
    assert h.mutations == []
    # Never recorded as terminally handled — the last word is 'failed', pending
    # Stripe's redelivery, not 'processed'.
    assert [s for _e, s, _r in h.event_status][-1] == "failed"
    assert "processed" not in [s for _e, s, _r in h.event_status]


# --------------------------------------------------------------------------
# Permanent: retrying can never help -> 200, no retry storm
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cross_tenant_blocked_payment_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal an attacker can trigger at will must NOT answer 503 — that
    would let a signed-but-refused event hammer the endpoint for days."""
    h = _install(monkeypatch, event=_succeeded("pi_foreign"), order_row=_order(OTHER))

    resp = await _post()

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unmatched",
        "event": "payment_intent.succeeded",
        "reason": "cross_tenant_blocked",
    }
    assert h.mutations == []
    assert ("unmatched", "cross_tenant_blocked") in [(s, r) for _e, s, r in h.event_status]


@pytest.mark.asyncio
async def test_a_payment_amount_mismatch_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A $1 charge against a $500 order: the same bytes will be refused forever."""
    h = _install(
        monkeypatch, event=_succeeded("pi_known", amount=100), order_row=_order(OWNER)
    )

    resp = await _post()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unmatched"
    assert body["reason"].startswith("amount_mismatch:")
    assert h.mutations == []


@pytest.mark.asyncio
async def test_a_cross_tenant_blocked_refund_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refund branches route through the same classifier; a permanent
    refusal there keeps its 200 exactly as before."""
    event = {
        "type": "charge.refunded",
        "data": {
            "object": {
                "id": "ch_foreign",
                "payment_intent": "pi_known",
                "amount_refunded": 50000,
                "currency": "usd",
            }
        },
    }
    h = _install(monkeypatch, event=event, order_row=_order(OTHER))

    resp = await _post()

    assert resp.status_code == 200
    assert resp.json()["reason"] == "cross_tenant_blocked"
    assert h.mutations == []


# --------------------------------------------------------------------------
# The classifier itself
# --------------------------------------------------------------------------


def test_every_reason_the_handler_produces_is_classified() -> None:
    """Guards producer/allowlist drift.

    The table below is hand-written, so on its own it proves nothing about the
    reasons the code ACTUALLY emits — its apparent completeness is unearned, and
    the first version of it silently omitted the two `*_parse_error` producers.
    Harvest the real reason strings from the source and require each to be
    classified deliberately, so a new producer cannot slip in unclassified."""
    import re
    from pathlib import Path

    src = Path("routes/webhook_routes.py").read_text(encoding="utf-8")
    # Every `return False, f"..."` / `return False, "..."` reason literal.
    produced = set(re.findall(r'return\s+False,\s+f?"([a-z_]+)', src))
    produced |= set(re.findall(r'return\s+\(\s*\n\s*False,\s*\n\s*f"([a-z_]+)', src))
    # The RESOLVERS reject with `return None, "reason"` — that family feeds the
    # deferral decision, and `cross_tenant_blocked` lives there. Omitting this
    # pattern let the single most important reason go unharvested.
    produced |= set(re.findall(r'return\s+None,\s+f?"([a-z_]+)', src))
    assert produced, "harvest found nothing — the regex has drifted from the source"
    # Floor on the HARVEST itself. Without this, narrowing the regex silently
    # shrinks what is checked and the completeness claim becomes vacuous — which
    # is exactly how `cross_tenant_blocked` (a `return None, "..."` producer) went
    # unharvested in the first version of this test.
    for anchor in ("cross_tenant_blocked", "refund_exceeds_order_total", "amount_parse_error"):
        assert anchor in produced, f"harvest regex no longer matches {anchor}"

    from routes.webhook_routes import _stripe_refusal_is_permanent

    unclassified = {
        r for r in produced if not _stripe_refusal_is_permanent(r) and r not in _KNOWN_TRANSIENT
    }
    assert not unclassified, (
        f"these refusal reasons are produced but not classified: {sorted(unclassified)}. "
        "Add each to _STRIPE_PERMANENT_REFUSAL_PREFIXES, or to _KNOWN_TRANSIENT here "
        "with a reason why redelivery could succeed."
    )


# Reasons deliberately left transient: redelivering could plausibly succeed.
_KNOWN_TRANSIENT = {
    "order_total_missing",  # the order could gain a total (auth-first paths)
}


@pytest.mark.parametrize(
    "reason,permanent",
    [
        ("cross_tenant_blocked", True),
        ("amount_parse_error:invalid literal", True),
        ("refund_amount_parse_error:invalid literal", True),
        ("amount_mismatch:expected_minor=50000,observed_minor=100", True),
        ("currency_mismatch:order=usd,event=eur", True),
        ("refund_currency_mismatch:order=usd,event=eur", True),
        ("refund_exceeds_order_total:order_total=500,observed=5000", True),
        ("refund_amount_not_positive:observed=0", True),
        ("event_amount_missing", True),
        ("refund_amount_missing", True),
        ("no_order_resolved", False),
        ("order_total_missing", False),
        ("", False),
        (None, False),
    ],
)
def test_refusal_classification(reason: Optional[str], permanent: bool) -> None:
    from routes.webhook_routes import _stripe_refusal_is_permanent

    assert _stripe_refusal_is_permanent(reason) is permanent


@pytest.mark.asyncio
async def test_a_forged_metadata_hint_on_the_payment_path_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The payment resolver has TWO ways to reach an order: the stored
    payment_intent, and the `metadata.order_id` hint. Only the second is
    attacker-chosen. If that path drops its reject reason, a forged hint
    degrades to `no_order_resolved` and the refusal answers 503 — turning an
    attacker-triggerable block into days of redelivery."""
    import routes.webhook_routes as webhook_routes_module
    import db.database as database_module

    h = _install(monkeypatch, event=_succeeded("pi_never_stored"), order_row=None)

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Any:
        if "FROM merchant_psps" in query and "provider_config" in query:
            return {"provider_config": {"webhook_endpoint_secret": SECRET}}
        if "FROM merchant_psps" in query and "merchant_id" in query:
            return {"merchant_id": OWNER}
        if "FROM orders" in query:
            return None  # the payment_intent is not stored anywhere
        raise AssertionError(f"Unexpected query: {query}")

    async def fake_get_order(order_id: str) -> Optional[Dict[str, Any]]:
        assert order_id == "ORD_0123456789ABCDEF"
        return _order(OTHER)  # the forged hint names a FOREIGN order

    monkeypatch.setattr(database_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(webhook_routes_module, "get_order", fake_get_order)

    resp = await _post()

    assert resp.status_code == 200
    assert resp.json()["reason"] == "cross_tenant_blocked"
    assert h.mutations == []


# --------------------------------------------------------------------------
# Blast radius: a per-psp endpoint lives on the MERCHANT'S OWN Stripe account
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unrelated_charge_on_the_merchants_account_is_not_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_ensure_stripe_webhook_endpoint` creates the endpoint with
    `stripe_account=<merchant account>` and subscribes it to
    `payment_intent.succeeded` (`_STRIPE_AFTERCARE_EVENTS`). Stripe endpoints are
    account-wide, so every charge the merchant takes OUTSIDE Pivota — their own
    storefront, invoices, subscriptions — arrives here and resolves to no order.

    Deferring those would 503 on the full ~3-day retry schedule for events that
    can never resolve, and Stripe disables endpoints that fail continuously.
    Losing the endpoint takes down payment finalization AND refunds for that
    merchant — strictly worse than the dropped event we are fixing. An event with
    no Pivota provenance must keep its 200."""
    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_merchants_own_storefront",
                "amount": 12345,
                "amount_received": 12345,
                "currency": "usd",
                "metadata": {},  # no order_id: not ours
            }
        },
    }
    h = _install(monkeypatch, event=event, order_row=None)

    resp = await _post()

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unmatched",
        "event": "payment_intent.succeeded",
        "reason": "no_order_resolved",
    }
    assert h.mutations == []


@pytest.mark.asyncio
async def test_a_cross_tenant_block_on_the_auth_first_branch_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`payment_intent.amount_capturable_updated` drives
    `finalize_authorized_payment_order` — a money event. It unpacked the reject
    reason and discarded it, so a cross-tenant block answered 200 `success` and
    was recorded `processed`: silently gone."""
    event = {
        "type": "payment_intent.amount_capturable_updated",
        "data": {
            "object": {
                "id": "pi_auth_foreign",
                "amount": 50000,
                "currency": "usd",
                "metadata": {"order_id": "ORD_0123456789ABCDEF"},
            }
        },
    }
    h = _install(monkeypatch, event=event, order_row=_order(OTHER))

    resp = await _post()

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "unmatched",
        "event": "payment_intent.amount_capturable_updated",
        "reason": "cross_tenant_blocked",
    }
    assert h.mutations == []
    assert ("unmatched", "cross_tenant_blocked") in [(s, r) for _e, s, r in h.event_status]


@pytest.mark.asyncio
async def test_a_storefront_order_id_is_not_treated_as_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`order_id` in PaymentIntent metadata is a WooCommerce/Magento/custom-cart
    convention, not a Pivota marker. A merchant's own storefront charge — on
    their own Stripe account, delivered to this account-wide endpoint — carries
    one too. Matching on mere PRESENCE would defer every such charge for the full
    retry schedule; only our id shape may defer."""
    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_woo_storefront",
                "amount": 4999,
                "amount_received": 4999,
                "currency": "usd",
                "metadata": {"order_id": "wc-10231", "site_url": "https://shop.example"},
            }
        },
    }
    h = _install(monkeypatch, event=event, order_row=None)

    resp = await _post()

    assert resp.status_code == 200
    assert resp.json()["reason"] == "no_order_resolved"
    assert h.mutations == []


def test_only_our_order_id_shape_counts_as_provenance() -> None:
    from routes.webhook_routes import _stripe_event_names_a_pivota_order

    assert _stripe_event_names_a_pivota_order({"order_id": "ORD_0123456789ABCDEF"}) is True
    # Other systems' ids, and shapes we no longer mint, fail closed to a 200.
    for hint in ("wc-10231", "1042", "ORD_lowercase123456", "ORD_TOOSHORT", "", None):
        assert _stripe_event_names_a_pivota_order({"order_id": hint}) is False
    assert _stripe_event_names_a_pivota_order({}) is False
    assert _stripe_event_names_a_pivota_order(None) is False
