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

import importlib

import pytest
from fastapi.testclient import TestClient

DOC_PATHS = ("/openapi.json", "/docs", "/redoc")


@pytest.fixture
def client_factory(monkeypatch):
    """Build a TestClient for a chosen environment.

    main.py reads PIVOTA_ENV at request time through config.platform.is_production(), so the app
    does not need rebuilding per case — but the import is done here so a failure to import shows up
    as a test error rather than a collection error.
    """
    import main

    def _build(*, production: bool, admin_key: str | None = "test-admin-key"):
        monkeypatch.setenv("PIVOTA_ENV", "production" if production else "staging")
        if admin_key is None:
            monkeypatch.delenv("ADMIN_API_KEY", raising=False)
            monkeypatch.delenv("PROMOTIONS_ADMIN_KEY", raising=False)
        else:
            monkeypatch.setenv("ADMIN_API_KEY", admin_key)
        importlib.reload(importlib.import_module("config.platform"))
        return TestClient(main.app)

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


@pytest.mark.parametrize("path", DOC_PATHS)
def test_production_serves_the_spec_to_an_admin(client_factory, path):
    """The guard must not simply delete the surface — ops scripts read it in production."""
    with client_factory(production=True) as client:
        resp = client.get(path, headers={"X-ADMIN-KEY": "test-admin-key"})
    assert resp.status_code == 200, f"{path} refused a valid admin key (status {resp.status_code})"


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


def test_security_headers_survive_an_error_response(client_factory):
    """Headers present on 200s and missing on errors protect the requests that matter least."""
    with client_factory(production=True) as client:
        resp = client.get("/this-path-does-not-exist-anywhere")
    assert resp.status_code == 404
    assert resp.headers.get("X-Content-Type-Options") == "nosniff", (
        "the middleware must wrap the error handler, not sit inside it"
    )
