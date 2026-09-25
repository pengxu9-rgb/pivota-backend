"""`redact_path`: no request log may write a secret carried in a URL PATH.

`StructuredLoggingMiddleware` redacts sensitive QUERY parameters and logs `path`
verbatim. That is fine until a route authenticates with a path segment — which
`POST /webhooks/webflow/{store_id}/{url_secret}` does, because Webflow does not
sign a webhook created with a Site API token — and then the credential is in the
INFO line of every delivery and the WARNING line of every refusal.

THERE ARE THREE CHANNELS, and the third is not an app middleware at all.
`uvicorn.access` writes the request line — `get_path_with_query_string(scope)` —
at INFO on every response, 200s and 401s alike, and infra/gcp/Dockerfile starts
uvicorn with neither `--no-access-log` nor a `--log-config`, so on Cloud Run it
goes straight to Cloud Logging.
(`tests/test_operations_authz_and_jwt_secret_enforcement.py` names that channel
as the one a credential in a URL actually leaks through, corrected after its own
review.) Nothing that drives the app over `httpx.ASGITransport` can observe it,
because there is no uvicorn in that loop — which is exactly how it stayed
unredacted while the two middlewares were fixed. So it is pinned here, against a
`LogRecord` built in uvicorn's own shape.

`tests/test_webflow_webhooks.py` pins the end-to-end middleware behaviour. What
is pinned HERE is the helper's own contract (including the cases that would make
it destructive if it over-matched) and the uvicorn filter.
"""

from __future__ import annotations

import logging

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


# ---------------------------------------------------------------------------
# uvicorn's own access log
# ---------------------------------------------------------------------------

SECRET = "H8fT-a-real-looking-url-secret"
WEBFLOW_PATH = f"/webhooks/webflow/store_wf_1/{SECRET}"


def _uvicorn_record(path: str, status_code: int) -> logging.LogRecord:
    """A record in `uvicorn.protocols.http`'s EXACT shape.

    uvicorn logs:

        logger.info('%s - "%s %s HTTP/%s" %d',
                    client_addr, method, full_path, http_version, status_code)

    Built by hand rather than by running uvicorn because the point is the shape
    of the record, and a test that started a real server to observe one would be
    pinning the socket rather than the redaction. The format string and the
    argument order are copied from uvicorn; if uvicorn changes them, the filter
    leaves the record ALONE (it only rewrites the 5-tuple it recognises), so
    this test is what would have to be updated alongside it.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.7:54321", "POST", path, "1.1", status_code),
        exc_info=None,
    )


@pytest.fixture
def access_filter():
    """The INSTALLED filter, taken off the real `uvicorn.access` logger.

    Not a fresh instance: a filter that works but was never attached redacts
    nothing in production, and that is precisely the failure this test exists to
    catch.
    """
    import main  # noqa: F401  (importing it is what installs the filter)
    from middleware.structured_logging import UvicornAccessPathRedactionFilter

    installed = [
        f
        for f in logging.getLogger("uvicorn.access").filters
        if isinstance(f, UvicornAccessPathRedactionFilter)
    ]
    assert installed, (
        "importing main did not install the path-redaction filter on "
        "`uvicorn.access` — uvicorn writes the URL secret verbatim on every "
        "request and nothing in the app can see it"
    )
    assert len(installed) == 1, "the install is not idempotent; filters stacked"
    return installed[0]


@pytest.mark.parametrize("status_code", [200, 401])
def test_uvicorn_access_log_never_writes_the_url_secret(access_filter, status_code):
    """Both statuses, because both are written.

    A 200 is the delivery that worked; a 401 is the one that did not — and the
    401 is the MORE dangerous line, because a wrong secret gets there too and an
    attacker probing the endpoint generates one per attempt.
    """
    record = _uvicorn_record(WEBFLOW_PATH, status_code)

    assert access_filter.filter(record) is True, "the filter dropped a log record"

    rendered = record.getMessage()
    assert "[REDACTED]" in rendered
    assert SECRET not in rendered
    # The rest of the line survives: a redaction that ate the store id or the
    # status would make the access log useless and get turned off.
    assert "/webhooks/webflow/store_wf_1/[REDACTED]" in rendered
    assert '"POST' in rendered and str(status_code) in rendered


def test_the_uvicorn_filter_is_installed_exactly_once(access_filter):
    """Idempotence, called the way main.py's startup hook calls it."""
    from middleware.structured_logging import (
        UvicornAccessPathRedactionFilter,
        install_uvicorn_access_log_redaction,
    )

    again = install_uvicorn_access_log_redaction()

    assert again is access_filter
    assert (
        len(
            [
                f
                for f in logging.getLogger("uvicorn.access").filters
                if isinstance(f, UvicornAccessPathRedactionFilter)
            ]
        )
        == 1
    )


def test_a_non_webflow_access_line_is_untouched(access_filter):
    """The filter must be invisible everywhere else, or it becomes the reason
    the access log stops being trusted."""
    record = _uvicorn_record("/agent/products/search?q=lipstick", 200)

    access_filter.filter(record)

    assert record.getMessage() == (
        '10.0.0.7:54321 - "POST /agent/products/search?q=lipstick HTTP/1.1" 200'
    )


def test_a_query_string_on_the_secret_path_does_not_defeat_the_redaction():
    """`full_path` is path AND query. The path half is what carries the secret,
    and a `?` appended to a delivery URL must not smuggle it through."""
    from middleware.structured_logging import UvicornAccessPathRedactionFilter

    record = _uvicorn_record(f"{WEBFLOW_PATH}?retry=3", 200)

    UvicornAccessPathRedactionFilter().filter(record)

    assert SECRET not in record.getMessage()
    assert "[REDACTED]?retry=3" in record.getMessage()


@pytest.mark.parametrize(
    "case, args",
    [
        ("uvicorn changed its argument count", ("1.2.3.4", "GET", WEBFLOW_PATH)),
        # A lone Mapping is what `logger.info("%(path)s", {...})` produces:
        # LogRecord unwraps the 1-tuple and `record.args` is the dict itself.
        ("a %(name)s-style dict format", ({"path": WEBFLOW_PATH},)),
        ("no args at all", None),
        (
            "a non-str path (a mock, a bytes, a future shape)",
            ("1.2.3.4", "GET", b"/webhooks/webflow/s/x", "1.1", 200),
        ),
    ],
)
def test_a_record_that_is_not_the_access_line_is_passed_through_unchanged(case, args):
    """The filter must never raise and never drop.

    It runs on EVERY `uvicorn.access` record, in a process where an exception in
    a logging filter is swallowed into stderr noise and the record is lost. A
    shape it does not recognise is left exactly alone.
    """
    from middleware.structured_logging import UvicornAccessPathRedactionFilter

    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="something else %s",
        args=args,
        exc_info=None,
    )
    before = record.args

    assert UvicornAccessPathRedactionFilter().filter(record) is True, case
    assert record.args == before, case
