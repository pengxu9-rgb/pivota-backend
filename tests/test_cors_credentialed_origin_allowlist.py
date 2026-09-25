"""CORS: browser credentials only for first-party origins; `*` and no credentials for the rest.

THE FINDING (adversarial review of #2346, 2026-09-25, confirmed on `main.app`): main.py reflected
ANY `Origin` and added `Access-Control-Allow-Credentials: true`. That pair is what lets a hostile
page read a cookie-authenticated response (`/accounts/*` authenticates from `acc_access_token`,
SameSite=None). The ALLOWED_ORIGINS env var production sets was parsed and ignored.

Every request test here runs against `main.app`, the object production serves, over
`httpx.ASGITransport` (house rule: never TestClient). The middleware is the outermost layer, so
what these tests see is what a browser sees, including on error responses.

MUTANTS, each applied alone, each confirmed to fail at least one test here (table in the PR body):
  M1  trusted regex back to ".*"                    (reflect-all returns)
  M2  public tier allow_credentials=True            (credentials for everyone)
  M3  preflight passthrough reflects Origin + credentials again
  M4  resolve_trusted_origins ignores `configured`   (env allowlist dropped)
  M5  trusted tier allow_credentials=False          (first-party cookie flows break)
  M6  TieredCORSMiddleware registered somewhere other than outermost
  M7  the Vary: Origin wrapper removed                (a shared cache could serve `*` to a portal)
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from middleware.cors import (  # noqa: E402
    DEV_ORIGIN_REGEX,
    FIRST_PARTY_ORIGINS,
    TieredCORSMiddleware,
    parse_origins,
    resolve_trusted_origins,
)
import main as main_module  # noqa: E402
from main import app  # noqa: E402

ACAO = "access-control-allow-origin"
ACAC = "access-control-allow-credentials"

TRUSTED = "https://agent.pivota.cc"
# Origins a lookalike, a scheme downgrade, a port and a suffix-trick would produce. None is first
# party; each is one character or one label away from one.
UNTRUSTED = (
    "https://evil.example",
    "https://agent.pivota.cc.evil.example",
    "https://xagent.pivota.cc",
    "https://agent-pivota.cc",
    "http://agent.pivota.cc",
    "https://agent.pivota.cc:8443",
    "https://pivota-evil.vercel.app",
    "null",
)

# Paths chosen for what answers them, not for what they do: one that exists and needs no auth,
# one that authenticates from the cookie (the finding's target), one that does not exist (the
# app-wide 404 envelope - CORS is outermost, so error responses are decorated too).
PATHS = ("/health", "/accounts/orders/list", "/no/such/path/PROBE")


async def _request(method: str, path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kw)


# ------------------------------------------------------------------ the resolution, no app


def test_parse_origins_normalises_and_drops_what_cannot_match():
    got = parse_origins(" https://A.Example/ ,, https://b.example:8443 , junk, *, https://a.example ")
    assert got == ["https://a.example", "https://b.example:8443"]


def test_parse_origins_accepts_a_list_and_none():
    assert parse_origins(None) == []
    assert parse_origins(["https://a.example", "https://a.example"]) == ["https://a.example"]


def test_resolve_unions_env_with_first_party_and_sets_no_regex_in_production():
    origins, regex = resolve_trusted_origins(
        "https://staging-portal.example,https://agent.pivota.cc", dev_mode=False
    )
    assert regex is None
    assert origins[: len(FIRST_PARTY_ORIGINS)] == list(FIRST_PARTY_ORIGINS)
    assert origins.count("https://agent.pivota.cc") == 1
    assert origins[-1] == "https://staging-portal.example"


def test_resolve_with_nothing_configured_is_first_party_only():
    assert resolve_trusted_origins(None, dev_mode=False) == (list(FIRST_PARTY_ORIGINS), None)
    assert resolve_trusted_origins("", dev_mode=False) == (list(FIRST_PARTY_ORIGINS), None)


def test_a_star_in_the_env_does_not_widen_the_trusted_tier():
    origins, regex = resolve_trusted_origins("*", dev_mode=False)
    assert "*" not in origins and regex is None


def test_dev_mode_adds_only_loopback_origins():
    import re

    _, regex = resolve_trusted_origins(None, dev_mode=True)
    assert regex == DEV_ORIGIN_REGEX
    pat = re.compile(regex)
    for ok in ("http://localhost:3000", "http://127.0.0.1:8080", "https://localhost", "http://[::1]:3000"):
        assert pat.fullmatch(ok), ok
    for bad in ("http://localhost.evil.example", "https://evil.example", "http://localhost:3000/x",
                "ftp://localhost", "http://[::1]:3000.evil.example", "http://localhost:3000\n"):
        assert not pat.fullmatch(bad), bad


def test_parse_origins_accepts_an_ipv6_literal_and_rejects_paths_and_userinfo():
    assert parse_origins("http://[::1]:3000, http://a.example/x, http://user@a.example") == ["http://[::1]:3000"]


def test_no_first_party_origin_is_a_platform_provider_hostname():
    """A `*.vercel.app` (or any PaaS) name is first-come: delete the project and anyone can
    re-register it and inherit a credentialed origin. Those belong in ALLOWED_ORIGINS, which a
    deploy can drop the day the alias dies. tests/test_gateway_hostname_is_pivota_owned.py is
    the repo-wide gate; this is the same rule stated where the list lives."""
    for origin in FIRST_PARTY_ORIGINS:
        host = origin.split("://", 1)[1]
        assert host.endswith((".pivota.cc", ".pivota-ai.com", ".woopay.tech")) or host in (
            "pivota.cc", "pivota-ai.com", "woopay.tech"), origin


def test_main_builds_its_trusted_tier_from_the_settings_allowlist():
    """main.py does not hand-roll the list: it is exactly what the resolver returns for the
    settings in force at import (`ALLOWED_ORIGINS` via settings.cors_origins, DEV_MODE via
    settings.dev_mode). With M4 (resolver ignores the env) this stays green only if the env was
    empty, so the env-honouring test is the one just below."""
    from config.settings import settings

    expected = resolve_trusted_origins(settings.cors_origins, dev_mode=settings.dev_mode)
    assert (main_module.trusted_cors_origins, main_module.trusted_cors_origin_regex) == expected


def test_the_env_allowlist_reaches_the_trusted_tier(monkeypatch):
    from config.settings import settings

    monkeypatch.setenv("ALLOWED_ORIGINS", "https://staging-portal.example, https://Preview.Example/")
    origins, _ = resolve_trusted_origins(settings.cors_origins, dev_mode=False)
    assert "https://staging-portal.example" in origins
    assert "https://preview.example" in origins


# ------------------------------------------------------------------ the middleware, by construction


def _noop_app():
    async def inner(scope, receive, send):  # pragma: no cover - never reached in these tests
        raise AssertionError("inner app should not run")

    return inner


def test_the_public_tier_can_never_carry_credentials():
    mw = TieredCORSMiddleware(
        _noop_app(), trusted_origins=[TRUSTED], allow_methods=["GET"], allow_headers=["X-API-Key"]
    )
    assert "Access-Control-Allow-Credentials" not in mw.public.simple_headers
    assert "Access-Control-Allow-Credentials" not in mw.public.preflight_headers
    assert mw.public.simple_headers["Access-Control-Allow-Origin"] == "*"
    assert mw.trusted.simple_headers.get("Access-Control-Allow-Credentials") == "true"
    assert mw.is_trusted_origin(TRUSTED) and not mw.is_trusted_origin("https://evil.example")
    assert not mw.is_trusted_origin(None) and not mw.is_trusted_origin("")


def test_the_tiered_middleware_is_the_outermost_layer_of_the_real_app():
    """add_middleware prepends, so index 0 is the outermost. Anything registered after it would
    wrap it and could answer (an error, a 404) without CORS headers."""
    assert app.user_middleware[0].cls is TieredCORSMiddleware


# ------------------------------------------------------------------ the real app, by request


@pytest.mark.parametrize("path", PATHS)
async def test_a_first_party_origin_gets_itself_reflected_with_credentials(path):
    resp = await _request("GET", path, headers={"Origin": TRUSTED})
    assert resp.headers.get(ACAO) == TRUSTED, (path, resp.status_code, dict(resp.headers))
    assert resp.headers.get(ACAC) == "true", (path, resp.status_code)
    assert "origin" in resp.headers.get("vary", "").lower()


@pytest.mark.parametrize("origin", FIRST_PARTY_ORIGINS)
async def test_every_first_party_origin_is_in_the_trusted_tier(origin):
    resp = await _request("GET", "/health", headers={"Origin": origin})
    assert resp.headers.get(ACAO) == origin
    assert resp.headers.get(ACAC) == "true"


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("origin", UNTRUSTED)
async def test_any_other_origin_gets_star_and_no_credentials(path, origin):
    resp = await _request("GET", path, headers={"Origin": origin})
    assert resp.headers.get(ACAO) == "*", (origin, path, resp.status_code, dict(resp.headers))
    assert ACAC not in resp.headers, (origin, path)
    # The answer varies by Origin, so a cache must key on it: `*` served to a portal page would
    # break that page's credentialed read. Starlette only adds Vary on the reflected path.
    assert "origin" in resp.headers.get("vary", "").lower(), (origin, path, resp.headers.get("vary"))


@pytest.mark.parametrize("origin", UNTRUSTED)
async def test_an_untrusted_origin_sending_the_auth_cookie_is_never_granted_credentials(origin):
    """The finding's exact shape: the cookie is on the request, the page is not ours. Starlette
    reflects the origin instead of `*` when a Cookie header is present (its own rule); the
    browser's check is the credentials header, and that must be absent."""
    resp = await _request(
        "GET", "/accounts/orders/list",
        headers={"Origin": origin, "Cookie": "acc_access_token=not-a-real-token"},
    )
    assert ACAC not in resp.headers, (origin, resp.status_code, dict(resp.headers))
    # Starlette's own rule: with a Cookie on the request the `*` tier reflects the origin instead
    # of `*`. That is fine ONLY because the line above holds; stated so a reader does not take
    # the reflection for the finding coming back.
    assert resp.headers.get(ACAO) in ("*", origin)


