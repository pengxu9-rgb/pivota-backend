"""Fashion field extractor for catalog_products.

Phase O-5b: heuristic (regex-based) extractor for `material`, `care`, and
`size_guide` from a product's title + description text. Substring-validated
so the extracted value is provably grounded in the source text (no
hallucinated specs even when a future LLM-backed extractor is wired in).

Extraction-engine versions written to `*_source` columns:
  - regex_extraction_v1 → this file (cheap, deterministic, free)
  - llm_extraction_v1   → reserved for a future provider-backed extractor
                          (see services/llm_providers/orchestrator.py for the
                          dispatch pattern when we get there)

Per-field provenance + confidence schema is documented in
db/migrations/094_catalog_fashion_fields.sql.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


EXTRACTION_SOURCE_REGEX = "regex_extraction_v1"
EXTRACTION_SOURCE_LLM = "llm_extraction_v1"


@dataclass(frozen=True)
class ExtractionResult:
    """One extracted field with provenance.

    value: extracted string (or None if the source didn't carry the field)
    confidence: 0.0-1.0; downstream code should drop low-confidence values
                from merchant-facing prose (default gate >= 0.6 — see
                ~/.claude/plans/let-s-build-a-full-breezy-taco.md)
    source: provenance enum (EXTRACTION_SOURCE_REGEX / EXTRACTION_SOURCE_LLM)
    """
    value: Optional[str]
    confidence: float
    source: str


# Heuristic patterns. Each entry: (label_regex, content_capture_regex, base_confidence).
# label_regex MUST match the line preamble (e.g. "Material:", "Material -")
# content_capture_regex extracts the value that follows on the same line.

_MATERIAL_PATTERNS = [
    # "Material: 100% cotton"  or  "Material - 100% cotton"
    re.compile(
        r"(?i)\bmaterial[s]?\s*[:\-–]\s*(?P<value>[^\n\r\.;|<]{3,200})",
    ),
    # "Fabric: ..."
    re.compile(
        r"(?i)\bfabric\s*[:\-–]\s*(?P<value>[^\n\r\.;|<]{3,200})",
    ),
    # "Composition: ..."
    re.compile(
        r"(?i)\bcomposition\s*[:\-–]\s*(?P<value>[^\n\r\.;|<]{3,200})",
    ),
    # "Made of: ..."
    re.compile(
        r"(?i)\bmade\s+of\s*[:\-–]\s*(?P<value>[^\n\r\.;|<]{3,200})",
    ),
]

_CARE_PATTERNS = [
    re.compile(
        r"(?i)\bcare(?:\s+instructions?)?\s*[:\-–]\s*(?P<value>[^\n\r|<]{3,250})",
    ),
    re.compile(
        r"(?i)\bwashing?\s+instructions?\s*[:\-–]\s*(?P<value>[^\n\r|<]{3,250})",
    ),
    re.compile(
        r"(?i)\bhow\s+to\s+care\s*[:\-–]\s*(?P<value>[^\n\r|<]{3,250})",
    ),
]

# Size guide: matches a header line like "Size guide:" / "Size chart:" /
# "Sizing:" and we capture the following block until a blank line or
# section break. The extractor only flags it; structured table parsing
# is intentionally deferred (regex on free-form HTML is brittle — the
# real win is to know a size guide EXISTS so the LLM upgrade path knows
# where to look).
_SIZE_GUIDE_HEADER = re.compile(
    r"(?i)\b(?:size\s+(?:guide|chart)|sizing|measurements?)\s*[:\-–]\s*(?P<value>[^\n\r]{3,500})",
)


def _validate_substring(value: Optional[str], source_text: str) -> bool:
    """Confidence gate: the extracted value MUST appear verbatim in the source text.
    Defends against future LLM extractors hallucinating specs."""
    if not value or not source_text:
        return False
    return value.strip().lower() in source_text.lower()


def _confidence_for_regex_hit(value: str) -> float:
    """Regex extractor base confidence is 0.7 on a clean short match.
    Longer captures (more likely to overshoot the actual value) get
    downgraded; very short ones (< 5 chars) too."""
    n = len(value.strip())
    if n < 5 or n > 180:
        return 0.55
    if n > 100:
        return 0.65
    return 0.75


def _extract_with_patterns(
    *, patterns, title: Optional[str], description: Optional[str], html_blob: Optional[str],
) -> ExtractionResult:
    haystack = "\n".join(filter(None, [title or "", description or "", html_blob or ""]))
    for pattern in patterns:
        match = pattern.search(haystack)
        if not match:
            continue
        raw = (match.group("value") or "").strip(" \t-–•|")
        if not raw:
            continue
        if not _validate_substring(raw, haystack):
            # Substring check is mostly a guard for LLM extractors. For
            # regex hits this is essentially always true since we captured
            # FROM the haystack. Kept here so the validator path stays
            # symmetric across extractor versions.
            continue
        return ExtractionResult(
            value=raw,
            confidence=_confidence_for_regex_hit(raw),
            source=EXTRACTION_SOURCE_REGEX,
        )
    return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_REGEX)


def extract_material(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
) -> ExtractionResult:
    """Extract material composition. Returns value=None when no pattern matches."""
    return _extract_with_patterns(
        patterns=_MATERIAL_PATTERNS,
        title=title, description=description, html_blob=html_blob,
    )


def extract_care(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
) -> ExtractionResult:
    """Extract care instructions. Returns value=None when no pattern matches."""
    return _extract_with_patterns(
        patterns=_CARE_PATTERNS,
        title=title, description=description, html_blob=html_blob,
    )


def extract_size_guide(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
) -> ExtractionResult:
    """Flag presence of a size guide and capture the header line.

    Returns the captured header line as the value. Structured table parsing
    is deferred to the LLM extractor follow-up — for now we just signal
    that a size guide is present so the agent can render the description
    rather than a "size info not available" empty state.
    """
    haystack = "\n".join(filter(None, [title or "", description or "", html_blob or ""]))
    match = _SIZE_GUIDE_HEADER.search(haystack)
    if not match:
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_REGEX)
    raw = (match.group("value") or "").strip(" \t-–•|")
    if not raw or not _validate_substring(raw, haystack):
        return ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_REGEX)
    return ExtractionResult(
        value=raw,
        confidence=_confidence_for_regex_hit(raw),
        source=EXTRACTION_SOURCE_REGEX,
    )
