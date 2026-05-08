"""Stage 2 — Gemini-backed URL validator for catalog enrichment.

Calls Gemini-2.5-flash with Google Search grounding to find the
canonical product page URL for each PDP candidate across the merchant
domains the candidate file specifies.

Production mode: reads GEMINI_API_KEY (or PIVOTA_GEMINI_API_KEY) from
env and hits the public generativeContent endpoint over httpx.

Local mode: when no key is configured, returns a deterministic mock so
the pipeline is end-to-end testable without network. The mock builds a
plausible canonical_url from `https://{first_expected_domain}/products/
{slugified product_name}` so Stage 3 ingestion can still produce row
dicts during dry-run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("catalog_enrichment_agent.gemini_url_validator")

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_RETRIES = 1
DEFAULT_MAX_OUTPUT_TOKENS = 4096
MAX_DROP_DETAIL_CHARS = 500
RETRYABLE_HTTP_STATUSES = {429, 503, 504}
NO_RETRY_HTTP_STATUSES = {400, 401, 403, 404}


def _truncate_detail(value: Any, *, limit: int = MAX_DROP_DETAIL_CHARS) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _drop_result(reason: str, detail: Any = "") -> Dict[str, Any]:
    return {
        "offers": [],
        "validation_drop_reason": str(reason or "unknown_drop"),
        "validation_drop_detail": _truncate_detail(detail),
    }


def _envelope(
    pdp_payload: Dict[str, Any],
    offers: Optional[List[Dict[str, Any]]] = None,
    *,
    drop_reason: Optional[str] = None,
    drop_detail: Any = "",
    attempts: int = 1,
    retried: bool = False,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "pdp": pdp_payload,
        "offers": offers or [],
        "validation_attempts": max(1, int(attempts or 1)),
        "validation_retried": bool(retried),
    }
    if not out["offers"]:
        out["validation_drop_reason"] = drop_reason or "no_offers"
        out["validation_drop_detail"] = _truncate_detail(drop_detail)
    return out


def _is_retryable_drop_reason(reason: Optional[str]) -> bool:
    return str(reason or "") in {
        "http_body_not_json",
        "gemini_json_no_balanced_block",
        "gemini_json_decode_failed",
    }


def _slugify(text: Optional[str]) -> str:
    if not text:
        return ""
    out = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return out


def _resolve_api_key() -> Optional[str]:
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _build_prompt(candidate: Dict[str, Any]) -> str:
    brand = candidate.get("brand") or ""
    product_name = candidate.get("product_name") or ""
    domains = candidate.get("expected_url_domains") or []
    attribute_summary = candidate.get("attribute_summary") or ""
    domain_list = "\n".join(f"- {d}" for d in domains)
    return f"""You are a catalog enrichment agent for an e-commerce search system.
Your task: for the product below, find its canonical product page URL on each
of the listed merchant domains. Use Google Search to verify the page exists
and is the correct product (not a category page, not a discontinued listing).

Product:
- Brand: {brand}
- Product name: {product_name}
- Attributes: {attribute_summary}

Merchant domains to check:
{domain_list}

Return a single JSON object with this exact shape (no markdown, no prose):

{{
  "offers": [
    {{
      "merchant_inferred": "<merchant name (e.g. 'Sephora', 'MAC')>",
      "domain": "<domain you actually found the URL on>",
      "canonical_url": "<the product detail page URL>",
      "image_url": "<a representative product image URL, or empty string>",
      "price": <number or null>,
      "currency": "USD",
      "in_stock": <true | false | null when uncertain>,
      "confidence": <0.0 to 1.0>,
      "notes": "<one-line note: page title or 'redirected to category' or 'not found'>"
    }}
  ]
}}

Rules:
- Include ONE entry per merchant domain you successfully validated. Skip
  domains where the product is not found or the page redirects to a
  category / unrelated PDP.
- canonical_url MUST be the product page (PDP), not a search-result URL.
- If price is hidden behind login or out-of-stock, set price=null.
- DO NOT invent URLs. Only return what Google Search actually surfaces.
- The "offers" array can be empty if the product is genuinely not found
  on any listed domain.
