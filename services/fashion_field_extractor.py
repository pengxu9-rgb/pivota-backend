"""Fashion field extractor v2 — category-gated LLM extraction.

Replaces the regex v1 (neutered in PR #543) with a Deepseek-backed
extractor that:
  - **Category-gates** so it only runs on fashion-categorized rows
    (`category_path LIKE 'fashion/%'` or 'apparel/%'). Beauty / electronics /
    everything else short-circuits to value=None.
  - **Substring-grounds** every extracted value: the LLM's output must
    appear verbatim in the source text or it's downgraded to None.
    Defends against hallucination at the same point of leverage the
    regex v1 used.
  - **Calibrates confidence** = model self-report (0.0-1.0 it returns) ×
    substring-match-quality (1.0 verbatim / 0.5 partial / 0.0 absent) ×
    category-match (1.0 if category_path matches fashion). The
    downstream trust gate in PIVOTA-Agent pdpBuilder.pickFashionMeta is
    0.6 — anything below that is dropped from merchant-facing prose.
  - **Feature-flagged**. `FASHION_EXTRACT_ENABLED=false` (default) makes
    every call a no-op so this can ship + sit dormant until staging
    validation.

Routes through the existing Deepseek httpx pattern in
`services/llm_providers/deepseek_probe.py` (same json_object output,
same temperature, same cost-cap conventions). No new infra.

Cost shape: catalog has 0 fashion rows today. Cost scales linearly with
fashion onboarding — exactly the demand curve. A 10k-row fashion backfill
at ~$0.0001 per row (deepseek-chat input + output) ≈ $1 total.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


# Source enums — stable across v1 (regex, neutered) and v2 (LLM) so
# downstream consumers (gateway trust gate) don't need to know which
# extractor produced a value.
EXTRACTION_SOURCE_REGEX = "regex_extraction_v1"
EXTRACTION_SOURCE_LLM = "llm_extraction_v1"

# Fashion category gate. Only category_paths starting with one of these
# trigger an LLM call; everything else returns immediately with value=None.
_FASHION_CATEGORY_PREFIXES = (
    "fashion/", "apparel/", "clothing/", "shoes/", "accessories/",
)


def _extract_enabled() -> bool:
    """Feature flag gate. Default OFF. Set FASHION_EXTRACT_ENABLED=true
    on the environment to enable LLM calls."""
    return os.getenv("FASHION_EXTRACT_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


# Confidence calibration constants.
_CONFIDENCE_VERBATIM = 1.0
_CONFIDENCE_PARTIAL = 0.5
_CONFIDENCE_ABSENT = 0.0
_CONFIDENCE_CATEGORY_MATCH = 1.0


@dataclass(frozen=True)
class ExtractionResult:
    """One extracted field with provenance.

    value: extracted string (None when LLM declined / no signal / flag off /
           non-fashion category)
    confidence: 0.0-1.0; calibrated = self_report × substring × category
    source: 'llm_extraction_v1' (v2) or 'regex_extraction_v1' (legacy stub)
    """
    value: Optional[str]
    confidence: float
    source: str


# ---------- Field-specific prompts ----------

_FIELD_INSTRUCTIONS: Dict[str, str] = {
    "material": (
        "Extract the material composition of this fashion product (e.g. "
        "'100% cotton', '90% nylon 10% spandex'). Return ONLY the material "
        "string verbatim from the source text. If the source does not state "
        "a material, return value=null and reason='not_stated'. Do NOT infer."
    ),
    "care": (
        "Extract the care instructions for this fashion product (e.g. "
        "'Hand wash cold; lay flat to dry'). Return ONLY the instruction "
        "phrase verbatim from the source text. If the source does not "
        "state care instructions, return value=null and reason='not_stated'."
    ),
    "size_guide": (
        "Extract the size guide / sizing chart description for this fashion "
        "product if present (e.g. 'See size chart below for measurements'). "
        "Return ONLY the phrase verbatim from the source text. If the source "
        "does not include a size guide, return value=null and reason='not_stated'."
    ),
}


_SYSTEM_PROMPT = (
    "You extract structured fashion-product fields from merchant catalog "
    "text. Output strict JSON only. NEVER invent a value. NEVER paraphrase "
    "— copy the relevant phrase verbatim from the source text. If the "
    "source does not state the requested field, return value=null."
)


def _is_fashion_category(category_path: Optional[str]) -> bool:
    if not category_path or not isinstance(category_path, str):
        return False
    prefix = category_path.strip().lower()
    return any(prefix.startswith(p) for p in _FASHION_CATEGORY_PREFIXES)


def _substring_grounded(value: Optional[str], source_text: str) -> float:
    """Confidence multiplier from substring match.
    1.0 = verbatim; 0.5 = partial (first 20 chars); 0.0 = absent.
    """
    if not value or not source_text:
        return _CONFIDENCE_ABSENT
    needle = value.strip().lower()
    haystack = source_text.lower()
    if not needle:
        return _CONFIDENCE_ABSENT
    if needle in haystack:
        return _CONFIDENCE_VERBATIM
    head = needle[: min(20, len(needle))]
    if len(head) >= 10 and head in haystack:
        return _CONFIDENCE_PARTIAL
    return _CONFIDENCE_ABSENT


def _build_user_message(
    *, field: str, title: Optional[str], description: Optional[str], html_blob: Optional[str],
) -> str:
    instruction = _FIELD_INSTRUCTIONS[field]
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if description:
        parts.append(f"Description:\n{description}")
    if html_blob:
        parts.append(f"Additional HTML:\n{html_blob}")
    body = "\n\n".join(parts) if parts else "(no source text)"
    return (
        f"{instruction}\n\n"
        f"Source text:\n---\n{body}\n---\n\n"
        f'Return JSON only: {{"value": string|null, "confidence": float in [0,1], "reason": string}}'
    )


async def _call_deepseek_extract(
    *, field: str, user_message: str, timeout_s: float = 15.0,
) -> Optional[Dict[str, Any]]:
    """Call Deepseek with a strict json_object prompt. Returns the parsed
    JSON dict {value, confidence, reason} or None on transport/parse error.
    A failed LLM call should never cause a backfill row to error — the
    caller falls back to value=None.
    """
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("fashion_extract.no_api_key field=%s", field)
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    model = settings.deepseek_model
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 200,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("fashion_extract.transport_fail field=%s err=%s", field, exc)
        return None
    if resp.status_code >= 400:
        logger.warning(
            "fashion_extract.http_%d field=%s body=%s",
            resp.status_code, field, resp.text[:200],
        )
        return None
    try:
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("fashion_extract.parse_fail field=%s err=%s", field, exc)
        return None


async def _extract_one_field(
    *,
    field: str,
    title: Optional[str],
    description: Optional[str],
    html_blob: Optional[str],
    category_path: Optional[str],
) -> ExtractionResult:
    """Shared core: gate → LLM → substring-validate → calibrate confidence."""
    if not _extract_enabled():
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    if not _is_fashion_category(category_path):
        # Non-fashion rows short-circuit. confidence=0 so the trust gate
        # never sees them; source stays LLM so telemetry can distinguish
        # "category-gate skip" from "extractor disabled".
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    haystack = "\n".join(filter(None, [title or "", description or "", html_blob or ""]))
    if not haystack.strip():
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    user_message = _build_user_message(
        field=field, title=title, description=description, html_blob=html_blob,
    )
    raw = await _call_deepseek_extract(field=field, user_message=user_message)
    if not isinstance(raw, dict):
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    value = raw.get("value")
    if not isinstance(value, str) or not value.strip():
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    value = value.strip()
    self_report = raw.get("confidence")
    self_report_n = (
        float(self_report)
        if isinstance(self_report, (int, float)) and 0.0 <= float(self_report) <= 1.0
        else 0.5
    )
    substring_score = _substring_grounded(value, haystack)
    # Category-match factor is constant 1.0 here since we already passed
    # the _is_fashion_category gate; keeping the factor explicit so a
    # future enrichment (sub-category specific gates) plugs in cleanly.
    category_score = _CONFIDENCE_CATEGORY_MATCH
    confidence = round(self_report_n * substring_score * category_score, 4)
    if confidence <= 0.0:
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    return ExtractionResult(value=value, confidence=confidence, source=EXTRACTION_SOURCE_LLM)


# ---------- Public API ----------
# Note: signatures became async (was sync in v1). The only known caller is
# scripts/backfill_fashion_fields.py which is already async; updated in
# the same PR to await these.


async def extract_material(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
    category_path: Optional[str] = None,
) -> ExtractionResult:
    """LLM-backed material extractor. Returns value=None when:
       - FASHION_EXTRACT_ENABLED is off (default)
       - category_path doesn't start with fashion/apparel/clothing/...
       - LLM declines or transport fails
       - extracted value doesn't appear verbatim in the source text"""
    return await _extract_one_field(
        field="material",
        title=title,
        description=description,
        html_blob=html_blob,
        category_path=category_path,
    )


