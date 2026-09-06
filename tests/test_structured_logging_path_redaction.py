"""`redact_path`: the access log must not write a secret carried in a URL PATH.

`StructuredLoggingMiddleware` redacts sensitive QUERY parameters and logs `path`
verbatim. That is fine until a route authenticates with a path segment — which
`POST /webhooks/webflow/{store_id}/{url_secret}` does, because Webflow does not
sign a webhook created with a Site API token — and then the credential is in the
INFO line of every delivery and the WARNING line of every refusal.

`tests/test_webflow_webhooks.py` pins the end-to-end behaviour through the real
middleware. What is pinned HERE is the helper's own contract, including the
cases that would make it destructive if it over-matched.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "case, path, expected",
    [
        (
            "the receiver's own path",
            "/webhooks/webflow/store_wf_1/H8fT-secret-value",
            "/webhooks/webflow/store_wf_1/[REDACTED]",
        ),
        (
            # A secret carrying a slash (or a request that appends anything)
            # must not leave a fragment of it behind.
            "extra segments past the secret",
            "/webhooks/webflow/store_wf_1/secret/extra",
            "/webhooks/webflow/store_wf_1/[REDACTED]/[REDACTED]",
        ),
        (
            # Short of the secret there is nothing to hide, and reshaping the
            # path would log something that never existed.
            "no secret segment at all",
            "/webhooks/webflow/store_wf_1",
            "/webhooks/webflow/store_wf_1",
        ),
        ("the bare prefix", "/webhooks/webflow", "/webhooks/webflow"),
        ("the prefix with a trailing slash", "/webhooks/webflow/", "/webhooks/webflow/"),
    ],
)
def test_a_path_secret_route_has_its_trailing_segments_redacted(case, path, expected):
    from middleware.structured_logging import redact_path

    assert redact_path(path) == expected, case


@pytest.mark.parametrize(
    "case, path",
    [
        ("another platform's receiver", "/webhooks/squarespace/store_wf_1"),
        ("an ordinary route", "/integrations/webflow/store_wf_1/reconcile"),
        # ANCHORED, not a substring test: a route that merely contains the
        # prefix keeps its own path, or an unrelated surface would start logging
        # `[REDACTED]` where its ids used to be and become undebuggable.
        ("the prefix embedded mid-path", "/proxy/webhooks/webflow/store_wf_1/x"),
        # And not a bare `startswith` on the prefix STRING either.
        ("a longer sibling prefix", "/webhooks/webflow-legacy/store_wf_1/x"),
        ("the root", "/"),
        ("empty", ""),
    ],
)
def test_a_path_that_is_not_a_registered_secret_route_is_untouched(case, path):
    from middleware.structured_logging import redact_path

    assert redact_path(path) == path, case


def test_the_registry_is_what_drives_it_rather_than_a_webflow_special_case():
    """The next path-secret route must be able to register a prefix instead of
    growing a second redaction rule somewhere else — so the pairs are data."""
    from middleware.structured_logging import _PATH_SECRET_PREFIXES

    assert ("/webhooks/webflow", 1) in _PATH_SECRET_PREFIXES
    for prefix, keep in _PATH_SECRET_PREFIXES:
        assert prefix.startswith("/") and not prefix.endswith("/")
        assert isinstance(keep, int) and keep >= 0


def test_the_middleware_logs_the_redacted_path(caplog):
    """The helper is only worth having where it is actually CALLED. Pinned end
    to end (both the 200 and the 401) in `tests/test_webflow_webhooks.py`; this
    is the cheap direct check that the access-log entry goes through it."""
    import json
    import logging

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from middleware.structured_logging import StructuredLoggingMiddleware

    app = FastAPI()

    @app.get("/webhooks/webflow/{store_id}/{url_secret}")
    async def _probe(store_id: str, url_secret: str):
        return {"ok": True}

    app.add_middleware(StructuredLoggingMiddleware)

    with caplog.at_level(logging.INFO, logger="structured_logs"):
        assert TestClient(app).get(
            "/webhooks/webflow/store_wf_1/a-real-looking-secret"
        ).status_code == 200

    entries = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "structured_logs"
    ]
    assert entries, "the access log wrote nothing — this test proves nothing"
    assert entries[0]["path"] == "/webhooks/webflow/store_wf_1/[REDACTED]"
    assert "a-real-looking-secret" not in caplog.text
