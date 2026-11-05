"""The product a Store Audit commerce checkout probe records its cart evidence against.

The commerce receipt route refuses cart evidence on a verification run with no product_key
(409 PRODUCT_NOT_RESOLVED), and the worker sends a cart section on every journey it completes.
So every enqueuer of a commerce_checkout_probe run must give the run a product_key: Store
Readiness (a merchant-submitted product URL) and the scheduled re-probe selector (the merchant's
storefront route) both use this one rule.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from db.database import database

# Store Readiness's own verification run is the only one that may complete its parent audit run (a
# scheduled re-probe reuses that run id). Enqueue and receipt both read these, so renaming one side
# cannot silently stop merchants' Store Readiness runs from ever completing.
SUBJECT_TYPE_STORE_READINESS = "store_readiness"
STORE_READINESS_IDEMPOTENCY_PREFIX = "store_readiness:"


def store_readiness_idempotency_key(run_id: str) -> str:
    return f"{STORE_READINESS_IDEMPOTENCY_PREFIX}{run_id}"


def is_store_readiness_probe(idempotency_key: object) -> bool:
    return str(idempotency_key or "").startswith(STORE_READINESS_IDEMPOTENCY_PREFIX)


def probe_url(url: str) -> str:
    """The URL a probe is keyed on, normalized like execution_routes.endpoint_normalized (lowercase
    host, no query or fragment, no trailing slash), which is the target_url the worker is handed.
    Store Readiness submits ``…/products/x/`` and the selector reads ``…/products/x`` back from the
    route: both must reach the same key."""
    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return raw
    if not host:
        return raw
    netloc = host if port in (None, 443) else f"{host}:{port}"
    return f"https://{netloc}{parsed.path.rstrip('/') or '/'}"


async def probe_product_key(*, merchant_id: str, url: str) -> str:
    """The catalog product at ``url`` when the merchant's catalog has one, else a stable
    ``url_probe:`` key for "the probe that started at this URL"."""
    key_url = probe_url(url)
    try:
        row = await database.fetch_one(
            """
            SELECT product_key
              FROM catalog_products
             WHERE merchant_id = :merchant_id
               AND canonical_url IS NOT NULL
               AND split_part(canonical_url, '?', 1) IN (:url, :url_slash)
               AND suppression_reason IS NULL
             ORDER BY updated_at DESC NULLS LAST
             LIMIT 1
            """,
            {"merchant_id": merchant_id, "url": key_url, "url_slash": key_url.rstrip("/") + "/"},
        )
    except Exception:  # noqa: BLE001 - URL audits do not require catalog sync
        row = None
    # Index, not .get(): a databases Postgres Record has no dict .get, and calling it raised outside
    # the try, so a URL that WAS in the catalog failed the whole enqueue.
    if row is not None and row["product_key"]:
        return str(row["product_key"])
    digest = hashlib.sha256(f"{merchant_id}|{key_url}".encode()).hexdigest()[:24]
    return f"url_probe:{digest}"
