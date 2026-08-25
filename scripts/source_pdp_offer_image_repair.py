#!/usr/bin/env python3
"""Dry-run-first repair for PDP offer/image gaps from public source pages.

This is intentionally narrow. It only fills missing price or missing image
state for live PDPs that are already in index_pipeline_state, and it treats
public PDP pages as evidence, not partnership. Existing positive prices,
existing catalog images, and existing APV images are never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
    fetch_external_seed_for_keys,
    fetch_offers_for_keys,
    fetch_products_for_key,
    fetch_skus_for_keys,
)
from services.external_offers_service import _extract_from_html, _fetch_html  # noqa: E402


SOURCE_SYSTEM = "public_source_pdp_repair_v1"
SOURCE_KIND = "public_web"
CATALOG_TRACK = "external_referral"
TRUTH_TIER = "observed"
READINESS_TIER = "referral_only"
OFFER_MODE = "redirect"
PRICE_CONFIDENCE = Decimal("0.85")

SPACE_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[a-z0-9]+")
IMAGE_BLACKLIST_RE = re.compile(
    r"(?:^|/|_|-)(?:logo|icon|sprite|favicon|badge|payment|klarna|paypal|placeholder|no-image)(?:$|/|\.|_|-)",
    re.IGNORECASE,
)

STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "de",
    "du",
    "eau",
    "for",
    "from",
    "in",
    "la",
    "le",
    "les",
    "of",
    "on",
    "set",
    "the",
    "to",
    "with",
}

GENERIC_PRODUCT_TOKENS = {
    "beauty",
    "makeup",
    "skin",
    "skincare",
    "hair",
    "fragrance",
    "product",
    "mini",
    "size",
    "shade",
    "pack",
}

VARIANT_TOKENS = {
    "amber",
    "beige",
    "berry",
    "black",
    "blonde",
    "blue",
    "bronze",
    "brown",
    "cherry",
    "clear",
    "copper",
    "coral",
    "cream",
    "dark",
    "deep",
    "fair",
    "gold",
    "grape",
    "grapesicle",
    "green",
    "grey",
    "gray",
    "honey",
    "ivory",
    "light",
    "mahogany",
    "medium",
    "mocha",
    "natural",
    "nude",
    "orange",
    "pink",
    "plum",
    "purple",
    "red",
    "rose",
    "silver",
    "tan",
    "ultra",
    "violet",
    "white",
    "yellow",
}


# Pack / count / SIZE / format indicators. VARIANT_TOKENS above covers only
# shades, so these are checked separately by the opt-in source-superset
# relaxation to keep a single unit from matching a bundle, a multi-count page,
# or a different size/format (or vice-versa).
#
# NOTE: this is matched against RAW tokens, not token_set() output — several of
# these ("set", "pack", "size", "mini", "shade") are stripped by
# STOPWORDS/GENERIC_PRODUCT_TOKENS and would be inert otherwise.
PACK_COUNT_TOKENS = {
    # count / bundle
    "set", "sets", "bundle", "bundles", "kit", "kits", "pack", "packs",
    "multipack", "multi", "combo", "duo", "trio", "twin", "double", "triple",
    "pair", "count", "ct", "ea", "pcs", "pc", "piece", "pieces", "bulk",
    "box", "boxes", "bottle", "bottles", "tube", "tubes", "sheet", "sheets",
    # size / format
    "size", "sized", "mini", "travel", "trial", "sample", "samples", "refill",
    "jumbo", "supersize", "large", "small", "full", "deluxe", "starter",
    # merchandising
    "type", "types", "color", "colors", "colour", "colours", "shade", "shades",
    "edition", "gift", "special", "value", "family", "promo", "deal", "bogo",
    "oversized", "supersized", "boxed", "packed", "bundled", "twopack",
    "sixpack", "sampler", "assortment", "case", "carton", "tray", "unit",
    "collection",
}


def _is_pack_count_token(token: str) -> bool:
    """Deny-set membership, plural-tolerant ("refills" -> "refill")."""
    if token in PACK_COUNT_TOKENS:
        return True
    return token.endswith("s") and token[:-1] in PACK_COUNT_TOKENS


CANDIDATE_QUERY = """
WITH product_rows AS (
  SELECT
    cp.product_key,
    cp.content_key,
    cp.merchant_id,
    cp.platform,
    cp.source_product_id,
    cp.title,
    cp.description AS cp_description,
    cp.brand,
    cp.canonical_url,
    cp.image_url AS cp_image_url,
    cp.pivota_signature_id,
    cp.updated_at,
    pgm.is_primary AS group_is_primary
  FROM catalog_products cp
  LEFT JOIN product_group_members pgm
    ON pgm.merchant_id = cp.merchant_id
   AND pgm.platform = cp.platform
   AND pgm.platform_product_id = cp.source_product_id
  WHERE cp.content_key IS NOT NULL
    AND cp.sync_status = 'live'
),
canonical_product AS (
  SELECT *
  FROM (
    SELECT
      product_rows.*,
      row_number() OVER (
        PARTITION BY content_key
        ORDER BY
          CASE
            WHEN lower(coalesce(canonical_url, '')) ~ '(sephora|nordstrom|ulta|amazon|amzn\\.to|bestbuy)\\.'
              THEN 1
            ELSE 0
          END,
          CASE WHEN product_key LIKE 'ext:%' THEN 0 ELSE 1 END,
          CASE WHEN length(btrim(coalesce(cp_description, ''))) >= 50 THEN 0 ELSE 1 END,
          CASE WHEN group_is_primary THEN 0 ELSE 1 END,
          CASE WHEN pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END,
          product_key ASC
      ) AS rn
    FROM product_rows
  ) ranked
  WHERE rn = 1
)
SELECT
  ips.content_key,
  ips.blocker_code,
  ips.has_price,
  ips.has_image,
  ips.description_length,
  cp.product_key,
  cp.merchant_id,
  cp.platform,
  cp.source_product_id,
  cp.title,
  cp.brand,
  cp.canonical_url,
  cp.cp_image_url,
  apv.title AS apv_title,
  apv.image_url AS apv_image_url
