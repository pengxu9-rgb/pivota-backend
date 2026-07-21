"""LabelAgent — Phase O-3 of the PDP onboarding standardization track.

Phase O-2 added 4 typed taxonomy columns and conservative deterministic
extractors. Those fire only on clear signals, leaving the long tail
NULL/empty (e.g. demographic on rows whose title doesn't say "men's"
or "women's" but a human can clearly tell from brand + category).

This module is the LLM-powered fill for that long tail. Given a
catalog_products row's content (title, description, brand, tags,
existing taxonomy), it asks Gemini to classify the missing typed
fields.

Design contract:
1. Never overwrite merchant-supplied values. The prompt explicitly
   passes current values; the response shape lets us tell "agent
   provided" from "agent left untouched".
2. Classify only the 4 typed columns from O-2 + category_path (when
   the regex classifier left it NULL). Don't hallucinate new columns.
3. Use Gemini 2.5 Flash with structured output (responseMimeType +
   responseSchema). Grounding is OFF — this is content classification,
   not URL validation, and disabling grounding lets us require
   structured output cleanly.
4. Persist a confidence score per row so O-3b's batch worker can apply
   tier-based thresholds (PUBLISHED-track rows need higher confidence
   than backfill long-tail).
5. Same drop_reason / retry semantics as gemini_url_validator (mig 075-
   era PR #363/#365 patterns) so failure modes are uniform across the
   onboarding track.

This file (O-3a) is the pure module + tests. The batch worker that
actually applies classifications to prod ships in O-3b.

See docs/PDP_ONBOARDING_PLAYBOOK.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from services import vertex_gemini


logger = logging.getLogger("pdp_label_agent")

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 2


# ---------------------------------------------------------------------------
# Constants — vocabularies the LabelAgent is allowed to emit. Keeping the
# vocab pinned in code (not learned from each call) keeps the recall index
# size predictable and prevents Gemini token drift.
# ---------------------------------------------------------------------------


DEMOGRAPHIC_VOCAB = ("women", "men", "unisex", "kids")

USE_CASE_VOCAB = (
    "daily",
    "special_occasion",
    "gift",
    "professional",
    "sport",
    "travel",
    "sample",
)

LIFESTYLE_VOCAB = (
    "vegan",
    "cruelty_free",
    "fragrance_free",
    "paraben_free",
    "sulfate_free",
    "hypoallergenic",
    "dermatologist_tested",
    "organic",
    "sustainable",
    "recyclable",
    "clean_beauty",
    "non_toxic",
    "gluten_free",
    "ethically_sourced",
)


# Fields the LabelAgent fills. Order matters for prompt readability.
FILLABLE_FIELDS = (
    "demographic",
    "use_case_tags",
    "lifestyle_tags",
    "category_path",
)


# ---------------------------------------------------------------------------
# Helpers — should-we-call decision + result shape
# ---------------------------------------------------------------------------


def should_classify(row: Dict[str, Any]) -> bool:
    """Cheap pre-call gate. Returns True iff ≥1 fillable field is
    NULL/empty, so we don't burn Gemini calls on already-complete
    rows. Treats `[]` as "filled" — the deterministic extractor
    explicitly wrote empty list meaning "we looked, no signal".
    Only NULL means "never classified"."""
    if not isinstance(row, dict):
        return False
    if row.get("demographic") is None:
        return True
    if row.get("category_path") is None:
        return True
    # JSONB list columns: NULL means "never classified", [] means
    # "extractor saw the row and emitted empty". Both are "agent
    # could possibly fill more", but per Decision 4 (tiered) we only
    # call the LabelAgent on NULL — operators can opt rows back in
    # via a re-classify worker if needed.
    if row.get("use_case_tags") is None:
        return True
    if row.get("lifestyle_tags") is None:
        return True
    return False


def _resolve_api_key(api_key: Optional[str]) -> Optional[str]:
    if api_key:
        return api_key
    for var in ("GEMINI_API_KEY", "PIVOTA_GEMINI_API_KEY"):
        v = os.environ.get(var)
        if v and v.strip():
            return v.strip()
    return None


def _normalize_token(token: Any, vocab: tuple) -> Optional[str]:
    """Coerce model output to a vocab token. Lower, strip, replace
    spaces/hyphens with underscores. Returns the matched vocab token
    or None if the model invented a token."""
    if not token:
        return None
    s = str(token).strip().lower().replace("-", "_").replace(" ", "_")
    return s if s in vocab else None


def _filter_vocab_list(items: Any, vocab: tuple, *, max_len: int = 10) -> List[str]:
    """Take a freeform list from Gemini and return only vocab-matching,
    deduped tokens. Caps at max_len to defend against runaway model
    output."""
    if not isinstance(items, list):
        return []
    out: List[str] = []
    for item in items:
        token = _normalize_token(item, vocab)
        if token and token not in out:
            out.append(token)
        if len(out) >= max_len:
            break
    return out


# ---------------------------------------------------------------------------
# Prompt + response parser
# ---------------------------------------------------------------------------


def build_label_prompt(row: Dict[str, Any]) -> str:
    """Build the Gemini prompt for one row. The prompt explicitly
    mentions the controlled vocabularies AND the existing values of
    each field, so Gemini knows what's already merchant-supplied vs
    what we want it to fill."""
    title = str(row.get("title") or "").strip() or "(no title)"
    description = str(row.get("description") or "").strip() or "(no description)"
    brand = str(row.get("brand") or "").strip() or "(unknown brand)"
    product_type = str(row.get("product_type") or "").strip() or "(unknown product_type)"
    category_path = row.get("category_path") or None
    tags = row.get("tags") or []
    if isinstance(tags, str):
        tags_str = tags
    else:
        tags_str = ", ".join(str(t) for t in tags) if tags else "(none)"

    current_demo = row.get("demographic") or "null"
    current_uc = row.get("use_case_tags")
    current_uc_str = json.dumps(current_uc) if current_uc is not None else "null"
    current_ls = row.get("lifestyle_tags")
    current_ls_str = json.dumps(current_ls) if current_ls is not None else "null"
    current_cat = category_path or "null"

    return f"""You are classifying one product into Pivota's controlled-vocabulary
