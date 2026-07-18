"""
The non-custodial conversion-report / receipt-ingest API (#1482).

A merchant reports a settled order referencing the Pivota click_id; Pivota verifies
(per-merchant HMAC over the raw body, keyed by the merchant's api_key), binds it to
the attribution edge via close_external_order_conversion (idempotent, GMV stamped),
and touches no money. These tests cover the auth gate, the binding, idempotency, and
the reconciliation discrepancy signal — the close primitive itself is covered by
tests/test_t2_2_external_conversion_closure.py.
"""
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

MERCHANT_ID = "merch_test"
API_KEY = "mk_secret_abc123"


def _client() -> TestClient:
    from routes.attribution_conversions import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(client, body_dict, *, merchant_id=MERCHANT_ID, sign_key=API_KEY, signature=None):
    body = json.dumps(body_dict).encode("utf-8")
    sig = signature if signature is not None else hmac.new(sign_key.encode(), body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json"}
    if merchant_id is not None:
        headers["X-Pivota-Merchant-Id"] = merchant_id
    if sig is not None:
        headers["X-Pivota-Signature"] = sig
    return client.post("/attribution/conversions/report", content=body, headers=headers)


@pytest.fixture
def patched(monkeypatch):
    """Patch the merchant lookup + the closure primitive; return captured close calls."""
    calls = []

    async def fake_merchant(mid):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY} if mid == MERCHANT_ID else None

    async def fake_close(**kw):
        calls.append(kw)
        return {
            "edge_id": "cae_1",
            "replayed": False,
            "click_matched": kw.get("click_id") is not None,
            "gross_attributed_gmv_cents": kw.get("gross_amount_cents"),
            "state": "converted",
        }

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr("services.commerce_attribution_service.close_external_order_conversion", fake_close)
    return calls


def test_valid_signed_report_records_edge(patched):
    res = _post(_client(), {"external_order_id": "6001", "click_id": "clk_abc",
                            "gross_amount_cents": 4999, "currency": "USD"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "recorded"
    assert body["edge_id"] == "cae_1"
    assert body["click_matched"] is True
    assert body["gross_attributed_gmv_cents"] == 4999
    assert body["gmv_discrepancy"] is False
    # bound via the closure primitive with the reported fields — no money touched
    assert len(patched) == 1
    c = patched[0]
    assert c["merchant_id"] == MERCHANT_ID   # from the AUTHENTICATED key, not the body
    assert c["click_id"] == "clk_abc"
    assert c["external_order_id"] == "6001"
    assert c["gross_amount_cents"] == 4999


def test_invalid_signature_rejected_401(patched):
    res = _post(_client(), {"external_order_id": "6001"}, sign_key="the_wrong_key")
    assert res.status_code == 401
    assert patched == [], "a bad signature must not reach the closure primitive"


def test_missing_signature_rejected_401(patched):
    # No X-Pivota-Signature header at all → 401, never reaching the closure.
    body = json.dumps({"external_order_id": "6001"}).encode()
    res = _client().post("/attribution/conversions/report", content=body,
                         headers={"X-Pivota-Merchant-Id": MERCHANT_ID, "Content-Type": "application/json"})
    assert res.status_code == 401
    assert patched == []


def test_unknown_merchant_rejected_401(patched):
    # A validly-formed signature but an unknown merchant id → 401 (no oracle, no edge).
    res = _post(_client(), {"external_order_id": "6001"}, merchant_id="merch_ghost")
    assert res.status_code == 401
    assert patched == []


def test_missing_external_order_id_400(patched):
    res = _post(_client(), {"click_id": "clk_abc"})  # signed, but no external_order_id
    assert res.status_code == 400
    assert patched == []


def test_replay_is_idempotent(monkeypatch):
    # The closure primitive dedups on (merchant_id, external_order_id); a replay
    # reports replayed=True and does not double-count.
    async def fake_merchant(mid):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY}

    async def fake_close(**kw):
        return {"edge_id": "cae_1", "replayed": True, "click_matched": True,
                "gross_attributed_gmv_cents": 4999}

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr("services.commerce_attribution_service.close_external_order_conversion", fake_close)

    res = _post(_client(), {"external_order_id": "6001", "click_id": "clk_abc", "gross_amount_cents": 4999})
    assert res.status_code == 200, res.text
    assert res.json()["replayed"] is True
    assert res.json()["gmv_discrepancy"] is False


def test_gmv_discrepancy_is_surfaced(monkeypatch):
    # A replay reporting a DIFFERENT gross than what Pivota already recorded must be
    # surfaced (reconciliation) — the first close wins (idempotent), not overwritten.
    async def fake_merchant(mid):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY}

    async def fake_close(**kw):
        return {"edge_id": "cae_1", "replayed": True, "click_matched": True,
                "gross_attributed_gmv_cents": 4999}  # already recorded 4999

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr("services.commerce_attribution_service.close_external_order_conversion", fake_close)

    res = _post(_client(), {"external_order_id": "6001", "click_id": "clk_abc", "gross_amount_cents": 5500})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["gmv_discrepancy"] is True                       # 5500 reported vs 4999 recorded
    assert body["gross_attributed_gmv_cents"] == 4999            # recorded value wins (not overwritten)