FROM index_pipeline_state ips
JOIN canonical_product cp
  ON cp.content_key = ips.content_key
LEFT JOIN agent_pdp_view apv
  ON apv.content_key = ips.content_key
WHERE ips.serving_eligible IS FALSE
  AND ips.blocker_code NOT IN ('not_live', 'non_core_product')
  {blocker_scope_clause}
  AND nullif(btrim(coalesce(cp.canonical_url, '')), '') IS NOT NULL
  AND (
    ips.has_price IS FALSE
    OR ips.has_image IS FALSE
  )
  AND (CAST(:content_key AS text) IS NULL OR ips.content_key = CAST(:content_key AS text))
ORDER BY
  CASE WHEN ips.has_price IS FALSE THEN 0 ELSE 1 END,
  CASE WHEN ips.has_image IS FALSE THEN 0 ELSE 1 END,
  ips.content_key ASC
{limit_clause}
"""


EXISTING_SKU_QUERY = """
SELECT sku_key
FROM catalog_skus
WHERE product_key = :product_key
ORDER BY
  CASE WHEN source_variant_id = :product_key THEN 0 ELSE 1 END,
  sku_key ASC
LIMIT 1
"""


INSERT_REPAIR_SKU_SQL = """
INSERT INTO catalog_skus (
  sku_key,
  product_key,
  merchant_id,
  platform,
  source_product_id,
  source_variant_id,
  sku,
  barcode,
  title,
  currency,
  image_url,
  visible_attributes,
  visible_option_labels,
  ingredient_ids,
  sku_payload,
  readiness_tier
)
VALUES (
  :sku_key,
  :product_key,
  :merchant_id,
  :platform,
  :source_product_id,
  :source_variant_id,
  :sku,
  NULL,
  :title,
  :currency,
  :image_url,
  CAST(:visible_attributes AS jsonb),
  CAST(:visible_option_labels AS jsonb),
  CAST(:ingredient_ids AS jsonb),
  CAST(:sku_payload AS jsonb),
  :readiness_tier
)
ON CONFLICT (sku_key) DO NOTHING
RETURNING sku_key
"""


INSERT_REPAIR_OFFER_SQL = """
INSERT INTO catalog_offers (
  offer_id,
  sku_key,
  product_key,
  merchant_id,
  catalog_track,
  truth_tier,
  readiness_tier,
  offer_mode,
  channel,
  availability,
  inventory_quantity,
  currency,
  list_price,
  merchant_effective_price,
  estimated_best_price,
  price_confidence,
  source_system,
  source_ref,
  offer_payload
)
SELECT
  CAST(:offer_id AS text),
  CAST(:sku_key AS text),
  CAST(:product_key AS text),
  CAST(:merchant_id AS text),
  CAST(:catalog_track AS text),
  CAST(:truth_tier AS text),
  CAST(:readiness_tier AS text),
  CAST(:offer_mode AS text),
  CAST(:channel AS text),
  CAST(:availability AS text),
  NULL,
  CAST(:currency AS text),
  CAST(:list_price AS numeric),
  CAST(:merchant_effective_price AS numeric),
  CAST(:estimated_best_price AS numeric),
  CAST(:price_confidence AS numeric),
  CAST(:source_system AS text),
  CAST(:source_ref AS text),
  CAST(:offer_payload AS jsonb)
