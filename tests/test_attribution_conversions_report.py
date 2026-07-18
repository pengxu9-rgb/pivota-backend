"""
The non-custodial conversion-report / receipt-ingest API (#1482).

A merchant reports a settled order referencing the Pivota click_id; Pivota verifies
(per-merchant HMAC over the raw body, keyed by the merchant's api_key), binds it to
the attribution edge via close_external_order_conversion (idempotent, GMV stamped),
and touches no money.

Two layers of coverage:
  * auth / validation gate — mock the closure primitive (isolate the route).
  * binding, idempotency & reconciliation — drive the REAL primitive against an
    ON-CONFLICT-emulating fake DB, so the discrepancy signal and the cross-merchant
    pre-emption guard are proven end-to-end, NOT asserted against a hand-mocked
    return shape. The primitive itself is also covered by
    tests/test_t2_2_external_conversion_closure.py.
"""
import hashlib
import hmac
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

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


# --- auth / validation gate (closure primitive mocked) ------------------------

@pytest.fixture
def patched(monkeypatch):
    """Patch the merchant lookup + the closure primitive; return captured close calls."""
    calls = []

    async def fake_merchant(mid):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY, "status": "approved"} if mid == MERCHANT_ID else None

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
    assert c["is_self_report"] is True       # untrusted self-report contract


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


def test_deleted_merchant_rejected_403(monkeypatch):
    # Soft-delete tombstones the record but does NOT clear the api_key: a valid
    # signature alone must not let a deleted merchant report (#1482 review).
    calls = []

    async def fake_merchant(mid):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY, "status": "deleted"}

    async def fake_close(**kw):
        calls.append(kw)
        return {"edge_id": "cae_x", "replayed": False, "click_matched": False}

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr("services.commerce_attribution_service.close_external_order_conversion", fake_close)

    res = _post(_client(), {"external_order_id": "6001"})
    assert res.status_code == 403
    assert calls == [], "an inactive merchant must never reach the closure primitive"


def test_missing_external_order_id_400(patched):
    res = _post(_client(), {"click_id": "clk_abc"})  # signed, but no external_order_id
    assert res.status_code == 400
    assert patched == []


def test_non_numeric_gross_is_400_not_500(patched):
    res = _post(_client(), {"external_order_id": "6001", "gross_amount_cents": "not-a-number"})
    assert res.status_code == 400
    assert patched == [], "a bad gross is a client error, never reaching the closure"


def test_negative_gross_is_400(patched):
    res = _post(_client(), {"external_order_id": "6001", "gross_amount_cents": -5})
    assert res.status_code == 400
    assert patched == []


# --- binding / idempotency / reconciliation (REAL primitive) ------------------

class _FakeAttribDB:
    """Emulates the ON CONFLICT (merchant_id, external_order_id) insert guard AND
    the self-report GMV re-read, so the route → REAL close_external_order_conversion
    → reconciliation path is exercised end-to-end (no divergent mock).

    - non-str fetch_one (SQLAlchemy select on surface_click_events) → click_row.
    - INSERT ... ON CONFLICT DO NOTHING RETURNING → stores on first, None on replay.
    - SELECT gross_attributed_gmv_cents ... → the STORED gross for the edge.
    """

    def __init__(self, click_row: Optional[Dict[str, Any]] = None) -> None:
        self.click_row = click_row
        self.edges: Dict[tuple, Dict[str, Any]] = {}

    async def fetch_one(self, query: Any, values: Any = None):
        if isinstance(query, str) and "INSERT INTO commerce_attribution_edges" in query:
            params = dict(values or {})
            key = (params["merchant_id"], params["external_order_id"])
            if key in self.edges:
                return None  # ON CONFLICT DO NOTHING → no RETURNING row (replay)
            self.edges[key] = params
            return {"edge_id": params["edge_id"]}
        if isinstance(query, str) and "SELECT gross_attributed_gmv_cents" in query:
            params = dict(values or {})
            stored = self.edges.get((params["merchant_id"], params["external_order_id"]))
            return {"gross_attributed_gmv_cents": stored["gross_attributed_gmv_cents"]} if stored else None
        return self.click_row  # the click lookup

    async def fetch_all(self, query: Any, values: Any = None):
        return []

    async def execute(self, query: Any, values: Any = None) -> int:
        return 0


