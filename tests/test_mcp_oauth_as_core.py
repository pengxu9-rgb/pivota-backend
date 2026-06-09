"""Unit tests for the MCP OAuth Authorization Server core (services/mcp_oauth_as.py)."""

from __future__ import annotations

import json
import os

import jwt
import pytest

import services.mcp_oauth_as as As
from services.mcp_oauth_as import (
    McpOAuthAsError,
    authorization_server_metadata,
    build_jwks,
    mint_access_token,
    shape_registration_request,
    verify_pkce,
)

ISSUER = "https://api.pivota.cc"
RESOURCE = "https://pivota-agent-production.up.railway.app/mcp"


@pytest.fixture(autouse=True)
def _as_env(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_KEY_ID", "test-kid-1")
    # reset cached key so each run is deterministic within the process
    As._KEY_CACHE.clear()
    yield
    As._KEY_CACHE.clear()


def test_jwks_shape():
    jwks = build_jwks()
    assert isinstance(jwks["keys"], list) and len(jwks["keys"]) == 1
    k = jwks["keys"][0]
    assert k["kty"] == "RSA"
    assert k["use"] == "sig"
    assert k["alg"] == "RS256"
    assert k["kid"] == "test-kid-1"
    assert k["n"] and k["e"]
    assert "d" not in k  # never leak the private exponent


def test_mint_and_verify_with_published_jwks():
    token = mint_access_token(subject="user-42", audience=RESOURCE, scope=["pivota.checkout"])
    jwks = build_jwks()
    key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwks["keys"][0]))
    claims = jwt.decode(token, key=key, algorithms=["RS256"], audience=RESOURCE, issuer=ISSUER)
    assert claims["sub"] == "user-42"
    assert claims["aud"] == RESOURCE
    assert claims["scope"] == "pivota.checkout"
    assert claims["iss"] == ISSUER
    assert claims["exp"] > claims["iat"]


def test_token_accepted_by_existing_agent_user_jwt_verifier(monkeypatch):
    """The AS token must be accepted by the backend's existing buyer-identity verifier."""
    import services.agent_user_jwt as verifier_mod

    token = mint_access_token(subject="buyer-99", audience=RESOURCE)
    jwks = build_jwks()
    verifier_mod._JWKS_CACHE.clear()
    monkeypatch.setenv("AGENT_USER_JWKS_JSON", json.dumps(jwks))
    monkeypatch.setenv("AGENT_USER_JWT_ISSUERS", ISSUER)
    monkeypatch.setenv("AGENT_USER_JWT_AUDIENCE", RESOURCE)
    monkeypatch.setenv("AGENT_USER_JWT_ALGS", "RS256")

    ident = verifier_mod.verify_agent_user_jwt(token)
    assert ident.subject == "buyer-99"
    assert ident.issuer == ISSUER
    assert ident.agent_user_ref == f"{ISSUER}:buyer-99"


def test_extra_claims_cannot_override_security_claims():
    token = mint_access_token(
        subject="u", audience=RESOURCE,
        extra_claims={"iss": "https://evil", "sub": "attacker", "scope": "admin"},
    )
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == ISSUER
    assert claims["sub"] == "u"
    assert claims["scope"] != "admin"


def test_pkce_s256_known_vector():
    # RFC 7636 Appendix B
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert verify_pkce(code_verifier=verifier, code_challenge=challenge) is True
    assert verify_pkce(code_verifier=verifier, code_challenge="tampered") is False


def test_pkce_rejects_plain_and_bad_lengths():
    verifier = "a" * 43
    import hashlib, base64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert verify_pkce(code_verifier=verifier, code_challenge=challenge, method="plain") is False
    assert verify_pkce(code_verifier="short", code_challenge=challenge) is False
    assert verify_pkce(code_verifier="a" * 200, code_challenge=challenge) is False


def test_dcr_public_client_default_no_secret():
    c = shape_registration_request({"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "client_name": "Claude"})
    assert c.client_id.startswith("mcpc_")
    assert c.client_secret is None
    assert c.token_endpoint_auth_method == "none"


def test_dcr_confidential_client_gets_secret():
    c = shape_registration_request({
        "redirect_uris": ["https://app.example.com/cb"],
        "token_endpoint_auth_method": "client_secret_basic",
    })
    assert c.client_secret and c.client_secret.startswith("mcps_")


def test_dcr_rejects_insecure_redirect():
    with pytest.raises(McpOAuthAsError):
        shape_registration_request({"redirect_uris": ["http://evil.example.com/cb"]})
    # loopback http is allowed
    c = shape_registration_request({"redirect_uris": ["http://127.0.0.1:8765/cb"]})
    assert c.client_id


def test_metadata_shape():
    m = authorization_server_metadata()
    assert m["issuer"] == ISSUER
    assert m["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
    assert m["token_endpoint"] == f"{ISSUER}/oauth/token"
    assert m["registration_endpoint"] == f"{ISSUER}/oauth/register"
    assert m["jwks_uri"] == f"{ISSUER}/.well-known/jwks.json"
    assert m["code_challenge_methods_supported"] == ["S256"]


def test_missing_issuer_fails_closed(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_AS_ISSUER", raising=False)
    with pytest.raises(McpOAuthAsError):
        authorization_server_metadata()


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY", raising=False)
    monkeypatch.delenv("MCP_OAUTH_AS_PRIVATE_KEY_PEM", raising=False)
    As._KEY_CACHE.clear()
    with pytest.raises(McpOAuthAsError):
        build_jwks()