WHERE NOT EXISTS (
  SELECT 1
  FROM catalog_offers existing_offer
  WHERE existing_offer.product_key = CAST(:product_key AS text)
    AND coalesce(existing_offer.list_price, 0) > 0
)
ON CONFLICT (offer_id) DO UPDATE SET
  availability = EXCLUDED.availability,
  currency = EXCLUDED.currency,
  list_price = EXCLUDED.list_price,
  merchant_effective_price = EXCLUDED.merchant_effective_price,
  estimated_best_price = EXCLUDED.estimated_best_price,
  price_confidence = EXCLUDED.price_confidence,
  offer_payload = EXCLUDED.offer_payload,
  updated_at = NOW()
WHERE (catalog_offers.list_price IS NULL OR catalog_offers.list_price <= 0)
  AND NOT EXISTS (
    SELECT 1
    FROM catalog_offers existing_offer
    WHERE existing_offer.product_key = CAST(:product_key AS text)
      AND existing_offer.offer_id <> CAST(:offer_id AS text)
      AND coalesce(existing_offer.list_price, 0) > 0
  )
RETURNING offer_id
"""


UPDATE_CATALOG_IMAGE_SQL = """
UPDATE catalog_products
SET
  image_url = :image_url,
  product_payload = jsonb_set(
    coalesce(product_payload, '{}'::jsonb),
    '{public_source_pdp_repair}',
    CAST(:repair_metadata AS jsonb),
    true
  ),
  updated_at = NOW()
WHERE product_key = :product_key
  AND sync_status = 'live'
  AND (image_url IS NULL OR btrim(image_url) = '')
RETURNING product_key
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _record_value(record: Any, key: str) -> Any:
    if not record:
        return None
    if isinstance(record, dict):
        return record.get(key)
    try:
        return record[key]
    except Exception:
        return getattr(record, key, None)


def compact(value: Any) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip()


def _tokens(value: Any) -> List[str]:
    return TOKEN_RE.findall(str(value or "").lower())


def token_set(value: Any, *, brand: Optional[str] = None) -> Set[str]:
    brand_tokens = set(_tokens(brand))
    return {
        token
        for token in _tokens(value)
        if (len(token) > 1 or token.isdigit())
        and token not in STOPWORDS
        and token not in GENERIC_PRODUCT_TOKENS
        and token not in brand_tokens
    }


def normalized_key(value: Any, *, brand: Optional[str] = None) -> str:
    return " ".join(sorted(token_set(value, brand=brand)))


def host(value: str) -> str:
    return (urlparse(value or "").hostname or "").lower().removeprefix("www.")


def same_host_family(a: str, b: str) -> bool:
    left = host(a)
    right = host(b)
    if not left or not right:
        return False
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def _variant_token_set(value: Any, *, brand: Optional[str] = None) -> Set[str]:
    return token_set(value, brand=brand) & VARIANT_TOKENS


