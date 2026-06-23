"""P1 — /api/brands/attest endpoint: maps service status -> HTTP, auth-scoped."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import brand_claim_routes
from services import brand_attest_service as attest_svc
from utils.auth import get_current_merchant


def _client(merchant: str = "m1") -> TestClient:
    app = FastAPI()
    app.include_router(brand_claim_routes.router)

    async def _override() -> str:
        return merchant

    app.dependency_overrides[get_current_merchant] = _override
    return TestClient(app)


def _patch_attest(monkeypatch, result):
    captured = {}

    async def _attest(**kw):
        captured.update(kw)
        return result

    monkeypatch.setattr(attest_svc, "attest_product", _attest)
    return captured


def test_attest_happy_uses_authed_merchant(monkeypatch):
    captured = _patch_attest(
        monkeypatch,
        {"status": "attested", "content_key": "ck", "fields_written": ["description_markdown"]},
    )
    r = _client("m_self").post(
        "/api/brands/attest",
        json={"product_key": "pk1", "fields": {"description": "x"}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "attested"
    assert captured["merchant_id"] == "m_self"  # from auth, not the body


def test_attest_not_claimed_409(monkeypatch):
    _patch_attest(monkeypatch, {"status": "not_claimed", "claim_state": "unclaimed"})
    r = _client().post(
        "/api/brands/attest", json={"product_key": "pk", "fields": {"description": "x"}}
    )
    assert r.status_code == 409


def test_attest_not_found_404(monkeypatch):
    _patch_attest(monkeypatch, {"status": "not_found"})
    r = _client().post(
        "/api/brands/attest", json={"product_key": "pk", "fields": {"description": "x"}}
    )
    assert r.status_code == 404


def test_attest_no_fields_422(monkeypatch):
    _patch_attest(monkeypatch, {"status": "no_attestable_fields"})
    r = _client().post(
        "/api/brands/attest", json={"product_key": "pk", "fields": {"unknown": "x"}}
    )
    assert r.status_code == 422