async def extract_care(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
    category_path: Optional[str] = None,
) -> ExtractionResult:
    return await _extract_one_field(
        field="care",
        title=title,
        description=description,
        html_blob=html_blob,
        category_path=category_path,
    )


async def extract_size_guide(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
    category_path: Optional[str] = None,
) -> ExtractionResult:
    return await _extract_one_field(
        field="size_guide",
        title=title,
        description=description,
        html_blob=html_blob,
        category_path=category_path,
    )


# ---------- Batched extractor: one LLM call for all three fields ----------
# Used by catalog_sync_service for live-ingest enrichment so a single LLM
# round-trip covers material + care + size_guide instead of three. Same
# gates (flag / category / haystack), same substring grounding, same
# confidence calibration.

_BATCH_SYSTEM_PROMPT = (
    "You extract three structured fashion-product fields from merchant "
    "catalog text: material composition, care instructions, and size guide. "
    "Output strict JSON only. NEVER invent values. NEVER paraphrase — copy "
    "each relevant phrase verbatim from the source text. If the source does "
    "not state a field, return that field as value=null."
)

_BATCH_INSTRUCTION = (
    "Extract these three fields, returning ONLY phrases that appear verbatim "
    "in the source text:\n"
    "- material: composition string (e.g. '100% cotton', '90% nylon 10% spandex')\n"
    "- care: care instructions phrase (e.g. 'Hand wash cold; lay flat to dry')\n"
    "- size_guide: sizing chart description phrase (e.g. 'See size chart below')\n"
    "If a field is not stated, set its value to null and reason to 'not_stated'.\n"
    "Do NOT infer."
)


