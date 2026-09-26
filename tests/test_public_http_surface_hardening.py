"""The public HTTP surface: no anonymous spec in production, security headers everywhere.

Both findings were MEASURED against the live hosts on 2026-08-22, not inferred:

  curl https://api.pivota.cc/openapi.json  -> 200, 1019 paths
  curl -I https://api.pivota.cc/health     -> no HSTS, no nosniff, no X-Frame-Options,
                                              no CSP, no Referrer-Policy

The spec is the full internal route list. Anonymously readable it is a map of every path worth
probing, which is worth more to an attacker than any single path on it. The curated partner-facing
spec is a different surface and stays public at /agent/docs/openapi.json — these tests assert that
distinction holds, because "we closed the docs" would be wrong if it took the partner one with it.
Anonymous GET /openapi.json redirects to the curated spec rather than 404ing: it is the URL the
marketing site publishes, and the dead end read as "no public spec" (measured 2026-08-26 via a
Claude session doing cold discovery). The full spec still requires the admin key.

Everything here drives the REAL app through TestClient. Asserting against a re-implementation of
the guard would prove only that the test agrees with itself.
"""
from __future__ import annotations



import pytest
from fastapi.testclient import TestClient

DOC_PATHS = ("/openapi.json", "/docs", "/redoc")


@pytest.fixture
def client_factory(monkeypatch):
    """Build a TestClient for a chosen environment.

    main.py bound `is_production` BY NAME at import, and that function reads os.environ live — so
    monkeypatch.setenv is what actually switches environments here. An importlib.reload of
    config.platform used to sit in this fixture and did nothing at all; it was removed rather than
    left in place reading as load-bearing.
    """
    import main

    def _build(*, production: bool, admin_key: str | None = "test-admin-key",
               raise_server_exceptions: bool = True):
        monkeypatch.setenv("PIVOTA_ENV", "production" if production else "staging")
        if admin_key is None:
            monkeypatch.delenv("ADMIN_API_KEY", raising=False)
            monkeypatch.delenv("PROMOTIONS_ADMIN_KEY", raising=False)
        else:
            monkeypatch.setenv("ADMIN_API_KEY", admin_key)
        return TestClient(main.app, raise_server_exceptions=raise_server_exceptions)

    return _build


@pytest.mark.parametrize("path", ("/docs", "/redoc"))
def test_production_serves_no_anonymous_doc_page(client_factory, path):
    with client_factory(production=True) as client:
        resp = client.get(path)
    # 404 rather than 401: a 401 confirms the endpoint exists and is merely guarded, which re-leaks
    # the fact worth hiding.
    assert resp.status_code == 404, (
        f"{path} is reachable anonymously in production (status {resp.status_code}). "
        "This is the full internal route list; serving it hands an attacker the path map."
    )


AGENT_SPEC_PREFIXES = ("/agent/v1", "/agent/v2", "/agent/shop/v1")


def test_anonymous_openapi_redirects_to_the_curated_public_spec(client_factory):
    """/openapi.json is the URL the marketing site publishes and the path agents probe first.

    Anonymously it must NOT serve the full internal spec — but a 404 read as "no public spec"
    while /agent/docs/openapi.json was public all along. The alias redirects.
    """
    with client_factory(production=True) as client:
        resp = client.get("/openapi.json", follow_redirects=False)
        # 307, not 308: clients cache a permanent redirect indefinitely, freezing the alias
        # target (and this URL's keyed behavior) into every cache that saw an anonymous response.
        assert resp.status_code == 307, f"expected a temporary redirect, got {resp.status_code}"
        assert resp.headers["location"] == "/agent/docs/openapi.json"
        # The response varies on X-ADMIN-KEY; a shared cache must never store either branch.
        assert resp.headers.get("cache-control") == "no-store"
        followed = client.get("/openapi.json", follow_redirects=True)
    assert followed.status_code == 200
    spec = followed.json()
    # The CURATED spec, not the internal one: every documented path must be agent-facing. A spec
    # with even one non-agent path means the redirect (or the target) leaked the internal map.
    paths = list(spec.get("paths", {}))
    assert paths, "the curated spec came back empty"
    leaked = [p for p in paths if not p.startswith(AGENT_SPEC_PREFIXES)]
    assert not leaked, f"anonymous /openapi.json exposed non-agent paths: {leaked[:5]}"


