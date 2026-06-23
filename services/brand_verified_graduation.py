"""ADR-007 SLICE 2 — "verify-to-serve" graduation for store-less brands.

When a store-less brand domain-verifies its claim (services.brand_claim_service
.verify_brand_claim), its brand-AUTHORED products should become citable
(index_eligible) on the agent surface — NEUTRAL and OFFER-FREE. This module owns
that graduation.

What it does, scoped to ONE merchant's platform='brand_authored' catalog rows:

  1. Resolve identity NEUTRALLY: pdp_scope := 'merchant_owned'. That is one of
     index_pipeline_state_service._RESOLVED_PDP_SCOPES, so the row reads as
     identity_resolved WITHOUT the +200 rank boost that only
     'multi_merchant_canonical' receives (pivot_query_service). We do NOT set a
     shared product_group_id — the brand's product stays ISOLATED (no GTIN
     auto-merge / no content_key mis-merge onto another entity).

  2. Ensure scored: index_eligible needs a product_quality_snapshot with
     content_quality_score >= QUALITY_SCORE_THRESHOLD, which brand-authored
     intake never writes. We run the EXISTING deterministic production scorer
     (product_quality_service.full_quality_eval) on the brand's own content
     (title/description/image + the product_enrichment overlay). full_quality_eval
     writes product_quality_snapshot keyed by (merchant_id, 'brand_authored',
     source_product_id) — exactly the key the eligibility query's pqs lateral
     join reads (platform_product_id = cp.source_product_id).

  3. Ensure an agent_pdp_view row exists: the eligibility gate reads
     apv.image_url / title / description. refresh_agent_pdp_view_for_content_key
     builds that row from catalog_products + the enrichment overlay alone (no
     external_product_seeds row required — brand-authored has none).

  4. Recompute: recompute_serving_eligibility(content_key, reason='brand_verified')
     so index_eligible flips TRUE (now identity-resolved + scored + APV present).

HARD CONSTRAINTS honored here:
  * NO offer minted — we never touch catalog_skus / catalog_offers. has_price
    stays False → serving_eligible / transact_eligible stay FALSE → checkout is
    fail-closed by construction. We graduate the CITATION floor only.
  * NEUTRALITY — pdp_scope='merchant_owned' only, never multi_merchant_canonical.
  * NO mis-merge — product_group_id is left untouched; the row stays isolated.
  * BEST-EFFORT + idempotent — every step is wrapped; this must NEVER break
    verify_brand_claim. Re-running is safe (UPSERT semantics throughout).

Flag-gated by the caller (readiness.flags.storeless_brand_catalog_enabled /
ENABLE_STORELESS_BRAND_CATALOG). With it OFF, the caller does not invoke this
module at all, so behavior is byte-identical to today.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

PLATFORM_BRAND_AUTHORED = "brand_authored"
GRADUATION_REASON = "brand_verified"

# Pull the brand-authored catalog rows we will graduate for this merchant.
_SELECT_BRAND_AUTHORED_ROWS = """
    SELECT product_key, source_product_id, content_key,
           title, description, image_url, brand, product_type, category
    FROM catalog_products
    WHERE merchant_id = :merchant_id
      AND platform = :platform
"""

# Neutral identity resolution: merchant_owned (resolved, NOT boosted) + ensure
# the row is live so the gate's cp_sync_status=='live' predicate passes. We do
# NOT touch product_group_id (no mis-merge) and NEVER set multi_merchant_canonical.
_PROMOTE_SCOPE_LIVE = """
    UPDATE catalog_products
       SET pdp_scope = 'merchant_owned',
           sync_status = 'live',
           updated_at = NOW()
     WHERE merchant_id = :merchant_id
       AND platform = :platform
       AND source_product_id = :source_product_id
       AND (pdp_scope IS DISTINCT FROM 'merchant_owned'
            OR sync_status IS DISTINCT FROM 'live')
