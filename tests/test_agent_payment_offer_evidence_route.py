from __future__ import annotations

from typing import Any, Dict

import pytest

import routes.agent_api as module
from models.order import RecordPaymentOfferEvidenceRequest


class _Context:
    agent_id = "agent_1"
    agent_name = "Test Agent"

    def can_access_merchant(self, merchant_id: str) -> bool:
        return merchant_id == "merch_1"


@pytest.mark.asyncio
async def test_record_payment_offer_evidence_updates_metadata_without_sensitive_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated: Dict[str, Any] = {}
    emitted: Dict[str, Any] = {}

    async def fake_get_order(order_id: str) -> Dict[str, Any]:
        assert order_id == "ORD_1"
        return {
            "order_id": "ORD_1",
            "merchant_id": "merch_1",
            "metadata": {
                "pricing_quote": {
                    "quote_id": "quote_1",
                    "payment_offer_evidence": {
                        "pricing_confidence": "display_estimate",
                        "offers": [{"payment_offer_id": "mc_5"}],
                    },
                }
            },
        }

    async def fake_update_order(order_id: str, update_data: Dict[str, Any]) -> bool:
        updated["order_id"] = order_id
        updated["update_data"] = update_data
        return True

    def fake_emit(**kwargs: Any) -> None:
        emitted.update(kwargs)

    monkeypatch.setattr(module, "get_order", fake_get_order)
    monkeypatch.setattr(module, "update_order", fake_update_order)
    monkeypatch.setattr(module, "emit_payment_offer_analytics_event", fake_emit)

    response = await module.agent_record_payment_offer_evidence(
        RecordPaymentOfferEvidenceRequest(
            order_id="ORD_1",
            selected_payment_offer_id="mc_5",
            payment_method_evidence={
                "psp": "stripe",
                "payment_method_type": "card",
                "card_network": "mastercard",
                "client_secret": "pi_secret_should_not_store",
                "card_last4": "4242",
            },
            surface="checkout",
        ),
        context=_Context(),
    )

    assert response["status"] == "success"
    metadata = updated["update_data"]["metadata"]
    event = metadata["payment_offer_events"][0]
    assert updated["order_id"] == "ORD_1"
    assert event["selected_payment_offer_id"] == "mc_5"
    assert event["payment_method_evidence"] == {
        "psp": "stripe",
        "payment_method_type": "card",
        "card_network": "mastercard",
    }
    assert "client_secret" not in str(metadata)
    assert "card_last4" not in str(metadata)
    assert metadata["selected_payment_offer_id"] == "mc_5"
    assert emitted["event_type"] == "payment_offer.selected"
    assert emitted["order_id"] == "ORD_1"


@pytest.mark.asyncio
async def test_record_payment_offer_evidence_allows_quote_only_non_mutating_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: Dict[str, Any] = {}

    async def fail_update_order(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("quote-only evidence must not mutate orders")

    def fake_emit(**kwargs: Any) -> None:
        emitted.update(kwargs)

    monkeypatch.setattr(module, "update_order", fail_update_order)
    monkeypatch.setattr(module, "emit_payment_offer_analytics_event", fake_emit)

    response = await module.agent_record_payment_offer_evidence(
        RecordPaymentOfferEvidenceRequest(
            merchant_id="merch_1",
            quote_id="quote_1",
            payment_method_evidence={
                "psp": "stripe",
                "available_payment_methods": ["apple_pay", "google_pay"],
            },
            surface="checkout",
        ),
        context=_Context(),
    )

    assert response["status"] == "success"
    assert response["event_type"] == "payment_offer.available"
    assert response["application_policy"]["affects_psp_amount_v1"] is False
    assert emitted["quote_id"] == "quote_1"
    assert emitted["payment_method_evidence"]["available_payment_methods"] == [
        "apple_pay",
        "google_pay",
    ]