def test_a_wrong_admin_key_is_indistinguishable_from_no_key(client_factory):
    """Any difference between missing/empty/wrong key is an oracle for whether a key is mounted.

    Headers are compared as a full set (not just Location): a debug or rate-limit header emitted
    on only one branch would re-open the probe while status/Location/body all still match. The
    same comparison runs across CONFIGURATIONS — a revision with a mounted key must answer
    anonymous callers identically to a revision without one.
    """
    def _stable_headers(resp):
        # date and x-request-id vary per REQUEST, not per branch — everything else must match.
        return {k: v for k, v in resp.headers.items() if k.lower() not in ("date", "x-request-id")}

    with client_factory(production=True) as client:
        anon = client.get("/openapi.json", follow_redirects=False)
        wrong = client.get(
            "/openapi.json", headers={"X-ADMIN-KEY": "not-the-key"}, follow_redirects=False
        )
        empty = client.get("/openapi.json", headers={"X-ADMIN-KEY": ""}, follow_redirects=False)
    # Requests above completed before this build mutates the env for the no-key configuration.
    with client_factory(production=True, admin_key=None) as client:
        unconfigured = client.get("/openapi.json", follow_redirects=False)
    for resp in (wrong, empty, unconfigured):
        assert resp.status_code == anon.status_code == 307
        assert resp.headers["location"] == anon.headers["location"]
        assert resp.content == anon.content
        assert _stable_headers(resp) == _stable_headers(anon)


def test_production_serves_the_spec_to_an_admin(client_factory):
    """The guard must not simply delete the surface — ops scripts read the spec in production."""
    with client_factory(production=True) as client:
        resp = client.get("/openapi.json", headers={"X-ADMIN-KEY": "test-admin-key"})
    assert resp.status_code == 200, f"/openapi.json refused a valid admin key ({resp.status_code})"
    # The keyed 200 must never be stored by a shared cache — it would replay the internal map
    # to the next anonymous caller.
    assert resp.headers.get("cache-control") == "no-store"
    paths = resp.json().get("paths")
    assert paths, "the spec came back empty — app.openapi() is not producing one"
    # The FULL spec, not the curated agent one — a redirect that also caught keyed callers would
    # hand ops scripts the wrong document with a 200.
    assert any(not p.startswith(AGENT_SPEC_PREFIXES) for p in paths), (
        "an admin key returned the curated agent spec instead of the full internal one"
    )


@pytest.mark.parametrize("path", ("/docs", "/redoc"))
def test_production_does_not_serve_a_doc_page_even_to_an_admin(client_factory, path):
    """A Swagger shell that cannot fetch its spec is worse than no page.

    The browser cannot attach X-ADMIN-KEY to the page's own /openapi.json fetch, so an
    admin-gated doc page renders "Failed to load API definition" — which reads as a broken API
    rather than a deliberate closure, while still advertising that the surface exists.
    """
    with client_factory(production=True) as client:
        resp = client.get(path, headers={"X-ADMIN-KEY": "test-admin-key"})
    assert resp.status_code == 404, (
        f"{path} served an HTML shell in production ({resp.status_code}); it cannot load its spec"
    )


@pytest.mark.parametrize("path", DOC_PATHS)
@pytest.mark.parametrize("method", ("post", "put", "patch", "delete"))
def test_non_get_does_not_reveal_the_route_exists(client_factory, path, method):
    """FastAPI answers 405 with `Allow: GET` for a GET-only route.

    That made one `curl -X POST` prove all three routes exist — the exact fact the 404 above is
    there to hide, leaked through the method channel instead of the status one.
    """
    with client_factory(production=True) as client:
        resp = getattr(client, method)(path)
    assert resp.status_code == 404, (
        f"{method.upper()} {path} returned {resp.status_code}, which distinguishes this path "
        "from an unmounted one"
    )


