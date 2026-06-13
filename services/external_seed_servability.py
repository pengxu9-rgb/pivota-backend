"""Make an external-seed (crawled) catalog product fully servable.

The external_seed mirror (scripts.mirror_external_seeds_to_catalog_products)
creates the catalog_products identity row, but the serving gate
(services.index_pipeline_state_service) needs three more artifacts that the
merchant-sync pipeline produces and that external-seed onboarding bypasses:

  (e) external_product_seeds.attached_product_key back-link
        -> the eligibility query joins the seed via attached_product_key =
           cp.product_key; without it the row reads as `no_seed`.
  (f) a product_quality_snapshot with content_quality_score >= threshold
        -> otherwise `low_quality`. Produced here by the production scorer
           product_quality_service.full_quality_eval (deterministic; no LLM).
  (g) an agent_pdp_view row (image/title/description)
        -> the gate reads apv.image_url; without it the row reads as `no_image`.

Each step is best-effort and isolated: a serving-artifact failure is logged and
never rolls back the catalog identity, mirroring the mirror's own per-row chain
write. Re-runnable / idempotent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from db.database import database
from services.index_pipeline_state_service import recompute_serving_eligibility
from services.product_quality_service import build_quality_payload, full_quality_eval
from services.seed_data_writer import refresh_agent_pdp_view_for_seed

logger = logging.getLogger(__name__)

EXTERNAL_SEED_MERCHANT_ID = "external_seed"
EXTERNAL_SEED_PLATFORM = "external_seed"


async def backlink_seed_to_product(seed_id: str, product_key: str, *, db: Any = database) -> None:
    """Set external_product_seeds.attached_product_key = product_key (the mirror
    creates the product from the seed but doesn't back-link it). Idempotent."""
    await db.execute(
        """
        UPDATE external_product_seeds
        SET attached_product_key = :product_key, updated_at = NOW()
        WHERE id = :seed_id
          AND (attached_product_key IS NULL OR attached_product_key <> :product_key)
        """,
        {"product_key": product_key, "seed_id": seed_id},
    )


def build_servable_quality_payload(
    *,
    title: Optional[str],
    description: Optional[str],
    price: Any,
    image_url: Optional[str],
    brand: Optional[str] = None,
    product_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Shape the fields the deterministic quality scorer reads."""
    return build_quality_payload(
        {
            "title": title,
            "description": description,
            "price": price,
            "image_url": image_url,
            "brand": brand,
            "product_type": product_type,
        }
    )


async def make_external_seed_servable(
    *,
    product_key: str,
    seed_id: str,
    source_product_id: str,
    quality_payload: Dict[str, Any],
    reason: str = "external_seed_servable",
    db: Any = database,
) -> Dict[str, Any]:
    """Produce the three serving-layer artifacts for an external-seed product and
    recompute eligibility. Returns a per-step summary; never raises."""
    summary: Dict[str, Any] = {"backlink": False, "quality": False, "apv": False, "serving_eligible": None}

    try:
        await backlink_seed_to_product(seed_id, product_key, db=db)
        summary["backlink"] = True
    except Exception:  # noqa: BLE001 -- best-effort; identity must not roll back
        logger.exception("attached_product_key back-link failed for %s", product_key)

    try:
        await full_quality_eval(
            merchant_id=EXTERNAL_SEED_MERCHANT_ID,
            platform=EXTERNAL_SEED_PLATFORM,
            platform_product_id=source_product_id,
            geo_code="default",
            payload=quality_payload,
        )
        summary["quality"] = True
    except Exception:  # noqa: BLE001
        logger.exception("quality snapshot failed for %s", product_key)

    try:
        await refresh_agent_pdp_view_for_seed(seed_id=seed_id, proposal_id=None, refresh_source=reason)
        summary["apv"] = True
    except Exception:  # noqa: BLE001
        logger.exception("agent_pdp_view refresh failed for %s", product_key)

    try:
        cp = await db.fetch_one(
            "SELECT content_key FROM catalog_products WHERE product_key = :pk", {"pk": product_key}
        )
        content_key = (dict(cp).get("content_key") if cp else None)
        if content_key:
            summary["serving_eligible"] = await recompute_serving_eligibility(content_key, reason=reason)
    except Exception:  # noqa: BLE001
        logger.exception("serving-eligibility recompute failed for %s", product_key)

    return summary
