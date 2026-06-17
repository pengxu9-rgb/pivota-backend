"""ADR-006 Phase 3 — per-SKU `request_indexing` trigger + status read-back.

Backs the portal's per-SKU `request_indexing` CTA (carrying `cta.target_sku_key`,
stamped by services/next_best_action.py). Resolves the SKU's Pivota *canonical*
PDP URL (agent.pivota.cc/products/<pivota_signature_id>) and submits THAT to
Google's Indexing API under the Pivota-owned credential
(services.gsc_integration.submit_pivota_canonical_url) — never the merchant's own
store URL (see ADR-006).

Flag-gated by settings.gsc_pivota_submit_enabled (default off). When off, the
trigger returns {"status": "not_enabled"} so the endpoint degrades cleanly and
the portal keeps showing the deterministic self-serve checklist instead.

No new merchant-facing promise lands until ADR-006 Phase-1 validation passes and
the flag is flipped on `pivota-backend-production` (Phase 4).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# last_status values that mean "already in flight or done" — re-submitting them
# wastes Indexing API quota (~200/day/project, ADR-006 open-Q#4).
_INFLIGHT_STATUSES = frozenset({"submitted", "pending", "indexed"})


async def resolve_canonical_pdp_url(
    sku_key: str, merchant_id: str,
) -> Optional[str]:
    """`sku_key` + `merchant_id` -> the SKU's canonical
    agent.pivota.cc/products/<sig> URL, or None when the SKU has no minted
    Pivota signature yet (in which case there is nothing for Pivota to submit —
    the merchant's own page must become crawlable first). Best-effort: returns
    None on any DB error rather than raising into the request path."""
    if not sku_key or not merchant_id:
        return None
    from db.database import database

    try:
        sku = await database.fetch_one(
            """
            SELECT product_key FROM catalog_skus
             WHERE sku_key = :sku_key AND merchant_id = :merchant_id
             LIMIT 1
            """,
            {"sku_key": sku_key, "merchant_id": merchant_id},
        )
        if not sku:
            return None
        product = await database.fetch_one(
            """
            SELECT pivota_canonical_url, pivota_signature_id, content_key
              FROM catalog_products
             WHERE product_key = :product_key AND merchant_id = :merchant_id
             LIMIT 1
            """,
            {"product_key": sku["product_key"], "merchant_id": merchant_id},
        )
        # Prefer the already-stored canonical URL — it's the merchant's own
        # minted URL and stays consistent with the environment's base host.
        stored = product["pivota_canonical_url"] if product else None
        if stored:
            return stored
        sig = product["pivota_signature_id"] if product else None
        if not sig and product and product["content_key"]:
            # Fallback: index_pipeline_state carries the signature too. MUST
            # stay merchant-scoped — content_key is cross-merchant (one global
            # row per physical product), but pivota_signature_id is minted
            # per merchant. Without the merchant_id filter this could resolve
            # (and submit) another merchant's canonical URL.
            ips = await database.fetch_one(
                """
                SELECT pivota_signature_id FROM index_pipeline_state
                 WHERE content_key = :content_key AND merchant_id = :merchant_id
                 LIMIT 1
                """,
                {"content_key": product["content_key"], "merchant_id": merchant_id},
            )
            sig = ips["pivota_signature_id"] if ips else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "resolve_canonical_pdp_url failed sku=%s merchant=%s: %s",
            sku_key, merchant_id, exc,
        )
        return None

    if not sig:
        return None
    # Build via the shared helper so the host honors CHECKOUT_UI_BASE_URL
    # (dev/staging stay self-consistent) instead of a hardcoded prefix.
    from services.catalog_sync_service import pivota_canonical_pdp_url

    return pivota_canonical_pdp_url(sig)


async def _get_submission_row(
    merchant_id: str, url: str,
) -> Optional[Dict[str, Any]]:
    from db.database import database

    try:
        row = await database.fetch_one(
            """
            SELECT last_status, submitted_at, indexed_at, last_status_at
              FROM gsc_url_submissions
             WHERE merchant_id = :merchant_id AND url = :url
             LIMIT 1
            """,
            {"merchant_id": merchant_id, "url": url},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("gsc_url_submissions read failed for %s: %s", url, exc)
        return None
    return dict(row) if row else None


async def request_sku_indexing(
    sku_key: str,
    merchant_id: str,
    *,
    audit_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the SKU's canonical PDP URL and submit it under the Pivota
    credential. Idempotent: an already submitted/pending/indexed URL is NOT
    re-submitted (quota guard). Never raises — returns a status dict the portal
    can render directly.

    Status values: "submitted" | "indexed" | "pending" | "error" (from the
    submit) · "no_canonical_url" (SKU has no Pivota page yet) · "not_enabled"
    (flag off).
    """
    url = await resolve_canonical_pdp_url(sku_key, merchant_id)
    if not url:
        return {
            "status": "no_canonical_url",
            "sku_key": sku_key,
            "message": "This product has no Pivota canonical page yet.",
        }

    # Read-then-submit dedupe. Not transactional: two concurrent requests for
    # the same SKU could both pass this check and both submit (wasting one
    # quota unit), but the gsc_url_submissions upsert is ON CONFLICT
    # (merchant_id, url) so no row duplicates result. Acceptable for a
    # button-triggered, flag-off path; tighten with a row lock if it matters.
    existing = await _get_submission_row(merchant_id, url)
    if existing and (existing.get("last_status") in _INFLIGHT_STATUSES):
        return {
            "status": existing["last_status"],
            "last_status": existing["last_status"],
            "url": url,
            "deduped": True,
        }

    from services.gsc_integration import (
        GscNotConfiguredError,
        submit_pivota_canonical_url,
    )
    try:
        result = await submit_pivota_canonical_url(
            url, merchant_id=merchant_id, audit_run_id=audit_run_id,
        )
    except GscNotConfiguredError:
        return {
            "status": "not_enabled",
            "url": url,
            "message": "Pivota-owned indexing isn't enabled yet.",
        }
    result["url"] = url
    return result


async def get_sku_indexing_status(
    sku_key: str, merchant_id: str,
) -> Dict[str, Any]:
    """Per-SKU read-back of gsc_url_submissions for the portal's status chip.

    Status values: "indexed" | "submitted" | "pending" | "error" (from the row)
    · "not_submitted" (no row yet) · "no_canonical_url" (no Pivota page).
    """
    url = await resolve_canonical_pdp_url(sku_key, merchant_id)
    if not url:
        return {"status": "no_canonical_url", "sku_key": sku_key}

    row = await _get_submission_row(merchant_id, url)
    if not row:
        return {"status": "not_submitted", "url": url}

    return {
        "status": row.get("last_status") or "unknown",
        "last_status": row.get("last_status"),
        "url": url,
        "submitted_at": str(row["submitted_at"]) if row.get("submitted_at") else None,
        "indexed_at": str(row["indexed_at"]) if row.get("indexed_at") else None,
    }
