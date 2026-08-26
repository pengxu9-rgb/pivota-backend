"""`GET /api/auth/test` must not advertise the hardcoded demo credentials.

The route is mounted unauthenticated in every environment, including
production, via the always-included `auth_api_router` (main.py). It exists
as a liveness/shape check ("Authentication API is running"), but its
response body used to include a `test_credentials` block listing every demo
account's real email/password/role in plaintext. PR #1889 stopped those
credentials from working in production; this route still advertised them to
any anonymous caller, which is reconnaissance for exploiting a future
regression or flag misconfiguration.

Mutant check: assert the specific leaked emails/passwords are absent, not
just that some key is missing, so re-adding the block under a different key
name would still fail this test.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.auth import router as auth_api_router

LEAKED_CREDENTIAL_VALUES = [
    "superadmin@pivota.com",
    "admin@pivota.com",
    "employee@pivota.com",
    "outsourced@pivota.com",
    "merchant@test.com",
    "agent@test.com",
    "Admin123!",
]


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(auth_api_router)
    return TestClient(app)


def test_auth_test_route_does_not_leak_demo_credentials():
    client = _client()

    res = client.get("/api/auth/test")

    assert res.status_code == 200
    assert "test_credentials" not in res.json()
    body_text = res.text
    for leaked in LEAKED_CREDENTIAL_VALUES:
        assert leaked not in body_text, f"{leaked!r} must not appear in /api/auth/test"


def test_auth_test_route_still_reports_liveness_shape():
    client = _client()

    res = client.get("/api/auth/test")

    payload = res.json()
    assert payload["success"] is True
    assert "endpoints" in payload
