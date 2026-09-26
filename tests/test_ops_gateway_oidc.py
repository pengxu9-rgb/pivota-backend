"""The gateway's Google OIDC identity token on `GET /ops/merchant-purchasability`.

WHAT THIS FILE IS DEFENDING. Production `web` runs `--allow-unauthenticated`, so Cloud Run IAM
does NOT stand in front of this route. `utils/gateway_oidc_auth.require_admin_or_gateway_identity`
is the whole guarantee, and it is a guarantee with SIX conjuncts (signature, `iss`, `aud`,
`email_verified`, allow-listed `email`, `exp`/`iat`). Every one of them is tested for REMOVAL
here, not merely for presence, because a conjunct nobody tried to delete is a conjunct nobody
knows is load-bearing.

NO NETWORK. `google.oauth2.id_token._fetch_certs` is patched to hand back a key generated in
this process, so `verify_oauth2_token` runs its real code path — real base64 decode, real RSA
verification, real `exp`/`iat` arithmetic, real `aud` compare — against our key. Nothing here
reaches Google.

NO `main` IMPORT, no database: the dependency touches neither, so this suite mounts minimal
FastAPI apps and drives them with httpx's `ASGITransport` (never `TestClient`, which hangs
against the asyncpg pool). There is deliberately NO `*_postgres.py` twin — there is nothing
dialect-shaped in an auth dependency to have one of.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import utils.auth as auth_module  # noqa: E402
import utils.gateway_oidc_auth as gw  # noqa: E402
from utils.auth import require_admin  # noqa: E402

pytestmark = pytest.mark.asyncio

AUDIENCE = "https://api.pivota.cc"
GATEWAY_SA = "sa-gateway@pivota-prod.iam.gserviceaccount.com"
OTHER_SA = "sa-worker@pivota-prod.iam.gserviceaccount.com"
KEY_ID = "test-kid-1"
JWT_SECRET = "a-test-signing-secret-long-enough-to-be-accepted-0123456789"


# ---------------------------------------------------------------------------
# A key, and a minter, that live entirely in this process
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def public_pem(rsa_key):
    return rsa_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@pytest.fixture(scope="module")
def private_pem(rsa_key):
    return rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _claims(**overrides: Any) -> Dict[str, Any]:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "azp": "1234567890",
        "sub": "1234567890",
        "email": GATEWAY_SA,
        "email_verified": True,
        "iat": now - 30,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _OMIT}


class _Omit:
    pass


_OMIT = _Omit()


@pytest.fixture
def mint(private_pem):
    """A correctly signed RS256 Google-shaped ID token, with any claim overridden or omitted."""
    from google.auth import crypt
    from google.auth import jwt as google_jwt

    signer = crypt.RSASigner.from_string(private_pem, KEY_ID)

    def _mint(**overrides: Any) -> str:
        payload = _claims(**overrides)
        return google_jwt.encode(signer, payload, header={"kid": KEY_ID}).decode()

    return _mint


class _CertsServer:
    """Google's certs document, served from this process — and COUNTED.

    Counting matters as much as serving. google-auth does NOT cache, and its fetch happens BEFORE
    the token is parsed, so on a `--allow-unauthenticated` route the number of outbound requests
    an ANONYMOUS caller can cause is a security property, not a performance one. Every fetch in
    this file goes through here, so "N requests made M fetches" is a measurement.
    """

    def __init__(self, certs):
        self.certs = dict(certs)
        self.calls = []
        self.failure = None

    def __call__(self, url):
        self.calls.append(url)
        if self.failure is not None:
            raise self.failure
        return dict(self.certs)

    @property
    def count(self):
        return len(self.calls)


class _ExplodingRequest:
    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - must never run
        raise AssertionError("the OUTBOUND certs transport was used — the seam did not hold")


@pytest.fixture(autouse=True)
def certs(monkeypatch, public_pem):
    """The module's ONE network seam, replaced. Nothing in this file touches the network."""
    server = _CertsServer({KEY_ID: public_pem})
    monkeypatch.setattr(gw, "_http_fetch_certs", server)
    # The OUTBOUND transport is still resolved by `certs_request()`; pin it to something that
    # raises, so that if the seam above ever stopped being the only one, this file fails loudly
    # rather than dialling out to Google from a unit test.
    monkeypatch.setattr(gw, "_certs_request", _ExplodingRequest(), raising=False)
    gw._reset_warn_state_for_test()
    gw._reset_certs_cache_for_test()
    yield server
    gw._reset_warn_state_for_test()
    gw._reset_certs_cache_for_test()


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    """Both envs set: the shipped default is UNSET, which disables the path entirely."""
    monkeypatch.setenv(gw.AUDIENCE_ENV, AUDIENCE)
    monkeypatch.setenv(gw.SERVICE_ACCOUNTS_ENV, f"{GATEWAY_SA}, {OTHER_SA}")


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch):
    """A usable HS256 secret for the ADMIN path, without touching the real settings check."""
    monkeypatch.setattr(auth_module, "require_jwt_secret", lambda: JWT_SECRET)


