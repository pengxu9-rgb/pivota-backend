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

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from services import vertex_gemini

logger = logging.getLogger("catalog_enrichment_agent.gemini_url_validator")

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 45.0


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
        return {"offers": []}
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
        return {"offers": []}
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text_parts: List[str] = []
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(text)
    raw = "\n".join(text_parts).strip()
    if not raw:
        return {"offers": []}
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
            return {"offers": []}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {"offers": []}
    if not isinstance(parsed, dict):
        return {"offers": []}
    if not isinstance(parsed.get("offers"), list):
        parsed["offers"] = []
    return parsed


async def validate_candidate(
    candidate: Dict[str, Any],
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    use_grounding: bool = True,
) -> Dict[str, Any]:
    """Validate one candidate. Returns a record in the validated.jsonl
    shape: {pdp: {...candidate fields...}, offers: [...validated offers]}.

    `api_key` defaults to GEMINI_API_KEY / PIVOTA_GEMINI_API_KEY env. When
    no key is available, returns a deterministic mock so the pipeline
    stays runnable end-to-end."""
    # W2: the seller of record derives from the PDP's source_domain, so the
    # validator must carry it. The candidate's PRIMARY expected domain is the
    # deterministic source (one per candidate — never offer-order dependent).
    _expected = candidate.get("expected_url_domains") or []
    _primary_domain = str(_expected[0] if _expected else "").strip().lower().removeprefix("www.")
    pdp_payload = {
        "brand": candidate.get("brand"),
        "product_name": candidate.get("product_name"),
        "category_path": candidate.get("category_path"),
        "attribute_summary": candidate.get("attribute_summary"),
        "source_domain": _primary_domain or None,
    }
    resolved_key = api_key if api_key is not None else _resolve_api_key()
    if not vertex_gemini.credentials_available(resolved_key):
        result = _mock_validation(candidate)
        result_offers = [
            {**offer, "validated_at": datetime.now(timezone.utc).isoformat()}
            for offer in result.get("offers", [])
        ]
        return {"pdp": pdp_payload, "offers": result_offers}

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
            "maxOutputTokens": 1024,
        },
    }
    if use_grounding:
        request_body["tools"] = [{"google_search": {}}]

    url = vertex_gemini.generate_content_url(model, base_url=base_url)
    headers = await vertex_gemini.auth_headers(resolved_key)

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(url, headers=headers, json=request_body)
        if response.status_code != 200:
            logger.warning(
                "gemini validation failed: status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            return {"pdp": pdp_payload, "offers": []}
        try:
            payload = response.json()
        except json.JSONDecodeError:
            logger.warning("gemini returned non-json: %s", response.text[:200])
            return {"pdp": pdp_payload, "offers": []}

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
    return {"pdp": pdp_payload, "offers": validated_offers}
