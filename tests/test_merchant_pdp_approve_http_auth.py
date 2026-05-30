"""HTTP auth-path tests for the SKU-Opt merchant approve endpoint.

The staging full-loop proved the SERVICE layer (rubric -> gate -> publish ->
overlay -> merge-read) in-process. This closes the remaining gap: that a real
merchant JWT flows through get_current_user -> _merchant_id -> the handler, and
that the flag / module / ownership guards behave correctly over real HTTP.

We mount ONLY the merchant_pdp router in a TestClient and use real JWT signing +
decode (the auth path under test). The already-proven downstream
(get_pdp_projection / generate_copy_review_rubric / review_module_version) is
patched so these tests isolate AUTH + GUARDS, not DeepSeek or the DB.
"""
import asyncio
import importlib
import json

from databases import Database
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.auth import create_access_token

MERCHANT_ID = "merch_http_test"
PLATFORM = "shopify"
PRODUCT_ID = "999000111"


def _client(monkeypatch, *, flag_on=True):
    monkeypatch.setenv("SKU_OPT_OVERLAY_V1", "on" if flag_on else "off")
    # Re-import the route module so the module-level flag picks up the env.
    import routes.merchant_pdp as mp
    importlib.reload(mp)
    app = FastAPI()
    app.include_router(mp.router)
    return TestClient(app), mp


def _token(merchant_id=MERCHANT_ID, role="merchant"):
    return create_access_token(
        data={"sub": "u1", "email": "m@example.com", "role": role, "merchant_id": merchant_id}
    )


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _real_shape_projection(pdp_id="pdp_http_test", *, has_staged=True):
    """Mirror the REAL get_pdp_projection module shape: one summary per module_key
    with the staged version nested under "staged" (NOT a top-level "stage" field).
    Using the real shape here is what guards against the projection-parsing bug."""
    staged = (
        {"id": "pdpmod_x", "stage": "staged",
         "payload": {"pdp_description_raw": "draft copy"}, "source_refs": []}
        if has_staged else None
    )
    return {
        "status": "success",
        "pdp": {"pdp_id": pdp_id},
        "modules": [
            {"module_key": "copy", "status": "draft", "current": None,
             "staged": staged, "published_payload": None, "source_refs": []},
        ],
        "published_payload": {},
        "activity": [],
    }


def _real_shape_projection_with_source(pdp_id="pdp_http_test"):
    return {
        "status": "success",
        "pdp": {
            "pdp_id": pdp_id,
            "title": "Triple Shine Grape",
            "brand": "Ownist",
        },
        "modules": [
            {
                "module_key": "copy",
                "status": "draft",
                "current": None,
                "staged": {
                    "id": "pdpmod_source",
                    "stage": "staged",
                    "payload": {
                        "pdp_description_raw": "Ownist Triple Shine Grape jelly.",
                    },
                    "source_refs": [{"url": "https://ownist.test/products/triple-shine"}],
                    "source_url": "https://ownist.test/products/triple-shine",
                },
                "published_payload": None,
                "source_refs": [{"url": "https://ownist.test/products/triple-shine"}],
            },
        ],
        "published_payload": {},
        "activity": [],
    }


def _patch_downstream(monkeypatch, mp, *, decision="pass", published=True,
                      rubric_ok=True, has_staged=True):
    async def fake_projection(*, product_key, market):
        return _real_shape_projection(has_staged=has_staged)

    async def fake_rubric(*, merchant_id, payload, source_refs=None):
        return {"decision": "pass", "checks": {}, "confidence": 0.9} if rubric_ok else None

    async def fake_review(**kwargs):
        return {"decision": decision, "published": published, "module": {}}

    monkeypatch.setattr(mp, "get_pdp_projection", fake_projection)
    monkeypatch.setattr(mp, "generate_copy_review_rubric", fake_rubric)
    monkeypatch.setattr(mp, "review_module_version", fake_review)
    monkeypatch.setattr(mp, "parse_product_key", lambda pk: tuple(pk.split("|")))


def _url():
    return f"/merchant/pdps/product/{PLATFORM}/{PRODUCT_ID}/approve"


# --- AUTH ---

