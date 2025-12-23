from __future__ import annotations

from mvp.ledger_events import build_ledger_event


def test_ledger_event_chain_advances_per_merchant():
    e1 = build_ledger_event(
        merchant_id="merch_chain_1",
        event_type="order_created",
        order_id="ord_1",
        source={"type": "backend"},
        amount={"value": 10.0, "currency": "USD"},
        refs=None,
        idempotency_key="idem_1",
        signature_verified=False,
    )
    e2 = build_ledger_event(
        merchant_id="merch_chain_1",
        event_type="payment_succeeded",
        order_id="ord_1",
        source={"type": "psp", "psp": "stripe", "external_event_id": "pi_1"},
        amount={"value": 10.0, "currency": "USD"},
        refs={"payment_intent_id": "pi_1"},
        idempotency_key="idem_2",
        signature_verified=False,
    )
    assert e2.chain.prev_chain_hash == e1.chain.chain_hash
    assert e2.payload_sha256