@pytest.fixture
def real_primitive(monkeypatch):
    """Wire the route to the REAL closure primitive backed by a fake DB. Returns a
    setter that installs the fake DB (optionally with a click row) and yields it."""
    from services import commerce_attribution_service as svc

    async def fake_merchant(mid):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY, "status": "approved"} if mid == MERCHANT_ID else None

    async def _noop_event(*a, **k):
        return {"interaction_id": "int_stub"}

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr(svc, "record_commerce_event_best_effort", _noop_event)

    def _install(click_row=None):
        fake = _FakeAttribDB(click_row=click_row)
        monkeypatch.setattr(svc, "database", fake)
        return fake

    return _install


def _self_click(**over):
    row = {"click_id": "clk_self", "merchant_id": MERCHANT_ID, "interaction_id": "int_c", "context": {}}
    row.update(over)
    return row


def test_real_first_close_stamps_gmv(real_primitive):
    real_primitive(click_row=_self_click())
    res = _post(_client(), {"external_order_id": "7001", "click_id": "clk_self",
                            "gross_amount_cents": 4999, "currency": "USD"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["replayed"] is False
    assert body["click_matched"] is True
    assert body["gross_attributed_gmv_cents"] == 4999
    assert body["gmv_discrepancy"] is False


def test_real_replay_reconciliation_surfaces_discrepancy(real_primitive):
    # Drive the REAL primitive twice for the same order with a DIFFERENT gross. The
    # first close wins (idempotent); the replay re-reads the STORED gross so the
    # discrepancy is surfaced — proving reconciliation is not dead code.
    fake = real_primitive(click_row=_self_click())
    c = _client()
    first = _post(c, {"external_order_id": "7002", "click_id": "clk_self", "gross_amount_cents": 4999})
    assert first.status_code == 200 and first.json()["gross_attributed_gmv_cents"] == 4999

    second = _post(c, {"external_order_id": "7002", "click_id": "clk_self", "gross_amount_cents": 5500})
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["replayed"] is True
    assert body["gross_attributed_gmv_cents"] == 4999    # STORED value wins (not overwritten)
    assert body["gmv_discrepancy"] is True               # 5500 reported vs 4999 recorded
    assert len(fake.edges) == 1                           # exactly one edge — never doubled


def test_real_replay_same_gross_no_discrepancy(real_primitive):
    real_primitive(click_row=_self_click())
    c = _client()
    _post(c, {"external_order_id": "7003", "click_id": "clk_self", "gross_amount_cents": 4999})
    second = _post(c, {"external_order_id": "7003", "click_id": "clk_self", "gross_amount_cents": 4999})
    assert second.status_code == 200
    body = second.json()
    assert body["replayed"] is True
    assert body["gmv_discrepancy"] is False


def test_real_foreign_seller_keyed_click_is_not_adopted(real_primitive):
    # A merchant self-report presenting another seller's seller-keyed click must NOT
    # re-subject that seller's edge (cross-merchant pre-emption). The foreign click
    # is dropped → recorded UNATTRIBUTED under the REPORTING merchant, never the
    # seller. Guarded by is_self_report in the primitive.
    foreign = {"click_id": "clk_foreign", "merchant_id": "other_merch", "interaction_id": "int_f",
               "context": {"seller_ref": "seller_S", "seed_kind": "cross"}}
    fake = real_primitive(click_row=foreign)
    res = _post(_client(), {"external_order_id": "7004", "click_id": "clk_foreign", "gross_amount_cents": 9000})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["click_matched"] is False, "a foreign seller-keyed click must not be adopted by a self-report"
    # the single stored edge is subject to the REPORTING merchant, not seller_S
    assert len(fake.edges) == 1
    (subject, _order), _params = next(iter(fake.edges.items()))
    assert subject == MERCHANT_ID
    assert subject != "seller_S"
