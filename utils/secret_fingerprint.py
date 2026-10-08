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

Stdlib only, no import-time side effects, so any adapter or script can use it.
`tests/test_no_secret_in_log_lines.py` keeps the slices from coming back.
"""

from __future__ import annotations

import hashlib


def secret_fingerprint(value: object) -> str:
    """`sha256:<8 hex>` for a non-empty string credential, else `absent`."""
    if not isinstance(value, str) or not value:
        return "absent"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
