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
    category: Optional[str] = None,
    raw_inci: Optional[str] = None,
    pdp_details_sections: Optional[Any] = None,
) -> Dict[str, Any]:
    """Shape the fields the deterministic quality scorer reads.

    External-seed crawl products often have a null ``product_type`` and carry
    their ingredient list separately (``raw_inci``). Without help, the scorer
    sees no category (fails the brand+category component) and no ingredients
    (fails the source-backed attributes component), so genuinely-stocked
    products score ~50 and never clear the serving threshold. We therefore:
      * fall back to ``category`` (e.g. category_kind) when product_type is null,
        so the brand+category component can pass,
      * attach the INCI to the payload's ``seed_data`` where
        ``source_backed_attribute_signal_count`` reads it, so a product with a
        real ingredient list earns the attributes component, and
      * carry the crawled ``pdp_details_sections`` through to that same
        ``seed_data`` slot. Both `score_source_backed_summary` and
        `source_backed_attribute_signal_count` read those sections, but nothing
        used to pass them here — so a product whose crawl DID capture its PDP
        detail sections was still scored as if it had none, losing the summary
        and attribute components it had already earned. Measured on 250 live
        rows carrying sections (2026-07-22): 98% clear the 65 bar once they are
        passed, vs 50% without, at +13.1 avg score.
    """
    payload = build_quality_payload(
        {
            "title": title,
            "description": description,
            "price": price,
            "image_url": image_url,
            "brand": brand,
            # product_type drives global_category_id; fall back to category_kind
            "product_type": product_type or category,
        }
    )

    # build_quality_payload does not carry source-backed content through; attach
    # it to the top-level seed_data slot that _source_backed_roots() reads.
    seed_data: Dict[str, Any] = {}

    inci = (raw_inci or "").strip()
    if inci:
        inci_list = [tok.strip() for tok in inci.split(",") if tok.strip()]
        seed_data["inci_list"] = inci_list
        seed_data["pdp_ingredients_raw"] = inci

    if isinstance(pdp_details_sections, (list, tuple)) and pdp_details_sections:
        seed_data["pdp_details_sections"] = list(pdp_details_sections)

    if seed_data:
        payload["seed_data"] = seed_data
    return payload


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

    # Resolve the product's REAL identity up front. Under ADR-009 an external
    # seed is keyed to its per-brand observed seller (merch_obs_…), not the
    # legacy "external_seed" merchant. The quality snapshot MUST be written
    # under that same merchant, because the serving classifier
    # (index_pipeline_state_service) reads the snapshot by the product's own
    # merchant — writing it under EXTERNAL_SEED_MERCHANT_ID leaves the snapshot
    # unfindable ("no quality snapshot found"), so serving_eligible/
    # index_eligible never flip for every merch_obs_-keyed seed. Fall back to
    # the legacy merchant only when the catalog row is missing (pre-ADR-009
    # rows resolve to "external_seed" anyway, so this is correct for both).
    quality_merchant_id = EXTERNAL_SEED_MERCHANT_ID
    content_key: Optional[str] = None
    try:
        cp = await db.fetch_one(
            "SELECT merchant_id, content_key FROM catalog_products WHERE product_key = :pk",
            {"pk": product_key},
        )
        if cp:
            cp = dict(cp)
            quality_merchant_id = str(cp.get("merchant_id") or "").strip() or EXTERNAL_SEED_MERCHANT_ID
            content_key = cp.get("content_key")
    except Exception:  # noqa: BLE001 -- fall back to legacy merchant, never raise
        logger.exception("catalog identity lookup failed for %s", product_key)

    try:
        await backlink_seed_to_product(seed_id, product_key, db=db)
        summary["backlink"] = True
    except Exception:  # noqa: BLE001 -- best-effort; identity must not roll back
        logger.exception("attached_product_key back-link failed for %s", product_key)

    try:
        await full_quality_eval(
            merchant_id=quality_merchant_id,
            platform=EXTERNAL_SEED_PLATFORM,
            platform_product_id=source_product_id,
            geo_code="default",
            payload=quality_payload,
            # External seeds ARE the source-backed case: their quality hinges on
            # the ingredient/attribute signals we attach above, so score those
            # components here rather than gating them behind the global env flag.
            score_source_backed_components=True,
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
        if content_key:
            summary["serving_eligible"] = await recompute_serving_eligibility(content_key, reason=reason)
    except Exception:  # noqa: BLE001
        logger.exception("serving-eligibility recompute failed for %s", product_key)

    return summary