"""


def _mock_validation(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic offline output. Picks the first expected domain and
    builds a plausible product page URL from the slug."""
    domains = candidate.get("expected_url_domains") or []
    brand = candidate.get("brand") or ""
    product_name = candidate.get("product_name") or ""
    if not domains or not brand or not product_name:
        missing = []
        if not domains:
            missing.append("expected_url_domains")
        if not brand:
            missing.append("brand")
        if not product_name:
            missing.append("product_name")
        return _drop_result("missing_input", f"missing {', '.join(missing)}")
    primary_domain = domains[0]
    slug = _slugify(f"{brand} {product_name}")
    canonical_url = f"https://{primary_domain}/products/{slug}"
    return {
        "offers": [
            {
                "merchant_inferred": brand,
                "domain": primary_domain,
                "canonical_url": canonical_url,
                "image_url": "",
                "price": None,
                "currency": "USD",
                "in_stock": None,
                "confidence": 0.3,
                "notes": "mock_no_gemini_key",
            }
        ]
    }


def _parse_gemini_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the JSON object the model emitted from the response. The
    grounding-tool response shape nests text under
    candidates[0].content.parts[*].text. We strip ```json fences too."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return _drop_result("gemini_no_candidates", "Gemini response had no candidates")
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text_parts: List[str] = []
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(text)
    raw = "\n".join(text_parts).strip()
    if not raw:
        return _drop_result("gemini_no_text_parts", "Gemini candidate content had no text parts")
    # Strip ```json ... ``` fence if present.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Find first balanced { ... } block.
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return _drop_result("gemini_json_no_balanced_block", raw[:MAX_DROP_DETAIL_CHARS])
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return _drop_result("gemini_json_decode_failed", match.group(0)[:MAX_DROP_DETAIL_CHARS])
    if not isinstance(parsed, dict):
        return _drop_result("gemini_response_not_dict", type(parsed).__name__)
    if not isinstance(parsed.get("offers"), list):
        return _drop_result("gemini_offers_not_list", "parsed offers field was not a list")
    if len(parsed.get("offers") or []) == 0:
        parsed["validation_drop_reason"] = "gemini_offers_empty"
        parsed["validation_drop_detail"] = "Gemini returned a valid JSON object with offers=[]"
    return parsed


async def validate_candidate(
    candidate: Dict[str, Any],
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    use_grounding: bool = True,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> Dict[str, Any]:
    """Validate one candidate. Returns a record in the validated.jsonl
    shape: {pdp: {...candidate fields...}, offers: [...validated offers]}.

    `api_key` defaults to GEMINI_API_KEY / PIVOTA_GEMINI_API_KEY env. When
    no key is available, returns a deterministic mock so the pipeline
    stays runnable end-to-end."""
    pdp_payload = {
        "brand": candidate.get("brand"),
        "product_name": candidate.get("product_name"),
        "category_path": candidate.get("category_path"),
        "attribute_summary": candidate.get("attribute_summary"),
    }
    resolved_key = api_key if api_key is not None else _resolve_api_key()
    if not resolved_key:
        result = _mock_validation(candidate)
        result_offers = [
            {**offer, "validated_at": datetime.now(timezone.utc).isoformat()}
            for offer in result.get("offers", [])
        ]
        return _envelope(
            pdp_payload,
            result_offers,
            drop_reason=result.get("validation_drop_reason"),
            drop_detail=result.get("validation_drop_detail"),
        )

    prompt = _build_prompt(candidate)
    request_body: Dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": max(256, int(max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS)),
        },
    }
    if use_grounding:
        request_body["tools"] = [{"google_search": {}}]

    url = f"{base_url}/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": resolved_key}

    safe_max_retries = max(0, int(max_retries or 0))
    attempt = 0
    current_timeout_s = max(1.0, float(timeout_s or DEFAULT_TIMEOUT_S))
    last_drop: Dict[str, Any] = _drop_result("unknown_drop", "")

    while attempt <= safe_max_retries:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=current_timeout_s) as client:
                response = await client.post(url, headers=headers, json=request_body)
        except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
            last_drop = _drop_result("http_timeout", str(exc))
            if attempt <= safe_max_retries:
                logger.warning(
                    "gemini validation timed out for %s; retrying attempt=%s/%s timeout_s=%.1f",
                    candidate.get("product_name"),
                    attempt + 1,
                    safe_max_retries + 1,
                    current_timeout_s * 1.5,
                )
                current_timeout_s *= 1.5
                await asyncio.sleep(1.0)
                continue
            return _envelope(
                pdp_payload,
                [],
                drop_reason=last_drop["validation_drop_reason"],
                drop_detail=last_drop["validation_drop_detail"],
                attempts=attempt,
                retried=attempt > 1,
            )

        if response.status_code != 200:
            reason = f"http_status_{response.status_code}"
            last_drop = _drop_result(reason, response.text[:MAX_DROP_DETAIL_CHARS])
            logger.warning(
                "gemini validation failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            should_retry = (
                response.status_code in RETRYABLE_HTTP_STATUSES
                and response.status_code not in NO_RETRY_HTTP_STATUSES
                and attempt <= safe_max_retries
            )
            if should_retry:
                sleep_s = 2.0 if response.status_code == 429 else 1.0
                logger.warning(
                    "gemini validation retrying HTTP %s for %s attempt=%s/%s sleep_s=%.1f",
                    response.status_code,
                    candidate.get("product_name"),
                    attempt + 1,
                    safe_max_retries + 1,
                    sleep_s,
                )
                await asyncio.sleep(sleep_s)
                continue
            return _envelope(
                pdp_payload,
                [],
                drop_reason=last_drop["validation_drop_reason"],
                drop_detail=last_drop["validation_drop_detail"],
                attempts=attempt,
                retried=attempt > 1,
            )

        try:
            payload = response.json()
        except json.JSONDecodeError:
            logger.warning("gemini returned non-json: %s", response.text[:200])
            last_drop = _drop_result("http_body_not_json", response.text[:200])
            if attempt <= safe_max_retries:
                logger.warning(
                    "gemini validation retrying non-json body for %s attempt=%s/%s",
                    candidate.get("product_name"),
                    attempt + 1,
                    safe_max_retries + 1,
                )
                await asyncio.sleep(1.0)
                continue
            return _envelope(
                pdp_payload,
                [],
                drop_reason=last_drop["validation_drop_reason"],
                drop_detail=last_drop["validation_drop_detail"],
                attempts=attempt,
                retried=attempt > 1,
            )

        parsed = _parse_gemini_response(payload)
        timestamp = datetime.now(timezone.utc).isoformat()
        validated_offers: List[Dict[str, Any]] = []
        for offer in parsed.get("offers", []):
            if not isinstance(offer, dict):
                continue
            canonical_url = str(offer.get("canonical_url") or "").strip()
            if not canonical_url:
                continue
            validated_offers.append({
                "merchant_inferred": str(offer.get("merchant_inferred") or "").strip(),
                "domain": str(offer.get("domain") or "").strip(),
                "canonical_url": canonical_url,
                "destination_url": canonical_url,
                "image_url": str(offer.get("image_url") or "").strip(),
                "price": offer.get("price") if isinstance(offer.get("price"), (int, float)) else None,
                "currency": str(offer.get("currency") or "USD").strip().upper(),
                "in_stock": offer.get("in_stock") if isinstance(offer.get("in_stock"), bool) else None,
                "confidence": (
                    float(offer["confidence"])
                    if isinstance(offer.get("confidence"), (int, float))
                    else 0.5
                ),
                "notes": str(offer.get("notes") or "").strip(),
                "validated_at": timestamp,
            })
        if validated_offers:
            return _envelope(
                pdp_payload,
                validated_offers,
                attempts=attempt,
                retried=attempt > 1,
            )

        last_drop = _drop_result(
            parsed.get("validation_drop_reason") or "gemini_offers_empty",
            parsed.get("validation_drop_detail") or "Gemini returned no usable offers",
        )
        if _is_retryable_drop_reason(last_drop["validation_drop_reason"]) and attempt <= safe_max_retries:
            logger.warning(
                "gemini validation retrying parse drop reason=%s product=%s attempt=%s/%s",
                last_drop["validation_drop_reason"],
                candidate.get("product_name"),
                attempt + 1,
                safe_max_retries + 1,
            )
            await asyncio.sleep(1.0)
            continue
        return _envelope(
            pdp_payload,
            [],
            drop_reason=last_drop["validation_drop_reason"],
            drop_detail=last_drop["validation_drop_detail"],
            attempts=attempt,
            retried=attempt > 1,
        )

    return _envelope(
        pdp_payload,
        [],
        drop_reason=last_drop.get("validation_drop_reason") or "unknown_drop",
        drop_detail=last_drop.get("validation_drop_detail") or "",
        attempts=attempt,
        retried=attempt > 1,
    )