"""


async def _fetch_brand_authored_rows(merchant_id: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        _SELECT_BRAND_AUTHORED_ROWS,
        {"merchant_id": merchant_id, "platform": PLATFORM_BRAND_AUTHORED},
    )
    return [dict(r) for r in rows or []]


async def _promote_scope_to_merchant_owned(
    merchant_id: str, source_product_id: str
) -> None:
    """Neutral identity resolution: pdp_scope='merchant_owned' (resolved, NOT
    boosted) + sync_status='live'. Idempotent; never raises on its own (caller
    isolates)."""
    await database.execute(
        _PROMOTE_SCOPE_LIVE,
        {
            "merchant_id": merchant_id,
            "platform": PLATFORM_BRAND_AUTHORED,
            "source_product_id": source_product_id,
        },
    )


async def _score_brand_authored_product(
    merchant_id: str, row: Dict[str, Any]
) -> None:
    """Run the EXISTING deterministic scorer on the brand's own content so a
    product_quality_snapshot lands at (merchant_id, 'brand_authored',
    source_product_id) — the key the eligibility pqs join reads. We feed it the
    catalog content PLUS the product_enrichment overlay (brand-authored content
    lives there), shaped via the same build_quality_payload the external-seed
    servability path uses."""
    from services.product_quality_service import (
        build_quality_payload,
        full_quality_eval,
    )

    source_product_id = row.get("source_product_id")
    if not source_product_id:
        return

    # Pull the merchant's enrichment overlay (summary/bullets/extra images/...)
    # so the score reflects the brand-authored content, not just the bare row.
    enrichment: Optional[Dict[str, Any]] = None
    try:
        from db.product_enrichment import get_enrichment

        enrichment = await get_enrichment(
            merchant_id, PLATFORM_BRAND_AUTHORED, source_product_id, "default"
        )
    except Exception:  # noqa: BLE001 — overlay is best-effort; score on bare row
        logger.exception(
            "brand_verified graduation: enrichment fetch failed for %s/%s",
            merchant_id,
            source_product_id,
        )
        enrichment = None

    payload = build_quality_payload(
        {
            "title": row.get("title"),
            "description": row.get("description"),
            "image_url": row.get("image_url"),
            "brand": row.get("brand"),
            "product_type": row.get("product_type") or row.get("category"),
        },
        enrichment or None,
    )

    # full_quality_eval writes product_quality_snapshot AND fires its own
    # recompute_serving_eligibility hook; we recompute again below so the order
    # (scope -> score -> apv -> recompute) is deterministic and idempotent.
    await full_quality_eval(
        merchant_id=merchant_id,
        platform=PLATFORM_BRAND_AUTHORED,
        platform_product_id=source_product_id,
        geo_code="default",
        payload=payload,
    )


async def _refresh_apv(content_key: str) -> None:
    """Ensure an agent_pdp_view row exists for the content_key. Built from
    catalog_products + the enrichment overlay alone (no external_product_seeds
    row needed — brand-authored has none)."""
    if not content_key:
        return
    from services.agent_pdp_view_assembler import (
        refresh_agent_pdp_view_for_content_key,
    )

    await refresh_agent_pdp_view_for_content_key(
        content_key, refresh_source=GRADUATION_REASON
    )


async def _recompute(content_key: str) -> Optional[bool]:
    if not content_key:
        return None
    from services.index_pipeline_state_service import (
        recompute_serving_eligibility,
    )

    return await recompute_serving_eligibility(
        content_key, reason=GRADUATION_REASON
    )


async def graduate_brand_authored_products(
    merchant_id: str,
) -> Dict[str, Any]:
    """Graduate a verified brand's brand-authored products to the OFFER-FREE
    citation floor (index_eligible). Neutral (merchant_owned), isolated (no
    GTIN merge), no offer minted (serving/transact stay false).

    Best-effort + idempotent at every step. NEVER raises — it runs off the
    claim-verify path and must not break it. Returns a per-merchant summary
    (counts only; safe to log)."""
    summary: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "products": 0,
        "scope_resolved": 0,
        "scored": 0,
        "apv_refreshed": 0,
        "recomputed": 0,
    }
    if not merchant_id:
        return summary

    try:
        rows = await _fetch_brand_authored_rows(merchant_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "brand_verified graduation: failed to load rows for %s", merchant_id
        )
        return summary

    summary["products"] = len(rows)

    for row in rows:
        source_product_id = row.get("source_product_id")
        content_key = row.get("content_key")
        if not source_product_id:
            continue

        # 1. Neutral identity resolution + live.
        try:
            await _promote_scope_to_merchant_owned(merchant_id, source_product_id)
            summary["scope_resolved"] += 1
        except Exception:  # noqa: BLE001 — best-effort; never break verify
            logger.exception(
                "brand_verified graduation: scope promote failed for %s/%s",
                merchant_id,
                source_product_id,
            )

        # 2. Ensure scored (writes product_quality_snapshot).
        try:
            await _score_brand_authored_product(merchant_id, row)
            summary["scored"] += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "brand_verified graduation: scoring failed for %s/%s",
                merchant_id,
                source_product_id,
            )

        # 3. Ensure an agent_pdp_view row (image/title/description).
        try:
            await _refresh_apv(content_key)
            summary["apv_refreshed"] += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "brand_verified graduation: apv refresh failed for %s/%s",
                merchant_id,
                content_key,
            )

        # 4. Recompute so index_eligible flips TRUE (identity + score + apv).
        try:
            await _recompute(content_key)
            summary["recomputed"] += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "brand_verified graduation: recompute failed for %s/%s",
                merchant_id,
                content_key,
            )

    logger.info({"event": "brand_verified_graduation", **summary})
    return summary