def test_a_non_ascii_admin_key_does_not_500(client_factory):
    """`hmac.compare_digest` on str raises TypeError above codepoint 0x7F.

    ASGI decodes header values as latin-1, so one byte >= 0x80 reached it and the guard returned
    an unhandled 500 — a remotely triggerable 5xx, and a sharper existence oracle than the 401
    this design rejected. Worse, `expected and ...` short-circuits, so it fired ONLY when a key
    was configured: a free unauthenticated probe for whether this revision mounts an admin key.
    """
    # BYTES, not str: httpx refuses to ascii-encode a non-ASCII str header, which is not what a
    # real client does. A raw socket sends the byte, and ASGI hands the app a latin-1 str — which
    # is precisely how a codepoint above 0x7F reaches compare_digest.
    with client_factory(production=True) as client:
        resp = client.get(
            "/openapi.json",
            headers={b"X-ADMIN-KEY": "caf\u00e9".encode("latin-1")},
            follow_redirects=False,
        )
    assert resp.status_code == 307, (
        f"a non-ASCII admin key returned {resp.status_code}; it must be indistinguishable from "
        "any other wrong key (which gets the public redirect)"
    )


@pytest.mark.parametrize("path", DOC_PATHS)
def test_non_production_leaves_the_docs_open(client_factory, path):
    """Staging and local must behave like the thing people develop against."""
    with client_factory(production=False) as client:
        resp = client.get(path)
    assert resp.status_code == 200, f"{path} is gated outside production (status {resp.status_code})"


def test_production_with_no_admin_key_configured_fails_closed(client_factory):
    """An empty expected key must never match an empty supplied one.

    Failing closed now means "the public redirect", never the full internal spec.
    """
    with client_factory(production=True, admin_key=None) as client:
        for headers in ({"X-ADMIN-KEY": ""}, None):
            resp = client.get("/openapi.json", headers=headers, follow_redirects=False)
            assert resp.status_code == 307, (
                f"no configured key returned {resp.status_code}; an empty expected value must "
                "never unlock the full spec"
            )
            # To the same place — a 307 to anywhere else would still be a distinguishable state.
            assert resp.headers["location"] == "/agent/docs/openapi.json"


def test_the_partner_facing_spec_is_still_public(client_factory):
    """/agent/docs/openapi.json is the curated surface and is NOT what this change closes."""
    with client_factory(production=True) as client:
        resp = client.get("/agent/docs/openapi.json")
    assert resp.status_code == 200, (
        "the partner-facing spec must stay public; closing it would be a different, "
        f"unintended change (status {resp.status_code})"
    )


def test_robots_txt_permits_the_public_discovery_surface(client_factory):
    """The blanket Disallow made robots-respecting agent fetchers refuse the public docs.

    Measured live on 2026-08-26: Claude's web fetch honored `User-agent: * / Disallow: /` and
    reported the OpenAPI spec — the artifact pivota.cc/agent-integration publishes as proof the
    surface is agent-readable — as unreachable. The deliberately-anonymous discovery surface
    (curated docs and spec, the /openapi.json alias, OAuth/JWKS metadata) must be fetchable;
    everything keyed stays disallowed.

    Parsed with urllib.robotparser, which resolves FIRST match in file order (Google resolves
    longest match) — so this test also pins the Allow-lines-first ordering both semantics need.
    """
    import urllib.robotparser

    with client_factory(production=True) as client:
        resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(resp.text.splitlines())

    fetchable = (
        "/openapi.json",
        "/agent/docs/openapi.json",
        "/agent/docs/overview",
        "/agent/docs/quickstart.md",
        "/.well-known/oauth-authorization-server",
        "/.well-known/jwks.json",
    )
    with client_factory(production=True) as client:
        for path in fetchable:
            assert parser.can_fetch("ClaudeBot", f"https://api.pivota.cc{path}"), (
                f"robots.txt blocks {path}, which this host publishes for anonymous agent discovery"
            )
            # Ground the allowlist against the mounted app: an Allow line pointing at a 404 is
            # the same dead end this change removes, recurring silently. The .well-known routes
            # are flag-gated (MCP_OAUTH_AS_ENABLED) and unmounted in the test env, so only the
            # spec surfaces are grounded here.
            if not path.startswith("/.well-known/"):
                grounded = client.get(path, follow_redirects=False)
                assert grounded.status_code != 404, (
                    f"robots.txt allows {path} but the app answers 404 there"
                )

    blocked = ("/", "/health", "/agent/v1/merchants", "/admin/psp/connect", "/oauth/authorize")
    for path in blocked:
        assert not parser.can_fetch("ClaudeBot", f"https://api.pivota.cc{path}"), (
            f"robots.txt allows crawling {path}; only the discovery surface should be open"
        )


