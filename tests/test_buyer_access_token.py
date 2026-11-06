"""`services.buyer_access_token`: a backend route the gateway calls knows its buyer
only from a pb-oauth-as access token, never from `X-Buyer-Ref`.

Each check in the module's docstring has a test that fails if the check is
dropped. Tokens are minted with the real `mint_access_token`, so the tests
exercise what the authorization server actually emits.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

ISSUER = "https://api.example.test"
RESOURCE = "https://mcp.example.test/mcp"
KID = "test-kid-1"


@pytest.fixture(autouse=True)
def _as_env(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", ISSUER)
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOW_EPHEMERAL_KEY", "1")
    monkeypatch.setenv("MCP_OAUTH_AS_KEY_ID", KID)
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOWED_RESOURCES", RESOURCE)
    monkeypatch.delenv("MCP_OAUTH_AS_PRIVATE_KEY_PEM", raising=False)


def _mint(*, subject="acct_123", audience=RESOURCE, scope=("pivota.checkout", "pivota.account"), client_id="cli_1", **kw):
    from services import mcp_oauth_as as core

    extra = {"client_id": client_id} if client_id is not None else None
    return core.mint_access_token(subject=subject, audience=audience, scope=list(scope), extra_claims=extra, **kw)


def _resign(claims: dict, *, key=None, alg="RS256", kid=KID) -> str:
    from services import mcp_oauth_as as core

    return jwt.encode(claims, key or core._load_private_key(), algorithm=alg, headers={"kid": kid} if kid else None)


def _claims(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def _code(token, **kw):
    from services.buyer_access_token import BuyerTokenError, verify_buyer_access_token

    with pytest.raises(BuyerTokenError) as excinfo:
        verify_buyer_access_token(token, **kw)
    return excinfo.value.code


def test_a_real_account_token_verifies():
    from services.buyer_access_token import verify_buyer_access_token

    buyer = verify_buyer_access_token(_mint())

    assert buyer.account_id == "acct_123"
    assert buyer.client_id == "cli_1"
    assert "pivota.account" in buyer.scopes
    assert buyer.jti


@pytest.mark.parametrize("token, code", [(None, "missing"), ("", "missing"), (123, "missing"), ("x" * 8193, "too_large")])
def test_missing_or_oversized_tokens(token, code):
    assert _code(token) == code


def test_garbage_is_malformed():
    assert _code("not.a.jwt") == "malformed"


def test_another_audience_is_refused():
    assert _code(_mint(audience="https://elsewhere.test/mcp")) == "bad_audience"


def test_a_multi_audience_token_is_refused_even_if_one_matches():
    claims = _claims(_mint())
    claims["aud"] = [RESOURCE, "https://elsewhere.test/mcp"]

    assert _code(_resign(claims)) == "bad_audience"


def test_another_issuer_is_refused():
    claims = _claims(_mint())
    claims["iss"] = "https://evil.test"

    assert _code(_resign(claims)) == "bad_issuer"


def test_an_expired_token_is_refused():
    claims = _claims(_mint())
    now = int(time.time())
    claims.update(iat=now - 7200, nbf=now - 7200, exp=now - 3600)

    assert _code(_resign(claims)) == "expired"


def test_an_overlong_lifetime_is_refused():
    claims = _claims(_mint())
    claims["exp"] = claims["iat"] + 86400

    assert _code(_resign(claims)) == "bad_lifetime"


def test_a_token_signed_by_another_key_is_refused():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    assert _code(_resign(_claims(_mint()), key=other)) == "bad_signature"


def test_hs256_signed_with_the_public_key_is_refused():
    """Algorithm confusion: an HS256 token keyed with our PUBLIC key."""
    from cryptography.hazmat.primitives import serialization

    from services import mcp_oauth_as as core

    public_pem = core._load_private_key().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    # PyJWT refuses to HMAC with a PEM key, so the forgery is built by hand.
    import base64
    import hashlib
    import hmac
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    body = b64(json.dumps(_claims(_mint())).encode())
    sig = b64(hmac.new(public_pem, f"{header}.{body}".encode(), hashlib.sha256).digest())
    forged = f"{header}.{body}.{sig}"

    assert _code(forged) == "bad_alg"


def test_alg_none_is_refused():
    import base64
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "none", "kid": KID}).encode())
    body = b64(json.dumps(_claims(_mint())).encode())

    assert _code(f"{header}.{body}.") == "bad_alg"


def test_a_foreign_kid_is_refused():
    assert _code(_resign(_claims(_mint()), kid="someone-else")) == "bad_kid"


def test_a_refresh_or_other_token_type_is_refused():
    claims = _claims(_mint())
    claims["token_type"] = "refresh"

    assert _code(_resign(claims)) == "not_access_token"


def test_a_guest_subject_is_refused():
    from services import mcp_oauth_as as core

    assert _code(_mint(subject=core.new_guest_subject())) == "guest_subject"


def test_a_token_without_the_account_scope_is_refused():
    assert _code(_mint(scope=("pivota.checkout",))) == "insufficient_scope"


def test_a_token_without_a_client_id_is_refused():
    assert _code(_mint(client_id=None)) == "missing_client"


def test_a_token_without_sub_is_refused():
    claims = _claims(_mint())
    del claims["sub"]

    assert _code(_resign(claims)) == "missing_claim"


def test_no_configured_resource_turns_the_verifier_off(monkeypatch):
    token = _mint()
    monkeypatch.setenv("MCP_OAUTH_AS_ALLOWED_RESOURCES", "")

    assert _code(token) == "not_configured"


def test_no_configured_issuer_turns_the_verifier_off(monkeypatch):
    token = _mint()
    monkeypatch.delenv("MCP_OAUTH_AS_ISSUER")

    assert _code(token) == "not_configured"


# --------------------------------------------------------------------------- the dependency


def _app():
    from fastapi import Depends

    from services.buyer_access_token import VerifiedBuyer, buyer_dependency

    app = FastAPI()

    @app.get("/whoami")
    def whoami(buyer: VerifiedBuyer = Depends(buyer_dependency())):
        return {"account_id": buyer.account_id, "client_id": buyer.client_id}

    return TestClient(app)


def test_the_dependency_reads_only_the_buyer_token_header():
    client = _app()

    ok = client.get("/whoami", headers={"X-Pivota-Buyer-Token": _mint()})

    assert ok.status_code == 200
    assert ok.json() == {"account_id": "acct_123", "client_id": "cli_1"}


def test_x_buyer_ref_alone_is_not_identity():
    client = _app()

    refused = client.get("/whoami", headers={"X-Buyer-Ref": "acct_123", "X-Agent-User-JWT": _mint()})

    assert refused.status_code == 401


@pytest.mark.parametrize("token_kind", ["guest", "expired", "garbage"])
def test_every_refusal_is_the_same_401_and_never_echoes_the_token(token_kind, caplog):
    from services import mcp_oauth_as as core

    if token_kind == "guest":
        token = _mint(subject=core.new_guest_subject())
    elif token_kind == "expired":
        claims = _claims(_mint())
        now = int(time.time())
        claims.update(iat=now - 7200, nbf=now - 7200, exp=now - 3600)
        token = _resign(claims)
    else:
        token = "garbage.token.value"

    with caplog.at_level("WARNING"):
        refused = _app().get("/whoami", headers={"X-Pivota-Buyer-Token": token})

    assert refused.status_code == 401
    assert refused.json() == {"detail": "buyer authentication required"}
    assert token not in refused.text
    assert token not in caplog.text


def test_the_buyer_token_header_is_redacted_from_request_logs():
    from middleware.structured_logging import redact_headers

    assert redact_headers({"X-Pivota-Buyer-Token": "abc"}) == {"X-Pivota-Buyer-Token": "[REDACTED]"}


def test_the_scope_cannot_be_downgraded_from_the_query_string():
    """FastAPI exposes a dependency's keyword arguments as query parameters. The
    scope is fixed when the route is declared, so `?required_scope=` does nothing."""
    client = _app()
    checkout_only = _mint(scope=("pivota.checkout",))

    refused = client.get(
        "/whoami?required_scope=pivota.checkout", headers={"X-Pivota-Buyer-Token": checkout_only}
    )

    assert refused.status_code == 401


def test_the_dependency_takes_only_the_request():
    import inspect

    from services.buyer_access_token import buyer_dependency

    assert list(inspect.signature(buyer_dependency()).parameters) == ["request"]
    with pytest.raises(ValueError):
        buyer_dependency("")


def test_a_token_without_nbf_is_refused():
    claims = _claims(_mint())
    del claims["nbf"]

    assert _code(_resign(claims)) == "missing_claim"


def test_a_token_with_exp_not_after_iat_is_refused():
    # Within the 10 s leeway, so PyJWT lets a just-future iat through and only
    # the lifetime check can refuse it.
    claims = _claims(_mint())
    soon = int(time.time()) + 5
    claims.update(iat=soon, nbf=soon - 5, exp=soon)

    assert _code(_resign(claims)) == "bad_lifetime"


def test_a_token_not_yet_valid_beyond_the_leeway_is_refused():
    claims = _claims(_mint())
    claims["nbf"] = int(time.time()) + 60

    assert _code(_resign(claims)) == "invalid"


def test_a_token_a_few_seconds_early_is_within_the_leeway():
    from services.buyer_access_token import verify_buyer_access_token

    claims = _claims(_mint())
    claims["nbf"] = int(time.time()) + 3

    assert verify_buyer_access_token(_resign(claims)).account_id == "acct_123"


@pytest.mark.parametrize("scope", ["pivota.accountx", "xpivota.account", "pivota.account.read", "pivota.checkout,pivota.account"])
def test_the_scope_is_matched_as_a_whole_word(scope):
    claims = _claims(_mint())
    claims["scope"] = scope

    assert _code(_resign(claims)) == "insufficient_scope"
