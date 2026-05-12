"""P5.3 shared helper — resolve a (audit_run_id, product_key) tuple
to the catalog_products row + Pivota canonical URL.

All three P5.3 verifiers need the same lookup:
  audit_run_id → merchant_id → catalog row → pivota_canonical_url + sig_id.

This module exists so the lookup is in one place rather than
copy-pasted across each verifier file. Verifiers call
load_product_context(verify_context) and get back a typed dict
(or None when the product can't be resolved).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def load_product_context(
    *,
    audit_run_id: str,
    product_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Return {merchant_id, product_key, title, pivota_signature_id,
    pivota_canonical_url, canonical_url}, or None if the product
    can't be resolved.

    None outcomes:
      - audit_run row not found
      - product_key is None (brand-level verifier called for a
        product-scoped row)
      - catalog row for (merchant, product_key) missing
    """
    if not product_key:
        return None
    try:
        from db.merchant_audit_runs import fetch_audit_run_by_id
        audit_row = await fetch_audit_run_by_id(run_id=audit_run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "load_product_context: fetch_audit_run_by_id raised "
            "for audit=%s: %s", audit_run_id, str(exc)[:200],
        )
        return None
    if audit_row is None:
        return None
    merchant_id = audit_row.get("merchant_id")
    if not merchant_id:
        return None

    try:
        from db.catalog import catalog_products
        from db.database import database
        row = await database.fetch_one(
            catalog_products.select().where(
                catalog_products.c.merchant_id == merchant_id,
                catalog_products.c.product_key == product_key,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "load_product_context: catalog lookup raised "
            "for merchant=%s product=%s: %s",
            merchant_id, product_key, str(exc)[:200],
        )
        return None
    if row is None:
        return None
    d = dict(row)
    return {
        "merchant_id": merchant_id,
        "product_key": product_key,
        "title": d.get("title"),
        "brand": d.get("brand"),
        "pivota_signature_id": d.get("pivota_signature_id"),
        "pivota_canonical_url": d.get("pivota_canonical_url"),
        "canonical_url": d.get("canonical_url"),
    }


def get_agent_pivota_base_url() -> str:
    """Read the agent.pivota.cc base from env. Matches
    catalog_sync_service's CHECKOUT_UI_BASE_URL setting so tests
    + non-prod deploys can override."""
    import os
    return (
        os.getenv("CHECKOUT_UI_BASE_URL")
        or "https://agent.pivota.cc"
    ).rstrip("/")