def title_gate(
    target_title: Any,
    extracted_title: Any,
    *,
    brand: Optional[str] = None,
    allow_source_superset: bool = False,
) -> Dict[str, Any]:
    target_tokens = token_set(target_title, brand=brand)
    source_tokens = token_set(extracted_title, brand=brand)
    target_variant_tokens = _variant_token_set(target_title, brand=brand)
    source_variant_tokens = _variant_token_set(extracted_title, brand=brand)

    if not target_tokens or not source_tokens:
        return {
            "ok": False,
            "reason": "missing_title_tokens",
            "score": 0.0,
            "exact": False,
            "target_variant_tokens": sorted(target_variant_tokens),
            "source_variant_tokens": sorted(source_variant_tokens),
        }

    intersection = target_tokens & source_tokens
    union = target_tokens | source_tokens
    jaccard = len(intersection) / max(len(union), 1)
    containment = len(intersection) / max(min(len(target_tokens), len(source_tokens)), 1)
    score = max(jaccard, containment)
    target_key = normalized_key(target_title, brand=brand)
    source_key = normalized_key(extracted_title, brand=brand)
    exact = bool(target_key and target_key == source_key)

    if target_variant_tokens != source_variant_tokens:
        if target_variant_tokens or source_variant_tokens:
            return {
                "ok": False,
                "reason": "variant_token_mismatch",
                "score": round(score, 3),
                "exact": exact,
                "target_variant_tokens": sorted(target_variant_tokens),
                "source_variant_tokens": sorted(source_variant_tokens),
            }

    source_extra_tokens = source_tokens - target_tokens
    # Opt-in safe-direction relaxation: accept when our title is FULLY contained
    # in the source (target ⊆ source) and the source's extra tokens are purely
    # descriptive. This admits "…Lotion" vs "…Lotion with Birch Sap" (feed titles
    # are abbreviated) while still rejecting single-unit → bundle / size / count
    # mismatches. Variant (shade) tokens were already required equal above.
    #
    # The pack/count check runs on RAW tokens, NOT token_set(): token_set strips
    # STOPWORDS ("set") and GENERIC_PRODUCT_TOKENS ("pack", "size", "mini",
    # "shade"), so checking the filtered set would silently miss exactly the
    # bundle words we care about ("X Twin Pack" -> filtered extras {twin}).
    raw_source_extra = (
        set(_tokens(extracted_title)) - set(_tokens(target_title)) - set(_tokens(brand))
    )
    safe_superset = (
        bool(source_extra_tokens)
        and not exact
        and allow_source_superset
        and target_tokens <= source_tokens
        # minimum specificity: a 1-2 token target is too generic to trust as a
        # subset ("Toner" ⊆ "Rice Water Bright Toner").
        and len(target_tokens) >= 3
        and not any(_is_pack_count_token(t) for t in raw_source_extra)
        and not any(ch.isdigit() for tok in raw_source_extra for ch in tok)
    )
    if source_extra_tokens and not exact and not safe_superset:
        return {
            "ok": False,
            "reason": "source_title_extra_tokens",
            "score": round(score, 3),
            "exact": exact,
            "target_variant_tokens": sorted(target_variant_tokens),
            "source_variant_tokens": sorted(source_variant_tokens),
            "source_extra_tokens": sorted(source_extra_tokens),
        }

    if exact:
        ok = True
    else:
        # A superset match still must clear the normal similarity floor — it only
        # bypasses the extra-token veto, never the score. Keeps "Lotion" vs
        # "Lotion with Birch Sap" (jaccard .71) while dropping a generic target
        # against a far more specific source.
        ok = containment >= 0.75 and jaccard >= 0.45

    return {
        "ok": ok,
        "reason": None if ok else "title_mismatch",
        "score": round(score, 3),
        "exact": exact,
        "title_superset_accepted": bool(safe_superset and ok),
        "target_variant_tokens": sorted(target_variant_tokens),
        "source_variant_tokens": sorted(source_variant_tokens),
        "source_extra_tokens": sorted(source_extra_tokens),
    }