async def test_a_first_party_origin_sending_the_auth_cookie_keeps_credentials():
    resp = await _request(
        "GET", "/accounts/orders/list",
        headers={"Origin": TRUSTED, "Cookie": "acc_access_token=not-a-real-token"},
    )
    assert resp.headers.get(ACAO) == TRUSTED
    assert resp.headers.get(ACAC) == "true"


async def test_no_origin_means_no_cors_headers_at_all():
    resp = await _request("GET", "/health")
    assert ACAO not in resp.headers and ACAC not in resp.headers


# ------------------------------------------------------------------ preflight, both shapes


@pytest.mark.parametrize("path", PATHS)
async def test_preflight_from_a_first_party_origin(path):
    resp = await _request(
        "OPTIONS", path,
        headers={
            "Origin": TRUSTED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, authorization, x-api-key",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get(ACAO) == TRUSTED
    assert resp.headers.get(ACAC) == "true"
    allowed = resp.headers.get("access-control-allow-headers", "").lower()
    for h in ("authorization", "x-api-key", "content-type"):
        assert h in allowed


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("origin", UNTRUSTED)
async def test_preflight_from_any_other_origin_is_star_without_credentials(path, origin):
    """Not a 400: an API-key page on an origin we cannot enumerate (a merchant storefront, a
    Vercel preview, an MCP inspector) still gets through. It just never gets credentials."""
    resp = await _request(
        "OPTIONS", path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, authorization, x-api-key",
        },
    )
    assert resp.status_code == 200, (origin, path, resp.text)
    assert resp.headers.get(ACAO) == "*"
    assert ACAC not in resp.headers
    assert "origin" in resp.headers.get("vary", "").lower()
    allowed = resp.headers.get("access-control-allow-headers", "").lower()
    assert "x-api-key" in allowed and "authorization" in allowed


@pytest.mark.parametrize("origin", UNTRUSTED)
async def test_a_bare_options_without_a_request_method_is_decorated_by_tier_not_by_hand(origin):
    """OPTIONS with an Origin but no Access-Control-Request-Method is not a preflight; the
    cors_preflight_passthrough middleware answers it. It used to write Allow-Origin=<origin>
    and Allow-Credentials=true itself - the finding on a second path."""
    resp = await _request("OPTIONS", "/accounts/orders/list", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get(ACAO) == "*"
    assert ACAC not in resp.headers
    assert "x-api-key" in resp.headers.get("access-control-allow-headers", "").lower()


async def test_a_bare_options_from_a_first_party_origin_still_gets_credentials():
    resp = await _request("OPTIONS", "/accounts/orders/list", headers={"Origin": TRUSTED})
    assert resp.status_code == 200
    assert resp.headers.get(ACAO) == TRUSTED
    assert resp.headers.get(ACAC) == "true"


async def test_a_bare_options_without_an_origin_carries_no_allow_origin():
    resp = await _request("OPTIONS", "/accounts/orders/list")
    assert resp.status_code == 200
    assert ACAO not in resp.headers and ACAC not in resp.headers
