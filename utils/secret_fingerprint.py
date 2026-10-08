"""A log-safe handle for a credential: never any of its characters.

Cloud Run ships every stdout line, `print` included, to Cloud Logging, where
anyone with log-viewer access on the project can read it. Merchant PSP keys
(Stripe, Checkout.com, Adyen) are the MERCHANT's credentials, so a prefix of
one in a log line is a partial disclosure of someone else's secret: a Stripe
`sk_live_` key printed as `[:20]` gave away 12 characters of key material.

What a debugging line actually needs is "is this the same key as before / as
the one in the database", and a short sha256 prefix answers that without
revealing anything an attacker can use. Deliberately no length: an exact
length identifies the issuing provider and narrows a search space (the same
reasoning as `utils.encryption.mask_credential`).

For HIGH-ENTROPY credentials only (API keys, tokens, signing secrets). An
unsalted hash of a password or OTP can be confirmed against a guess list, so
never fingerprint those; log nothing about them.

Stdlib only, no import-time side effects, so any adapter or script can use it.
`tests/test_no_secret_in_log_lines.py` keeps the slices from coming back.
"""

from __future__ import annotations

import hashlib
import re


def secret_fingerprint(value: object) -> str:
    """`sha256:<8 hex>` for a non-empty str/bytes credential, else `absent`."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, (bytes, bytearray)) or not value:
        return "absent"
    return "sha256:" + hashlib.sha256(value).hexdigest()[:8]


# Greedy to the LAST "@": an unencoded "@" in a password must not leave its tail
# behind. Over-redacting a query that contains "@" is harmless in an error message.
_URL_USERINFO_PASSWORD = re.compile(r"(://[^:/@?#]*):.*@")
_URL_QUERY_PASSWORD = re.compile(r"(?i)([?&](?:password|passwd|pwd)=)[^&#]*")


def redact_url_credentials(url: object) -> str:
    """A DSN/URL with its password replaced by `***` (userinfo and `?password=`).

    `postgresql://user:pass@host/db`[:60] puts the password in a log line; this
    keeps scheme, user, host and database, which is what an error message needs.
    """
    text = url if isinstance(url, str) else str(url or "")
    text = _URL_USERINFO_PASSWORD.sub(r"\1:***@", text)
    return _URL_QUERY_PASSWORD.sub(r"\1***", text)