def _safe_price_amount(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except Exception:
        return None
    if amount <= 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _variant_prices(extracted: Dict[str, Any]) -> Set[Decimal]:
    prices: Set[Decimal] = set()
    for variant in extracted.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        for key in ("price_amount", "price", "list_price", "amount"):
            amount = _safe_price_amount(variant.get(key))
            if amount is not None:
                prices.add(amount)
                break
    return prices


def price_gate(extracted: Dict[str, Any], *, market: str, exact_title: bool) -> Dict[str, Any]:
    amount = _safe_price_amount(extracted.get("price_amount"))
    if amount is None:
        return {"ok": False, "reason": "missing_or_invalid_price"}

    currency = str(extracted.get("price_currency") or "").strip().upper()
    if market.upper() == "US" and currency != "USD":
        return {"ok": False, "reason": "currency_mismatch", "currency": currency}
    if not currency:
        return {"ok": False, "reason": "missing_currency"}

    variant_prices = _variant_prices(extracted)
    if len(variant_prices) > 1 and not exact_title:
        return {
            "ok": False,
            "reason": "variant_price_ambiguous",
            "variant_prices": [str(p) for p in sorted(variant_prices)],
        }

    return {"ok": True, "amount": amount, "currency": currency}


def safe_image_url(extracted: Dict[str, Any]) -> Optional[str]:
    candidates: List[str] = []
    primary = compact(extracted.get("image_url"))
    if primary:
        candidates.append(primary)
    for value in extracted.get("image_urls") or []:
        url = compact(value)
        if url and url not in candidates:
            candidates.append(url)

    for url in candidates:
        if not url.startswith(("http://", "https://")):
            continue
        if IMAGE_BLACKLIST_RE.search(url):
            continue
        return url
    return None


def evaluate_candidate(
    row: Dict[str, Any],
    extracted: Dict[str, Any],
    *,
    market: str,
    allow_source_superset: bool = False,
) -> Dict[str, Any]:
    target_title = row.get("apv_title") or row.get("title")
    extracted_title = extracted.get("title") or ""
    title = title_gate(
        target_title, extracted_title, brand=row.get("brand"),
        allow_source_superset=allow_source_superset,
    )
    source_url = row.get("canonical_url") or ""
    extracted_canonical = extracted.get("canonical_url") or source_url
    host_ok = same_host_family(source_url, extracted_canonical)
    need_price = not bool(row.get("has_price"))
    need_image = (
        not bool(row.get("has_image"))
        and not compact(row.get("apv_image_url"))
        and not compact(row.get("cp_image_url"))
    )

    price = {"ok": False, "reason": "not_needed"}
    if need_price and title["ok"] and host_ok:
        price = price_gate(extracted, market=market, exact_title=bool(title["exact"]))
    elif need_price and not title["ok"]:
        price = {"ok": False, "reason": title["reason"]}
    elif need_price and not host_ok:
        price = {"ok": False, "reason": "canonical_host_mismatch"}

    image_url = safe_image_url(extracted)
    image = {"ok": False, "reason": "not_needed"}
    if need_image and title["ok"] and host_ok:
        image = {"ok": bool(image_url), "url": image_url, "reason": None if image_url else "missing_or_unsafe_image"}
    elif need_image and not title["ok"]:
        image = {"ok": False, "reason": title["reason"]}
    elif need_image and not host_ok:
        image = {"ok": False, "reason": "canonical_host_mismatch"}

    reject_reason = None
    if not price["ok"] and not image["ok"]:
        reject_reason = price.get("reason") if need_price else image.get("reason")
        if reject_reason == "not_needed":
            reject_reason = image.get("reason") or price.get("reason")

    return {
        "content_key": row.get("content_key"),
        "product_key": row.get("product_key"),
        "blocker_code": row.get("blocker_code"),
        "domain": host(source_url),
        "source_url": source_url,
        "canonical_url": extracted_canonical,
        "host_ok": host_ok,
        "target_title": compact(target_title)[:180],
        "extracted_title": compact(extracted_title)[:180],
        "title_score": title["score"],
        "title_exact": title["exact"],
        "title_superset_accepted": title.get("title_superset_accepted", False),
        "source_extra_tokens": title.get("source_extra_tokens"),
        "title_gate_reason": title["reason"],
        "target_variant_tokens": title["target_variant_tokens"],
        "source_variant_tokens": title["source_variant_tokens"],
        "provider": extracted.get("evidence_provider"),
        "need_price": need_price,
        "need_image": need_image,
        "price_amount": str(price.get("amount")) if price.get("amount") is not None else extracted.get("price_amount"),
        "price_currency": price.get("currency") or extracted.get("price_currency"),
        "image_url": image.get("url") or image_url,
        "image_count": len(extracted.get("image_urls") or []),
        "variant_count": len(extracted.get("variants") or []),
        "safe_price_repair": bool(price.get("ok")),
        "safe_image_repair": bool(image.get("ok")),
        "safe_any_repair": bool(price.get("ok") or image.get("ok")),
        "reject_reason": reject_reason,
        "price_reject_reason": None if price.get("ok") else price.get("reason"),
        "image_reject_reason": None if image.get("ok") else image.get("reason"),
    }


def _deterministic_id(prefix: str, *parts: Any, max_hex: int = 32) -> str:
    raw = "::".join(str(part or "") for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:max_hex]
    return f"{prefix}{digest}"


def build_candidate_query(*, limit: int, include_upstream_blockers: bool) -> Tuple[str, Dict[str, Any]]:
    limit_clause = ""
    values: Dict[str, Any] = {}
    if limit > 0:
        limit_clause = "LIMIT :limit"
        values["limit"] = limit
    blocker_scope_clause = ""
    if not include_upstream_blockers:
        blocker_scope_clause = "AND ips.blocker_code IN ('no_image', 'no_price')"
    query = CANDIDATE_QUERY.format(
        blocker_scope_clause=blocker_scope_clause,
        limit_clause=limit_clause,
    )
    return query, values


async def fetch_candidate_rows(
    *,
    limit: int,
    content_key: Optional[str],
    include_upstream_blockers: bool,
) -> List[Dict[str, Any]]:
    query, values = build_candidate_query(
        limit=limit,
        include_upstream_blockers=include_upstream_blockers,
    )
    values["content_key"] = content_key
    rows = await database.fetch_all(query, values)
    return [dict(row) for row in rows or []]


async def probe_one(
    row: Dict[str, Any], *, market: str, allow_source_superset: bool = False
) -> Dict[str, Any]:
    url = compact(row.get("canonical_url"))
    base: Dict[str, Any] = {
        "content_key": row.get("content_key"),
        "product_key": row.get("product_key"),
        "blocker_code": row.get("blocker_code"),
        "domain": host(url),
        "source_url": url,
        "target_title": compact(row.get("apv_title") or row.get("title"))[:180],
        "need_price": not bool(row.get("has_price")),
        "need_image": not bool(row.get("has_image")),
    }
    try:
        html, content_type = await _fetch_html(url, max_wait=0)
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "fetch_failed", "error": str(exc)[:200]}

    if "html" not in str(content_type).lower() and "text" not in str(content_type).lower():
        return {**base, "status": "unsupported_content_type", "content_type": content_type}

    try:
        extracted = _extract_from_html(url, html)
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "extract_failed", "error": str(exc)[:200]}

    return {
        **base,
        "status": "ok",
        **evaluate_candidate(
            row, extracted, market=market,
            allow_source_superset=allow_source_superset,
        ),
        "_row": row,
        "_extracted": extracted,
    }


