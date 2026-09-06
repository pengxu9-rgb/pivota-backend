"""The HMAC collector proves the merchant, not the store (PR-0.3).

Before this, `POST /merchant-events/v1/batch` wrote whatever `store_id` and
`platform` the body carried. Interaction ids and every stitch lookup are
scoped by (merchant_id, store_id), so an event under a store id the native
webhook never uses fragments one purchase into two interactions that cannot
merge — and a collector could label itself any platform it liked.
"""

from __future__ import annotations

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

from services.merchant_event_ingest_service import MerchantEventBatch  # noqa: E402
from services.merchant_event_store_binding import (  # noqa: E402
    MerchantEventBindingError,
    bind_batch_to_stores,
)

MERCHANT_ID = "merch_binding"
API_KEY = "mk_binding_secret"


def _batch(*events):
    return MerchantEventBatch.model_validate({"events": list(events)})


def _event(**overrides):
    event = {
        "event_id": "evt_1",
        "event_type": "cart.item_added",
        "occurred_at": "2026-09-04T10:00:00Z",
        "session_id": "sess_1",
    }
    event.update(overrides)
    return event


# ---- the pure rule ----------------------------------------------------------


def test_a_known_store_is_bound_and_its_platform_is_authoritative():
    batch = bind_batch_to_stores(
        _batch(_event(store_id="store_a")), stores={"store_a": "shopify"}
    )
    assert batch.events[0].store_id == "store_a"
    # The model default `custom` means "not said"; the store decides.
    assert batch.events[0].platform == "shopify"


def test_an_explicit_matching_platform_is_accepted_case_insensitively():
    batch = bind_batch_to_stores(
        _batch(_event(store_id="store_a", platform="Shopify")), stores={"store_a": "shopify"}
    )
    assert batch.events[0].platform == "shopify"


def test_an_explicit_platform_that_disagrees_with_the_store_is_refused():
    with pytest.raises(MerchantEventBindingError) as error:
        bind_batch_to_stores(
            _batch(_event(store_id="store_a", platform="woocommerce")),
            stores={"store_a": "shopify"},
        )
    assert error.value.status_code == 422
    assert "platform" in error.value.detail


def test_an_unknown_store_and_an_inactive_store_share_one_message():
    with pytest.raises(MerchantEventBindingError) as error:
        bind_batch_to_stores(_batch(_event(store_id="store_zzz")), stores={"store_a": "shopify"})
    assert error.value.status_code == 422
    assert "store_zzz" not in error.value.detail
    assert error.value.detail.endswith("is not an active connected store")


def test_a_missing_store_id_is_filled_only_for_a_single_store_merchant():
    batch = bind_batch_to_stores(_batch(_event()), stores={"only": "cafe24"})
    assert batch.events[0].store_id == "only"
    assert batch.events[0].platform == "cafe24"

    with pytest.raises(MerchantEventBindingError) as error:
        bind_batch_to_stores(_batch(_event()), stores={"a": "cafe24", "b": "shopify"})
    assert error.value.status_code == 422
    assert "more than one connected store" in error.value.detail


def test_a_merchant_with_no_connected_store_cannot_write():
    with pytest.raises(MerchantEventBindingError) as error:
        bind_batch_to_stores(_batch(_event(store_id="store_a")), stores={})
    assert error.value.status_code == 422


def test_a_merchant_collector_may_not_claim_the_psp_surface():
    with pytest.raises(MerchantEventBindingError) as error:
        bind_batch_to_stores(
            _batch(_event(store_id="store_a", surface="PSP")), stores={"store_a": "shopify"}
        )
    assert error.value.status_code == 422
    # The probe surface only lowers the caller's standing and stays allowed.
    batch = bind_batch_to_stores(
        _batch(_event(store_id="store_a", surface="ops_canary")), stores={"store_a": "shopify"}
    )
    assert batch.events[0].surface == "ops_canary"


def test_one_bad_event_refuses_the_whole_batch_before_any_mutation():
    good = _event(event_id="evt_good", store_id="store_a")
    bad = _event(event_id="evt_bad", store_id="store_zzz")
    batch = _batch(good, bad)
    with pytest.raises(MerchantEventBindingError):
        bind_batch_to_stores(batch, stores={"store_a": "shopify"})
    # The caller gets a 422 and retries the same batch; nothing was written.


