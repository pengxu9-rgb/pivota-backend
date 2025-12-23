from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mvp.schemas import (
    AuditEvent,
    Geo,
    HashChain,
    LedgerEvent,
    LedgerIngest,
    LedgerSource,
    Money,
    OfferObject,
    OfferPricing,
    PreFlightResult,
    ProductRef,
    QuoteRef,
)


def test_offer_object_validates_and_has_version():
    now = datetime.now(timezone.utc)
    offer = OfferObject(
        offer_id="offer_test",
        merchant_id="merch_1",
        product_ref=ProductRef(platform="shopify", platform_product_id="p1", variant_id="v1"),
        geo=Geo(country="US", postal_code="94107"),
        pricing=OfferPricing(currency="USD", subtotal=10.0, discount_total=0.0, shipping_fee=1.0, tax=0.8, total=11.8),
        quote_ref=QuoteRef(quote_id="q_1", expires_at=now, engine="shopify_rest_checkout", engine_ref="tok"),
    )
    assert offer.schema_version == "0.1"
    payload = offer.model_dump(mode="json")
    assert payload["schema_version"] == "0.1"
    assert payload["quote_ref"]["quote_id"] == "q_1"


def test_preflight_result_requires_status():
    with pytest.raises(Exception):
        PreFlightResult(
            merchant_id="merch_1",
            product_ref=ProductRef(platform="shopify", platform_product_id="p1", variant_id="v1"),
            geo=Geo(country="US"),
        )


def test_audit_event_requires_chain_hash():
    now = datetime.now(timezone.utc)
    with pytest.raises(Exception):
        AuditEvent(
            audit_event_id="audit_1",
            merchant_id="merch_1",
            actor={"type": "agent", "ref": "agent_1"},
            action="submit_payment",
            occurred_at=now,
            chain={"prev_chain_hash": None},
        )

    ok = AuditEvent(
        audit_event_id="audit_2",
        merchant_id="merch_1",
        actor={"type": "agent", "ref": "agent_1"},
        action="submit_payment",
        occurred_at=now,
        chain=HashChain(prev_chain_hash=None, chain_hash="x" * 64),
    )
    assert ok.chain.chain_hash == "x" * 64


def test_ledger_event_validates():
    now = datetime.now(timezone.utc)
    e = LedgerEvent(
        event_id="led_1",
        merchant_id="merch_1",
        order_id="ORD_1",
        event_type="payment_succeeded",
        source=LedgerSource(type="psp_webhook", psp="stripe", external_event_id="evt_1"),
        amount=Money(value=10.0, currency="USD"),
        occurred_at=now,
        ingest=LedgerIngest(received_at=now, signature_verified=True, idempotency_key="stripe:evt_1"),
        payload_sha256="y" * 64,
        chain=HashChain(prev_chain_hash=None, chain_hash="z" * 64),
    )
    assert e.schema_version == "0.1"

