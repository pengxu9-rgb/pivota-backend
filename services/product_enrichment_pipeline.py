"""
Product Enrichment Pipeline v1

Automated overlay generation on top of StandardProduct / products_cache.

Responsibilities:
- Load StandardProduct from products_cache
- Build a StandardProductContext
- Generate summary / bullets / tags via heuristic "AI" helpers
- Run a lightweight compliance guard
- Upsert into product_enrichment (without overwriting merchant edits)
- Call product_quality_service.full_quality_eval to persist a snapshot
- Optionally publish a PRODUCT_ENRICHED event
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import datetime
import logging

from db.products import get_product_cache_row
from db.product_enrichment import get_enrichment, upsert_enrichment
from models.standard_product import StandardProduct
from services.product_quality_service import build_quality_payload, full_quality_eval
from services.product_enrichment_ai import (
  build_context_from_standard_product,
  compute_auto_confidence,
  generate_enrichment_draft,
)
from utils.event_publisher import publish_custom_event

logger = logging.getLogger(__name__)


def _simple_compliance_check(texts: Dict[str, str]) -> Dict[str, Any]:
  """
  Very lightweight compliance guard.

  V1: just checks for a small set of obviously risky keywords to avoid
  generating blatant medical/financial promises.

  Returns:
      {"allowed": bool, "reason": Optional[str]}
  """
  risky_keywords = [
    "治愈", "治好", "保证收益", "保本", "稳赚", "稳赚不赔",
    "guaranteed return", "guaranteed profit",
  ]

  aggregate = " ".join(texts.values()).lower()
  for kw in risky_keywords:
    if kw.lower() in aggregate:
      return {
        "allowed": False,
        "reason": f"contains risky keyword: {kw}",
      }

  return {"allowed": True, "reason": None}
async def run_enrichment_for_product(
  merchant_id: str,
  platform: str,
  platform_product_id: str,
  geo_code: str = "default",
) -> Optional[Dict[str, Any]]:
  """
  Main enrichment pipeline entrypoint for a single product.

  Safe to call multiple times; it will only fill missing enrichment
  fields and not overwrite existing merchant edits.
  """
  # 1) Load cache row
  cache_row = await get_product_cache_row(
    merchant_id=merchant_id,
    platform=platform,
    platform_product_id=platform_product_id,
    include_expired=False,
  )
  if not cache_row:
    logger.warning(
      "No products_cache row found for %s/%s/%s",
      merchant_id,
      platform,
      platform_product_id,
    )
    return None

  product_data: Dict[str, Any] = cache_row.get("product_data") or {}

  # 2) Parse into StandardProduct when possible
  try:
    product = StandardProduct(**product_data)
  except Exception as exc:  # defensive: cache may contain legacy shapes
    logger.warning(
      "Failed to parse StandardProduct for %s/%s/%s: %s; product_data=%r",
      merchant_id,
      platform,
      platform_product_id,
      exc,
      list(product_data.keys()),
    )
    return None

  context = build_context_from_standard_product(product)

  # 3) Generate enrichment fields
  generated_draft, _title_suggestion = generate_enrichment_draft(
    product,
    preferred_language="en",
  )
  summary = str(generated_draft.get("summary_short") or "")
  bullets = list(generated_draft.get("bullet_points") or [])
  usage_scenarios = list(generated_draft.get("usage_scenarios") or [])
  audience_tags = list(generated_draft.get("audience_tags") or [])
  topic_tags = list(generated_draft.get("topic_tags") or [])
  auto_confidence = max(
    compute_auto_confidence(summary, bullets, context),
    float(generated_draft.get("llm_readability_score") or 0.0),
  )

  # 4) Compliance guard (very lightweight)
  compliance = _simple_compliance_check(
    {
      "summary": summary,
      "joined_bullets": " ".join(bullets),
    }
  )
  if not compliance["allowed"]:
    logger.warning(
      "Enrichment blocked by compliance guard for %s/%s/%s: %s",
      merchant_id,
      platform,
      platform_product_id,
      compliance["reason"],
    )
    # We still record the error on enrichment row (if any).
    existing = await get_enrichment(
      merchant_id=merchant_id,
      platform=platform,
      platform_product_id=platform_product_id,
      geo_code=geo_code,
    )
    # Only mark error_reason via llm_safety_flags to avoid schema changes
    error_flags = {"error": True, "reason": compliance["reason"]}
    data: Dict[str, Any] = {}
    if existing:
      data = {
        "llm_safety_flags": error_flags,
      }
    else:
      data = {
        "summary_short": None,
        "bullet_points": None,
        "usage_scenarios": None,
        "audience_tags": None,
        "topic_tags": None,
        "regulatory_disclaimer_local": None,
        "extra_images": None,
        "llm_readability_score": 0.0,
        "llm_safety_flags": error_flags,
      }

    await upsert_enrichment(
      merchant_id=merchant_id,
      platform=platform,
      platform_product_id=platform_product_id,
      geo_code=geo_code,
      data=data,
    )
    return None

  # 5) Merge with existing enrichment (do not overwrite merchant edits)
  existing = await get_enrichment(
    merchant_id=merchant_id,
    platform=platform,
    platform_product_id=platform_product_id,
    geo_code=geo_code,
  )

  def _merge_field(key: str, generated_value: Any) -> Any:
    if not existing:
      return generated_value
    current = existing.get(key)
    # Only fill when empty / null; respect existing values
    if current is None or current == "" or current == []:
      return generated_value
    return current

  # Simple default disclaimer (can later be replaced with per-category templates)
  if "鞋" in context.title or "shoe" in context.title.lower():
    default_disclaimer = "本产品为运动鞋，不具备医疗功效，具体体验因人而异。"
  else:
    default_disclaimer = "本产品为日常消费品，不提供任何医疗或金融收益承诺。"

  enrichment_data: Dict[str, Any] = {
    "title_override": _merge_field("title_override", generated_draft.get("title_override")),
    "summary_short": _merge_field("summary_short", summary),
    "bullet_points": _merge_field("bullet_points", bullets),
    "usage_scenarios": _merge_field("usage_scenarios", usage_scenarios),
    "audience_tags": _merge_field("audience_tags", audience_tags),
    "topic_tags": _merge_field("topic_tags", topic_tags),
    "regulatory_disclaimer_local": _merge_field(
      "regulatory_disclaimer_local", default_disclaimer
    ),
    "extra_images": _merge_field("extra_images", None),
    "llm_readability_score": _merge_field("llm_readability_score", auto_confidence),
    "llm_safety_flags": _merge_field("llm_safety_flags", None),
  }

  await upsert_enrichment(
    merchant_id=merchant_id,
    platform=platform,
    platform_product_id=platform_product_id,
    geo_code=geo_code,
    data=enrichment_data,
  )

  # 6) Quality evaluation (writes snapshot)
  try:
    quality_payload = build_quality_payload(product, enrichment_data)
    quality_result = await full_quality_eval(
      merchant_id=merchant_id,
      platform=platform,
      platform_product_id=platform_product_id,
      geo_code=geo_code,
      payload=quality_payload,
    )
  except Exception as exc:
    logger.error(
      "Quality eval failed for %s/%s/%s: %s",
      merchant_id,
      platform,
      platform_product_id,
      exc,
    )
    quality_result = None

  # 7) Emit PRODUCT_ENRICHED event (best effort)
  try:
    event = {
      "type": "PRODUCT_ENRICHED",
      "merchant_id": merchant_id,
      "platform": platform,
      "platform_product_id": platform_product_id,
      "geo_code": geo_code,
      "auto_generated": True,
      "auto_confidence": auto_confidence,
      "content_quality_score": quality_result.get("content_quality_score")
      if isinstance(quality_result, dict)
      else None,
      "model_readiness_score": quality_result.get("model_readiness_score")
      if isinstance(quality_result, dict)
      else None,
      "timestamp": datetime.utcnow().isoformat(),
    }
    await publish_custom_event(event)
  except Exception as exc:
    logger.warning(
      "Failed to publish PRODUCT_ENRICHED event for %s/%s/%s: %s",
      merchant_id,
      platform,
      platform_product_id,
      exc,
    )

  return {
    "enrichment": enrichment_data,
    "quality": quality_result,
    "auto_confidence": auto_confidence,
  }
