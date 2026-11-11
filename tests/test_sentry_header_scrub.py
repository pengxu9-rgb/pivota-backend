"""`config.sentry_config.filter_sensitive_data` scrubs every credential header we
redact from request logs. Inert while sentry-sdk is not installed, but the list
must not lag `middleware.structured_logging.REDACTED_HEADER_NAMES` for the
headers that carry a bearer credential."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("header", ["X-Pivota-Buyer-Token", "x-pivota-synthetic-assertion", "Authorization"])
def test_a_credential_header_is_redacted(header):
    from config.sentry_config import filter_sensitive_data

    event = {"request": {"headers": {header: "secret-SHOULD-NOT-APPEAR", "user-agent": "ua"}}}

    out = filter_sensitive_data(event, None)

    assert out["request"]["headers"][header] == "[REDACTED]"
    assert out["request"]["headers"]["user-agent"] == "ua"
