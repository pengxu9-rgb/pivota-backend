"""No request log may write an OAuth callback's `code` or `state`.

Every OAuth callback this app serves takes them as query parameters
(`routes/admin_amazon_oauth.py`, `routes/gsc_oauth_routes.py`,
`routes/merchant_store_connections.py`). Two channels write the query string:

- `StructuredLoggingMiddleware`'s `query_params`, through `_redact_query_params`;
- `uvicorn.access`, whose request line is path AND query. Until this change its
  filter rewrote only the path, so a callback's code and state reached Cloud
  Logging on every 302 and 400.

Both are pinned here against the same key rule.
"""

from __future__ import annotations

import logging

import pytest

CODE = "c0de-SHOULD-NOT-APPEAR"
STATE = "st4te-SHOULD-NOT-APPEAR"


def _access_record(full_path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.7:54321", "GET", full_path, "1.1", 302),
        exc_info=None,
    )


@pytest.fixture
def access_filter():
    """The filter actually installed on `uvicorn.access` by importing main."""
    import main  # noqa: F401
    from middleware.structured_logging import UvicornAccessPathRedactionFilter

    installed = [
        f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, UvicornAccessPathRedactionFilter)
    ]
    assert len(installed) == 1
    return installed[0]


def test_the_access_line_of_an_oauth_callback_carries_neither_code_nor_state(access_filter):
    record = _access_record(f"/api/gsc/oauth/callback?code={CODE}&state={STATE}&scope=read")

    assert access_filter.filter(record) is True
    line = record.getMessage()

    assert CODE not in line and STATE not in line
    # Keys and the harmless parameter survive, so the line still says what happened.
    assert "/api/gsc/oauth/callback?code=[REDACTED]&state=[REDACTED]&scope=read" in line


@pytest.mark.parametrize(
    "query, expected",
    [
        ("code=abc", "code=[REDACTED]"),
        ("CODE=abc", "CODE=[REDACTED]"),
        # A percent-encoded key is decoded only to decide; the key is kept as sent.
        ("%63ode=abc&x=1", "%63ode=[REDACTED]&x=1"),
        ("ticket=t1&code_verifier=v1&nonce=n1", "ticket=[REDACTED]&code_verifier=[REDACTED]&nonce=[REDACTED]"),
        ("hmac=h&shop=a.myshopify.com", "hmac=[REDACTED]&shop=a.myshopify.com"),
        ("id_token=j&refresh_token=r", "id_token=[REDACTED]&refresh_token=[REDACTED]"),
        ("client_secret=s", "client_secret=[REDACTED]"),
        ("webhook_secret=s&secret_key=t", "webhook_secret=[REDACTED]&secret_key=[REDACTED]"),
        ("apikey=a&api-key=b&access_key=c&private_key=d", "apikey=[REDACTED]&api-key=[REDACTED]&access_key=[REDACTED]&private_key=[REDACTED]"),
        ("client_assertion=j&assertion=k", "client_assertion=[REDACTED]&assertion=[REDACTED]"),
        # Repeats are each redacted.
        ("state=a&state=b", "state=[REDACTED]&state=[REDACTED]"),
    ],
)
def test_sensitive_values_are_replaced_and_keys_kept(query, expected):
    from middleware.structured_logging import redact_sensitive_query_values

    assert redact_sensitive_query_values(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "q=lipstick",
        "q=red%20lipstick&page=2",
        # Blank values, bare flags and odd separators stay byte for byte.
        "a=&b&&c=1",
        # A key that only CONTAINS a sensitive word as a value is not a key.
        "q=code",
    ],
)
def test_a_query_without_sensitive_keys_is_returned_unchanged(query):
    from middleware.structured_logging import redact_sensitive_query_values

    assert redact_sensitive_query_values(query) == query


def test_a_bare_sensitive_flag_without_a_value_is_left_alone():
    """`?code` with no `=` carries no value, so there is nothing to hide and
    rewriting it would log something that never existed."""
    from middleware.structured_logging import redact_sensitive_query_values

    assert redact_sensitive_query_values("code&x=1") == "code&x=1"


def test_a_non_callback_access_line_is_untouched(access_filter):
    record = _access_record("/agent/products/search?q=lipstick&page=2")

    access_filter.filter(record)

    assert record.getMessage() == '10.0.0.7:54321 - "GET /agent/products/search?q=lipstick&page=2 HTTP/1.1" 302'


def test_the_app_logger_redacts_the_same_keys():
    from middleware.structured_logging import _redact_query_params

    out = _redact_query_params({"code": CODE, "state": STATE, "ticket": "t", "shop": "a.myshopify.com"})

    assert out == {"code": "[REDACTED]", "state": "[REDACTED]", "ticket": "[REDACTED]", "shop": "a.myshopify.com"}