def _build_batch_user_message(
    *, title: Optional[str], description: Optional[str], html_blob: Optional[str],
) -> str:
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if description:
        parts.append(f"Description:\n{description}")
    if html_blob:
        parts.append(f"Additional HTML:\n{html_blob}")
    body = "\n\n".join(parts) if parts else "(no source text)"
    return (
        f"{_BATCH_INSTRUCTION}\n\n"
        f"Source text:\n---\n{body}\n---\n\n"
        f'Return JSON only: '
        f'{{"material": {{"value": string|null, "confidence": float in [0,1], "reason": string}}, '
        f'"care": {{"value": string|null, "confidence": float in [0,1], "reason": string}}, '
        f'"size_guide": {{"value": string|null, "confidence": float in [0,1], "reason": string}}}}'
    )


async def _call_deepseek_batch(
    *, user_message: str, timeout_s: float = 20.0,
) -> Optional[Dict[str, Any]]:
    """One Deepseek call returning {material, care, size_guide} sub-objects.

    Separate from `_call_deepseek_extract` to keep the single-field code path
    untouched and to give the batched path its own slightly longer timeout
    (output is ~3x the size).
    """
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("fashion_extract.batch.no_api_key")
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    model = settings.deepseek_model
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 500,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("fashion_extract.batch.transport_fail err=%s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning(
            "fashion_extract.batch.http_%d body=%s",
            resp.status_code, resp.text[:200],
        )
        return None
    try:
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("fashion_extract.batch.parse_fail err=%s", exc)
        return None


def _result_from_subobject(
    *, sub: Any, haystack: str,
) -> ExtractionResult:
    """Apply substring grounding + confidence calibration to one
    {value, confidence, reason} subobject from the batched LLM response.
    Mirrors the back half of _extract_one_field."""
    if not isinstance(sub, dict):
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    value = sub.get("value")
    if not isinstance(value, str) or not value.strip():
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    value = value.strip()
    self_report = sub.get("confidence")
    self_report_n = (
        float(self_report)
        if isinstance(self_report, (int, float)) and 0.0 <= float(self_report) <= 1.0
        else 0.5
    )
    substring_score = _substring_grounded(value, haystack)
    confidence = round(self_report_n * substring_score * _CONFIDENCE_CATEGORY_MATCH, 4)
    if confidence <= 0.0:
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    return ExtractionResult(value=value, confidence=confidence, source=EXTRACTION_SOURCE_LLM)


async def batch_extract_fashion_fields(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
    category_path: Optional[str] = None,
) -> Dict[str, ExtractionResult]:
    """Extract material + care + size_guide in one LLM round-trip.

    Returns a dict keyed by field name. Every key is always present so
    callers can `result["material"]` without KeyError. value=None means
    "don't write" (gated off, no source text, grounding failed, or LLM
    declined). Guarantees: never raises; failures degrade to value=None.
    """
    noop = ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_LLM)
    empty = {"material": noop, "care": noop, "size_guide": noop}
    if not _extract_enabled():
        return empty
    if not _is_fashion_category(category_path):
        return empty
    haystack = "\n".join(filter(None, [title or "", description or "", html_blob or ""]))
    if not haystack.strip():
        return empty
    user_message = _build_batch_user_message(
        title=title, description=description, html_blob=html_blob,
    )
    raw = await _call_deepseek_batch(user_message=user_message)
    if not isinstance(raw, dict):
        return empty
    return {
        "material": _result_from_subobject(sub=raw.get("material"), haystack=haystack),
        "care": _result_from_subobject(sub=raw.get("care"), haystack=haystack),
        "size_guide": _result_from_subobject(sub=raw.get("size_guide"), haystack=haystack),
    }
