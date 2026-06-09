"""End-to-end protocol tests for the MCP OAuth authorization-code + refresh flow (in-memory store)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import jwt
import pytest

import services.mcp_oauth_as as core
from services.mcp_oauth_flow import (
    InMemoryStore,
    OAuthFlowError,
    exchange_authorization_code,
    exchange_refresh_token,
    issue_authorization_code,
    register_client,
    validate_authorization_request,
)

ISSUER = "https://api.pivota.cc"
RESOURCE = "https://pivota-agent-production.up.railway.app/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def run(coro):
    return asyncio.run(coro)


def pkce_pair():
    verifier = base64.urlsafe_b64encode(b"x" * 48).rstrip(b"=").decode()  # 64 chars, valid range
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture(autouse=True)
def _as_env(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_KEY_ID", "flow-kid")
    core._KEY_CACHE.clear()
    yield
    core._KEY_CACHE.clear()


async def _register(store, method="none", redirects=None):
    body = {"redirect_uris": redirects or [REDIRECT]}
    if method != "none":
        body["token_endpoint_auth_method"] = method
    return await register_client(store, body)


def test_full_authorization_code_happy_path():
    store = InMemoryStore()
    reg = run(_register(store))
    verifier, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE,
        "scope": "pivota.checkout", "state": "xyz",
    }))
    code = run(issue_authorization_code(store, validated=validated, subject="buyer-1"))
    tok = run(exchange_authorization_code(store, params={
        "client_id": reg["client_id"], "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier,
    }))
    assert tok["token_type"] == "Bearer"
    assert tok["refresh_token"]
    jwks = core.build_jwks()
    key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwks["keys"][0]))
    claims = jwt.decode(tok["access_token"], key=key, algorithms=["RS256"], audience=RESOURCE, issuer=ISSUER)
    assert claims["sub"] == "buyer-1"
    assert claims["aud"] == RESOURCE


def test_code_is_single_use():
    store = InMemoryStore()
    reg = run(_register(store))
    verifier, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}))
    code = run(issue_authorization_code(store, validated=validated, subject="b"))
    params = {"client_id": reg["client_id"], "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}
    run(exchange_authorization_code(store, params=params))
    with pytest.raises(OAuthFlowError) as e:
        run(exchange_authorization_code(store, params=params))
    assert e.value.error == "invalid_grant"


def test_pkce_mismatch_rejected():
    store = InMemoryStore()
    reg = run(_register(store))
    _, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}))
    code = run(issue_authorization_code(store, validated=validated, subject="b"))
    with pytest.raises(OAuthFlowError) as e:
        run(exchange_authorization_code(store, params={
            "client_id": reg["client_id"], "code": code, "redirect_uri": REDIRECT,
            "code_verifier": "wrong-verifier-but-right-length-aaaaaaaaaaaaaaa"}))
    assert e.value.error == "invalid_grant"


def test_redirect_uri_mismatch_at_exchange_rejected():
    store = InMemoryStore()
    reg = run(_register(store, redirects=[REDIRECT, "https://claude.ai/other"]))
    verifier, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}))
    code = run(issue_authorization_code(store, validated=validated, subject="b"))
    with pytest.raises(OAuthFlowError):
        run(exchange_authorization_code(store, params={
            "client_id": reg["client_id"], "code": code,
            "redirect_uri": "https://claude.ai/other", "code_verifier": verifier}))


def test_code_bound_to_issuing_client():
    store = InMemoryStore()
    reg1 = run(_register(store))
    reg2 = run(_register(store))
    verifier, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg1["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}))
    code = run(issue_authorization_code(store, validated=validated, subject="b"))
    with pytest.raises(OAuthFlowError):
        run(exchange_authorization_code(store, params={
            "client_id": reg2["client_id"], "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}))


def test_expired_code_rejected():
    store = InMemoryStore()
    reg = run(_register(store))
    verifier, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}))
    code = run(issue_authorization_code(store, validated=validated, subject="b", now=1000))
    with pytest.raises(OAuthFlowError):
        run(exchange_authorization_code(store, params={
            "client_id": reg["client_id"], "code": code, "redirect_uri": REDIRECT,
            "code_verifier": verifier}, now=1000 + 99999))


def test_refresh_rotation_and_reuse_revoked():
    store = InMemoryStore()
    reg = run(_register(store))
    verifier, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}))
    code = run(issue_authorization_code(store, validated=validated, subject="b"))
    tok = run(exchange_authorization_code(store, params={
        "client_id": reg["client_id"], "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}))
    rt = tok["refresh_token"]
    tok2 = run(exchange_refresh_token(store, params={"client_id": reg["client_id"], "refresh_token": rt}))
    assert tok2["access_token"] and tok2["refresh_token"] != rt
    with pytest.raises(OAuthFlowError):  # old refresh revoked after rotation
        run(exchange_refresh_token(store, params={"client_id": reg["client_id"], "refresh_token": rt}))


def test_confidential_client_requires_secret():
    store = InMemoryStore()
    reg = run(_register(store, method="client_secret_basic"))
    assert reg.get("client_secret")
    verifier, challenge = pkce_pair()
    validated = run(validate_authorization_request(store, {
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}))
    code = run(issue_authorization_code(store, validated=validated, subject="b"))
    with pytest.raises(OAuthFlowError) as e:  # no secret provided
        run(exchange_authorization_code(store, params={
            "client_id": reg["client_id"], "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}))
    assert e.value.error == "invalid_client"
    # correct secret works
    tok = run(exchange_authorization_code(store, params={
        "client_id": reg["client_id"], "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier},
        client_secret=reg["client_secret"]))
    assert tok["access_token"]


def test_validate_rejects_bad_requests():
    store = InMemoryStore()
    reg = run(_register(store))
    _, challenge = pkce_pair()
    base = {"response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE}
    with pytest.raises(OAuthFlowError):  # bad response_type
        run(validate_authorization_request(store, {**base, "response_type": "token"}))
    with pytest.raises(OAuthFlowError):  # unknown client
        run(validate_authorization_request(store, {**base, "client_id": "nope"}))
    with pytest.raises(OAuthFlowError):  # unregistered redirect
        run(validate_authorization_request(store, {**base, "redirect_uri": "https://evil.example/cb"}))
    with pytest.raises(OAuthFlowError):  # missing PKCE
        run(validate_authorization_request(store, {**base, "code_challenge": ""}))
    with pytest.raises(OAuthFlowError):  # plain PKCE
        run(validate_authorization_request(store, {**base, "code_challenge_method": "plain"}))
    with pytest.raises(OAuthFlowError):  # missing resource
        run(validate_authorization_request(store, {**base, "resource": ""}))
