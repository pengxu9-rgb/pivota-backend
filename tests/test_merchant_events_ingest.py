import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
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


@pytest.mark.parametrize(
    "key",
    ["email", "customer_email", "phone", "billing_address", "access_token", "cookie"],
)
def test_metadata_rejects_sensitive_keys(patched_route, key):
    response = _post({"events": [_event(metadata={key: "must-not-be-stored"})]})

    assert response.status_code == 422
    assert patched_route == []


def test_metadata_rejects_unknown_top_level_keys(patched_route):
    response = _post({"events": [_event(metadata={"arbitrary_blob": {"safe": True}})]})

    assert response.status_code == 422
    assert patched_route == []


def test_metadata_rejects_nested_sensitive_keys(patched_route):
    response = _post(
        {
            "events": [
                _event(
                    metadata={
                        "native_line_items": [
                            {"product_id": "p1", "customer_email": "buyer@example.com"}
                        ]
                    }
                )
            ]
        }
    )

    assert response.status_code == 422
    assert patched_route == []


def test_allowlisted_metadata_is_accepted(patched_route):
    response = _post(
        {
            "events": [
                _event(
                    metadata={
                        "quantity": 2,
                        "native_topic": "orders/create",
                        "native_line_items": [{"product_id": "p1", "quantity": 2}],
                    }
                )
            ]
        }
    )

    assert response.status_code == 200, response.text


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
    assert _status_from_event("order.created", "paid") == "paid"
    assert _status_from_event("order.paid", "cancelled") == "cancelled"
    assert _status_from_event("payment.failed", "refunded") == "refunded"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("ref_name", ["order_id", "checkout_id", "quote_id", "refund_id", "return_id"])
async def test_commerce_ref_lookup_is_scoped_to_merchant_and_store(monkeypatch, ref_name):
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
            ref_name: "ref_shared",
        }
    )

    assert result == {"interaction_id": "int_existing"}
    compiled = queries[0].compile()
    sql = str(compiled)
    values = set(compiled.params.values())
    assert "commerce_interactions.merchant_id" in sql
    assert "commerce_interactions.store_id" in sql
    assert f"commerce_interactions.{ref_name}" in sql
    assert {"merchant_a", "store_a", "ref_shared"}.issubset(values)


@pytest.mark.asyncio
async def test_order_lookup_requires_tenant_scope(monkeypatch):
    from services import commerce_interaction_service as service

    queries = []

    class FakeDB:
        async def fetch_one(self, query):
            queries.append(query)
            return None

    monkeypatch.setattr(service, "database", FakeDB())
    await service.find_interaction_by_order_id(
        "order_shared",
        merchant_id="merchant_a",
        store_id="store_a",
    )

    compiled = queries[0].compile()
    sql = str(compiled)
    values = set(compiled.params.values())
    assert "commerce_interactions.merchant_id" in sql
    assert "commerce_interactions.store_id" in sql
    assert "commerce_interactions.order_id" in sql
    assert {"merchant_a", "store_a", "order_shared"}.issubset(values)


@pytest.mark.asyncio
async def test_duplicate_event_is_returned_before_interaction_mutation(monkeypatch):
    from services import commerce_interaction_service as service

    class FakeDB:
        async def fetch_one(self, _query):
            return {
                "event_id": "evt_existing",
                "interaction_id": "int_original",
            }

    async def fail_if_called(**_kwargs):
        raise AssertionError("duplicate delivery must not mutate the interaction")

    monkeypatch.setattr(service, "database", FakeDB())
    monkeypatch.setattr(service, "ensure_interaction", fail_if_called)
    result = await service._record_commerce_event_unlocked(
        event_type="order.paid",
        metadata_with_taxonomy={},
        occurred=datetime(2026, 8, 26, tzinfo=timezone.utc),
        source="test",
        upstream_idempotency_key="delivery-1",
        actor_type=None,
        actor_id=None,
        refs={"merchant_id": "merchant_a"},
    )

    assert result == {
        "event_id": "evt_existing",
        "interaction_id": "int_original",
        "duplicate": True,
    }


def test_unique_violation_detection_handles_wrapped_driver_error():
    from services.commerce_interaction_service import _is_unique_violation

    class DriverError(Exception):
        sqlstate = "23505"

    class WrapperError(Exception):
        pass

    wrapped = WrapperError("insert failed")
    wrapped.__cause__ = DriverError("duplicate key")
    assert _is_unique_violation(wrapped) is True


@pytest.mark.asyncio
async def test_out_of_order_event_preserves_terminal_status_and_latest_event(monkeypatch):
    from services import commerce_interaction_service as service

    newer = datetime(2026, 8, 26, 13, 0, tzinfo=timezone.utc)
    older = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    existing = {
        "interaction_id": "int_1",
        "merchant_id": "merchant_a",
        "store_id": "store_a",
        "order_id": "order_1",
        "status": "paid",
        "latest_event_type": "order.paid",
        "first_occurred_at": newer,
        "last_occurred_at": newer,
        "metadata": {},
    }
    writes = []

    class FakeDB:
        async def fetch_one(self, _query):
            return existing

        async def execute(self, query, *_args):
            writes.append(query)

    monkeypatch.setattr(service, "database", FakeDB())
    await service.ensure_interaction(
        merchant_id="merchant_a",
        store_id="store_a",
        order_id="order_1",
        latest_event_type="order.created",
        first_occurred_at=older,
        last_occurred_at=older,
    )

    params = writes[0].compile().params
    assert params["status"] == "paid"
    assert params["latest_event_type"] == "order.paid"
    assert params["last_occurred_at"] == newer
    assert params["first_occurred_at"] == older


def test_store_scoped_unique_indexes_coalesce_missing_store_id():
    from db.commerce_interactions import commerce_interactions

    target_names = {
        "idx_commerce_interactions_click_id_unique",
        "idx_commerce_interactions_quote_id_unique",
        "idx_commerce_interactions_checkout_id_unique",
        "idx_commerce_interactions_order_id_unique",
        "idx_commerce_interactions_refund_id_unique",
        "idx_commerce_interactions_return_id_unique",
    }
    indexes = {index.name: index for index in commerce_interactions.indexes}

    for name in target_names:
        index = indexes[name]
        rendered = str(index).lower()
        assert index.unique is True
        assert "merchant_id" in rendered
        assert "coalesce" in rendered
