"""routes/reap_return.py — the page Reap's hosted checkout sends the buyer's browser back to.

Two apps, on purpose:

  ROUTER APP  a bare FastAPI app with only this router on it. What the HANDLER does, with no
              middleware in the way that could add (or mask) a header.
  REAL APP    `main.app`, the object production serves. That the page is MOUNTED, that no
              app-wide middleware or auth layer turns it into something else, and that the
              headers survive SecurityHeadersMiddleware (which only sets what is absent).

SQLite / no database at all: the handler reads and writes nothing, and `_no_database` below is the
control that proves it rather than a docstring asserting it. NEVER `TestClient` (house rule);
`httpx.ASGITransport`.

MUTANTS, each applied alone and confirmed to fail at least one test here (table in the PR body):
reflect the query into the body, drop `no-store`, drop the CSP, mount under an auth dependency,
revert the order of `DEFAULT_RETURN_URL_HOSTS`.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import routes.reap_return as reap_return  # noqa: E402
import services.reap_agentic_client as rc  # noqa: E402
from routes.agent_commerce_reap import _default_return_url  # noqa: E402

#: Module level, like tests/test_agent_commerce_reap_routes.py: the mounting test and the
#: request tests exercise the same app object production builds.
from main import app as real_app  # noqa: E402

PATH = "/reap/return"

EXPECTED_HEADERS = {
    "cache-control": "no-store",
    "x-robots-tag": "noindex, nofollow",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}

#: Written out, not imported from the module: a test that took its expectation from the code
#: under test could not notice the sentence being changed in both.
SENTENCES = (
    "Payment approved",
    "Your approval was received. You can close this window: your assistant will confirm "
    "the order once the merchant accepts it.",
    "If you did not approve anything, you can ignore this page.",
)

SCRIPT_PROBE = "<script>alert(1)</script>"
PLAIN_PROBE = "PROBE_9f3a"
PATH_PROBE = "PROBE_PATH_7c1e"


def _router_app() -> FastAPI:
    app = FastAPI()
    app.include_router(reap_return.router)
    return app


async def _request(app, method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://api.pivota.cc") as http:
        return await http.request(method, url, **kwargs)


@pytest.fixture(autouse=True)
def _no_database(monkeypatch):
    """The page writes nothing and reads nothing. Every entry point on the shared `databases`
    handle raises, so a handler that grew a query fails here instead of passing quietly."""
    from db.database import database

    def _forbidden(*_a, **_k):
        raise AssertionError("the /reap/return page touched the database")

    for name in ("execute", "execute_many", "fetch_all", "fetch_one", "fetch_val",
                 "transaction", "connection"):
        monkeypatch.setattr(database, name, _forbidden, raising=False)


@pytest.fixture(params=["router_app", "real_app"])
def app(request):
    return _router_app() if request.param == "router_app" else real_app


def _assert_css_only_csp(value: str) -> None:
    directives = {d.strip() for d in value.split(";") if d.strip()}
    assert "default-src 'none'" in directives, value
    assert "style-src 'unsafe-inline'" in directives, value
    # Nothing may re-open script, images, fonts, connections or forms.
    assert not any(d.startswith(("script-src", "img-src", "connect-src", "font-src",
                                 "form-action", "frame-src")) for d in directives), value


# ── the page ─────────────────────────────────────────────────────────────────────────────────


async def test_get_answers_200_html_with_every_header(app):
    resp = await _request(app, "GET", PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    for name, value in EXPECTED_HEADERS.items():
        assert resp.headers.get(name) == value, (name, resp.headers.get(name))
    _assert_css_only_csp(resp.headers.get("content-security-policy", ""))


async def test_head_answers_200_with_the_same_headers(app):
    resp = await _request(app, "HEAD", PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert resp.headers.get("cache-control") == "no-store"
    _assert_css_only_csp(resp.headers.get("content-security-policy", ""))


async def test_the_body_says_the_three_things_and_nothing_active(app):
    resp = await _request(app, "GET", PATH)
    body = resp.text
    for sentence in SENTENCES:
        assert sentence in body, sentence
    assert "<title>Payment approved</title>" in body
    lowered = body.lower()
    # No script, no external asset, no form, no link anywhere (not to Reap, not to the merchant).
    for forbidden in ("<script", "<form", "<a ", "<a>", "<link", "<img", "<iframe",
                      "src=", "href=", "http://", "https://", "reap", "prava"):
        assert forbidden not in lowered, forbidden


@pytest.mark.parametrize("query", [
    {"click_id": SCRIPT_PROBE},
    {"x": PLAIN_PROBE},
    {"stage": "checkout", "click_id": PLAIN_PROBE},
    {"stage": PLAIN_PROBE},
])
async def test_no_query_parameter_is_reflected(app, query):
    resp = await _request(app, "GET", PATH, params=query)
    assert resp.status_code == 200
    body = resp.text
    assert SCRIPT_PROBE not in body
    assert "alert(1)" not in body
    assert PLAIN_PROBE not in body
    # And the page is byte-identical to the bare one: nothing about the request shaped it.
    bare = await _request(app, "GET", PATH)
    assert resp.content == bare.content


async def test_no_header_is_reflected(app):
    resp = await _request(app, "GET", PATH, headers={
        "Referer": f"https://evil.example/{PLAIN_PROBE}",
        "User-Agent": PLAIN_PROBE,
        "X-Forwarded-Host": f"{PLAIN_PROBE}.example",
        "Accept-Language": PLAIN_PROBE,
    })
    assert resp.status_code == 200
    assert PLAIN_PROBE not in resp.text


async def test_a_path_suffix_is_not_this_page_and_is_not_reflected():
    """`/reap/return/<anything>` is not the page (404 from the router), and whatever answers it
    does not echo the suffix. Router app only: the REAL app's 404 is the app-wide error
    envelope, which is not this module's to change."""
    resp = await _request(_router_app(), "GET", f"{PATH}/{PATH_PROBE}")
    assert resp.status_code == 404
    assert PATH_PROBE not in resp.text


