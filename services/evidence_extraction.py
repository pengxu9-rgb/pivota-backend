"""Lab-report → candidate-claim extraction (Phase 2b intake).

A merchant uploads a lab / third-party test report (PDF or pasted text); this
service turns it into CANDIDATE claims for the merchant to review. It is the
"propose" half of the trust spine:

  upload  →  extract_lab_claims  →  candidates (returned, NOT stored substantiated)
          →  merchant reviews & confirms each
          →  POST /evidence  source_type='merchant_lab_report' source_ref=<artifact_id>
          →  normalize_intake_claims grades it 'a' / substantiated
          →  serve gate emits it to agents

The LLM never self-substantiates: candidates come back unverified and only become
citable when the merchant confirms one against the stored artifact. Extraction is
deliberately conservative — it returns only findings the report explicitly states
and drops anything that reads like a disease / drug claim (those need the
category cosmetic-vs-drug modules in services.claim_safety, not an LLM guess).

Pure-ish: the PDF text extraction + JSON parsing + safety screen are pure and
unit-tested; only extract_lab_claims touches the network (via llm_synthesis).
"""
from __future__ import annotations

import io
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Cap the report text we send to the model — bounds token cost and latency. Lab
# reports rarely carry more verifiable findings than the first ~20k chars hold.
LAB_TEXT_CHAR_LIMIT = 20000
# Default ceiling on candidates returned for review.
DEFAULT_MAX_CLAIMS = 8

# Provider preference for extraction — first one with a configured key wins.
# DeepSeek first (cheapest, already the audit pipeline's provider).
_PROVIDER_PREFERENCE = ("deepseek", "openai", "anthropic")

# Conservative disease/drug screen. A lab report can be cited for what it
# MEASURED, never to assert a product treats a condition — so we don't even
# SURFACE such a candidate. Disease nouns + hard therapeutic verbs only; ordinary
# cosmetic phrasing ("prevents moisture loss", "reduces the look of wrinkles")
# survives. The full cosmetic-vs-drug rules live with the claim_safety category
# modules; this is the intake-time guardrail.
_DISEASE_TERMS = (
    "eczema", "psoriasis", "dermatitis", "rosacea", "acne", "cancer", "tumor",
    "covid", "influenza", "infection", "disease", "illness", "diabetes",
    "arthritis", "hypertension", "alzheimer", "depression",
)
_DRUG_VERBS = ("cure", "cures", "cured", "diagnose", "diagnoses", "heal disease")


class EvidenceExtractionError(RuntimeError):
    """Raised when a report can't be read or no LLM provider is configured."""


def extract_pdf_text(blob: bytes) -> str:
    """Best-effort text from a PDF blob. Raises EvidenceExtractionError when the
    PDF library is unavailable, the bytes aren't a readable PDF, or the PDF holds
    no extractable text (e.g. a scanned image) — the caller surfaces a 422 telling
    the merchant to paste the report text instead."""
    if not blob:
        raise EvidenceExtractionError("Empty upload")
    try:
        import pypdf  # lazy + optional so the module imports without the dep
    except Exception as exc:  # pragma: no cover - only when dep missing
        raise EvidenceExtractionError(
            "PDF parsing is unavailable on this server — paste the report text instead."
        ) from exc
    try:
        reader = pypdf.PdfReader(io.BytesIO(blob))
        parts: List[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(p for p in parts if p).strip()
    except Exception as exc:
        raise EvidenceExtractionError(f"Could not read the PDF: {exc}") from exc
    if not text:
        raise EvidenceExtractionError(
            "No extractable text in the PDF (it may be a scanned image) — "
            "paste the report text instead."
        )
    return text


def _is_claim_safe(claim_text: str) -> bool:
    """Drop obvious disease/drug claims at intake time (see module docstring)."""
    low = f" {claim_text.lower()} "
    if any(re.search(rf"\b{re.escape(v)}\b", low) for v in _DRUG_VERBS):
        return False
    if any(term in low for term in _DISEASE_TERMS):
        return False
    return True


def _safe_json(text: Optional[str]) -> Optional[Any]:
    """Parse a model's JSON response, tolerating code fences / surrounding prose."""
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        nl = s.find("\n")
        if nl != -1 and s[:nl].strip().lower() in ("json", ""):
            s = s[nl + 1:]
    try:
        return json.loads(s)
    except Exception:
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                return None
    return None


def _parse_candidates(result: Any, *, max_claims: int) -> List[Dict[str, Any]]:
    """Coerce a synthesize() result into deduped, claim-safe candidate dicts:
    `{claim_text, source_excerpt}`. Pure — unit-tested without the network."""
    text = result.get("text") if isinstance(result, dict) else None
    data = _safe_json(text)
    items = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("claim_text") or raw.get("claim") or "").strip()
        if not claim_text:
            continue
        key = claim_text.lower()
        if key in seen:
            continue
        if not _is_claim_safe(claim_text):
            logger.info("evidence_extraction dropped unsafe candidate: %s", claim_text[:80])
            continue
        seen.add(key)
        excerpt = str(raw.get("source_excerpt") or raw.get("excerpt") or "").strip()
        out.append({"claim_text": claim_text, "source_excerpt": excerpt or None})
        if len(out) >= max_claims:
            break
    return out


