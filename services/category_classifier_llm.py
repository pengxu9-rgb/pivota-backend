"""LLM-backed category classifier fallback.

Runs ONLY when the regex classifier
(`services.pdp_category_classifier.fold_category_from_variants`) returns
None — i.e., the product's title/product_type don't match any of the
~48 known regex patterns. The regex stays the fast/free path for
known categories; the LLM picks up the long tail (new merchants,
new product types) without operators having to ship a pattern PR.

Output is written to catalog_products.{category_path,
category_label_source='llm_category_v1', category_confidence}. The
gateway already reads category_path on every PDP render via the
catalog_products read-through; no downstream changes required.

Cost shape:
  - Cached per (merchant_id, normalized_product_type) within a single
    sync process — a merchant with 50 "Dog Harness" products triggers
    one LLM call, not 50.
  - Default OFF (LLM_CATEGORY_CLASSIFIER_ENABLED=true to enable).
  - Per-classification cost ≈ $0.0001 (Deepseek chat, ~300 input + ~80
    output tokens). A 600-product catalog with 10 distinct product_types
    costs ≈ $0.001 to fully classify.

Schema constraints on the LLM's output:
  - path: must match ^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*){1,4}$
    (lowercase slash-separated, 2-5 segments).
  - top-level segment: one of beauty | fashion | electronics | home |
    outdoor | sports | toys | pet | food | books | other. If the LLM
    proposes something else, we coerce to 'other' and downgrade
    confidence to 0.4.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import httpx

from config.settings import settings
from services.llm_fence import PRODUCT_DATA_FENCE

logger = logging.getLogger(__name__)


CATEGORY_SOURCE_LLM = "llm_category_v1"

# Top-level taxonomy roots we explicitly recognize. Coerce anything
# else into 'other' with downgraded confidence — better to mark
# uncertain than to invent a new top-level branch in the taxonomy.
_ALLOWED_ROOTS = {
    "beauty", "fashion", "electronics", "home", "outdoor",
    "sports", "toys", "pet", "food", "books", "other",
}

# Path shape: lowercase, slash-separated, 2-5 segments. Each segment
# is letters/digits with optional dashes/underscores.
_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*){1,4}$")


def _enabled() -> bool:
    """LLM_CATEGORY_CLASSIFIER_ENABLED feature flag. Default OFF."""
    return os.getenv("LLM_CATEGORY_CLASSIFIER_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Per-process cache: {(merchant_id, product_type_norm): (label, path, confidence)}
# A future enhancement could persist this to Redis or catalog_products
# itself, but per-process is enough to amortize repeat calls within a
# single sync run.
_cache: Dict[Tuple[str, str], Optional[Tuple[str, str, float]]] = {}


def _normalize_cache_key(merchant_id: Optional[str], product_type: Optional[str], title: Optional[str]) -> Tuple[str, str]:
    """When product_type is present, cache by it (a merchant's catalog
    usually has many products with the same product_type — e.g. 600
    'Makeup Brush' rows). When absent, fall back to the title (which
    means each title triggers its own LLM call — still bounded but
    less amortized)."""
    m = (merchant_id or "").strip()
    pt = (product_type or "").strip().lower()
    if pt:
        return (m, f"product_type::{pt}")
    return (m, f"title::{(title or '').strip().lower()[:200]}")


_SYSTEM_PROMPT = (
    "You classify e-commerce products into a hierarchical category path. "
    "Output strict JSON only. Pick the most specific path that matches "
    "the product. The first segment must be one of: beauty, fashion, "
    "electronics, home, outdoor, sports, toys, pet, food, books, other. "
    "Use lowercase slash-separated segments, 2-5 deep. Examples: "
    "'beauty/skincare/treat/serum', 'fashion/apparel/tops/sweater', "
    "'electronics/audio/headphones', 'home/kitchen/cookware/pan', "
    "'pet/accessory/leash'. If genuinely unclassifiable, return "
    "value=null and reason='insufficient_signal'. "
    + PRODUCT_DATA_FENCE.notice
)


def _build_user_message(*, category: Optional[str], product_type: Optional[str], title: Optional[str], description: Optional[str]) -> str:
    # Every value is merchant text; the field labels are ours. The block is
    # fenced as one unit and the instruction that follows stays outside it.
    clean = PRODUCT_DATA_FENCE.sanitize_text
    parts = []
    if title: parts.append(f"Title: {clean(title)}")
    if product_type: parts.append(f"Product type: {clean(product_type)}")
    if category: parts.append(f"Merchant category hint: {clean(category)}")
    if description:
        # Cap description to avoid blowing up the prompt — first 500 chars
        # is plenty for classification.
        snippet = clean(description[:500].replace("\n", " "))
        parts.append(f"Description: {snippet}")
    body = PRODUCT_DATA_FENCE.wrap("\n".join(parts) if parts else "(no signal)")
    return (
        f"{body}\n\n"
        "Return JSON only: "
        '{"label": short_human_label, "path": "lowercase/slash/path", '
        '"confidence": float in [0,1], "reason": short_string}'
    )


async def _call_deepseek_classify(*, user_message: str, timeout_s: float = 15.0) -> Optional[Dict[str, Any]]:
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("llm_category.no_api_key")
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 120,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("llm_category.transport_fail err=%s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning("llm_category.http_%d body=%s", resp.status_code, resp.text[:200])
        return None
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("llm_category.parse_fail err=%s", exc)
        return None


def _validate_path(raw_path: Any) -> Optional[str]:
    """Reject anything not matching the path shape. Return validated
    lowercase path or None."""
    if not isinstance(raw_path, str):
        return None
    path = raw_path.strip().lower().strip("/")
    if not path or not _PATH_RE.match(path):
        return None
    return path


async def classify_via_llm(
    *,
    merchant_id: Optional[str] = None,
    category: Optional[str] = None,
    product_type: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[Tuple[str, str, float]]:
    """Return (label, path, confidence) or None.
    Bails immediately when:
      - LLM_CATEGORY_CLASSIFIER_ENABLED is not set
      - no LLM signal (all inputs empty)
      - cached null entry exists for this (merchant, product_type)
    """
    if not _enabled():
        return None
    if not any([category, product_type, title, description]):
        return None
    cache_key = _normalize_cache_key(merchant_id, product_type, title)
    if cache_key in _cache:
        return _cache[cache_key]
    user_msg = _build_user_message(
        category=category, product_type=product_type,
        title=title, description=description,
    )
    raw = await _call_deepseek_classify(user_message=user_msg)
    if not isinstance(raw, dict):
        _cache[cache_key] = None
        return None
    path = _validate_path(raw.get("path"))
    if not path:
        _cache[cache_key] = None
        return None
    label = (raw.get("label") if isinstance(raw.get("label"), str) else "").strip()
    if not label:
        label = path.split("/")[-1].replace("-", " ").replace("_", " ").title()
    self_report = raw.get("confidence")
    confidence_n = (
        float(self_report)
        if isinstance(self_report, (int, float)) and 0.0 <= float(self_report) <= 1.0
        else 0.5
    )
    # If the LLM proposed a top-level segment outside the recognized
    # taxonomy, coerce to 'other' and downgrade — protects the taxonomy
    # tree from accidentally growing new branches.
    root = path.split("/", 1)[0]
    if root not in _ALLOWED_ROOTS:
        path = "other/" + path
        confidence_n = min(confidence_n, 0.4)
    result = (label, path, round(confidence_n, 4))
    _cache[cache_key] = result
    return result


def _invalidate_cache_for_tests() -> None:
    """Test helper — clears the in-process cache."""
    _cache.clear()