async def test_no_cookie_is_set(app):
    resp = await _request(app, "GET", PATH, params={"click_id": "clk_1"})
    assert "set-cookie" not in {k.lower() for k in resp.headers.keys()}


async def test_other_methods_are_not_served(app):
    resp = await _request(app, "POST", PATH, content=b"x")
    assert resp.status_code == 405


# ── no auth ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer garbage.not.a.jwt"},
    {"Authorization": "Basic Zm9vOmJhcg=="},
    {"X-API-Key": "ak_garbage"},
])
async def test_the_page_needs_no_credential_and_ignores_a_bad_one(app, headers):
    """A buyer's browser arriving from Reap carries no credential of ours. An auth dependency on
    the router, or an app-level layer that 401s unknown callers, would put an error page back
    exactly where this page replaced one."""
    resp = await _request(app, "GET", PATH, headers=headers)
    assert resp.status_code == 200
    assert SENTENCES[0] in resp.text


# ── mounted on the real app ──────────────────────────────────────────────────────────────────


def test_the_router_is_mounted_on_the_real_app():
    """A merged route is not a running route: the path and BOTH verbs on the app `main` builds."""
    methods = set()
    for route in real_app.routes:
        if getattr(route, "path", None) == PATH:
            methods.update(getattr(route, "methods", set()) or set())
    assert {"GET", "HEAD"} <= methods, methods


# ── no logging of its own ────────────────────────────────────────────────────────────────────


async def test_the_handler_logs_nothing_about_the_click_id(caplog):
    """The click id is an attribution key. The HANDLER logs nothing (router app: no middleware
    in the loop, so any record here would be ours). The app-wide channels are documented in
    docs/runbooks/reap_agentic_purchase.md, not changed here."""
    caplog.set_level(logging.DEBUG)
    resp = await _request(_router_app(), "GET", PATH, params={"click_id": "clk_" + PLAIN_PROBE})
    assert resp.status_code == 200
    # `httpx` is the TEST CLIENT logging its own request line, not the app.
    leaked = [r for r in caplog.records
              if PLAIN_PROBE in r.getMessage() and not r.name.startswith("httpx")]
    assert not leaked, [(r.name, r.getMessage()) for r in leaked]


# ── the default return URL points here ───────────────────────────────────────────────────────


def test_the_rails_default_return_url_is_this_page(monkeypatch):
    """With neither env var set the rail sends buyers to THIS route on the API host. Composes
    `_default_return_url` with the client allowlist, so reverting the host order fails here."""
    monkeypatch.delenv("REAP_AGENTIC_RETURN_URL", raising=False)
    monkeypatch.delenv("REAP_RETURN_URL_HOSTS", raising=False)
    default = _default_return_url()
    assert default == "https://api.pivota.cc/reap/return"
    assert rc.validate_return_url(default) == default
    assert default.endswith(reap_return.RETURN_PATH)
