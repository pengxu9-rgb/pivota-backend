"""Router-level e2e for the MCP OAuth AS (isolated app, in-memory store, stubbed buyer login)."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import services.mcp_oauth_as as core
import routes.mcp_oauth_as as as_router

ISSUER = "https://api.pivota.cc"
RESOURCE = "https://pivota-agent-production.up.railway.app/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_AS_ENABLED", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_KEY_ID", "router-kid")
    monkeypatch.setenv("MCP_OAUTH_AS_STORE", "memory")
    monkeypatch.setenv("MCP_OAUTH_AS_REQUEST_SECRET", "router-test-secret-0123456789")
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOWED_RESOURCES", RESOURCE)
    core._KEY_CACHE.clear()
    as_router._MEMORY_STORE.clients.clear()
    as_router._MEMORY_STORE.codes.clear()
    as_router._MEMORY_STORE.consumed.clear()
    as_router._MEMORY_STORE.refresh.clear()

    async def _fake_subject(request):
        return "buyer-test"
    monkeypatch.setattr(as_router, "resolve_buyer_subject", _fake_subject)

    app = FastAPI()
    app.include_router(as_router.router)
    return TestClient(app)


def pkce_pair():
    verifier = base64.urlsafe_b64encode(b"v" * 48).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def test_discovery_and_jwks(client):
    m = client.get("/.well-known/oauth-authorization-server").json()
    assert m["issuer"] == ISSUER
    assert m["registration_endpoint"].endswith("/oauth/register")
    j = client.get("/.well-known/jwks.json").json()
    assert j["keys"][0]["kty"] == "RSA"


def test_full_browser_flow_to_access_token(client):
    # 1. DCR
    reg = client.post("/oauth/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude"})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]

    # 2. /authorize -> consent page (buyer is "logged in" via the stub)
    verifier, challenge = pkce_pair()
    auth = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "scope": "pivota.checkout", "state": "s1",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE,
    })
    assert auth.status_code == 200, auth.text
    signed = re.search(r'name="signed_request" value="([^"]+)"', auth.text).group(1)
    signed = signed.replace("&amp;", "&")

    # 3. approve -> redirect with code
    dec = client.post("/oauth/authorize/decision",
                      data={"signed_request": signed, "decision": "approve", "state": "s1"},
                      follow_redirects=False)
    assert dec.status_code == 302, dec.text
    loc = dec.headers["location"]
    assert loc.startswith(REDIRECT)
    code = re.search(r"[?&]code=([^&]+)", loc).group(1)
    assert re.search(r"[?&]state=s1", loc)

    # 4. token exchange
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier,
    })
    assert tok.status_code == 200, tok.text
    body = tok.json()
    assert body["token_type"] == "Bearer"
    assert body["refresh_token"]

    # 5. the access token verifies against the published JWKS, bound to buyer + resource
    jwks = client.get("/.well-known/jwks.json").json()
    key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwks["keys"][0]))
    claims = jwt.decode(body["access_token"], key=key, algorithms=["RS256"], audience=RESOURCE, issuer=ISSUER)
    assert claims["sub"] == "buyer-test"
    assert claims["aud"] == RESOURCE


def test_deny_redirects_with_access_denied(client):
    reg = client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = pkce_pair()
    auth = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE})
    signed = re.search(r'name="signed_request" value="([^"]+)"', auth.text).group(1).replace("&amp;", "&")
    dec = client.post("/oauth/authorize/decision",
                      data={"signed_request": signed, "decision": "deny", "state": ""},
                      follow_redirects=False)
    assert dec.status_code == 302
    assert "error=access_denied" in dec.headers["location"]


def test_authorize_refuses_unlisted_resource(client):
    reg = client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = pkce_pair()
    auth = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": RESOURCE + "/other"})
    assert auth.status_code == 400
    assert auth.json()["error"] == "invalid_target"


def test_disabled_returns_404(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_AS_ENABLED", "0")
    app = FastAPI()
    app.include_router(as_router.router)
    c = TestClient(app)
    assert c.get("/.well-known/oauth-authorization-server").status_code == 404
    assert c.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).status_code == 404


def test_token_rejects_tampered_pkce(client):
    reg = client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = pkce_pair()
    auth = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE})
    signed = re.search(r'name="signed_request" value="([^"]+)"', auth.text).group(1).replace("&amp;", "&")
    dec = client.post("/oauth/authorize/decision",
                      data={"signed_request": signed, "decision": "approve", "state": ""},
                      follow_redirects=False)
    code = re.search(r"[?&]code=([^&]+)", dec.headers["location"]).group(1)
    tok = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": "y" * 64})  # wrong verifier
    assert tok.status_code == 400
    assert tok.json()["error"] == "invalid_grant"