async def _ensure_repair_sku(
    row: Dict[str, Any],
    *,
    currency: str,
    image_url: Optional[str],
) -> str:
    existing = await database.fetch_one(EXISTING_SKU_QUERY, {"product_key": row.get("product_key")})
    existing_sku_key = _record_value(existing, "sku_key")
    if existing_sku_key:
        return str(existing_sku_key)

    sku_key = _deterministic_id("sku:psrc:", row.get("product_key"), row.get("canonical_url"))
    source_variant_id = _deterministic_id("psrc:", row.get("product_key"), row.get("canonical_url"))
    payload = {
        "source_system": SOURCE_SYSTEM,
        "source_kind": SOURCE_KIND,
        "source_url": row.get("canonical_url"),
        "partnership": False,
        "created_for": "missing_price_or_image_repair",
    }
    inserted = await database.fetch_one(
        INSERT_REPAIR_SKU_SQL,
        {
            "sku_key": sku_key,
            "product_key": row.get("product_key"),
            "merchant_id": row.get("merchant_id"),
            "platform": row.get("platform"),
            "source_product_id": row.get("source_product_id"),
            "source_variant_id": source_variant_id,
            "sku": row.get("source_product_id"),
            "title": row.get("title") or row.get("apv_title"),
            "currency": currency,
            "image_url": image_url,
            "visible_attributes": json.dumps({}, ensure_ascii=False),
            "visible_option_labels": json.dumps([], ensure_ascii=False),
            "ingredient_ids": json.dumps([], ensure_ascii=False),
            "sku_payload": json.dumps(payload, ensure_ascii=False, default=_json_default),
            "readiness_tier": READINESS_TIER,
        },
    )
    return str(_record_value(inserted, "sku_key") or sku_key)