@pytest.fixture
def admin_jwt():
    return auth_module.create_access_token(
        {"sub": "u-1", "email": "ops@pivota.cc", "role": "admin"},
    )


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    """The dependency under test, plus the CONTROL: a route on the untouched `require_admin`.

    The control is how "every other ops route is unchanged" is measured rather than asserted —
    the same request goes to both and the refusal must be byte-identical.
    """
    application = FastAPI()

    @application.get("/probe")
    async def _probe(principal: Dict[str, Any] = Depends(gw.require_admin_or_gateway_identity)):
        return {"principal": principal}

    @application.get("/control")
    async def _control(principal: Dict[str, Any] = Depends(require_admin)):
        return {"principal": principal}

    return application


@pytest.fixture
def ops_app(monkeypatch):
    """The REAL route, with only its database reads stubbed — so a 200 here proves the whole
    request path, not just the dependency in isolation."""
    import db.merchant_purchasability as purchasability
    from routes.merchant_purchasability_ops import router

    async def _no_rows(domain, market):
        return []

    async def _not_purchasable(domain, market):
        return False

    monkeypatch.setattr(purchasability, "list_facts", _no_rows)
    monkeypatch.setattr(purchasability, "is_purchasable", _not_purchasable)
    monkeypatch.setattr(purchasability, "buyer_vantage", lambda: "us-west1")
    monkeypatch.setattr(purchasability, "is_enforcement_enabled", lambda: False)
    monkeypatch.setattr(purchasability, "is_sweep_enabled", lambda: False)
    monkeypatch.setattr(purchasability, "ttl_hours", lambda: 72)

    application = FastAPI()
    application.include_router(router)
    return application


async def _get(application, url: str, token: Optional[str] = None,
               headers: Optional[Dict[str, str]] = None) -> httpx.Response:
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(url, headers=request_headers)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

async def test_a_valid_gateway_identity_token_is_accepted(app, mint):
    response = await _get(app, "/probe", mint())
    assert response.status_code == 200, response.text
    principal = response.json()["principal"]
    assert principal["role"] == "gateway_identity"
    assert principal["email"] == GATEWAY_SA
    # NO USER ID. A machine must not be attributable to a person by anything reading `sub`.
    assert "sub" not in principal
    assert principal["role"] not in auth_module.ADMIN_ROLES


async def test_the_real_ops_route_serves_a_gateway_identity_token(ops_app, mint):
    response = await _get(
        ops_app, "/ops/merchant-purchasability?domain=judydoll.com&market=US", mint(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["tier"] == "browse_only"


async def test_a_second_allow_listed_service_account_is_accepted(app, mint):
    assert (await _get(app, "/probe", mint(email=OTHER_SA))).status_code == 200


async def test_the_email_comparison_is_case_insensitive(app, mint):
    assert (await _get(app, "/probe", mint(email=GATEWAY_SA.upper()))).status_code == 200


# ---------------------------------------------------------------------------
# Each conjunct, refused — and refused IDENTICALLY
# ---------------------------------------------------------------------------

BAD_ADMIN_JWT = "not.a.jwt"


async def _assert_refused_like_a_bad_admin_jwt(app, token: str) -> None:
    """The no-fingerprinting rule, measured: same status AND same body as the control route
    gives a garbage admin JWT. A prober must not learn that this path exists."""
    baseline = await _get(app, "/control", BAD_ADMIN_JWT)
    response = await _get(app, "/probe", token)
    assert response.status_code == baseline.status_code == 401, response.text
    assert response.content == baseline.content
    assert "gateway" not in response.text.lower()
    assert "oidc" not in response.text.lower()


async def test_a_wrong_audience_is_refused(app, mint):
    await _assert_refused_like_a_bad_admin_jwt(app, mint(aud="https://someone-else.example"))


async def test_an_audience_that_differs_only_by_a_trailing_slash_is_refused(app, mint):
    await _assert_refused_like_a_bad_admin_jwt(app, mint(aud=AUDIENCE + "/"))


async def test_a_wrong_issuer_is_refused(app, mint):
    await _assert_refused_like_a_bad_admin_jwt(app, mint(iss="https://evil.example"))


async def test_the_bare_google_issuer_spelling_is_accepted(app, mint):
    assert (await _get(app, "/probe", mint(iss="accounts.google.com"))).status_code == 200


async def test_an_unlisted_service_account_is_refused(app, mint):
    await _assert_refused_like_a_bad_admin_jwt(app, mint(email="attacker@gmail.com"))


async def test_an_unverified_email_is_refused(app, mint):
    await _assert_refused_like_a_bad_admin_jwt(app, mint(email_verified=False))


async def test_a_truthy_but_non_boolean_email_verified_is_refused(app, mint):
    # `"false"` is truthy. So is `1`. Neither is what Google mints, and a truthiness test here
    # would admit the first of them.
    await _assert_refused_like_a_bad_admin_jwt(app, mint(email_verified="false"))


async def test_a_missing_email_claim_is_refused(app, mint):
    await _assert_refused_like_a_bad_admin_jwt(app, mint(email=_OMIT))


async def test_an_expired_token_is_refused(app, mint):
    now = int(time.time())
    await _assert_refused_like_a_bad_admin_jwt(app, mint(iat=now - 7200, exp=now - 3600))


async def test_a_token_expired_by_less_than_the_clock_skew_is_still_accepted(app, mint):
    now = int(time.time())
    # The skew is 10 s and MUST NOT be larger: a token dead for 5 s is inside it.
    assert (await _get(app, "/probe", mint(iat=now - 60, exp=now - 5))).status_code == 200


async def test_a_not_yet_valid_token_is_refused(app, mint):
    now = int(time.time())
    await _assert_refused_like_a_bad_admin_jwt(app, mint(iat=now + 3600, exp=now + 7200))


async def test_a_bad_signature_is_refused(app, mint):
    token = mint()
    head, payload, signature = token.split(".")
    tampered = f"{head}.{payload}.{_b64(b'x' * 256)}"
    await _assert_refused_like_a_bad_admin_jwt(app, tampered)
    # And the SAME payload with its real signature is fine, so the case above measured the
    # signature and not something else about the token.
    assert (await _get(app, "/probe", f"{head}.{payload}.{signature}")).status_code == 200


async def test_alg_none_is_refused(app):
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": KEY_ID}).encode())
    payload = _b64(json.dumps(_claims()).encode())
    # Both spellings an attacker tries: with and without a trailing empty signature.
    await _assert_refused_like_a_bad_admin_jwt(app, f"{header}.{payload}.")
    await _assert_refused_like_a_bad_admin_jwt(app, f"{header}.{payload}.{_b64(b'')}")


async def test_hs256_signed_with_the_public_key_as_the_secret_is_refused(app, public_pem):
    import hashlib
    import hmac

    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KEY_ID}).encode())
    payload = _b64(json.dumps(_claims()).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = _b64(hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest())
    await _assert_refused_like_a_bad_admin_jwt(app, f"{header}.{payload}.{signature}")


async def test_an_unknown_key_id_is_refused(app, mint):
    token = mint()
    header, payload, signature = token.split(".")
    forged_header = _b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "not-a-kid"}).encode())
    await _assert_refused_like_a_bad_admin_jwt(app, f"{forged_header}.{payload}.{signature}")