def test_non_production_robots_txt_stays_fully_disallowed(client_factory):
    """Outside production, /openapi.json serves the FULL internal spec anonymously.

    The production allowlist would therefore invite crawlers straight to the route map the
    production guard exists to hide. Staging keeps the blanket disallow.
    """
    import urllib.robotparser

    with client_factory(production=False) as client:
        resp = client.get("/robots.txt")
    assert resp.status_code == 200
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(resp.text.splitlines())
    for path in ("/", "/openapi.json", "/agent/docs/openapi.json"):
        assert not parser.can_fetch("ClaudeBot", f"https://staging.example{path}"), (
            f"non-production robots.txt permits {path}; staging must stay fully disallowed"
        )


@pytest.mark.parametrize(
    "header,expected",
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
    ],
)
def test_security_headers_present(client_factory, header, expected):
    with client_factory(production=True) as client:
        resp = client.get("/health")
    assert resp.headers.get(header) == expected, f"{header} missing or wrong on /health"


def test_csp_is_strict_for_json_and_relaxed_for_the_docs_pages(client_factory):
    """A JSON-API CSP would blank Swagger UI, so the doc pages get the clickjacking half only."""
    with client_factory(production=False) as client:
        api = client.get("/health")
        docs = client.get("/docs")
    assert api.headers.get("Content-Security-Policy") == "default-src 'none'; frame-ancestors 'none'"
    assert docs.headers.get("Content-Security-Policy") == "frame-ancestors 'none'"
    assert docs.status_code == 200, "the relaxed CSP is pointless if the page does not render"


def test_hsts_only_on_https(client_factory):
    """Behind the load balancer the scheme is in X-Forwarded-Proto; the internal hop is http."""
    with client_factory(production=True) as client:
        plain = client.get("/health")
        secure = client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert "Strict-Transport-Security" not in plain.headers
    assert secure.headers.get("Strict-Transport-Security") == "max-age=31536000"
    # Neither includeSubDomains nor preload: both are commitments this change does not make.
    assert "includeSubDomains" not in secure.headers["Strict-Transport-Security"]
    assert "preload" not in secure.headers["Strict-Transport-Security"]


def test_security_headers_survive_an_unhandled_exception(client_factory):
    """Headers present on 200s and missing on 500s protect the requests that matter least.

    This must drive a REAL unhandled exception, not a 404. middleware/error_handler.py copies
    inner headers onto its 4xx responses but builds its 500 from scratch with none — so a 404
    carries nosniff whichever side of the error handler this middleware sits on, and only a 500
    tells the two orderings apart. The earlier version of this test used a 404 and survived a
    mutant that moved the middleware inside the error handler.
    """
    import main

    path = "/__security_headers_probe__"

    async def _boom():
        raise RuntimeError("deliberate failure for the ordering assertion")

    main.app.add_api_route(path, _boom, methods=["GET"], include_in_schema=False)
    try:
        with client_factory(production=True, raise_server_exceptions=False) as client:
            resp = client.get(path, headers={"X-Forwarded-Proto": "https"})
        assert resp.status_code == 500
        assert resp.headers.get("X-Content-Type-Options") == "nosniff", (
            "the middleware must WRAP the error handler, not sit inside it"
        )
        assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000"
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes if getattr(r, "path", None) != path
        ]


def test_forwarded_proto_takes_the_first_hop(client_factory):
    """X-Forwarded-Proto accumulates left-to-right; the ORIGINAL client scheme is first.

    Taking the last element would read the scheme of the hop nearest this service, which behind
    the load balancer is http — HSTS would then never be sent in production.
    """
    with client_factory(production=True) as client:
        resp = client.get("/health", headers={"X-Forwarded-Proto": "https,http"})
    assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000"


def test_a_header_set_by_a_handler_is_not_overwritten(client_factory):
    """The middleware documents setdefault semantics; nothing asserted it."""
    import main

    path = "/__security_headers_override_probe__"

    async def _explicit():
        from fastapi.responses import JSONResponse as _JSONResponse

        return _JSONResponse({"ok": True}, headers={"Referrer-Policy": "same-origin"})

    main.app.add_api_route(path, _explicit, methods=["GET"], include_in_schema=False)
    try:
        with client_factory(production=True) as client:
            resp = client.get(path)
        assert resp.headers.get("Referrer-Policy") == "same-origin", (
            "the middleware overwrote a value the handler set deliberately"
        )
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes if getattr(r, "path", None) != path
        ]
