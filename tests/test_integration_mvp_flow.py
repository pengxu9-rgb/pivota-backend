from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mvp.governance import DefaultPolicyEvaluator, GovernanceService, PolicyInput
from mvp.ledger_events import emit_ledger_event_best_effort
from mvp.offer import build_offers_from_quote, preflight_offers


def test_integration_offer_preflight_governance_and_ledger_emission(monkeypatch):
    now = datetime.now(timezone.utc)
    offers = build_offers_from_quote(
        merchant_id="merch_1",
        quote_id="q_1",
        expires_at=now + timedelta(minutes=5),
        engine="shopify_rest_checkout",
        engine_ref="ref_1",
        currency="USD",
        pricing={"subtotal": "10.00", "discount_total": "0.00", "shipping_fee": "0.00", "tax": "0.00", "total": "10.00"},
        line_items=[{"product_id": "p1", "variant_id": "v1"}],
        delivery_options=None,
        shipping_address={"country": "US", "postal_code": "94103", "city": "SF", "state": "CA"},
    )
    assert offers and offers[0].offer_id

    svc = GovernanceService(evaluator=DefaultPolicyEvaluator(enforce=False))
    decision = svc.evaluate(
        PolicyInput(
            merchant_id="merch_1",
            actor_type="agent",
            actor_ref="agent_1",
            action="submit_payment",
            amount=10.0,
            currency="USD",
            geo={"country": "US"},
            consent_scopes=[],
            approval_id=None,
        )
    )

    results = preflight_offers(
        offers=offers,
        policy_hashes_available=False,
        hil_required=(decision.decision == "require_hil"),
        hil_reason=",".join(decision.reason_codes) if decision.reason_codes else None,
        now=now,
    )
    assert len(results) == 1
    assert results[0].status in {"pass", "warn", "fail"}

    captured = []

    def fake_emit_best_effort(*, event_type, payload, merchant_id, geo, surface, adapter, risk_tier, idempotency_key):
        captured.append(
            {
                "event_type": event_type,
                "payload": payload,
                "merchant_id": merchant_id,
                "risk_tier": risk_tier,
                "idempotency_key": idempotency_key,
            }
        )

    monkeypatch.setattr("mvp.ledger_events.emit_best_effort", fake_emit_best_effort)

    emit_ledger_event_best_effort(
        merchant_id="merch_1",
        event_type="order_created",
        order_id="ord_1",
        source={"type": "backend"},
        amount={"value": 10.0, "currency": "USD"},
        refs={"payment_intent_id": None},
        geo={"country": "US"},
        surface="backend",
        adapter="unit_test",
        risk_tier=decision.risk_tier,
        idempotency_key="idem_1",
    )
    assert captured