async def test_garbage_is_refused(app):
    await _assert_refused_like_a_bad_admin_jwt(app, "aaaa.bbbb.cccc")


# ---------------------------------------------------------------------------
# The two kill switches — unset means DISABLED, not unconstrained
# ---------------------------------------------------------------------------

async def test_with_the_audience_env_unset_the_path_is_disabled(app, mint, monkeypatch):
    monkeypatch.delenv(gw.AUDIENCE_ENV, raising=False)
    assert gw.gateway_identity_enabled() is False
    await _assert_refused_like_a_bad_admin_jwt(app, mint())


async def test_an_empty_audience_env_is_the_same_as_unset(app, mint, monkeypatch):
    monkeypatch.setenv(gw.AUDIENCE_ENV, "   ")
    await _assert_refused_like_a_bad_admin_jwt(app, mint())


async def test_with_the_allowlist_env_unset_the_path_is_disabled(app, mint, monkeypatch):
    monkeypatch.delenv(gw.SERVICE_ACCOUNTS_ENV, raising=False)
    assert gw.gateway_identity_enabled() is False
    await _assert_refused_like_a_bad_admin_jwt(app, mint())


async def test_an_allowlist_of_only_separators_is_the_same_as_unset(app, mint, monkeypatch):
    monkeypatch.setenv(gw.SERVICE_ACCOUNTS_ENV, " , , ")
    assert gw.configured_service_accounts() == set()
    await _assert_refused_like_a_bad_admin_jwt(app, mint())


