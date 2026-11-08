"""Managed merchant onboarding (founder decision D-09, 2026-11-08).

Only staff-approved merchants are listed to partners; self-serve with gates comes later.

  1. No signup is approved automatically. The registration-first "approval floor", the
     *.myshopify.com fast lane, a passing auto-KYB, the store-less lane and the
     transaction-abort retry all used to approve; each now leaves the row
     `pending_verification` for staff, keeping the automatic findings for the reviewer.
  2. Approval is a staff decision: `update_kyc_status(..., "approved")` refuses unless the
     caller says `approved_by_staff=True`, and the two staff routes (the employee portal's
     `/merchant/onboarding/kyb/{id}` and `/merchant/onboarding/approve/{id}`) say so. The
     staff write clears `auto_approved`, which is how the partner list tells the two apart
     (tests/test_agent_sdk_merchants_list.py drives that list).
  3. A PSP connection alone never makes a merchant transactable: agent order creation through
     the merchant's PSP needs an approved merchant, and cross-merchant search admits only
     approved merchants.

Every test drives the real producer (the route coroutine or handler, the real
`update_kyc_status` over a recording DB), never a re-typed copy of a condition.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.merchant_onboarding_routes as onboarding
import utils.auto_kyb_validator as kyb
from db import merchant_onboarding as mo
from routes.merchant_onboarding_routes import MerchantRegisterRequest


def _params(query) -> dict:
    try:
        return dict(query.compile().params)
    except Exception:  # noqa: BLE001 - a raw SQL string has no compiled params
        return {"_sql": str(query)}


class _DummyBackgroundTasks:
    def __init__(self):
        self.tasks = []

    def add_task(self, *args, **kwargs):
        self.tasks.append(args)


# ---- 1. signup never approves ------------------------------------------------------------


@pytest.fixture
def signup(monkeypatch):
    """register_merchant over recording fakes. `update_kyc_status` is the REAL one over a
    recording DB, so a signup path that tried to approve would hit the staff-only refusal."""
    rec = {"writes": [], "kyc_calls": [], "kyb_called": False}

    async def fake_execute(query, values=None):
        rec["writes"].append(_params(query))
        return None

    async def none(*a, **k):
        return None

    async def fake_identity_merge(**kwargs):
        return None, None

    async def fake_create(merchant_dict):
        rec["create_dict"] = dict(merchant_dict)
        return "merch_managed_signup"

    real_update = mo.update_kyc_status

    async def recording_update(merchant_id, status, *a, **k):
        rec["kyc_calls"].append((merchant_id, status, k))
        return await real_update(merchant_id, status, *a, **k)

    monkeypatch.setattr(onboarding.database, "execute", fake_execute)
    monkeypatch.setattr(mo.database, "execute", fake_execute)
    monkeypatch.setattr(onboarding.database, "fetch_one", none)
    monkeypatch.setattr(onboarding, "get_active_onboarding_by_email", none)
    monkeypatch.setattr(onboarding, "get_user_auth_binding", none)
    monkeypatch.setattr(onboarding, "resolve_public_merchant_identity_merge", fake_identity_merge)
    monkeypatch.setattr(onboarding, "get_merchant_onboarding", none)
    monkeypatch.setattr(onboarding, "create_merchant_onboarding", fake_create)
    monkeypatch.setattr(onboarding, "update_kyc_status", recording_update)
    monkeypatch.setattr(onboarding, "sync_merchant_auth_user", none)
    return rec


def _kyb(monkeypatch, rec, outcome):
    async def fake_auto_kyb(business_name, store_url, region):
        rec["kyb_called"] = True
        if outcome == "raises":
            raise RuntimeError("validator down")
        return {
            "approved": outcome == "passes",
            "confidence_score": 0.91 if outcome == "passes" else 0.1,
            "validation_results": {"url_validation": {"valid": outcome == "passes"}},
            "requires_full_kyb": True,
            "full_kyb_deadline": "2030-01-01T00:00:00",
        }

    monkeypatch.setattr(kyb, "auto_kyb_pre_approval", fake_auto_kyb)


SIGNUP_CASES = {
    # case: (operating_mode, store_url, auto-KYB outcome, expected confidence)
    "kyb_fails_the_old_floor": ("storefront", "https://unverifiable.example.com", "fails", 0.1),
    "kyb_passes": ("storefront", "https://real-store.example.com", "passes", 0.91),
    "kyb_raises": ("storefront", "https://flaky.example.com", "raises", 0),
    "myshopify_fast_lane": ("storefront", "https://some-shop.myshopify.com", "passes", 0.96),
    "store_less_kyb_skipped": ("store_less", None, "fails", onboarding.STORE_LESS_APPROVAL_CONFIDENCE),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(SIGNUP_CASES))
async def test_no_signup_is_approved_automatically(signup, monkeypatch, case):
    mode, url, outcome, confidence = SIGNUP_CASES[case]
    _kyb(monkeypatch, signup, outcome)
    req = MerchantRegisterRequest(
        business_name="Some Business",
        region="US",
        contact_email="owner@example.com",
        password="hunter2hunter2",
        operating_mode=mode,
        store_url=url,
    )

    resp = await onboarding.register_merchant(req, _DummyBackgroundTasks())

    assert resp["status"] == "success"
    assert resp["auto_approved"] is False
    assert resp["next_step"] == "Wait for Pivota review"
    assert "pending review" in resp["message"].lower()
    assert "approved!" not in resp["message"].lower()
    assert resp["full_kyb_deadline"] is None
    assert resp["confidence_score"] == confidence
    assert "staff_review" in resp["validation_details"]
    assert "approval_floor" not in resp["validation_details"]
    # Nothing approved the row: no status write at all, and no auto_approved=True anywhere.
    assert signup["kyc_calls"] == []
    assert all(w.get("status") != "approved" for w in signup["writes"]), signup["writes"]
    assert all(w.get("auto_approved") is not True for w in signup["writes"]), signup["writes"]
    # The triage write: auto_approved explicitly False, the check's confidence kept.
    triage = [w for w in signup["writes"] if "approval_confidence" in w]
    assert triage == [{"auto_approved": False, "approval_confidence": confidence, "merchant_id_1": "merch_managed_signup"}]
    # The myshopify lane and store-less never call the URL validator.
    assert signup["kyb_called"] is (case in {"kyb_fails_the_old_floor", "kyb_passes", "kyb_raises"})


@pytest.mark.asyncio
async def test_the_transaction_abort_retry_does_not_schedule_an_approval(signup, monkeypatch):
    calls = []

    async def flaky_create(merchant_dict):
        calls.append(dict(merchant_dict))
        if len(calls) == 1:
            raise Exception("current transaction is aborted, commands ignored until end of transaction block")
        return "merch_managed_retry"

    async def none(*a, **k):
        return None

    monkeypatch.setattr(onboarding, "create_merchant_onboarding", flaky_create)
    monkeypatch.setattr(onboarding.database, "disconnect", none)
    monkeypatch.setattr(onboarding.database, "connect", none)
    monkeypatch.setattr(onboarding.asyncio, "sleep", none)
    tasks = _DummyBackgroundTasks()
    req = MerchantRegisterRequest(
        business_name="Retry Co",
        region="US",
        contact_email="retry@example.com",
        password="hunter2hunter2",
        operating_mode="store_less",
    )

    resp = await onboarding.register_merchant(req, tasks)

    assert len(calls) == 2
    assert resp["merchant_id"] == "merch_managed_retry"
    assert resp["auto_approved"] is False
    # This used to queue a task that approved the merchant five seconds later.
    assert tasks.tasks == []
    assert not hasattr(onboarding, "auto_approve_kyc")


@pytest.mark.asyncio
@pytest.mark.parametrize("status,approved", [("pending_verification", False), ("approved", True)])
async def test_a_recovered_signup_reports_its_own_status_not_the_old_flag(signup, monkeypatch, status, approved):
    """Re-registering with the same email and store recovers the row. A row still pending
    review that carries the old auto_approved=True is not told it is approved."""
    async def existing(_email):
        return {
            "merchant_id": "merch_recovered",
            "store_url": "https://recover.example.com",
            "status": status,
            "auto_approved": True,
            "approval_confidence": 0.2,
        }

    monkeypatch.setattr(onboarding, "get_active_onboarding_by_email", existing)
    req = MerchantRegisterRequest(
        business_name="Recovered Co",
        region="US",
        contact_email="owner@example.com",
        password="hunter2hunter2",
        store_url="https://recover.example.com",
    )
    resp = await onboarding.register_merchant(req, _DummyBackgroundTasks())
    assert resp["merchant_id"] == "merch_recovered"
    assert resp["auto_approved"] is approved
    assert resp["next_step"] == ("Connect PSP" if approved else "Wait for Pivota review")
    assert signup["kyc_calls"] == []


# ---- 2. approval is a staff decision -----------------------------------------------------


class _RecordingDB:
    def __init__(self):
        self.values = []

    async def execute(self, query, values=None):
        self.values.append(_params(query))
        return 1


@pytest.fixture
def db(monkeypatch):
    fake = _RecordingDB()
    monkeypatch.setattr(mo, "database", fake)
    return fake


@pytest.mark.asyncio
async def test_approval_without_a_staff_caller_is_refused(db):
    with pytest.raises(ValueError, match="staff decision"):
        await mo.update_kyc_status("merch_1", "approved")
    assert db.values == [], "a refused approval still wrote"


@pytest.mark.asyncio
async def test_a_staff_approval_clears_auto_approved_and_stamps_verified_at(db):
    """The marker the partner list reads: a staff approval of a merchant the signup floor
    had approved (auto_approved=True) must stop looking automatic."""
    await mo.update_kyc_status("merch_1", "approved", approved_by_staff=True)
    written = db.values[-1]
    assert written["status"] == "approved"
    assert written["auto_approved"] is False
    assert written["verified_at"] is not None


@pytest.mark.asyncio
async def test_the_staff_approval_stamp_is_utc(db, monkeypatch):
    """The partner list compares verified_at with the D-09 cutover, which is naive UTC. A
    local-clock stamp on a non-UTC host would shift every approval by the UTC offset."""
    from datetime import datetime as real_datetime

    class _Clock(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 8, 3, 0, 0)  # a host eight hours behind UTC

        @classmethod
        def utcnow(cls):
            return real_datetime(2026, 11, 8, 11, 0, 0)

    monkeypatch.setattr(mo, "datetime", _Clock)
    await mo.update_kyc_status("merch_1", "approved", approved_by_staff=True)
    assert db.values[-1]["verified_at"] == real_datetime(2026, 11, 8, 11, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "pending_verification", "deleted"])
async def test_leaving_approved_needs_no_staff_flag(db, status):
    await mo.update_kyc_status("merch_1", status)
    assert db.values[-1]["status"] == status


def _staff_client(monkeypatch, db):
    admin = {"role": "admin", "email": "staff@example.com", "sub": "staff"}
    app = FastAPI()
    app.include_router(onboarding.router)
    app.dependency_overrides[onboarding.get_current_user] = lambda: admin
    app.dependency_overrides[onboarding.require_admin] = lambda: admin

    async def fake_get(merchant_id):
        return {"merchant_id": merchant_id, "status": "approved", "auto_approved": True, "business_name": "M"}

    async def fake_sync(merchant_id, **kwargs):
        return {"changed": False, "skipped": None}

    import services.store_lifecycle_service as sls

    monkeypatch.setattr(onboarding, "get_merchant_onboarding", fake_get)
    # The REAL update_kyc_status, over the recording DB.
    monkeypatch.setattr(onboarding, "update_kyc_status", mo.update_kyc_status)
    monkeypatch.setattr(sls, "sync_catalog_merchant_status", fake_sync)
    return TestClient(app)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/merchant/onboarding/kyb/merch_floor", {"status": "approved"}),  # the employee portal's call
        ("/merchant/onboarding/approve/merch_floor", None),
    ],
)
def test_the_staff_approval_routes_record_a_staff_approval(monkeypatch, db, path, body):
    client = _staff_client(monkeypatch, db)
    resp = client.post(path, json=body) if body is not None else client.post(path)
    assert resp.status_code == 200, resp.text
    written = db.values[-1]
    assert written["status"] == "approved"
    assert written["auto_approved"] is False


def test_the_staff_kyb_route_can_still_reject(monkeypatch, db):
    client = _staff_client(monkeypatch, db)
    resp = client.post("/merchant/onboarding/kyb/merch_floor", json={"status": "rejected", "reason": "x"})
    assert resp.status_code == 200, resp.text
    assert db.values[-1]["status"] == "rejected"


@pytest.mark.asyncio
async def test_platform_onboarding_v2_does_not_insert_an_approved_merchant(monkeypatch):
    import services.platform_onboarding_service as svc
    from db.database import database

    inserted = []

    class _Stop(Exception):
        pass

    async def fake_execute(query, values=None):
        inserted.append(_params(query))
        raise _Stop()

    async def none(*a, **k):
        return None

    monkeypatch.setattr(svc, "get_merchant_onboarding", none)
    monkeypatch.setattr(database, "execute", fake_execute)
    with pytest.raises(_Stop):
        await svc.register_platform_merchant_v2(merchant_id="merch_v2", business_name="B", region="US")
    assert inserted and inserted[0]["status"] == "pending_verification"
    assert inserted[0]["auto_approved"] is False


# ---- 3. a PSP connection alone is not transactable ---------------------------------------


def _order_request():
    from models.order import CreateOrderRequest

    return CreateOrderRequest(
        merchant_id="merch_1",
        customer_email="buyer@example.com",
        quote_id="q_test",
        items=[{"product_id": "10064558129449", "variant_id": "53012602618153", "quantity": 1}],
        shipping_address={
            "name": "Buyer",
            "address_line1": "1 Market St",
            "address_line2": "",
            "city": "San Francisco",
            "state": "CA",
            "postal_code": "94105",
            "country": "US",
            "phone": "",
        },
        currency="USD",
        metadata={"agent_id": "agt_1"},
    )


@pytest.fixture
def order_create(monkeypatch):
    """The real create_new_order; harness shape follows
    tests/test_order_create_strips_server_only_metadata.py. `state` sets the merchant's
    onboarding status and which PSP the order resolves to."""
    import routes.order_routes as module

    state = {"status": "approved", "psp": ("stripe", "psp_1"), "created": []}

    async def fake_get_merchant_onboarding(merchant_id: str):
        return {"merchant_id": merchant_id, "status": state["status"], "psp_connected": True}

    async def fake_check_inventory(_merchant_id, _items):
        return True, {}

    async def fake_load_quote(self, *, quote_id: str):
        return SimpleNamespace(
            quote_id=quote_id,
            expires_at=None,
            engine="shopify_storefront_cart",
            engine_ref="cart_ref",
            request_fingerprint="fp_1",
            quote_hash_sha256="h" * 64,
            debug_id="debug_1",
            merchant_id="merch_1",
            request_json={
                "items": [{"product_id": "10064558129449", "variant_id": "53012602618153", "quantity": 1}],
                "discount_codes": [],
                "shipping_address": {"country": "US", "postal_code": "94105", "city": "San Francisco", "state": "CA"},
            },
            snapshot_json={
                "currency": "USD",
                "pricing": {"subtotal": "10.00", "discount_total": "0.00", "shipping_fee": "0.00", "tax": "0.00", "total": "10.00"},
                "promotion_lines": [],
                "line_items": [
                    {
                        "product_id": "10064558129449",
                        "variant_id": "53012602618153",
                        "quantity": 1,
                        "title": "Serum",
                        "unit_price_original": "10.00",
                        "unit_price_effective": "10.00",
                        "line_discount_total": "0.00",
                    }
                ],
            },
        )

    async def fake_validate_live(self, quote, **_kwargs):
        return {"status": "validated", "engine": "shopify_storefront_cart", "engine_ref": "cart_ref_live"}

    async def fake_select_psp(self, **_kwargs):
        return "stripe", {"route_id": "route_test"}

    async def fake_resolve_psp(_merchant_id, _hint, **_kwargs):
        return state["psp"]

    async def fake_create_order(order_data):
        state["created"].append(copy.deepcopy(order_data))
        return "ORD_TEST"

    async def noop(*a, **k):
        return None

    async def fake_payment(*a, **k):
        return False, None, "payment skipped", "stripe"

    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "check_inventory_availability", fake_check_inventory)
    monkeypatch.setattr(module.QuoteService, "load_active_quote_or_raise", fake_load_quote)
    monkeypatch.setattr(module.QuoteService, "validate_quote_snapshot_live", fake_validate_live)
    monkeypatch.setattr(module, "compute_request_fingerprint", lambda **_: "fp_1")
    monkeypatch.setattr(module.PaymentRoutingService, "select_psp", fake_select_psp)
    monkeypatch.setattr(module, "_resolve_active_order_psp", fake_resolve_psp)
    monkeypatch.setattr(module, "create_order", fake_create_order)
    monkeypatch.setattr(module, "get_primary_store", noop)
    monkeypatch.setattr(module, "upsert_order_attribution_edge", noop)
    monkeypatch.setattr(module, "emit_merchant_webhook_event", noop)
    monkeypatch.setattr(module, "log_order_event", noop)
    monkeypatch.setattr(module.QuoteService, "consume_quote_best_effort", noop)
    monkeypatch.setattr(module.database, "fetch_one", noop)
    monkeypatch.setattr(module, "create_payment_with_failover", fake_payment)
    return module, state


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending_verification", "pending", "suspended", ""])
async def test_a_merchant_not_approved_cannot_take_an_order_through_its_psp(order_create, status):
    """A pending signup that connected a PSP (any 10+ character key) and a store used to be
    able to take agent orders: order creation refused only rejected and deleted."""
    module, state = order_create
    state["status"] = status
    with pytest.raises(HTTPException) as exc:
        await module.create_new_order(_order_request(), BackgroundTasks(), current_user={"user_id": "t"})
    assert exc.value.status_code == 403
    assert exc.value.detail["error"] == "MERCHANT_NOT_ACCEPTING_ORDERS"
    assert state["created"] == []


@pytest.mark.asyncio
async def test_an_approved_merchant_still_takes_orders(order_create):
    module, state = order_create
    await module.create_new_order(_order_request(), BackgroundTasks(), current_user={"user_id": "t"})
    assert len(state["created"]) == 1


@pytest.mark.asyncio
async def test_the_capability_deferred_lane_is_not_this_gates_business(order_create):
    """That lane books no charge against a merchant PSP and keeps its own gate
    (services/merchant_capability_gate.py), so this check leaves it alone."""
    module, state = order_create
    state["status"] = "pending_verification"
    state["psp"] = (module.CAPABILITY_DEFERRED_PSP_PROVIDER, module._capability_deferred_psp_id("merch_1"))
    await module.create_new_order(_order_request(), BackgroundTasks(), current_user={"user_id": "t"})
    assert len(state["created"]) == 1


@pytest.fixture
def search_client(monkeypatch):
    try:
        from main import app
    except RuntimeError as exc:
        if "Repo governance violation" in str(exc):
            pytest.skip("main app import requires a clean repo")
        raise
    import routes.agent_api as agent_api

    seen = []

    async def recording_fetch_all(query, values=None):
        seen.append(" ".join(str(query).split()))
        return []

    monkeypatch.setattr(agent_api.database, "fetch_all", recording_fetch_all)
    monkeypatch.setattr(agent_api, "log_agent_request", AsyncMock(return_value=None))
    return TestClient(app), seen


def _psp_rule_queries(seen):
    return [q for q in seen if "merchant_onboarding" in q and "psp_connected = true" in q]


@pytest.mark.asyncio
async def test_cross_merchant_search_admits_only_approved_psp_merchants(search_client, tmp_path):
    """Runs the merchant query the search route emits against a real SQLite table: a merchant
    pending review that connected a PSP is not searchable; an approved one is."""
    from databases import Database

    client, seen = search_client
    resp = client.get(
        "/agent/v1/products/search",
        params={"query": "serum", "limit": 10, "search_all_merchants": "true", "fast_mode": "false",
                "allow_external_seed": "false", "allow_stale_cache": "false"},
        headers={"X-API-Key": "test-api-key"},
    )
    assert resp.status_code == 200, resp.text
    queries = [q for q in _psp_rule_queries(seen) if q.startswith("SELECT merchant_id, business_name FROM merchant_onboarding")]
    assert queries, seen

    db_ = Database(f"sqlite:///{tmp_path / 'm.db'}")
    await db_.connect()
    try:
        await db_.execute(
            "CREATE TABLE merchant_onboarding (merchant_id TEXT, business_name TEXT, status TEXT, psp_connected BOOLEAN)"
        )
        for mid, status in (("m_approved", "approved"), ("m_pending", "pending_verification"), ("m_restored", "pending")):
            await db_.execute(
                "INSERT INTO merchant_onboarding VALUES (:m, 'B', :s, 1)", {"m": mid, "s": status}
            )
        rows = await db_.fetch_all(queries[0])
    finally:
        await db_.disconnect()
    assert {r["merchant_id"] for r in rows} == {"m_approved"}


def test_fast_mode_search_with_the_merchant_join_on_applies_the_same_rule(search_client, monkeypatch):
    """Fast mode skips the merchant join by default (AGENT_SEARCH_FAST_MODE_SKIP_MERCHANT_JOIN);
    with the join on, its merchant predicate is the approved-only PSP rule too. Postgres-only
    SQL, so checked as the route emits it."""
    import routes.agent_api as agent_api

    monkeypatch.setattr(agent_api, "AGENT_SEARCH_FAST_MODE_SKIP_MERCHANT_JOIN", False)
    client, seen = search_client
    resp = client.get(
        "/agent/v1/products/search",
        params={"query": "serum", "limit": 10, "search_all_merchants": "true", "fast_mode": "true",
                "allow_external_seed": "false", "allow_stale_cache": "false"},
        headers={"X-API-Key": "test-api-key"},
    )
    assert resp.status_code == 200, resp.text
    joined = [q for q in seen if "mo.psp_connected = true" in q and "products_cache" in q]
    assert joined, seen
    for q in joined:
        assert "mo.status = 'approved'" in q, q
        assert "NOT IN ('deleted', 'rejected')" not in q, q
