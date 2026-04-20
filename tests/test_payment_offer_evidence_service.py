from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest

import services.payment_offer_evidence_service as module
from models.catalog import PivotPaymentContext


def _row(**overrides: Any) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    base: Dict[str, Any] = {
        "offer_id": "offer::1",
        "source_product_id": "prod_1",
        "source_variant_id": "var_1",
        "incentive_id": "inc_mc",
        "incentive_type": "card_discount",
        "funding_source": "issuer",
        "payment_method_type": "card",
        "card_network": "mastercard",
        "issuer_name": None,
        "wallet_type": None,
        "installment_provider": None,
        "label": "Mastercard 5% Off",
        "benefit_kind": "percentage_off",
        "benefit_value": Decimal("5.00"),
        "benefit_currency": "USD",
        "market": "US",
        "eligibility_confidence": Decimal("0.80"),
        "source_system": "merchant_config",
        "status": "active",
        "starts_at": now - timedelta(days=1),
        "ends_at": now + timedelta(days=1),
        "metadata_json": {},
        "scope_json": {},
        "conditions_json": {},
        "schedule_json": {},
    }
    base.update(overrides)
    return base


def test_emit_payment_offer_analytics_event_redacts_payment_method_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: Dict[str, Any] = {}

    def fake_emit_best_effort(**kwargs: Any) -> None:
        emitted.update(kwargs)

    monkeypatch.setattr("mvp.events.emit_best_effort", fake_emit_best_effort)

    evidence = {
        "pricing_confidence": "context_matched",
        "offers": [
            {
                "payment_offer_id": "inc_mc",
                "benefit_kind": "percentage_off",
                "estimated_savings": "5.00",
                "eligibility": {"status": "context_matched", "reason_codes": []},
            }
        ],
        "decisions": [{"type": "payment_offer_resolution", "reason": "matched"}],
    }

    module.emit_payment_offer_analytics_event(
        event_type="payment_offer.selected",
        merchant_id="merch_1",
        surface="order_create",
        evidence=evidence,
        payment_context=PivotPaymentContext(psp="stripe", payment_method_type="card", card_network="mastercard"),
        selected_payment_offer_id="inc_mc",
        payment_method_evidence={
            "psp": "stripe",
            "payment_method_type": "card",
            "card_network": "mastercard",
            "client_secret": "pi_secret_should_not_emit",
            "card_last4": "4242",
            "verification_status": "psp_verified",
        },
        quote_id="q_1",
        order_id="ord_1",
        adapter="stripe",
        idempotency_key="test-idk",
    )

    payload = emitted["payload"]
    assert emitted["event_type"] == "payment_offer.selected"
    assert payload["selected_payment_offer_id"] == "inc_mc"
    assert payload["status_counts"] == {"context_matched": 1}
    assert payload["payment_method_evidence"] == {
        "psp": "stripe",
        "payment_method_type": "card",
        "card_network": "mastercard",
        "verification_status": "psp_verified",
    }
    assert "client_secret" not in payload["payment_method_evidence"]
    assert "card_last4" not in payload["payment_method_evidence"]


@pytest.mark.asyncio
async def test_payment_offer_evidence_returns_display_estimate_without_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_all(_query: str, _params: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [_row()]

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    target = module.PaymentOfferTarget(
        target_id="var_1",
        merchant_id="merch_1",
        product_id="prod_1",
        variant_id="var_1",
        amount=Decimal("100.00"),
        currency="USD",
        market="US",
    )
    result = await module.resolve_payment_offer_evidence_for_targets(
        merchant_id="merch_1",
        targets=[target],
    )

    evidence = result["var_1"]
    assert evidence["pricing_confidence"] == "display_estimate"
    assert evidence["offers"][0]["eligibility"]["status"] == "potential"
    assert evidence["offers"][0]["estimated_savings"] == "5.00"
    assert evidence["offers"][0]["estimated_total_after_payment_offer"] == "95.00"
    assert evidence["offers"][0]["application_policy"]["affects_psp_amount_v1"] is False
    payment_pricing = module.payment_pricing_summary(
        evidence=evidence,
        checkout_total=Decimal("100.00"),
        currency="USD",
    )
    assert payment_pricing == {
        "checkout_total": "100.00",
        "currency": "USD",
        "estimated_payment_benefit": "5.00",
        "estimated_total_after_payment_offer": "95.00",
        "display_only": True,
        "affects_psp_amount_v1": False,
    }


@pytest.mark.asyncio
async def test_payment_offer_evidence_filters_wrong_card_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch_all(_query: str, _params: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            _row(incentive_id="inc_mc", card_network="mastercard", label="Mastercard 5% Off"),
            _row(incentive_id="inc_visa", card_network="visa", label="Visa 5% Off"),
        ]

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    target = module.PaymentOfferTarget(
        target_id="var_1",
        merchant_id="merch_1",
        product_id="prod_1",
        variant_id="var_1",
        amount=Decimal("100.00"),
        currency="USD",
        market="US",
    )
    result = await module.resolve_payment_offer_evidence_for_targets(
        merchant_id="merch_1",
        targets=[target],
        payment_context=PivotPaymentContext(payment_method_type="card", card_network="mastercard"),
    )

    evidence = result["var_1"]
    assert evidence["pricing_confidence"] == "context_matched"
    assert [offer["payment_offer_id"] for offer in evidence["offers"]] == ["inc_mc"]
    assert evidence["offers"][0]["eligibility"]["status"] == "context_matched"
    assert any(decision["reason"] == "card_network_mismatch" for decision in evidence["decisions"])


@pytest.mark.asyncio
async def test_payment_offer_evidence_rejects_expired_market_and_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)

    async def fake_fetch_all(_query: str, _params: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            _row(incentive_id="expired", ends_at=now - timedelta(minutes=1)),
            _row(incentive_id="wrong_market", market="CA"),
            _row(incentive_id="wrong_scope", scope_json={"variantIds": ["other_var"]}),
        ]

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)

    target = module.PaymentOfferTarget(
        target_id="var_1",
        merchant_id="merch_1",
        product_id="prod_1",
        variant_id="var_1",
        amount=Decimal("100.00"),
        currency="USD",
        market="US",
    )
    result = await module.resolve_payment_offer_evidence_for_targets(
        merchant_id="merch_1",
        targets=[target],
    )

    evidence = result["var_1"]
    assert evidence["offers"] == []
    reasons = {decision["reason"] for decision in evidence["decisions"]}
    assert {"expired", "market_mismatch", "target_out_of_scope"}.issubset(reasons)