async def test_both_envs_unset_is_the_shipped_default(monkeypatch):
    monkeypatch.delenv(gw.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(gw.SERVICE_ACCOUNTS_ENV, raising=False)
    assert gw.gateway_identity_enabled() is False


# ---------------------------------------------------------------------------
# Size ceiling
# ---------------------------------------------------------------------------

async def test_a_token_over_8_kib_is_refused_before_any_parsing(app, mint, monkeypatch):
    from google.oauth2 import id_token as google_id_token

    called = []

    def _must_not_run(*args, **kwargs):
        called.append(args)
        raise AssertionError("an oversized token reached the verifier")

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", _must_not_run)
    oversized = mint() + ("A" * (gw.MAX_TOKEN_BYTES + 1))
    await _assert_refused_like_a_bad_admin_jwt(app, oversized)
    assert called == []


async def test_a_token_at_the_ceiling_still_reaches_the_verifier(app, mint):
    # The ceiling is a ceiling, not a fence in front of the real tokens: a normal Google ID
    # token is ~1 KB and must be nowhere near it.
    assert len(mint().encode()) < gw.MAX_TOKEN_BYTES


# ---------------------------------------------------------------------------
# The admin path is untouched
# ---------------------------------------------------------------------------

async def test_the_admin_jwt_still_works(app, admin_jwt):
    response = await _get(app, "/probe", admin_jwt)
    assert response.status_code == 200
    assert response.json()["principal"]["role"] == "admin"


async def test_the_admin_jwt_still_works_with_the_oidc_path_disabled(app, admin_jwt, monkeypatch):
    monkeypatch.delenv(gw.AUDIENCE_ENV, raising=False)
    monkeypatch.delenv(gw.SERVICE_ACCOUNTS_ENV, raising=False)
    assert (await _get(app, "/probe", admin_jwt)).status_code == 200


async def test_a_non_admin_jwt_is_refused_exactly_as_before(app):
    token = auth_module.create_access_token(
        {"sub": "u-2", "email": "m@x.com", "role": "merchant"},
    )
    probe = await _get(app, "/probe", token)
    control = await _get(app, "/control", token)
    assert probe.status_code == control.status_code == 403
    assert probe.content == control.content


async def test_an_x_admin_key_header_is_refused_exactly_as_before(app):
    # The brief expected a 401 here. The BASE behaviour is a 403 from
    # `HTTPBearer(auto_error=True)` ("Not authenticated") because there is no Authorization
    # header at all, and `require_admin_or_key` — which WOULD accept this header — is not used
    # by this route or any other ops route. What matters and is asserted: the header buys
    # nothing, and the refusal is byte-identical to the control's.
    headers = {"X-ADMIN-KEY": "whatever"}
    probe = await _get(app, "/probe", None, headers)
    control = await _get(app, "/control", None, headers)
    assert probe.status_code == control.status_code
    assert probe.status_code in (401, 403)
    assert probe.content == control.content


async def test_a_missing_authorization_header_is_refused_exactly_as_before(app):
    probe = await _get(app, "/probe")
    control = await _get(app, "/control")
    assert probe.status_code == control.status_code
    assert probe.content == control.content


async def test_a_non_bearer_scheme_is_refused_exactly_as_before(app):
    headers = {"Authorization": "Basic dXNlcjpwYXNz"}
    probe = await _get(app, "/probe", None, headers)
    control = await _get(app, "/control", None, headers)
    assert probe.status_code == control.status_code
    assert probe.content == control.content


# ---------------------------------------------------------------------------
# Every OTHER ops route is unchanged
# ---------------------------------------------------------------------------

async def test_a_route_on_require_admin_refuses_an_oidc_token(app, mint):
    """The control route IS `require_admin`, the dependency every other ops route uses. A valid
    gateway token must buy nothing there."""
    response = await _get(app, "/control", mint())
    assert response.status_code == 401
    baseline = await _get(app, "/control", BAD_ADMIN_JWT)
    assert response.content == baseline.content


async def test_exactly_one_route_module_uses_the_new_dependency():
    """The blast radius, measured. A future edit that pastes this dependency onto a WRITE route
    fails here rather than in a review nobody ran."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "routes"
    users = sorted(
        path.name for path in root.rglob("*.py")
        if "require_admin_or_gateway_identity" in path.read_text(encoding="utf-8", errors="replace")
    )
    assert users == ["merchant_purchasability_ops.py"], users


async def test_the_dependency_is_not_reachable_through_an_admin_key():
    import ast
    import inspect

    # The NAMES THE CODE USES, not the prose: the module docstring names `require_admin_or_key`
    # and `X-ADMIN-KEY` precisely to say it does not use them, and a substring test over the
    # source would fail on its own documentation.
    tree = ast.parse(inspect.getsource(gw))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.asname or node.name.split(".")[-1])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # String CONSTANTS other than docstrings would be how a header name sneaks in.
            if node.value.strip().upper() in {"X-ADMIN-KEY", "ADMIN_API_KEY", "PROMOTIONS_ADMIN_KEY"}:
                used.add(node.value)
    # The header rail exists in utils/auth.py as `require_admin_or_key`. This dependency must
    # never reach it: widening an ops route to `ADMIN_API_KEY` is a different decision with a
    # different blast radius, and it is not the one this change made.
    assert "require_admin_or_key" not in used
    assert not ({"X-ADMIN-KEY", "ADMIN_API_KEY", "PROMOTIONS_ADMIN_KEY"} & used)


# ---------------------------------------------------------------------------
# The token never reaches a log, and the reason never reaches the wire
# ---------------------------------------------------------------------------

async def test_no_log_record_ever_contains_token_material(app, mint, admin_jwt, caplog):
    caplog.set_level(logging.DEBUG, logger="utils.gateway_oidc_auth")
    caplog.set_level(logging.DEBUG, logger="utils.auth")
    tokens = []
    for token in [mint(), mint(aud="wrong"), mint(email="nobody@example.com"),
                  mint(email_verified=False), "aaaa.bbbb.cccc", admin_jwt]:
        tokens.append(token)
        gw._reset_warn_state_for_test()
        await _get(app, "/probe", token)

    rendered = "\n".join(
        f"{record.getMessage()} {record.args!r} {record.__dict__!r}" for record in caplog.records
    )
    for token in tokens:
        assert token not in rendered
        for segment in token.split("."):
            if len(segment) > 24:
                assert segment not in rendered


async def test_the_failure_warning_carries_a_reason_code_and_is_rate_limited(app, mint, caplog):
    caplog.set_level(logging.DEBUG, logger="utils.gateway_oidc_auth")
    for _ in range(5):
        await _get(app, "/probe", mint(aud="https://wrong.example"))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "reason=verify_failed" in message, message
    # A CODE, not a sentence, and nothing attacker-controlled in it.
    assert "https://wrong.example" not in message


async def test_a_different_reason_gets_its_own_warning(app, mint, caplog):
    caplog.set_level(logging.DEBUG, logger="utils.gateway_oidc_auth")
    await _get(app, "/probe", mint(aud="https://wrong.example"))
    await _get(app, "/probe", mint(email="nobody@example.com"))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    messages = [r.getMessage() for r in warnings]
    assert any("reason=verify_failed" in m for m in messages), messages
    assert any("reason=email_not_allowlisted" in m for m in messages), messages


async def test_the_success_line_is_debug_only(app, mint, caplog):
    caplog.set_level(logging.DEBUG, logger="utils.gateway_oidc_auth")
    assert (await _get(app, "/probe", mint())).status_code == 200
    ours = [r for r in caplog.records if r.name == "utils.gateway_oidc_auth"]
    assert ours, "expected the success line at debug"
    assert all(r.levelno <= 10 for r in ours), [(r.levelname, r.getMessage()) for r in ours]


# ---------------------------------------------------------------------------
# Fail closed on an unexpected error
# ---------------------------------------------------------------------------

async def test_an_exception_inside_verification_fails_closed(app, mint, monkeypatch):
    def _boom(*args, **kwargs):
        raise MemoryError("anything at all")

    monkeypatch.setattr(gw, "verify_gateway_identity", _boom)
    await _assert_refused_like_a_bad_admin_jwt(app, mint())


async def test_a_certs_fetch_failure_fails_closed(app, mint, certs):
    certs.failure = OSError("connection refused")
    await _assert_refused_like_a_bad_admin_jwt(app, mint())


# ---------------------------------------------------------------------------
# The bounded certs transport
# ---------------------------------------------------------------------------

async def test_the_certs_request_forces_its_own_timeout():
    seen = {}

    def _inner(url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        seen.update({"url": url, "timeout": timeout})
        return None

    wrapper = gw._BoundedCertsRequest(_inner, timeout=2.5)
    # The library calls it with NO timeout; a caller that passes 120 must not get 120 either.
    wrapper("https://certs.example")
    assert seen["timeout"] == 2.5
    wrapper("https://certs.example", timeout=120)
    assert seen["timeout"] == 2.5


async def test_the_clock_skew_is_within_the_agreed_ceiling():
    assert 0 < gw.CLOCK_SKEW_SECONDS <= 10
    assert gw.CERTS_TIMEOUT_SECONDS <= 10
    assert gw.MAX_TOKEN_BYTES == 8 * 1024


# ---------------------------------------------------------------------------
# THE CONJUNCTS, MEASURED INDEPENDENTLY OF THE LIBRARY
#
# `verify_oauth2_token` checks `iss` and `aud` itself, which means a test that only mints a
# bad-`iss` token cannot tell whether OUR check or the LIBRARY's refused it — and a mutant that
# deletes ours would survive. `an unreachable guard reads as protection that does not exist`, and
# a double guard is inert. So these drive `verify_gateway_identity` with the library stubbed to
# RETURN whatever claims we name: the only thing left standing between those claims and an
# accepted principal is this module's own conjuncts.
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_verify(monkeypatch):
    from google.oauth2 import id_token as google_id_token

    def _install(claims: Any):
        def _verify(token, request, audience=None, clock_skew_in_seconds=0):
            return claims

        monkeypatch.setattr(google_id_token, "verify_oauth2_token", _verify)

    return _install


def _good_claims(**overrides: Any) -> Dict[str, Any]:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "email": GATEWAY_SA,
        "email_verified": True,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _OMIT}


async def test_conjunct_control_the_stubbed_claims_are_otherwise_accepted(stub_verify, mint):
    # A REAL token shape: the header pre-check and the certs lookup are upstream of the library
    # and must still be satisfied. Only the library's VERDICT is stubbed.
    stub_verify(_good_claims())
    identity = gw.verify_gateway_identity(mint())
    assert identity["email"] == GATEWAY_SA
    assert identity["role"] == "gateway_identity"


@pytest.mark.parametrize(
    "overrides,expected_code",
    [
        ({"iss": "https://evil.example"}, "bad_iss"),
        ({"iss": _OMIT}, "bad_iss"),
        ({"aud": "https://someone-else.example"}, "bad_aud"),
        ({"aud": AUDIENCE + "/"}, "bad_aud"),
        ({"aud": _OMIT}, "bad_aud"),
        ({"email_verified": False}, "email_not_verified"),
        ({"email_verified": "true"}, "email_not_verified"),
        ({"email_verified": 1}, "email_not_verified"),
        ({"email_verified": _OMIT}, "email_not_verified"),
        ({"email": "attacker@gmail.com"}, "email_not_allowlisted"),
        ({"email": ""}, "no_email"),
        ({"email": _OMIT}, "no_email"),
        ({"email": 12345}, "no_email"),
    ],
)
async def test_each_conjunct_refuses_on_its_own(stub_verify, mint, overrides, expected_code):
    stub_verify(_good_claims(**overrides))
    with pytest.raises(gw._Refused) as refusal:
        gw.verify_gateway_identity(mint())
    assert refusal.value.code == expected_code


async def test_a_non_mapping_from_the_library_is_refused(stub_verify, mint):
    stub_verify(["not", "a", "mapping"])
    with pytest.raises(gw._Refused) as refusal:
        gw.verify_gateway_identity(mint())
    assert refusal.value.code == "claims_not_mapping"


async def test_the_library_is_called_with_the_configured_audience_and_the_skew(monkeypatch, mint):
    from google.oauth2 import id_token as google_id_token

    seen = {}

    def _verify(token, request, audience=None, clock_skew_in_seconds=0):
        seen.update({"audience": audience, "skew": clock_skew_in_seconds,
                     "token": token, "request": request})
        return _good_claims()

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", _verify)
    token = mint()
    gw.verify_gateway_identity(token)
    # The AUDIENCE goes to the library too, so the library's own compare is armed as well as ours.
    assert seen["audience"] == AUDIENCE
    assert seen["skew"] == gw.CLOCK_SKEW_SECONDS <= 10
    assert seen["token"] == token
    # And the transport it is handed CANNOT reach the network: it replays the cached document.
    assert isinstance(seen["request"], gw._ReplayCertsRequest)


# ===========================================================================
# REVIEW FINDING P1-1 — OUTBOUND REQUESTS AN ANONYMOUS CALLER CAN CAUSE
#
# google-auth does NOT cache certs. `_fetch_certs` issues an unconditional GET on every
# `verify_oauth2_token`, and it does so BEFORE `jwt.decode` looks at the token. On a
# `--allow-unauthenticated` service that composes into: any stranger makes this backend call
# googleapis, once per request they send, by posting `aaaa.bbbb.cccc`.
#
# These cases MEASURE the fetch count. That is the only honest way to state the property — a
# docstring claiming the library caches is exactly what the first cut had.
# ===========================================================================

async def test_many_valid_reads_cause_exactly_one_certs_fetch(app, mint, certs):
    for _ in range(5):
        assert (await _get(app, "/probe", mint())).status_code == 200
    assert certs.count == 1, f"5 valid reads made {certs.count} outbound fetches"


@pytest.mark.parametrize("junk", [
    "aaaa.bbbb.cccc",              # the measured attack: three base64-ish segments
    "not-a-token",
    "a.b",
    "a.b.c.d",
    "...",
    "x." + "!" * 40 + ".z",        # an undecodable header
])
async def test_an_anonymous_junk_token_causes_ZERO_certs_fetches(app, certs, junk):
    await _assert_refused_like_a_bad_admin_jwt(app, junk)
    assert certs.count == 0, f"{junk!r} made {certs.count} outbound fetches"


async def test_alg_none_and_hs256_cause_zero_certs_fetches(app, public_pem, certs):
    import hashlib
    import hmac

    payload = _b64(json.dumps(_claims()).encode())
    none_header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": KEY_ID}).encode())
    await _assert_refused_like_a_bad_admin_jwt(app, f"{none_header}.{payload}.{_b64(b'')}")

    hs_header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KEY_ID}).encode())
    signature = _b64(hmac.new(public_pem.encode(), f"{hs_header}.{payload}".encode(), hashlib.sha256).digest())
    await _assert_refused_like_a_bad_admin_jwt(app, f"{hs_header}.{payload}.{signature}")

    # The library WOULD have refused both — after paying for a fetch each. The pre-check is a
    # cost control, not a second security check, and this is the cost it controls.
    assert certs.count == 0


async def test_a_token_with_no_kid_causes_zero_certs_fetches(app, private_pem, certs):
    from google.auth import crypt
    from google.auth import jwt as google_jwt

    # Correctly signed, correct alg — but no `kid`, so no certs entry could ever match it.
    # The signer carries no key id, which is what keeps `encode` from adding one.
    signer = crypt.RSASigner.from_string(private_pem, None)
    token = google_jwt.encode(signer, _claims(), header={"alg": "RS256"}).decode()
    assert json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "==")).get("kid") is None
    await _assert_refused_like_a_bad_admin_jwt(app, token)
    assert certs.count == 0


async def test_an_oversized_token_causes_zero_certs_fetches(app, mint, certs):
    await _assert_refused_like_a_bad_admin_jwt(app, mint() + "A" * (gw.MAX_TOKEN_BYTES + 1))
    assert certs.count == 0


async def test_an_unknown_kid_refreshes_ONCE_and_then_refuses_without_fetching(app, private_pem, certs):
    from google.auth import crypt
    from google.auth import jwt as google_jwt

    # Prime the cache with a good read, so the document in hand is FRESH.
    signer = crypt.RSASigner.from_string(private_pem, KEY_ID)
    good = google_jwt.encode(signer, _claims()).decode()
    assert (await _get(app, "/probe", good)).status_code == 200
    assert certs.count == 1

    # `encode` stamps `kid` from the SIGNER after the caller's header, so the kid has to come
    # from a signer built with it — passing it in `header` is silently overwritten.
    stranger_signer = crypt.RSASigner.from_string(private_pem, "rotated-kid")
    stranger = google_jwt.encode(stranger_signer, _claims()).decode()
    await _assert_refused_like_a_bad_admin_jwt(app, stranger)
    assert certs.count == 2, "an unknown kid earns exactly one refresh"

    # And then nothing, however many times it is repeated inside the interval. This is the
    # attacker's cheapest lever on our egress and it is bounded.
    for _ in range(8):
        await _assert_refused_like_a_bad_admin_jwt(app, stranger)
    assert certs.count == 2, f"a repeated unknown kid made {certs.count} fetches"


async def test_a_kid_rotation_after_the_ttl_costs_exactly_two_fetches(app, private_pem, certs, monkeypatch):
    from google.auth import crypt
    from google.auth import jwt as google_jwt

    monkeypatch.setenv(gw.CERTS_TTL_SECONDS_ENV, "60")
    signer = crypt.RSASigner.from_string(private_pem, KEY_ID)
    assert (await _get(app, "/probe", google_jwt.encode(signer, _claims()).decode())).status_code == 200
    assert certs.count == 1

    # Time passes past the TTL, and Google has rotated to a new kid.
    base = time.monotonic()
    monkeypatch.setattr(gw.time, "monotonic", lambda: base + 61.0)
    rotated_signer = crypt.RSASigner.from_string(private_pem, "kid-2")
    certs.certs = {"kid-2": certs.certs[KEY_ID]}
    rotated = google_jwt.encode(rotated_signer, _claims()).decode()

    assert (await _get(app, "/probe", rotated)).status_code == 200
    assert certs.count == 2, "an expired document costs one fetch, not more"
    assert (await _get(app, "/probe", rotated)).status_code == 200
    assert certs.count == 2, "and the refreshed document is then reused"


async def test_the_process_wide_fetch_budget_is_the_last_line(app, private_pem, certs, monkeypatch):
    from google.auth import crypt
    from google.auth import jwt as google_jwt

    # Defeat the TTL and the kid-refresh limiter, so the ONLY thing left is the budget.
    monkeypatch.setattr(gw, "certs_ttl_seconds", lambda: 0.0)
    monkeypatch.setattr(gw, "CERTS_KID_REFRESH_MIN_INTERVAL_SECONDS", 0.0)
    signer = crypt.RSASigner.from_string(private_pem, KEY_ID)
    token = google_jwt.encode(signer, _claims()).decode()

    for _ in range(gw.CERTS_FETCH_MAX_PER_WINDOW + 10):
        await _get(app, "/probe", token)
    assert certs.count <= gw.CERTS_FETCH_MAX_PER_WINDOW, (
        f"{certs.count} fetches exceeded the {gw.CERTS_FETCH_MAX_PER_WINDOW}/window ceiling"
    )


async def test_the_certs_ttl_env_is_clamped_at_both_ends(monkeypatch):
    monkeypatch.delenv(gw.CERTS_TTL_SECONDS_ENV, raising=False)
    assert gw.certs_ttl_seconds() == gw.DEFAULT_CERTS_TTL_SECONDS
    for value, expected in [
        ("0", gw.MIN_CERTS_TTL_SECONDS),        # 0 would restore the unbounded-fetch defect
        ("-99999", gw.MIN_CERTS_TTL_SECONDS),
        ("999999999", gw.MAX_CERTS_TTL_SECONDS),
        ("not-a-number", gw.DEFAULT_CERTS_TTL_SECONDS),
        ("120", 120.0),
    ]:
        monkeypatch.setenv(gw.CERTS_TTL_SECONDS_ENV, value)
        assert gw.certs_ttl_seconds() == expected, value


async def test_the_verifier_is_handed_a_transport_that_cannot_reach_the_network(app, mint, certs):
    # `_ReplayCertsRequest` refusing an uncached URL is what makes "0 fetches inside verify" a
    # structural property rather than a claim about library behaviour we do not control.
    replay = gw._ReplayCertsRequest(gw.GOOGLE_OAUTH2_CERTS_URL, {"k": "v"})
    assert replay(gw.GOOGLE_OAUTH2_CERTS_URL).status == 200
    with pytest.raises(RuntimeError):
        replay("https://somewhere-else.example/certs")


# ===========================================================================
# REVIEW FINDING P1-2 — THE EVENT LOOP MUST NOT STALL
#
# `verify_gateway_identity` is synchronous and does blocking I/O. Called inline from an
# `async def` dependency it runs ON THE LOOP, and a slow googleapis response stalls every other
# request this worker is serving — remotely triggerable on a public route.
# ===========================================================================

async def test_a_slow_certs_fetch_does_not_stall_the_event_loop(app, mint, certs):
    import asyncio as _asyncio

    def _slow(url):
        time.sleep(0.6)          # blocking, exactly like a socket read
        return dict(certs.certs)

    certs.__call__  # noqa: B018 - documents that the seam below replaces this object's behaviour
    original = gw._http_fetch_certs
    gw._http_fetch_certs = _slow
    try:
        beats = 0

        async def heartbeat():
            nonlocal beats
            for _ in range(12):
                await _asyncio.sleep(0.05)
                beats += 1

        pulse = _asyncio.ensure_future(heartbeat())
        response = await _get(app, "/probe", mint())
        await pulse
    finally:
        gw._http_fetch_certs = original

    assert response.status_code == 200
    # Inline, the first cut let 0 of 12 beats run. Off the loop, essentially all of them do.
    assert beats >= 8, f"the loop stalled: only {beats} of 12 heartbeats ran"


async def test_the_whole_verification_is_bounded_and_a_timeout_refuses(app, mint, monkeypatch):
    def _forever(token):
        time.sleep(5.0)
        raise AssertionError("should have been abandoned")

    monkeypatch.setattr(gw, "verify_gateway_identity", _forever)
    monkeypatch.setattr(gw, "VERIFY_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    await _assert_refused_like_a_bad_admin_jwt(app, mint())
    assert time.monotonic() - started < 3.0, "the verification was not bounded"


async def test_the_timeouts_are_inside_the_gateways_own_call_budget():
    # The gateway caps this whole read at 2000 ms (MAX_TIMEOUT_MS there). Anything we spend past
    # that is spent on a client that has already given up and failed open.
    assert gw.VERIFY_TIMEOUT_SECONDS <= 2.0
    # `requests` applies its timeout PER SOCKET OPERATION, so this must leave room for more
    # than one of them inside the ceiling above.
    assert gw.CERTS_TIMEOUT_SECONDS <= 1.5


# ===========================================================================
# REVIEW FINDING P2-1 — THE TWO SIDES MUST NORMALISE THE AUDIENCE IDENTICALLY
#
# The gateway's `cloudRunAudience()` lower-cases the host, drops a default `:443` and folds one
# trailing slash. This side used to only `.strip()`. The same string pasted into both envs
# therefore produced a 401 — and a 401 fails OPEN on the gateway, so the gate disarmed silently.
# ===========================================================================

# (spelling written into BOTH envs, what the gateway puts on the wire / what this side accepts)
AUDIENCE_PARITY = [
    ("https://api.pivota.cc", "https://api.pivota.cc"),
    ("https://api.pivota.cc/", "https://api.pivota.cc"),
    ("https://API.PIVOTA.CC", "https://api.pivota.cc"),
    ("https://api.pivota.cc:443", "https://api.pivota.cc"),
    ("https://api.pivota.cc:443/", "https://api.pivota.cc"),
    ("  https://api.pivota.cc  ", "https://api.pivota.cc"),
]

AUDIENCE_REFUSED = [
    "http://api.pivota.cc",          # not https
    "api.pivota.cc",                 # not a URL
    "foo",                           # the value the first cut accepted verbatim
    "https://api.pivota.cc/ops",     # a path
    "https://api.pivota.cc:8443",    # a non-default port
    "https://api.pivota.cc?x=1",
    "https://api.pivota.cc#f",
    "https://user:pw@api.pivota.cc",
    "https://",
    "https://api.pivota.cc:notaport",
]


@pytest.mark.parametrize("spelling,expected", AUDIENCE_PARITY)
async def test_the_audience_normalises_the_way_the_gateway_normalises_it(spelling, expected, monkeypatch):
    assert gw.normalize_audience(spelling) == expected
    monkeypatch.setenv(gw.AUDIENCE_ENV, spelling)
    assert gw.configured_audience() == expected


@pytest.mark.parametrize("spelling", AUDIENCE_REFUSED)
async def test_an_audience_that_is_not_a_bare_https_origin_DISABLES_the_path(spelling, monkeypatch):
    assert gw.normalize_audience(spelling) is None, spelling
    monkeypatch.setenv(gw.AUDIENCE_ENV, spelling)
    # Disabled, not "passed through": a value that is not an origin cannot be what the gateway
    # asked the metadata server for, so accepting it would arm a door that can never open.
    assert gw.configured_audience() == ""
    assert gw.gateway_identity_enabled() is False


async def test_a_refused_audience_env_is_logged_once_with_its_value(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="utils.gateway_oidc_auth")
    monkeypatch.setenv(gw.AUDIENCE_ENV, "api.pivota.cc")
    for _ in range(4):
        gw.configured_audience()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    # The VALUE is operator-supplied configuration, not caller input; naming it is what makes the
    # line actionable, and not naming it is what made this failure mode invisible.
    assert "api.pivota.cc" in warnings[0].getMessage()
    assert "audience_env_invalid" in warnings[0].getMessage()


async def test_a_token_minted_for_the_unnormalised_spelling_is_still_accepted(app, private_pem, monkeypatch):
    from google.auth import crypt
    from google.auth import jwt as google_jwt

    # The operator wrote the trailing-slash spelling on BOTH sides. The gateway normalises it
    # before asking the metadata server, so the token's `aud` is the origin — and this side must
    # now agree, which is the entire point of the finding.
    monkeypatch.setenv(gw.AUDIENCE_ENV, "https://api.pivota.cc/")
    signer = crypt.RSASigner.from_string(private_pem, KEY_ID)
    token = google_jwt.encode(signer, _claims(aud="https://api.pivota.cc")).decode()
    assert (await _get(app, "/probe", token)).status_code == 200