def _pick_provider() -> Optional[str]:
    from services.llm_synthesis import configured_key_for_provider

    for provider in _PROVIDER_PREFERENCE:
        try:
            if configured_key_for_provider(provider):
                return provider
        except Exception:
            continue
    return None


_SYSTEM_PROMPT = (
    "You extract factual, verifiable claims from a lab or third-party test report "
    "that a merchant uploaded for one product. Return ONLY findings the report "
    "EXPLICITLY states — measured results, test outcomes, certifications, verified "
    "properties. Never invent, infer, extrapolate, or add marketing language. "
    "Never produce disease, treatment, or drug claims (e.g. 'cures', 'treats X "
    "disease', 'prevents <illness>'). If the report states no verifiable findings, "
    "return an empty list.\n\n"
    "Respond with STRICT JSON only, no prose, in this exact shape:\n"
    '{"claims": [{"claim_text": "<one concise, citable factual finding>", '
    '"source_excerpt": "<the verbatim phrase or number from the report>"}]}'
)


def _build_user_prompt(report_text: str, product_title: Optional[str]) -> str:
    header = f"Product: {product_title}\n\n" if product_title else ""
    return (
        f"{header}Lab / test report text follows. Extract the verifiable claims.\n\n"
        f"-----\n{report_text}\n-----"
    )


async def extract_lab_claims(
    lab_text: str,
    *,
    product_title: Optional[str] = None,
    max_claims: int = DEFAULT_MAX_CLAIMS,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Extract CANDIDATE claims from a lab-report text via the configured LLM.

    Returns `[{claim_text, source_excerpt}]` — candidates for merchant review, NOT
    substantiated claims. Empty list when the text is empty or the model finds
    nothing verifiable. Raises EvidenceExtractionError when no provider is
    configured or the LLM call fails (the caller maps that to a 503)."""
    text = (lab_text or "").strip()
    if not text:
        return []
    text = text[:LAB_TEXT_CHAR_LIMIT]

    from services.llm_synthesis import (
        LLMSynthesisError,
        default_model_for_provider,
        synthesize,
    )

    chosen = provider or _pick_provider()
    if not chosen:
        raise EvidenceExtractionError(
            "No LLM provider is configured for lab-report extraction."
        )
    chosen_model = model or default_model_for_provider(chosen)

    try:
        result = await synthesize(
            system=_SYSTEM_PROMPT,
            user=_build_user_prompt(text, product_title),
            provider=chosen,
            model=chosen_model,
            max_tokens=1200,
        )
    except LLMSynthesisError as exc:
        raise EvidenceExtractionError(f"Lab-report extraction failed: {exc}") from exc

    return _parse_candidates(result, max_claims=max_claims)