async def _insert_missing_offer(result: Dict[str, Any]) -> bool:
    row = result["_row"]
    amount = _safe_price_amount(result.get("price_amount"))
    currency = compact(result.get("price_currency")).upper()
    if amount is None or not currency:
        return False
    sku_key = await _ensure_repair_sku(row, currency=currency, image_url=None)
    offer_id = _deterministic_id("offer:psrc:", row.get("product_key"), row.get("canonical_url"))
    observed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "source_system": SOURCE_SYSTEM,
        "source_kind": SOURCE_KIND,
        "source_url": row.get("canonical_url"),
        "canonical_url": result.get("canonical_url"),
        "extracted_title": result.get("extracted_title"),
        "title_score": result.get("title_score"),
        "title_exact": result.get("title_exact"),
        "evidence_provider": result.get("provider"),
        "observed_at": observed_at,
        "partnership": False,
    }
    inserted = await database.fetch_one(
        INSERT_REPAIR_OFFER_SQL,
        {
            "offer_id": offer_id,
            "sku_key": sku_key,
            "product_key": row.get("product_key"),
            "merchant_id": row.get("merchant_id"),
            "catalog_track": CATALOG_TRACK,
            "truth_tier": TRUTH_TIER,
            "readiness_tier": READINESS_TIER,
            "offer_mode": OFFER_MODE,
            "channel": SOURCE_KIND,
            "availability": result.get("_extracted", {}).get("availability") or "unknown",
            "currency": currency,
            "list_price": str(amount),
            "merchant_effective_price": str(amount),
            "estimated_best_price": str(amount),
            "price_confidence": str(PRICE_CONFIDENCE),
            "source_system": SOURCE_SYSTEM,
            "source_ref": _deterministic_id("ref:", row.get("content_key"), row.get("canonical_url"), max_hex=24),
            "offer_payload": json.dumps(payload, ensure_ascii=False, default=_json_default),
        },
    )
    return bool(_record_value(inserted, "offer_id"))


async def _update_missing_catalog_image(result: Dict[str, Any]) -> bool:
    row = result["_row"]
    image_url = compact(result.get("image_url"))
    if not image_url:
        return False
    metadata = {
        "source_system": SOURCE_SYSTEM,
        "source_kind": SOURCE_KIND,
        "source_url": row.get("canonical_url"),
        "canonical_url": result.get("canonical_url"),
        "extracted_title": result.get("extracted_title"),
        "title_score": result.get("title_score"),
        "title_exact": result.get("title_exact"),
        "evidence_provider": result.get("provider"),
        "image_url": image_url,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "partnership": False,
    }
    updated = await database.fetch_one(
        UPDATE_CATALOG_IMAGE_SQL,
        {
            "product_key": row.get("product_key"),
            "image_url": image_url,
            "repair_metadata": json.dumps(metadata, ensure_ascii=False, default=_json_default),
        },
    )
    return bool(_record_value(updated, "product_key"))


async def _refresh_agent_pdp_view(content_key: str) -> bool:
    # 🚨 DO NOT reintroduce a local assemble_row + UPSERT here. This function
    # used to call assemble_row with no `evidence=` / `enrichment=` /
    # `seller_trust_by_id=`, then run the full UPSERT, which assigns every
    # column from EXCLUDED — so `--apply` NULLed the curated description,
    # bullet_points, usage_scenarios, evidence_profile and required_disclaimers
    # on every row it repaired that had them. This script targets rows with
    # missing content, a cohort that overlaps the enriched one, so it was
    # repairing one field while destroying five others.
    return await refresh_agent_pdp_view_for_content_key(
        content_key, refresh_source=SOURCE_SYSTEM, db=database
    )


