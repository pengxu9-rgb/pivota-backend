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


# ---------------------------------------------------------------- guest-subject mode

@pytest.fixture()
def guest_client(monkeypatch):
    """AS with guest subjects ENABLED and NO account session (resolve_buyer_subject -> None)."""
    monkeypatch.setenv("MCP_OAUTH_AS_ENABLED", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_KEY_ID", "guest-kid")
    monkeypatch.setenv("MCP_OAUTH_AS_STORE", "memory")
    monkeypatch.setenv("MCP_OAUTH_AS_REQUEST_SECRET", "router-test-secret-0123456789")
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOWED_RESOURCES", RESOURCE)
    monkeypatch.setenv("MCP_OAUTH_AS_GUEST_SUBJECTS", "1")
    core._KEY_CACHE.clear()
    for attr in ("clients", "codes", "consumed", "refresh"):
        getattr(as_router._MEMORY_STORE, attr).clear()

    async def _no_subject(request):
        return None
    monkeypatch.setattr(as_router, "resolve_buyer_subject", _no_subject)

    app = FastAPI()
    app.include_router(as_router.router)
    # https base_url so the Secure guest cookie is actually stored+sent across requests
    return TestClient(app, base_url="https://testserver")


def _authorize_and_get_signed(c, client_id, challenge, scope="pivota.checkout"):
    auth = c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "scope": scope, "state": "g1",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE})
    assert auth.status_code == 200, auth.text
    return re.search(r'name="signed_request" value="([^"]+)"', auth.text).group(1).replace("&amp;", "&")


def _decode(c, access_token):
    jwks = c.get("/.well-known/jwks.json").json()
    key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwks["keys"][0]))
    return jwt.decode(access_token, key=key, algorithms=["RS256"], audience=RESOURCE, issuer=ISSUER)


def test_guest_disabled_returns_login_required(client, monkeypatch):
    # the default `client` fixture logs a buyer in; drop that so no subject resolves and guests
    # are OFF — must reproduce the pre-feature login_required behavior byte-for-byte.
    async def _no_subject(request):
        return None
    monkeypatch.setattr(as_router, "resolve_buyer_subject", _no_subject)
    monkeypatch.delenv("MCP_OAUTH_AS_GUEST_SUBJECTS", raising=False)
    reg = client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = pkce_pair()
    auth = client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE})
    assert auth.status_code == 401
    assert auth.json()["error"] == "login_required"


def test_guest_full_flow_mints_namespaced_subject(guest_client):
    reg = guest_client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce_pair()
    signed = _authorize_and_get_signed(guest_client, client_id, challenge)
    dec = guest_client.post("/oauth/authorize/decision",
                            data={"signed_request": signed, "decision": "approve", "state": "g1"},
                            follow_redirects=False)
    assert dec.status_code == 302, dec.text
    # a guest cookie is set on approval
    assert as_router.GUEST_COOKIE_NAME in dec.headers.get("set-cookie", "")
    code = re.search(r"[?&]code=([^&]+)", dec.headers["location"]).group(1)
    tok = guest_client.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    claims = _decode(guest_client, tok.json()["access_token"])
    assert claims["sub"].startswith("mcpguest_")
    assert claims["scope"] == "pivota.checkout"


def test_guest_cannot_obtain_account_scope(guest_client):
    reg = guest_client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce_pair()
    # request BOTH scopes; the account scope must be stripped for a guest
    signed = _authorize_and_get_signed(guest_client, client_id, challenge,
                                       scope="pivota.checkout pivota.account")
    dec = guest_client.post("/oauth/authorize/decision",
                            data={"signed_request": signed, "decision": "approve", "state": "g1"},
                            follow_redirects=False)
    code = re.search(r"[?&]code=([^&]+)", dec.headers["location"]).group(1)
    tok = guest_client.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier})
    claims = _decode(guest_client, tok.json()["access_token"])
    assert "pivota.account" not in claims["scope"].split()
    assert claims["scope"] == "pivota.checkout"


def test_authorize_rejects_unsupported_scope(guest_client):
    reg = guest_client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = pkce_pair()
    auth = guest_client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "scope": "pivota.checkout pivota.admin",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE})
    assert auth.status_code == 400
    assert auth.json()["error"] == "invalid_scope"


def test_guest_cookie_reused_gives_stable_subject(guest_client):
    reg = guest_client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]

    def one_round():
        verifier, challenge = pkce_pair()
        signed = _authorize_and_get_signed(guest_client, client_id, challenge)
        dec = guest_client.post("/oauth/authorize/decision",
                                data={"signed_request": signed, "decision": "approve", "state": "g1"},
                                follow_redirects=False)
        code = re.search(r"[?&]code=([^&]+)", dec.headers["location"]).group(1)
        tok = guest_client.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier})
        return _decode(guest_client, tok.json()["access_token"])["sub"]

    # TestClient persists cookies across requests, so the 2nd authorize carries the 1st cookie
    sub1 = one_round()
    sub2 = one_round()
    assert sub1 == sub2


def test_guest_cookie_tamper_is_ignored(guest_client):
    reg = guest_client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    # a forged cookie value (attacker-chosen subject, bad/missing MAC) must not be honored:
    # the flow ignores it and mints a fresh subject instead of adopting the injected one.
    guest_client.cookies.set(as_router.GUEST_COOKIE_NAME, "mcpguest_ATTACKERCHOSEN.deadbeef")
    verifier, challenge = pkce_pair()
    signed = _authorize_and_get_signed(guest_client, client_id, challenge)
    dec = guest_client.post("/oauth/authorize/decision",
                            data={"signed_request": signed, "decision": "approve", "state": "g1"},
                            follow_redirects=False)
    code = re.search(r"[?&]code=([^&]+)", dec.headers["location"]).group(1)
    tok = guest_client.post("/oauth/token", data={
        "grant_type": "authorization_code", "client_id": client_id, "code": code,
        "redirect_uri": REDIRECT, "code_verifier": verifier})
    claims = _decode(guest_client, tok.json()["access_token"])
    assert claims["sub"] != "mcpguest_ATTACKERCHOSEN"
    assert claims["sub"].startswith("mcpguest_")


def test_consent_request_expires(guest_client, monkeypatch):
    reg = guest_client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = pkce_pair()
    signed = _authorize_and_get_signed(guest_client, client_id, challenge)
    # jump past the consent TTL: a captured blob must not stay replayable forever
    real_time = as_router.time.time
    monkeypatch.setattr(as_router.time, "time",
                        lambda: real_time() + as_router.CONSENT_REQUEST_TTL_SECONDS + 5)
    dec = guest_client.post("/oauth/authorize/decision",
                            data={"signed_request": signed, "decision": "approve", "state": "g1"},
                            follow_redirects=False)
    assert dec.status_code == 400
    assert dec.json()["error"] == "invalid_request"


def test_guest_deny_mints_no_subject(guest_client):
    reg = guest_client.post("/oauth/register", json={"redirect_uris": [REDIRECT]})
    client_id = reg.json()["client_id"]
    _, challenge = pkce_pair()
    signed = _authorize_and_get_signed(guest_client, client_id, challenge)
    dec = guest_client.post("/oauth/authorize/decision",
                            data={"signed_request": signed, "decision": "deny", "state": "g1"},
                            follow_redirects=False)
    assert dec.status_code == 302
    assert "error=access_denied" in dec.headers["location"]
    # a deny must not set a guest cookie
    assert as_router.GUEST_COOKIE_NAME not in dec.headers.get("set-cookie", "")
