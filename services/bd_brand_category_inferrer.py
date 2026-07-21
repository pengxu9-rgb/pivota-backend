"""Brand-level category inference for BD cold-start audits.

When a cold-start target's source data is missing `product_type` (very
common for D2C Shopify merchants who don't fill in that field), the
audit pipeline skips `category_visibility_test` because it can't
generate meaningful queries like "best X 2026" without a category
phrase.

This module synthesizes that missing category at the BRAND level — one
Gemini call per audit, applied to every product lacking a real
product_type. Brand-level (not per-product) is the right resolution:
- Most D2C cold-start targets sell a single category focus
  (gruns = kids multivitamins, BeautyOfJoseon = K-beauty skincare).
- Per-product inference would multiply LLM calls by N and add
  meaningful latency.

Honesty rules:
- Returns None when the API key isn't configured or the call fails —
  caller falls back to skipping category test, NOT fabricating one.
- The inferred category is a buyer-query-ready phrase
  ("kids multivitamins", "sleepwear", "K-beauty face masks"), not a
  taxonomy path. Designed to slot into queries like
  "best <category> 2026" the audit pipeline emits.

Mirrors the gemini_url_validator HTTP pattern (httpx, no SDK
dependency) — keeps pivota-backend free of google-genai.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

import httpx
from services import vertex_gemini

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 15.0


def _resolve_api_key() -> Optional[str]:
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _build_prompt(
    brand: str,
    domain: Optional[str],
    sample_titles: List[str],
) -> str:
    titles_block = "\n".join(f"- {t}" for t in sample_titles if t)
    domain_line = f"\nWebsite: {domain}" if domain else ""
    return f"""You classify D2C brands into a single product category for buyer-intent search queries.

Brand: {brand}{domain_line}
Sample products:
{titles_block}

Reply with ONLY a single category phrase (2-4 words, plural-ready) suitable for slotting into queries like:
  "best <CATEGORY> 2026"
  "top <CATEGORY> reviews"
  "what <CATEGORY> do experts recommend"

Examples of good outputs:
  kids multivitamins
  K-beauty face masks
  women's sleepwear
  cold-brew coffee
  natural deodorants
  pet food

Bad outputs (do NOT use):
  bundle             (too generic — every store has bundles)
  general merchandise (meaningless category)
  Grüns Adult Daily  (a product name, not a category)
  health & wellness  (too broad)

Reply with the category phrase ONLY. No prose, no quotes, no JSON, no explanation.
"""


def _parse_response(payload: dict) -> Optional[str]:
    """Extract the category phrase from Gemini's response. The model
    sometimes wraps in quotes or adds a trailing period — clean those."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text_parts: List[str] = []
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())
    if not text_parts:
        return None
    raw = " ".join(text_parts).strip()
    # Strip surrounding quotes / backticks / trailing punctuation.
    raw = raw.strip("\"'`. \n\t")
    # Reject obvious failure modes — if the model returned a sentence,
    # don't trust it. A real category is short.
    if not raw or len(raw) > 60 or "\n" in raw:
        logger.info(
            "bd_brand_category_inferrer: rejecting overlong/multiline "
            "response: %r", raw[:80],
        )
        return None
    # Strip leading "Category:" / "Answer:" prefixes if model added one.
    raw = re.sub(r"^(category|answer|result)\s*[:\-]\s*", "", raw, flags=re.IGNORECASE)
    raw = raw.strip()
    return raw or None


async def infer_brand_category(
    brand: str,
    sample_titles: List[str],
    *,
    domain: Optional[str] = None,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Optional[str]:
    """Infer a single category phrase for a brand from its name +
    sample product titles. Returns the phrase (e.g. "kids multivitamins")
    or None when no key is configured / call failed / response was
    unparseable.

    One LLM call per audit. The caller applies the result to every
    product lacking a real product_type."""
    if not brand or not brand.strip():
        return None
    cleaned_titles = [t.strip() for t in (sample_titles or []) if t and t.strip()]
    if not cleaned_titles:
        return None

    resolved_key = api_key if api_key is not None else _resolve_api_key()
    if not resolved_key:
        logger.info(
            "bd_brand_category_inferrer: no GEMINI_API_KEY configured; "
            "skipping inference for brand=%r", brand,
        )
        return None

    prompt = _build_prompt(brand.strip(), domain, cleaned_titles[:6])
    request_body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 64,
        },
    }
    url = vertex_gemini.generate_content_url(model, base_url=base_url)
    headers = await vertex_gemini.auth_headers(resolved_key)

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, headers=headers, json=request_body)
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        logger.warning(
            "bd_brand_category_inferrer: HTTP error for brand=%r: %s",
            brand, exc,
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "bd_brand_category_inferrer: non-200 for brand=%r: status=%s body=%s",
            brand, response.status_code, response.text[:300],
        )
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        logger.warning(
            "bd_brand_category_inferrer: non-JSON response for brand=%r",
            brand,
        )
        return None

    inferred = _parse_response(payload)
    if inferred:
        logger.info(
            "bd_brand_category_inferrer: brand=%r → category=%r",
            brand, inferred,
        )
    return inferred
