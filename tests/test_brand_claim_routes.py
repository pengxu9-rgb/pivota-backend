"""P1 — brand claim endpoint tests (fresh app + auth override + monkeypatched
service, mirroring tests/test_merchant_audit_routes.py — no DB).

The security property under test: the claim is scoped to the AUTHENTICATED
merchant (never the request body), and /claim/verify cross-tenant-guards the
claim row.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from db import brand_claims as bc
from routes import brand_claim_routes
from services import brand_claim_service as svc
from utils.auth import get_current_merchant


def _client(merchant: str = "merch_self") -> TestClient:
    app = FastAPI()
    app.include_router(brand_claim_routes.router)

    async def _override() -> str:
        return merchant

    app.dependency_overrides[get_current_merchant] = _override
    return TestClient(app)


def test_start_claim_happy_path_uses_authed_merchant(monkeypatch):
    async def _fake_start(**kw):
        # merchant_id must come from auth, NOT the body
        assert kw["merchant_id"] == "merch_self"
        return {
            "claim_id": "c1",
            "method": "dns",
            "challenge_token": "pivota-verify=tok",
            "instructions": "add TXT",
        }

    monkeypatch.setattr(svc, "start_brand_claim", _fake_start)
    r = _client().post("/api/brands/claim", json={"brand_domain": "anua.com", "method": "dns"})
    assert r.status_code == 200
    body = r.json()
    assert body["claim_id"] == "c1"
    assert body["challenge_token"].startswith("pivota-verify=")


def test_start_claim_rejects_unknown_method(monkeypatch):
    r = _client().post("/api/brands/claim", json={"method": "bribe"})
    assert r.status_code == 422


def test_start_claim_dns_requires_domain(monkeypatch):
    r = _client().post("/api/brands/claim", json={"method": "dns"})
    assert r.status_code == 422


def test_verify_rejects_cross_tenant_claim(monkeypatch):
    # Claim belongs to a DIFFERENT merchant -> 404 (no existence leak), and the
    # verifier is never invoked.
    async def _fake_get(claim_id):
        return {"claim_id": claim_id, "merchant_id": "SOMEONE_ELSE", "claim_method": "dns"}

    called = {"verify": False}

    async def _fake_verify(claim_id, **kw):
        called["verify"] = True
        return {"status": "verified"}

    monkeypatch.setattr(bc, "get_brand_claim", _fake_get)
    monkeypatch.setattr(svc, "verify_brand_claim", _fake_verify)
    r = _client(merchant="merch_self").post("/api/brands/claim/verify", json={"claim_id": "c1"})
    assert r.status_code == 404
    assert called["verify"] is False


def test_verify_not_found(monkeypatch):
    async def _fake_get(claim_id):
        return None

    monkeypatch.setattr(bc, "get_brand_claim", _fake_get)
    r = _client().post("/api/brands/claim/verify", json={"claim_id": "missing"})
    assert r.status_code == 404


def test_verify_happy_path(monkeypatch):
    async def _fake_get(claim_id):
        return {"claim_id": claim_id, "merchant_id": "merch_self", "claim_method": "dns"}

    async def _fake_verify(claim_id, **kw):
        return {"status": "verified", "brand_direct_set": True}

    monkeypatch.setattr(bc, "get_brand_claim", _fake_get)
    monkeypatch.setattr(svc, "verify_brand_claim", _fake_verify)
    r = _client(merchant="merch_self").post("/api/brands/claim/verify", json={"claim_id": "c1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "verified"
    assert body["brand_direct_set"] is True
