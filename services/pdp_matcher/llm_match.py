"""Phase 3B — LLM tail matcher for external_product_seeds rows that the
deterministic matchers (Phase 3A) couldn't link.

For each unmatched seed:
  1. Caller (runner) fetches top-N catalog_products candidates by lower
     trigram similarity threshold (e.g. ≥0.5) — wider net than the
     0.85-cutoff used in Phase 3A.
  2. This module calls gemini-2.5-flash with the seed + the candidates
     and asks the model to pick the best match (or "none").
  3. Returns the chosen product_key + confidence + reasoning, or None
     when the model's confidence is below MIN_LLM_MATCH_CONFIDENCE.

Mirrors the Stage 2 validator's HTTP shape (httpx + GEMINI_API_KEY) so
both LLM-touching modules age together.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import httpx
from services import vertex_gemini

logger = logging.getLogger("pdp_matcher.llm_match")

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 30.0
MIN_LLM_MATCH_CONFIDENCE = 0.7
MAX_CANDIDATES_PER_CALL = 12


def _resolve_api_key(provided: Optional[str]) -> Optional[str]:
    if provided is not None:
        return provided.strip() or None
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    return None


def _seed_summary(seed: Dict[str, Any]) -> str:
    bits: List[str] = []
    title = (seed.get("title") or "").strip()
    if title:
        bits.append(f"title={title!r}")
    domain = (seed.get("domain") or "").strip()
    if domain:
        bits.append(f"domain={domain}")
    canonical = (seed.get("canonical_url") or "").strip()
    if canonical:
        bits.append(f"canonical_url={canonical}")
    elif (dest := (seed.get("destination_url") or "").strip()):
        bits.append(f"destination_url={dest}")
    seed_data = seed.get("seed_data")
    if isinstance(seed_data, dict):
        brand = seed_data.get("brand") or seed_data.get("brand_name")
        if brand:
            bits.append(f"brand={brand!r}")
    return ", ".join(bits)


def _candidate_summary(idx: int, cand: Dict[str, Any]) -> str:
    return (
        f"  [{idx}] product_key={cand.get('product_key')!r} "
        f"title={cand.get('title') or ''!r} "
        f"brand={cand.get('brand') or ''!r} "
        f"canonical_url={cand.get('canonical_url') or ''!r}"
    )


def _build_prompt(*, seed: Dict[str, Any], candidates: Sequence[Dict[str, Any]]) -> str:
    cand_block = "\n".join(_candidate_summary(i, c) for i, c in enumerate(candidates))
    return f"""You are a catalog dedupe agent. Decide whether an external referral
seed row is the SAME product as one of the catalog PDP candidates listed
below, or none of them.

The seed row:
  {_seed_summary(seed)}

Candidate PDPs (numbered 0..N-1):
{cand_block}

Return a single JSON object (no markdown, no prose):

{{
  "match_index": <integer 0..N-1, or -1 if none of the candidates is the same product>,
  "product_key": "<the chosen candidate's product_key, or empty string when match_index=-1>",
  "confidence": <0.0..1.0 — how sure you are>,
  "reasoning": "<one short sentence>"
}}

Rules:
- Only return match_index >= 0 when the seed and the candidate clearly
  describe the SAME product (same brand, same product line, same
  variant if specified). Different shades / sizes of the same product
  line still count as the same PDP.
- If two or more candidates look equally plausible, pick match_index=-1
  (let a human review).
- confidence must reflect actual certainty: 0.9+ for unambiguous,
  0.7-0.9 for likely, <0.7 for unsure (caller will reject below 0.7).
- DO NOT invent a product_key — copy it verbatim from the chosen candidate.
"""


def _mock_match(*, seed: Dict[str, Any], candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Offline behavior: don't fabricate matches. The deterministic matcher
    already did a thorough job; without an LLM key we should NOT add false
    positives. Returns None so the caller defers."""
    return None


def _parse_response_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _validate_match(
    parsed: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Coerce the model's reply into a confident match dict, or None."""
    try:
        match_index = int(parsed.get("match_index", -1))
    except (TypeError, ValueError):
        return None
    if match_index < 0 or match_index >= len(candidates):
        return None
    confidence_raw = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        return None
    if confidence < MIN_LLM_MATCH_CONFIDENCE:
        return None
    chosen = candidates[match_index]
    expected_key = chosen.get("product_key")
    returned_key = (parsed.get("product_key") or "").strip()
    # Trust match_index over the model's free-text product_key — but if
    # the model echoed a bogus product_key, log it and use the candidate
    # at match_index. Reduces hallucination surface.
    if returned_key and returned_key != str(expected_key):
        logger.warning(
            "llm_match: model returned product_key=%s but match_index=%s points to %s — using candidate",
            returned_key,
            match_index,
            expected_key,
        )
    reasoning = str(parsed.get("reasoning") or "").strip()
    return {
        "product_key": expected_key,
        "confidence": confidence,
        "matcher": "llm_match_v1",
        "evidence": (reasoning or f"llm_match_index={match_index}")[:200],
        "model_match_index": match_index,
    }


async def llm_match_seed(
    *,
    seed: Dict[str, Any],
    candidates: Sequence[Dict[str, Any]],
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Optional[Dict[str, Any]]:
    """Match a single seed against a candidate list via gemini-2.5-flash.

    Returns the same shape as the deterministic matchers: {product_key,
    confidence, matcher='llm_match_v1', evidence, model_match_index}.
    Returns None when:
      - candidates list is empty
      - no API key available (offline mode is intentionally pessimistic;
        the deterministic matcher already covered the safe cases)
      - model returns -1 / low-confidence / unparseable JSON / HTTP error
    """
    if not candidates:
        return None
    cand_subset = list(candidates)[:MAX_CANDIDATES_PER_CALL]

    resolved_key = _resolve_api_key(api_key)
    if not vertex_gemini.credentials_available(resolved_key):
        return _mock_match(seed=seed, candidates=cand_subset)

    prompt = _build_prompt(seed=seed, candidates=cand_subset)
    request_body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 512},
    }
    url = vertex_gemini.generate_content_url(model, base_url=base_url)
    headers = await vertex_gemini.auth_headers(resolved_key)

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(url, headers=headers, json=request_body)
        if response.status_code != 200:
            logger.warning(
                "llm_match: gemini http=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return None
        try:
            payload = response.json()
        except json.JSONDecodeError:
            logger.warning("llm_match: gemini returned non-json: %s", response.text[:200])
            return None

    parsed = _parse_response_payload(payload)
    if parsed is None:
        return None
    match = _validate_match(parsed, cand_subset)
    if match is None:
        return None
    match["validated_at"] = datetime.now(timezone.utc).isoformat()
    return match
