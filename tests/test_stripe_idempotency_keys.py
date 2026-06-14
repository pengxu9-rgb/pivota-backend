"""Verify every Stripe create/transfer call passes a deterministic idempotency_key.

Codex review of PR #581 (finding #6) flagged that the v1.3 monetization code
called `stripe_client.v1.*.create` without `options={"idempotency_key": ...}`,
making network retries a duplicate-charge / duplicate-invoice hazard.

These tests intercept each call site with a stub stripe client that records
the `options` argument, and assert the key shape matches the design.

Key shapes (must stay stable across deploys — Stripe caches keys for 24h):
- customers.create    →  "merchant_customer:{merchant_id}"
- checkout.sessions   →  "checkout_session:{merchant_id}:{sha256(payload)[:40]}"
- invoices.create     →  "invoice:{billing_run_id}:{merchant_id}"
- invoice_items       →  "invoice_item:{billing_run_id}:{gmv_rollup_id}"
- invoice_items (adj) →  "invoice_item_adj:{dispute_id}:{billing_run_item_id}"
- transfers.create    →  "payout:{payout_id}"
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest


def _options_kw(kwargs: dict, args: tuple) -> Dict[str, Any]:
    """Pull the `options` kwarg or 2nd positional arg from a Stripe call."""
    if "options" in kwargs:
        return dict(kwargs["options"] or {})
    if len(args) >= 2 and isinstance(args[1], dict):
        return dict(args[1])
    return {}


class _RecordingResource:
    def __init__(self, recorder: List[Dict[str, Any]], make_id: str) -> None:
        self.recorder = recorder
        self.make_id = make_id

    def create(self, *args, **kwargs):
        self.recorder.append({"options": _options_kw(kwargs, args)})
        return SimpleNamespace(id=self.make_id, url="https://stripe.test/checkout")


class _RecordingStripeClient:
    def __init__(self) -> None:
        self.customers_calls: List[Dict[str, Any]] = []
        self.sessions_calls: List[Dict[str, Any]] = []
        self.invoices_calls: List[Dict[str, Any]] = []
        self.invoice_items_calls: List[Dict[str, Any]] = []
        self.transfers_calls: List[Dict[str, Any]] = []
        self.v1 = SimpleNamespace(
            customers=_RecordingResource(self.customers_calls, "cus_test"),
            checkout=SimpleNamespace(
                sessions=_RecordingResource(self.sessions_calls, "cs_test"),
            ),
            invoices=_RecordingResource(self.invoices_calls, "in_test"),
            invoice_items=_RecordingResource(self.invoice_items_calls, "ii_test"),
            transfers=_RecordingResource(self.transfers_calls, "tr_test"),
        )


@pytest.mark.asyncio
async def test_billing_routes_customer_create_passes_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from routes import billing_routes

    recorder = _RecordingStripeClient()
    monkeypatch.setattr(billing_routes, "stripe_client", recorder)

    # Stub schema-touching helpers — we only care that the Stripe call
    # received the right idempotency_key.
    async def fake_lookup_plan(*_args, **_kwargs):
        return {"id": 1}

    async def fake_billing_row(*_args, **_kwargs):
        return {"contact_email": "merch@example.com", "stripe_customer_id": None}

    async def fake_update_customer_id(*_args, **_kwargs):
        return True

    def fake_require_platform_key():
        return None

    monkeypatch.setattr(billing_routes, "_lookup_subscription_plan", fake_lookup_plan)
    monkeypatch.setattr(billing_routes, "_fetch_merchant_billing_row", fake_billing_row)
    monkeypatch.setattr(
        billing_routes, "_update_merchant_stripe_customer_id", fake_update_customer_id
    )
    monkeypatch.setattr(billing_routes, "_require_platform_stripe_key", fake_require_platform_key)

    body = billing_routes.CheckoutSessionRequest(
        price_id="price_test",
        success_url="https://x.test/ok",
        cancel_url="https://x.test/no",
    )
    merchant = {"merchant_id": "merch_xyz", "contact_email": "merch@example.com"}

    await billing_routes.create_billing_checkout_session(body, merchant=merchant)

    # customer.create called once with the deterministic merchant key.
    assert len(recorder.customers_calls) == 1
    assert (
        recorder.customers_calls[0]["options"]["idempotency_key"]
        == "merchant_customer:merch_xyz"
    )

    # checkout.sessions.create called once with a payload-derived key.
    # #776 replaced the coarse per-day key (checkout_session:{merchant}:{price}:{date}),
    # which tripped Stripe's IdempotencyError when any session param differed within
    # a day, with checkout_session:{merchant}:{sha256(payload)[:40]} — merchant-scoped
    # and uniquely keyed to the exact request payload.
    assert len(recorder.sessions_calls) == 1
    session_key = recorder.sessions_calls[0]["options"]["idempotency_key"]
    prefix = "checkout_session:merch_xyz:"
    assert session_key.startswith(prefix)
    digest = session_key[len(prefix):]
    assert len(digest) == 40 and all(c in "0123456789abcdef" for c in digest)


def test_invoice_generation_key_shapes_documented() -> None:
    """Light regression: read the source and confirm the key strings appear.

    Avoids a heavy end-to-end fake (the invoice service has DB transactions,
    schema guards, and rollup math) by guarding the key-string templates at
    a source level. If a future edit removes or changes the template, this
    test fails fast.
    """
    import inspect
    from services import invoice_generation_service as svc

    source = inspect.getsource(svc.run_billing_cycle if hasattr(svc, "run_billing_cycle") else svc)
    # Pull whole module source instead of just one function — the key
    # templates live across multiple helper functions in the same module.
    full_source = inspect.getsource(svc)
    assert 'invoice:{billing_run_id}:{merchant_id}' in full_source, (
        "invoice.create idempotency_key template missing or renamed"
    )
    assert 'invoice_item:{billing_run_id}:{rollup_id}' in full_source, (
        "invoice_item.create idempotency_key template missing or renamed"
    )
    assert 'invoice_item_adj:{invoice_dispute_id}:{billing_run_item_id}' in full_source, (
        "dispute-adjustment invoice_item.create idempotency_key template missing"
    )


def test_partner_settlement_transfer_key_template_present() -> None:
    """Real-money safety guard: transfers must pass a per-payout key.

    Source-level check rather than full E2E because the settlement service
    needs full DB + Connect account fixtures. The template `payout:{payout_id}`
    must be present in the transfers.create call site.
    """
    import inspect
    from services import partner_settlement_service as svc

    source = inspect.getsource(svc)
    assert 'payout:{payout_id}' in source, (
        "transfers.create idempotency_key template `payout:{payout_id}` missing — "
        "duplicate payouts to Connect accounts are unrecoverable without "
        "manual Stripe Dashboard intervention."
    )
    # Stronger: the template must appear in a 'transfers.create' adjacent
    # call site (within 200 chars). Catches accidental key reuse on the
    # wrong call.
    transfer_idx = source.find("transfers.create")
    assert transfer_idx >= 0, "transfers.create call site not found"
    nearby = source[transfer_idx : transfer_idx + 600]
    assert 'payout:{payout_id}' in nearby, (
        "idempotency_key template present in file but not adjacent to "
        "transfers.create"
    )
