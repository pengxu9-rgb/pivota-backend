"""The public HTTP surface: no anonymous spec in production, security headers everywhere.

Both findings were MEASURED against the live hosts on 2026-08-22, not inferred:

  curl https://api.pivota.cc/openapi.json  -> 200, 1019 paths
  curl -I https://api.pivota.cc/health     -> no HSTS, no nosniff, no X-Frame-Options,
                                              no CSP, no Referrer-Policy

The spec is the full internal route list. Anonymously readable it is a map of every path worth
probing, which is worth more to an attacker than any single path on it. The curated partner-facing
spec is a different surface and stays public at /agent/docs/openapi.json — these tests assert that
distinction holds, because "we closed the docs" would be wrong if it took the partner one with it.

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


@pytest.mark.parametrize("path", DOC_PATHS)
def test_production_serves_no_anonymous_spec(client_factory, path):
    with client_factory(production=True) as client:
        resp = client.get(path)
    # 404 rather than 401: a 401 confirms the endpoint exists and is merely guarded, which re-leaks
    # the fact worth hiding.
    assert resp.status_code == 404, (
        f"{path} is reachable anonymously in production (status {resp.status_code}). "
        "This is the full internal route list; serving it hands an attacker the path map."
    )


def test_production_serves_the_spec_to_an_admin(client_factory):
    """The guard must not simply delete the surface — ops scripts read the spec in production."""
    with client_factory(production=True) as client:
        resp = client.get("/openapi.json", headers={"X-ADMIN-KEY": "test-admin-key"})
    assert resp.status_code == 200, f"/openapi.json refused a valid admin key ({resp.status_code})"
    assert resp.json().get("paths"), "the spec came back empty — app.openapi() is not producing one"


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
        resp = client.get("/openapi.json", headers={b"X-ADMIN-KEY": "caf\u00e9".encode("latin-1")})
    assert resp.status_code == 404, (
        f"a non-ASCII admin key returned {resp.status_code}; it must be indistinguishable from "
        "any other wrong key"
    )


@pytest.mark.parametrize("path", DOC_PATHS)
def test_non_production_leaves_the_docs_open(client_factory, path):
    """Staging and local must behave like the thing people develop against."""
    with client_factory(production=False) as client:
        resp = client.get(path)
    assert resp.status_code == 200, f"{path} is gated outside production (status {resp.status_code})"


def test_production_with_no_admin_key_configured_fails_closed(client_factory):
    """An empty expected key must never match an empty supplied one."""
    with client_factory(production=True, admin_key=None) as client:
        assert client.get("/openapi.json", headers={"X-ADMIN-KEY": ""}).status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_the_partner_facing_spec_is_still_public(client_factory):
    """/agent/docs/openapi.json is the curated surface and is NOT what this change closes."""
    with client_factory(production=True) as client:
        resp = client.get("/agent/docs/openapi.json")
    assert resp.status_code == 200, (
        "the partner-facing spec must stay public; closing it would be a different, "
        f"unintended change (status {resp.status_code})"
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
