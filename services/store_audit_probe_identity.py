"""The product a Store Audit commerce checkout probe records its cart evidence against.

The commerce receipt route refuses cart evidence on a verification run with no product_key
(409 PRODUCT_NOT_RESOLVED), and the worker sends a cart section on every journey it completes.
So every enqueuer of a commerce_checkout_probe run must give the run a product_key: Store
Readiness (a merchant-submitted product URL) and the scheduled re-probe selector (the merchant's
storefront route) both use this one rule.
"""

from __future__ import annotations

import hashlib

from db.database import database


async def probe_product_key(*, merchant_id: str, url: str) -> str:
    """The catalog product at ``url`` when the merchant's catalog has one, else a stable
    ``url_probe:`` key for "the probe that started at this URL"."""
    try:
        row = await database.fetch_one(
            """
            SELECT product_key
              FROM catalog_products
             WHERE merchant_id = :merchant_id
               AND canonical_url IS NOT NULL
               AND split_part(canonical_url, '?', 1) = :url
               AND suppression_reason IS NULL
             ORDER BY updated_at DESC NULLS LAST
             LIMIT 1
            """,
            {"merchant_id": merchant_id, "url": url},
        )
    except Exception:  # noqa: BLE001 - URL audits do not require catalog sync
        row = None
    if row and row.get("product_key"):
        return str(row["product_key"])
    digest = hashlib.sha256(f"{merchant_id}|{url}".encode()).hexdigest()[:24]
    return f"url_probe:{digest}"
