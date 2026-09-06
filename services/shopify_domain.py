"""The one host shape a Shopify Admin API call may ever target.

`merchant_stores.domain` is a plain text column that becomes
`https://{domain}/admin/api/...` in ~30 places across this repo and in the PIVOTA-Agent gateway,
carrying a live `X-Shopify-Access-Token`. That makes a wrong row not merely an SSRF but an EXPORT of
a working Admin credential to whatever host the row names.

Until this module existed the guard ran on the OAuth *input* only, while the value actually
PERSISTED came from the upstream `shop.json` `myshopify_domain` field and was re-validated by
neither this repo nor the gateway. Validating the input and storing something else is not a guard;
this module is what both ends of that now share.

The Admin API is served only on `<shop>.myshopify.com`, so pinning the shape removes no working
call — it turns a request that was already guaranteed to fail into a local refusal that costs no
packet and leaks no credential.

The regex is deliberately identical to the gateway's `normalizeShopifyAdminHost`
(PIVOTA-Agent `src/services/shopifyAdminHost.js`, #2145) so the two repos agree on one shop-handle
contract rather than drifting into two.

THE ONLY DEFINITION OF THE CANONICALISER, since #2081 merged `routes/webhook_routes.py`'s
byte-identical private copy into this one.

That is NOT the same as saying every host is pinned. Canonicalising and pinning are different jobs:
webhook_routes compares hosts (an untrusted `X-Shopify-Shop-Domain` header against a merchant's
connected stores) and must NOT be pinned to `*.myshopify.com`, or a store connected under any other
domain stops matching its own deliveries. `normalize_myshopify_domain` is for the sites that turn a
domain into an Admin API URL; `canonicalize_shop_domain` is for the sites that compare.

Several credential-sending sites are still unpinned — `services/shopify_products_sync.py`,
`routes/ops_shopify_integration_routes.py`, `readiness/service.py`, `jobs/catalog_import_worker.py`,
`routes/agent_products.py`, `routes/merchant_api_extensions.py`. They deserve one sweep rather than
one-at-a-time patches; naming them here so this module is not read as a claim that the job is done.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

# Anchored, ASCII-only, single label. Everything hostile fails on the character class rather than on
# a special case: `@` (userinfo), `%` (percent-encoding), a trailing dot, an ideographic full stop
# `。` (U+3002) and every other IDN homograph. It is also why a raw IP literal can never match —
# there is no digits-only form ending in .myshopify.com.
MYSHOPIFY_HOST = re.compile(r"[a-z0-9][a-z0-9-]*\.myshopify\.com")


def canonicalize_shop_domain(value: Optional[str]) -> Optional[str]:
    """Reduce a user- or upstream-supplied value to its bare host, or None.

    `urlparse` is safe to lean on here in a way its JavaScript counterpart is NOT: WHATWG
    `new URL()` applies IDNA and maps `。` (U+3002) onto a real `.`, so a parse-then-check guard in
    JS reads `shop。myshopify.com` — a name Shopify never issued — as a valid shop. Python does no
    such mapping, so the character survives parsing and is refused by the regex below. Verified for
    `。`, percent-encoding, homoglyphs, userinfo, tabs/newlines, IP literals and a trailing dot.
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").strip().lower()
        return host or None
    except Exception:
        return raw.lower()


def normalize_myshopify_domain(value: Optional[str]) -> Optional[str]:
    """Canonical `<shop>.myshopify.com`, or None if the value is not one.

    Non-raising ON PURPOSE. The callers that matter are re-checking a value that arrived from
    upstream or from the database, where the right answer is to fall back to the already-validated
    input or to skip the write — not to fail a merchant's install with a 400 because Shopify sent
    something unexpected. `_validate_myshopify_domain` in routes/merchant_store_connections.py wraps
    this for the one place a 400 IS correct: rejecting the caller's own input.
    """
    host = (canonicalize_shop_domain(value) or "").strip().lower()
    if not host:
        return None
    return host if MYSHOPIFY_HOST.fullmatch(host) else None
