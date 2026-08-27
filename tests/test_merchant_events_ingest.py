import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

MERCHANT_ID = "merch_universal"
API_KEY = "mk_universal_secret"


def _client() -> TestClient:
    from routes.merchant_events import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _event(**overrides):
    event = {
        "event_id": "cafe24:evt_1",
        "event_type": "cart.item_added",
        "occurred_at": "2026-08-26T12:34:56Z",
        "platform": "cafe24",
        "store_id": "mall_123",
        "session_id": "sess_abc",
        "cart_id": "cart_456",
        "canonical_product_id": "prod_789",
        "agent_id": "chatgpt-agent",
        "source_channel": "chatgpt",
        "amount_cents": 2599,
        "currency": "usd",
        "metadata": {"quantity": 2},
    }
    event.update(overrides)
    return event


def _post(payload, *, sign_key=API_KEY, merchant_id=MERCHANT_ID, signature=None):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signed = signature if signature is not None else hmac.new(
        sign_key.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Pivota-Merchant-Id": merchant_id,
        "X-Pivota-Signature": signed,
    }
    return _client().post("/merchant-events/v1/batch", content=body, headers=headers)


@pytest.fixture
def patched_route(monkeypatch):
    calls = []

    async def fake_merchant(merchant_id):
        if merchant_id != MERCHANT_ID:
            return None
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY, "status": "approved"}

    async def fake_ingest(**kwargs):
        calls.append(kwargs)
        return {
            "accepted": len(kwargs["batch"].events),
            "duplicates": 0,
            "events": [
                {
                    "event_id": event.event_id,
                    "ledger_event_id": f"ledger:{event.event_id}",
                    "interaction_id": "int_1",
                    "duplicate": False,
                }
                for event in kwargs["batch"].events
            ],
        }

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr("routes.merchant_events.ingest_merchant_event_batch", fake_ingest)
    return calls


def test_signed_platform_neutral_batch_is_accepted_and_normalized(patched_route):
    response = _post({"events": [_event()]})

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1
    assert len(patched_route) == 1
    call = patched_route[0]
    assert call["merchant_id"] == MERCHANT_ID
    event = call["batch"].events[0]
    assert event.platform == "cafe24"
    assert event.currency == "USD"
    assert event.occurred_at.isoformat() == "2026-08-26T12:34:56+00:00"


def test_invalid_signature_never_reaches_ingest(patched_route):
    response = _post({"events": [_event()]}, sign_key="wrong-key")

    assert response.status_code == 401
    assert patched_route == []


def test_unknown_event_type_is_rejected(patched_route):
    response = _post({"events": [_event(event_type="shopify.magic_event")]})

    assert response.status_code == 422
    assert patched_route == []


def test_event_requires_a_stitch_key(patched_route):
    event = _event()
    for key in ("session_id", "cart_id"):
        event.pop(key)
    response = _post({"events": [event]})

    assert response.status_code == 422
    assert patched_route == []


def test_batch_is_bounded_to_one_hundred_events(patched_route):
    events = [_event(event_id=f"evt_{index}") for index in range(101)]
    response = _post({"events": events})

    assert response.status_code == 422
    assert patched_route == []


@pytest.mark.asyncio
async def test_ingest_maps_adapter_refs_and_idempotency_to_canonical_ledger(monkeypatch):
    from services import merchant_event_ingest_service as service

    calls = []

    async def fake_record(**kwargs):
        calls.append(kwargs)
        return {"event_id": "evt_ledger", "interaction_id": "int_ledger", "duplicate": False}

    monkeypatch.setattr(service, "record_commerce_event", fake_record)
    batch = service.MerchantEventBatch.model_validate({"events": [_event()]})
    result = await service.ingest_merchant_event_batch(merchant_id=MERCHANT_ID, batch=batch)

    assert result["accepted"] == 1
    assert result["duplicates"] == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["merchant_id"] == MERCHANT_ID
    assert call["event_type"] == "cart.item_added"
    assert call["upstream_idempotency_key"] == "cafe24:evt_1"
    assert call["store_id"] == "mall_123"
    assert call["cart_id"] == "cart_456"
    assert call["session_id"] == "sess_abc"
    assert call["metadata"]["quantity"] == 2
    assert call["metadata"]["amount_cents"] == 2599
    assert call["metadata"]["currency"] == "USD"


def test_universal_event_types_advance_interaction_status():
    from services.commerce_interaction_service import _status_from_event

    assert _status_from_event("product.viewed", None) == "pdp_viewed"
    assert _status_from_event("cart.item_added", "pdp_viewed") == "cart_active"
    assert _status_from_event("checkout.started", "cart_active") == "checkout_started"
    assert _status_from_event("payment.succeeded", "payment_pending") == "paid"
    assert _status_from_event("refund.succeeded", "paid") == "refunded"


@pytest.mark.asyncio
async def test_session_stitch_lookup_is_scoped_to_merchant_and_store(monkeypatch):
    from services import commerce_interaction_service as service

    queries = []

    class FakeDB:
        async def fetch_one(self, query):
            queries.append(query)
            return {"interaction_id": "int_existing"}

    monkeypatch.setattr(service, "database", FakeDB())
    result = await service._lookup_existing_interaction(
        {
            "merchant_id": "merchant_a",
            "store_id": "store_a",
            "session_id": "session_shared",
        }
    )

    assert result == {"interaction_id": "int_existing"}
    compiled = queries[0].compile()
    sql = str(compiled)
    values = set(compiled.params.values())
    assert "commerce_interactions.merchant_id" in sql
    assert "commerce_interactions.store_id" in sql
    assert "commerce_interactions.session_id" in sql
    assert {"merchant_a", "store_a", "session_shared"}.issubset(values)
