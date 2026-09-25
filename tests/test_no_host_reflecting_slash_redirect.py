"""No trailing-slash redirect: it built its Location from the request's Host header.

THE FINDING (adversarial review of #2346, 2026-09-25, confirmed on `main.app` and then on
production): Starlette's `redirect_slashes` answers `GET /reap/return/` with
`307 Location: https://<Host header>/reap/return`. The Host is whatever the client sent - the
production edge forwards it unchanged (probed 2026-09-25: SNI api.pivota.cc, `Host: evil.example`
-> `location: https://evil.example/reap/return` from revision web-01161-vac), because the
`pivota-urlmap` default backend is the web service, so any Host that resolves to the LB reaches it.
`X-Forwarded-Host` is not consulted; the Host header is the one that lands in the redirect.
A second site of the same defect: routes/mcp_oauth_as.py built the login page's `next` from
`str(request.url)` (flag-off today; fixed and pinned here too).

THE FIX: `redirect_slashes=False` on the app. Nothing depended on the redirect. Measured over
30 days of production request logs (2026-08-26..09-25): the only 307s from the web service were
the anonymous `/openapi.json` alias (a path-only Location built from `url_path_for`, not this
mechanism) and the reviewer's own curl probes of `/reap/return/` and `/merchants`. No portal
calls a bare `/merchants`, `/protocols` or `/queue`.

Every request test runs against `main.app` over `httpx.ASGITransport` (house rule: never
TestClient), so the answer includes the app-wide 404 envelope, not a bare router's.

MUTANTS, each alone, each confirmed to fail at least one test here: `redirect_slashes=True` (or
the kwarg removed - Starlette defaults it on); a `/reap/return/` alias route added.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from main import app  # noqa: E402

HOSTILE_HOSTS = ("evil.example", "evil.example:8443", "api.pivota.cc.evil.example")

#: (request path, the path a slash-redirect would have sent the browser to). Each first element
#: is a real route on `main.app` with its slash toggled: the reap page is `/reap/return`;
#: `/merchants/` and `/protocols/` are routers whose list route is `"/"` under a prefix;
#: `/agents/{agent_id}/protocols/` (GET and POST) is the one a sibling repo once called in the
#: bare form. Not `/agents`: both `/agents/` (agent_management) and `/agents` (agent_metrics) are
#: routes, so nothing there ever redirected. Not `/queue`: that router is never mounted.
TOGGLED = (
    ("/reap/return/", "/reap/return"),
    ("/merchants", "/merchants/"),
    ("/protocols", "/protocols/"),
    ("/agents/agent_x/protocols", "/agents/agent_x/protocols/"),
)


async def _get(path: str, host: str, method: str = "GET") -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(method, path, headers={"Host": host})


def test_every_toggled_path_is_the_slash_twin_of_a_real_route():
    """The parametrisation is only a test while each row names a route that exists: a row for a
    router nobody mounts passes under every mutant. Pinned against the app's own route table."""
    paths = {getattr(r, "path", None) for r in app.router.routes}
    for _, real in TOGGLED:
        assert real in paths or "/agents/{agent_id}/protocols/" == real.replace("agent_x", "{agent_id}"), real
    assert "/agents/{agent_id}/protocols/" in paths


def test_the_app_does_not_redirect_slashes():
    """The knob itself, so a revert reads as a failing test and not as a header assertion that
    happened to pass because a route moved."""
    assert app.router.redirect_slashes is False


@pytest.mark.parametrize("host", HOSTILE_HOSTS)
@pytest.mark.parametrize("path,would_redirect_to", TOGGLED)
async def test_a_toggled_slash_path_never_redirects_to_the_request_host(path, would_redirect_to, host):
    resp = await _get(path, host)
    assert resp.status_code != 307 and resp.status_code != 308, (path, host, resp.headers)
    assert "location" not in resp.headers, (path, host, resp.headers.get("location"))
    assert host.split(":")[0] not in resp.text, (path, host)


@pytest.mark.parametrize("host", HOSTILE_HOSTS)
async def test_a_bare_form_post_never_redirects_either(host):
    """A 307 preserves the method and the body, so a bare-form POST was the stronger pre-fix
    hazard: the browser would re-send the payload to the attacker's host."""
    resp = await _get("/agents/agent_x/protocols", host, method="POST")
    assert resp.status_code not in (307, 308)
    assert "location" not in resp.headers


@pytest.mark.parametrize("path,_", TOGGLED)
async def test_a_toggled_slash_path_is_a_404_not_a_redirect_for_the_real_host(path, _):
    """With the redirect gone, the toggled path is simply not a route: 404 from the app-wide
    envelope. Not 405 (no phantom method match), not 3xx."""
    resp = await _get(path, "api.pivota.cc")
    assert resp.status_code == 404, (path, resp.status_code, resp.text[:200])
    assert "location" not in resp.headers


async def test_the_real_path_still_answers_under_a_hostile_host():
    """Positive control: disabling the redirect did not touch routing. The reap page is served
    whatever the Host says, and it never echoes the Host either (that page's own tests cover the
    body; this pins that the app-level change did not regress it)."""
    resp = await _get("/reap/return", "evil.example")
    assert resp.status_code == 200
    assert "evil.example" not in resp.text



# ------------------------------------------------------------------ the second site


def _request_with_host(host: str, path: str, query: str) -> Request:
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": path, "root_path": "",
        "query_string": query.encode(), "server": ("testserver", 443),
        "headers": [(b"host", host.encode())],
    }
    return Request(scope)


def test_the_mcp_login_next_url_is_on_the_configured_host_not_the_request_host(monkeypatch):
    from routes.mcp_oauth_as import _login_next_url, _login_redirect

    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.pivota.cc")
    monkeypatch.setenv("MCP_OAUTH_AS_LOGIN_URL", "https://agent.pivota.cc/login")
    req = _request_with_host("evil.example", "/oauth/authorize", "client_id=c&state=s")
    assert _login_next_url(req) == "https://api.pivota.cc/oauth/authorize?client_id=c&state=s"
    location = _login_redirect(req).headers["location"]
    assert location.startswith("https://agent.pivota.cc/login?next=")
    assert "evil.example" not in location
    assert "api.pivota.cc%2Foauth%2Fauthorize" in location


def test_the_mcp_login_next_url_keeps_path_only_when_there_is_no_query(monkeypatch):
    from routes.mcp_oauth_as import _login_next_url

    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.pivota.cc/")
    req = _request_with_host("evil.example:8443", "/oauth/authorize", "")
    assert _login_next_url(req) == "https://api.pivota.cc/oauth/authorize"