async def apply_result(result: Dict[str, Any]) -> Dict[str, Any]:
    changed_price = False
    changed_image = False
    if result.get("safe_price_repair"):
        changed_price = await _insert_missing_offer(result)
    if result.get("safe_image_repair"):
        changed_image = await _update_missing_catalog_image(result)
    refreshed = False
    if changed_price or changed_image:
        refreshed = await _refresh_agent_pdp_view(str(result.get("content_key")))
    return {
        "content_key": result.get("content_key"),
        "product_key": result.get("product_key"),
        "price_written": changed_price,
        "image_written": changed_image,
        "agent_pdp_view_refreshed": refreshed,
    }


def _public_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in result.items() if not k.startswith("_")}


def _summarize(results: List[Dict[str, Any]], *, started: float, apply: bool, apply_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    status_counts = Counter(result.get("status") for result in results)
    reject_counts = Counter(
        result.get("reject_reason") or "accepted"
        for result in results
        if result.get("status") == "ok"
    )
    safe = [result for result in results if result.get("safe_any_repair")]
    safe_by_need: Dict[str, int] = defaultdict(int)
    for result in safe:
        if result.get("safe_price_repair"):
            safe_by_need["price"] += 1
        if result.get("safe_image_repair"):
            safe_by_need["image"] += 1
    return {
        "mode": "apply" if apply else "dry_run",
        "source_system": SOURCE_SYSTEM,
        "elapsed_sec": round(time.time() - started, 2),
        "rows": len(results),
        "status_counts": dict(status_counts.most_common()),
        "reject_counts": dict(reject_counts.most_common()),
        "safe_repair_count": len(safe),
        "safe_by_need": dict(safe_by_need),
        "safe_samples": [_public_result(result) for result in safe[:50]],
        "failed_samples": [
            _public_result(result)
            for result in results
            if not result.get("safe_any_repair")
        ][:50],
        "apply_results": apply_results,
    }


def _write_if_requested(path_str: Optional[str], payload: Dict[str, Any]) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="US")
    parser.add_argument("--limit", type=int, default=80, help="Candidate rows to probe; 0 means no SQL LIMIT.")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--content-key", default=None, help="Repair/probe one content_key.")
    parser.add_argument(
        "--include-upstream-blockers",
        action="store_true",
        help=(
            "Also probe rows currently blocked by earlier gates such as no_seed "
            "or low_quality. Default targets only current no_image/no_price blockers."
        ),
    )
    parser.add_argument(
        "--allow-source-superset",
        action="store_true",
        help=(
            "Accept a source page whose title fully CONTAINS our title plus only "
            "descriptive extra tokens (no digits, no pack/count words) — e.g. an "
            "abbreviated feed title vs the brand's fuller product name. Shade and "
            "pack/count mismatches still reject. Default off (strict)."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Write approved repairs. Default is dry-run.")
    parser.add_argument("--output-json", default=None)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    started = time.time()
    await database.connect()
    apply_results: List[Dict[str, Any]] = []
    try:
        rows = await fetch_candidate_rows(
            limit=args.limit,
            content_key=args.content_key,
            include_upstream_blockers=bool(args.include_upstream_blockers),
        )
        semaphore = asyncio.Semaphore(max(int(args.concurrency or 1), 1))

        async def guarded(row: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await probe_one(
                    row, market=args.market,
                    allow_source_superset=bool(args.allow_source_superset),
                )

        results = await asyncio.gather(*(guarded(row) for row in rows))
        if args.apply:
            for result in results:
                if result.get("safe_any_repair"):
                    apply_results.append(await apply_result(result))
        return _summarize(results, started=started, apply=bool(args.apply), apply_results=apply_results)
    finally:
        await database.disconnect()


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = _parse_args(argv)
    summary = asyncio.run(run(args))
    _write_if_requested(args.output_json, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default))


if __name__ == "__main__":
    main()