taxonomy. Return STRICT JSON matching the schema. Do not invent new tokens.
Only emit values from the listed vocabularies.

Product:
  title: {title}
  brand: {brand}
  product_type: {product_type}
  description: {description}
  merchant tags: {tags_str}

Current values (you may LEAVE NULL/empty if already filled by merchant
or if the product genuinely doesn't match anything in the vocab):
  demographic: {current_demo}
  use_case_tags: {current_uc_str}
  lifestyle_tags: {current_ls_str}
  category_path: {current_cat}

Vocabularies:
  demographic — exactly one of: {", ".join(DEMOGRAPHIC_VOCAB)}, or null
    if you cannot tell.
  use_case_tags — zero or more of: {", ".join(USE_CASE_VOCAB)}.
  lifestyle_tags — zero or more of: {", ".join(LIFESTYLE_VOCAB)}.
  category_path — Pivota slash-separated path. Examples:
      beauty/skincare/treat/serum
      beauty/makeup/lip/lipstick
      beauty/fragrance/eau_de_parfum
      home/kitchen/cookware
      electronics/audio/headphones_wireless
    Use null if the product doesn't fit any clear category.

Rules:
- Return ONLY tokens from the listed vocab. Anything else MUST be null
  or empty list.
- demographic: ONLY override the current value if it is null. If the
  product is genuinely unisex (most beauty / electronics / home), say
  unisex. Don't guess "women" just because the brand markets to women.
- use_case_tags / lifestyle_tags: only emit tokens you can justify
  from the title or description. No speculation.
- category_path: if the current value is non-null, you MUST return
  the same value (don't reclassify). Only fill when current is null.
- confidence: 0.0 (no idea) to 1.0 (certain), reflecting your worst
  field's certainty.

Output schema:
{{
  "demographic": "<vocab token or null>",
  "use_case_tags": ["<vocab tokens>"],
  "lifestyle_tags": ["<vocab tokens>"],
  "category_path": "<slash-path or null>",
  "confidence": 0.0,
  "reasoning": "<one short sentence>"
}}
"""


def _gemini_response_schema() -> Dict[str, Any]:
    """Structured-output schema for the Gemini API. Compatible without
    grounding (which we don't use for content classification)."""
    return {
        "type": "OBJECT",
        "properties": {
            "demographic": {"type": "STRING", "nullable": True},
            "use_case_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "lifestyle_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
            "category_path": {"type": "STRING", "nullable": True},
            "confidence": {"type": "NUMBER"},
            "reasoning": {"type": "STRING", "nullable": True},
        },
        "required": ["use_case_tags", "lifestyle_tags", "confidence"],
    }


_RESPONSE_SHAPE_KEYS = ("demographic", "use_case_tags", "lifestyle_tags", "category_path", "confidence", "reasoning")


def parse_label_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pure parser: take the raw Gemini API response and return the
    classification fields, vocab-filtered. On any extraction failure,
    returns a result with `drop_reason` set and empty/null fields.

    The runner / batch worker uses drop_reason to track how many
    classifications got dropped at parse time, mirroring the
    gemini_url_validator pattern from PR #363."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return _empty_result(drop_reason="gemini_no_candidates")

    parts = (((candidates[0] or {}).get("content") or {}).get("parts") or [])
    text_parts: List[str] = []
    for part in parts:
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(text)
    raw = "\n".join(text_parts).strip()
    if not raw:
        return _empty_result(drop_reason="gemini_no_text_parts")

    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return _empty_result(drop_reason="gemini_json_no_balanced_block")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return _empty_result(drop_reason="gemini_json_decode_failed")

    if not isinstance(parsed, dict):
        return _empty_result(drop_reason="gemini_response_not_dict")

    demographic = _normalize_token(parsed.get("demographic"), DEMOGRAPHIC_VOCAB)
    use_case_tags = _filter_vocab_list(parsed.get("use_case_tags"), USE_CASE_VOCAB)
    lifestyle_tags = _filter_vocab_list(parsed.get("lifestyle_tags"), LIFESTYLE_VOCAB)
    category_path = parsed.get("category_path")
    if category_path is not None:
        category_path = str(category_path).strip() or None
    confidence_raw = parsed.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(parsed.get("reasoning") or "").strip() or None

    return {
        "demographic": demographic,
        "use_case_tags": use_case_tags,
        "lifestyle_tags": lifestyle_tags,
        "category_path": category_path,
        "confidence": confidence,
        "reasoning": reasoning,
        "drop_reason": None,
    }


def _empty_result(*, drop_reason: Optional[str] = None) -> Dict[str, Any]:
    return {
        "demographic": None,
        "use_case_tags": [],
        "lifestyle_tags": [],
        "category_path": None,
        "confidence": 0.0,
        "reasoning": None,
        "drop_reason": drop_reason,
    }


def merge_classification_into_row(
    row: Dict[str, Any], result: Dict[str, Any]
) -> Dict[str, Any]:
    """Apply the agent's classification to the row, preserving merchant-
    supplied values per Decision 2. Returns a new dict; doesn't mutate
    the input.

    Rules:
      - demographic: only fill if current is None.
      - category_path: only fill if current is None.
      - use_case_tags / lifestyle_tags: only fill if current is None.
        (We deliberately don't merge with existing list values — if
        the deterministic extractor wrote `[]`, that means "I looked
        and saw nothing", and the LabelAgent shouldn't second-guess.)
    """
    out = dict(row)
    if out.get("demographic") is None and result.get("demographic"):
        out["demographic"] = result["demographic"]
    if out.get("category_path") is None and result.get("category_path"):
        out["category_path"] = result["category_path"]
    if out.get("use_case_tags") is None:
        out["use_case_tags"] = list(result.get("use_case_tags") or [])
    if out.get("lifestyle_tags") is None:
        out["lifestyle_tags"] = list(result.get("lifestyle_tags") or [])
    return out


# ---------------------------------------------------------------------------
# The actual Gemini call (with retry)
# ---------------------------------------------------------------------------


_RETRYABLE_STATUS_CODES = {429, 503, 504}

# Parse-time drop reasons that can also be retried — Gemini occasionally
# returns prose-wrapped output even with structured output enabled, and
# repeating the call is cheap insurance. Empirically the same product
# rarely fails twice in a row (drops are non-overlapping across runs in
# the O-6 canonical dry-runs). Cap retries with max_retries to bound cost.
_RETRYABLE_PARSE_DROP_REASONS = {
    "gemini_no_text_parts",
    "gemini_json_no_balanced_block",
    "gemini_json_decode_failed",
    "gemini_response_not_dict",
}


async def classify_pdp(
    row: Dict[str, Any],
    *,
    api_key: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_retries: int = DEFAULT_MAX_RETRIES,
    http_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Classify one row. Returns the same result shape as
    `parse_label_response` plus `model` (which model was used). On
    any unrecoverable failure, returns an empty result with
    `drop_reason` populated.

    `http_client` is for tests — pass a context-manager that yields
    something with `.post()`. Production calls should leave it
    None and the function will use httpx.AsyncClient.

    Doesn't touch the DB. Caller is responsible for deciding what to
    do with the result (write back to catalog_products, log, etc.) —
    Phase O-3b ships the batch worker that does that.
    """
    resolved_key = _resolve_api_key(api_key)
    if not resolved_key:
        return {**_empty_result(drop_reason="no_api_key"), "model": model}

    prompt = build_label_prompt(row)
    request_body: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "responseSchema": _gemini_response_schema(),
        },
    }

    url = vertex_gemini.generate_content_url(model, base_url=base_url)
    headers = await vertex_gemini.auth_headers(resolved_key)

    client_cm = http_client if http_client is not None else httpx.AsyncClient(timeout=timeout_s)

    attempts_left = max(1, int(max_retries) + 1)
    last_drop: Optional[str] = None

    async with client_cm as client:
        while attempts_left > 0:
            attempts_left -= 1
            try:
                response = await client.post(url, headers=headers, json=request_body)
            except Exception as exc:  # noqa: BLE001
                last_drop = "http_request_exception"
                logger.warning("pdp_label_agent: request exception: %s", exc)
                if attempts_left == 0:
                    return {**_empty_result(drop_reason=last_drop), "model": model}
                continue

            if response.status_code != 200:
                last_drop = f"http_status_{response.status_code}"
                logger.warning(
                    "pdp_label_agent: gemini status=%s body=%s",
                    response.status_code,
                    response.text[:300] if hasattr(response, "text") else "",
                )
                if response.status_code in _RETRYABLE_STATUS_CODES and attempts_left > 0:
                    continue
                result = _empty_result(drop_reason=last_drop)
                result["model"] = model
                if hasattr(response, "text"):
                    result["drop_detail"] = (response.text or "")[:500]
                return result

            try:
                payload = response.json()
            except Exception:
                last_drop = "http_body_not_json"
                if attempts_left > 0:
                    continue
                return {**_empty_result(drop_reason=last_drop), "model": model}

            parsed = parse_label_response(payload)
            parse_drop = parsed.get("drop_reason")
            if parse_drop in _RETRYABLE_PARSE_DROP_REASONS and attempts_left > 0:
                last_drop = parse_drop
                logger.info(
                    "pdp_label_agent: retrying after parse-time drop %s (attempts left: %d)",
                    parse_drop,
                    attempts_left,
                )
                continue
            parsed["model"] = model
            return parsed

    return {**_empty_result(drop_reason=last_drop or "exhausted_retries"), "model": model}