def test_missing_token_401(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.post(_url(), json={"module_key": "copy"})
    assert r.status_code in (401, 403), r.text


def test_garbage_token_401(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.post(_url(), json={"module_key": "copy"}, headers=_auth("not-a-jwt"))
    assert r.status_code == 401, r.text


def test_valid_merchant_jwt_happy_path(monkeypatch):
    client, mp = _client(monkeypatch)
    _patch_downstream(monkeypatch, mp)
    r = client.post(_url(), json={"module_key": "copy"}, headers=_auth(_token()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "pass"
    assert body["published"] is True
    assert body["product_key"] == f"{MERCHANT_ID}|{PLATFORM}|{PRODUCT_ID}"


# --- FLAG GUARD ---

def test_flag_off_404(monkeypatch):
    client, mp = _client(monkeypatch, flag_on=False)
    _patch_downstream(monkeypatch, mp)
    r = client.post(_url(), json={"module_key": "copy"}, headers=_auth(_token()))
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "SKU_OPT_OVERLAY_V1_DISABLED"


# --- MODULE GUARD ---

def test_non_copy_module_400(monkeypatch):
    client, mp = _client(monkeypatch)
    _patch_downstream(monkeypatch, mp)
    r = client.post(_url(), json={"module_key": "gallery"}, headers=_auth(_token()))
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "MODULE_NOT_MERCHANT_APPROVABLE"


# --- OWNERSHIP: product_key is derived from the JWT merchant_id, so a merchant
# can only ever address its OWN products. We assert the key the handler resolves
# carries the caller's merchant_id (structural ownership, not a fallible check). ---

def test_product_key_bound_to_jwt_merchant(monkeypatch):
    client, mp = _client(monkeypatch)
    seen = {}

    async def capture_projection(*, product_key, market):
        seen["product_key"] = product_key
        return _real_shape_projection()

    async def fake_rubric(*, merchant_id, payload, source_refs=None):
        seen["rubric_merchant_id"] = merchant_id
        return {"decision": "pass", "checks": {}, "confidence": 0.9}

    async def fake_review(**kwargs):
        return {"decision": "pass", "published": True, "module": {}}

    monkeypatch.setattr(mp, "get_pdp_projection", capture_projection)
    monkeypatch.setattr(mp, "generate_copy_review_rubric", fake_rubric)
    monkeypatch.setattr(mp, "review_module_version", fake_review)
    monkeypatch.setattr(mp, "parse_product_key", lambda pk: tuple(pk.split("|")))

    # Token for merchant A; even though the URL path is the same, the resolved
    # product_key must begin with A's merchant_id, never anyone else's.
    r = client.post(_url(), json={"module_key": "copy"}, headers=_auth(_token(merchant_id="merch_AAA")))
    assert r.status_code == 200, r.text
    assert seen["product_key"] == f"merch_AAA|{PLATFORM}|{PRODUCT_ID}"
    assert seen["rubric_merchant_id"] == "merch_AAA"


def test_rubric_unavailable_returns_needs_human_review(monkeypatch):
    client, mp = _client(monkeypatch)
    _patch_downstream(monkeypatch, mp, rubric_ok=False)
    r = client.post(_url(), json={"module_key": "copy"}, headers=_auth(_token()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "needs_human_review"
    assert body["published"] is False
    assert body["reason"] == "copy_review_unavailable"


# --- REGRESSION: no staged module in the real projection shape -> 404.
# Guards the projection-parsing bug where the handler looked for a top-level
# "stage" key that get_pdp_projection never emits (it nests under "staged"),
# which made EVERY real approve return NO_STAGED_MODULE. ---

def test_no_staged_module_404(monkeypatch):
    client, mp = _client(monkeypatch)
    _patch_downstream(monkeypatch, mp, has_staged=False)
    r = client.post(_url(), json={"module_key": "copy"}, headers=_auth(_token()))
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "NO_STAGED_MODULE"


def test_approve_route_persists_source_text_in_deepseek_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SKU_OPT_OVERLAY_V1", "on")
    monkeypatch.setenv("OVERLAY_DEEPSEEK_GROUND_AGAINST_SOURCE", "on")
    import routes.merchant_pdp as mp
    import services.pdp_copy_review as review
    import db.llm_probe_runs as lpr
    from config.settings import settings

    importlib.reload(mp)

    test_db = Database(f"sqlite+aiosqlite:///{tmp_path / 'probe_runs.db'}")
    asyncio.run(test_db.connect())
    lpr._DDL_READY = False
    monkeypatch.setattr(lpr, "database", test_db)

    captured = {"post_count": 0, "get_count": 0}

    class FakeResponse:
        def __init__(self, *, text="", payload=None):
            self.text = text
            self._payload = payload or {}
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url):
            captured["get_count"] += 1
            assert url == "https://ownist.test/products/triple-shine"
            return FakeResponse(
                text="<html><body><h1>Triple Shine Grape</h1><p>Marine collagen source text.</p></body></html>"
            )

        async def post(self, url, json=None, headers=None):
            captured["post_count"] += 1
            return FakeResponse(
                payload={
                    "choices": [
                        {
                            "message": {
                                "content": json_module.dumps({
                                    "decision": "pass",
                                    "checks": {
                                        "source_grounded": True,
                                        "seller_entity_checkout_not_confused": True,
                                        "variant_market_consistent": True,
                                        "no_medical_regulated_promo_or_fake_review_claim": True,
                                        "machine_publish_allowed_module": True,
                                    },
                                    "confidence": 0.92,
                                    "evidence_refs": ["Marine collagen source text"],
                                    "reviewed_in": "codex_external_window",
                                })
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                }
            )

    json_module = json

    async def fake_projection(*, product_key, market):
        return _real_shape_projection_with_source()

    async def fake_review(**kwargs):
        return {"decision": "pass", "published": True, "module": {}}

    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_api_base_url", "https://deepseek.test")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-chat")
    monkeypatch.setattr(review.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(mp.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(mp, "get_pdp_projection", fake_projection)
    monkeypatch.setattr(mp, "review_module_version", fake_review)
    monkeypatch.setattr(mp, "parse_product_key", lambda pk: tuple(pk.split("|")))

    app = FastAPI()
    app.include_router(mp.router)
    client = TestClient(app)
    try:
        r = client.post(_url(), json={"module_key": "copy"}, headers=_auth(_token()))
        assert r.status_code == 200, r.text

        row = asyncio.run(test_db.fetch_one(
            """
            SELECT request_payload_jsonb
              FROM llm_probe_runs
             WHERE scan_mode = 'pdp_copy_review'
             ORDER BY completed_at DESC
             LIMIT 1
            """
        ))
        assert row is not None
        stored_request = row["request_payload_jsonb"]
        if isinstance(stored_request, str):
            stored_request = json.loads(stored_request)
        user_message = stored_request["messages"][1]["content"]
        assert "SOURCE TEXT (verbatim from the source URL, truncated):" in user_message
        assert "Marine collagen source text." in user_message
        assert "SOURCE URL: https://ownist.test/products/triple-shine" in user_message
        assert captured["get_count"] == 1
        assert captured["post_count"] == 1
    finally:
        lpr._DDL_READY = False
        asyncio.run(test_db.disconnect())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
