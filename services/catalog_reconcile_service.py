"""Reconcile front-end-visible products into the audit's source of truth.

A product can be visible on the merchant front-end (GET /merchant/products,
which reads `products_cache`) yet be ABSENT from `catalog_products` — the
table the AI Commerce Readiness audit (POST /api/audits) validates against.
When that happens the audit returns 404 `missing_refs` for a product the
merchant can plainly see, which is confusing and blocks the audit.

This happens because the live sync writes `products_cache` per-page and then
calls `ingest_standard_products` once at the end; if that catalog ingest
fails it is surfaced as a non-fatal HTTP 207 and nothing reconciles it
afterward (and seed-style products only reach catalog via a manual batch
mirror). This module heals that divergence.

Design notes:
- **Idempotent.** `ingest_standard_products` upserts by `product_key`, so
  re-running is a no-op once rows exist.
- **Same source of truth.** The catalog `product_key` is computed EXACTLY the
  way `ingest_standard_products` computes it (from the StandardProduct's
  product_id/id), so the "missing" diff matches what an ingest would write.
- **Bounded.** `limit` caps how many products one invocation will ingest, so a
  large catalog can't fan out an unbounded number of (LLM-bearing) ingests in
  a single call. The reconcile reads `products_cache.product_data` directly —
  it does NOT re-fetch from the platform API.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.sql import select as _select

from db.database import database
from db.products import products_cache
from db.catalog import catalog_products
from models.standard_product import StandardProduct
from services.catalog_sync_service import (
    ingest_standard_products,
    make_catalog_product_key,
)

logger = logging.getLogger(__name__)

DEFAULT_RECONCILE_LIMIT = 500
_IN_CHUNK = 500

# Candidate row: (catalog product_key, platform, StandardProduct payload dict)
_Candidate = Tuple[str, str, Dict[str, Any]]


def _candidate_for_cache_row(
    merchant_id: str, row: Dict[str, Any]
) -> Optional[_Candidate]:
    """Compute the catalog product_key for a products_cache row exactly as
    `ingest_standard_products` would, so the missing-diff is accurate.

    Returns None when the row can't yield a usable (platform, source_id).
    """
    product_data = row.get("product_data") or {}
    platform = str(row.get("platform") or "").strip()
    if not platform:
        return None
    source_pid = ""
    try:
        product = StandardProduct(**product_data)
        source_pid = str(product.product_id or product.id or "").strip()
    except Exception:
        # Payload won't parse as StandardProduct — fall back to the cache
        # column. ingest will likely reject it too, but we still surface it
        # as "missing" so the operator sees a real reconcile failure rather
        # than a silent skip.
        source_pid = str(row.get("platform_product_id") or "").strip()
    if not source_pid:
        return None
    key = make_catalog_product_key(merchant_id, platform, source_pid)
    if not key:
        return None
    return key, platform, product_data


async def find_missing_catalog_products(
    *, merchant_id: str, platform: Optional[str] = None
) -> List[_Candidate]:
    """Return de-duplicated cache candidates that are visible on the
    front-end (products_cache) but absent from catalog_products."""
    filters = [products_cache.c.merchant_id == merchant_id]
    if platform:
        filters.append(products_cache.c.platform == platform)
    rows = await database.fetch_all(products_cache.select().where(*filters))

    candidates: List[_Candidate] = []
    for row in rows or []:
        cand = _candidate_for_cache_row(merchant_id, dict(row))
        if cand:
            candidates.append(cand)
    if not candidates:
        return []

    keys = [c[0] for c in candidates]
    existing: set = set()
    for i in range(0, len(keys), _IN_CHUNK):
        chunk = keys[i : i + _IN_CHUNK]
        found = await database.fetch_all(
            _select(catalog_products.c.product_key).where(
                catalog_products.c.merchant_id == merchant_id,
                catalog_products.c.product_key.in_(chunk),
            )
        )
        existing.update(str(dict(f).get("product_key")) for f in found or [])

    missing: List[_Candidate] = []
    seen: set = set()
    for key, plat, payload in candidates:
        if key in existing or key in seen:
            continue
        seen.add(key)
        missing.append((key, plat, payload))
    return missing


async def reconcile_catalog_from_cache(
    *,
    merchant_id: str,
    platform: Optional[str] = None,
    limit: int = DEFAULT_RECONCILE_LIMIT,
    dry_run: bool = False,
    source_system: str = "catalog_reconcile",
) -> Dict[str, Any]:
    """Backfill catalog_products from products_cache for `merchant_id`.

    Returns a report dict. When `dry_run` is True, only the diff is computed
    (no writes). At most `limit` products are ingested per call.
    """
    missing = await find_missing_catalog_products(
        merchant_id=merchant_id, platform=platform
    )
    report: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": platform,
        "missing_count": len(missing),
        "limit": limit,
        "dry_run": dry_run,
        "truncated": len(missing) > limit,
        "ingested": 0,
        "failed": 0,
        "sample_missing_keys": [k for k, _, _ in missing[:25]],
    }
    if dry_run or not missing:
        return report

    to_process = missing[:limit]
    by_platform: Dict[str, List[Dict[str, Any]]] = {}
    for _key, plat, payload in to_process:
        by_platform.setdefault(plat, []).append(payload)

    for plat, payloads in by_platform.items():
        try:
            stats = await ingest_standard_products(
                merchant_id=merchant_id,
                platform=plat,
                product_payloads=payloads,
                source_system=source_system,
                source_ref=f"{source_system}:{merchant_id}:{plat}",
            )
            report["ingested"] += int((stats or {}).get("products_ingested") or 0)
            report["failed"] += int((stats or {}).get("products_failed") or 0)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "catalog reconcile ingest failed merchant=%s platform=%s err=%s",
                merchant_id,
                plat,
                exc,
            )
            report["failed"] += len(payloads)
    return report