# ---- the route ---------------------------------------------------------------


def _client() -> TestClient:
    from routes.merchant_events import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signed = hmac.new(API_KEY.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _client().post(
        "/merchant-events/v1/batch",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-Merchant-Id": MERCHANT_ID,
            "X-Pivota-Signature": signed,
        },
    )


@pytest.fixture
def route(monkeypatch):
    state = {"stores": {"store_a": "shopify", "store_b": "cafe24"}, "ingested": [], "lookups": []}

    async def fake_merchant(merchant_id):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY, "status": "approved"} if merchant_id == MERCHANT_ID else None

    async def fake_stores(merchant_id):
        state["lookups"].append(merchant_id)
        return dict(state["stores"])

    async def fake_ingest(**kwargs):
        state["ingested"].append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr("routes.merchant_events.connected_store_index", fake_stores)
    monkeypatch.setattr("routes.merchant_events.ingest_merchant_event_batch", fake_ingest)
    return state


def test_route_binds_the_authenticated_merchants_stores_and_writes(route):
    response = _post({"events": [_event(store_id="store_b", platform="cafe24")]})
    assert response.status_code == 200, response.text
    assert route["lookups"] == [MERCHANT_ID]
    assert len(route["ingested"]) == 1
    event = route["ingested"][0]["batch"].events[0]
    assert (event.store_id, event.platform) == ("store_b", "cafe24")


def test_route_refuses_a_store_the_merchant_has_not_connected(route):
    response = _post({"events": [_event(store_id="someone_elses_store", platform="shopify")]})
    assert response.status_code == 422
    assert "not an active connected store" in response.text
    assert route["ingested"] == []


def test_route_refuses_a_platform_that_disagrees_with_the_store(route):
    response = _post({"events": [_event(store_id="store_a", platform="woocommerce")]})
    assert response.status_code == 422
    assert route["ingested"] == []


def test_route_requires_store_id_for_a_multi_store_merchant(route):
    response = _post({"events": [_event()]})
    assert response.status_code == 422
    assert "more than one connected store" in response.text
    assert route["ingested"] == []


def test_route_fills_the_sole_store_when_the_collector_omits_it(route):
    route["stores"] = {"store_a": "shopify"}
    response = _post({"events": [_event()]})
    assert response.status_code == 200, response.text
    event = route["ingested"][0]["batch"].events[0]
    assert (event.store_id, event.platform) == ("store_a", "shopify")


def test_route_refuses_the_psp_surface(route):
    response = _post({"events": [_event(store_id="store_a", surface="psp")]})
    assert response.status_code == 422
    assert route["ingested"] == []


def test_route_looks_stores_up_only_after_the_signature_is_proven(route):
    body = json.dumps({"events": [_event(store_id="store_a")]}, separators=(",", ":")).encode()
    response = _client().post(
        "/merchant-events/v1/batch",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Pivota-Merchant-Id": MERCHANT_ID,
            "X-Pivota-Signature": "deadbeef",
        },
    )
    assert response.status_code == 401
    # An unauthenticated caller must not be able to make us enumerate stores.
    assert route["lookups"] == []
    assert route["ingested"] == []


# ---- the lookup ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_connected_store_index_uses_the_psp_bridges_store_set(monkeypatch):
    """Same helper as resolve_order_store_scope, so HMAC events and Stripe
    events for one purchase land in the same (merchant, store) scope. Rows
    without a usable id or platform cannot be bound and are dropped."""
    import services.merchant_event_store_binding as module

    seen = []

    async def fake_active_stores(merchant_id):
        seen.append(merchant_id)
        return [
            {"store_id": "store_a", "platform": "Shopify", "status": "active"},
            {"store_id": "store_b", "platform": "cafe24", "status": "connected"},
            {"store_id": "", "platform": "wix", "status": "active"},
            {"store_id": "store_c", "platform": None, "status": "active"},
        ]

    monkeypatch.setattr(module, "get_merchant_active_stores", fake_active_stores)
    assert await module.connected_store_index(" merch_x ") == {"store_a": "shopify", "store_b": "cafe24"}
    assert seen == [" merch_x "]
