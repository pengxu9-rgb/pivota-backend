"""Fashion field extractor — DEPRECATED regex v1 stage.

Historical context:
  - Shipped 2026-05-17 in PR #540 as a regex-based v1: matched common
    Shopify text patterns ("Material: 100% cotton", "Care: hand wash", etc.)
    with substring-grounded validation.
  - 2026-05-18 dry-run against prod (1000 rows, mostly beauty catalog)
    revealed quality problems: 1 material hit + 9 care hits, but every
    care hit was a false positive ("Skin care: 1 Rêve de Miel Honey Lip
    Balm" captured "1 Rêve de Miel Honey Lip Balm" as the care value).
    Length-based confidence (0.75) didn't reflect quality — the trust
    gate would have let these through.

Decision: the regex extractor was a transitional placeholder. Replacing
with a category-gated LLM extractor (services/llm_providers/orchestrator.py
route) is the durable v2. To prevent any accidental backfill --apply
from poisoning catalog rows in the interim, these functions are now
no-ops that always return value=None.

Backwards compatibility: scripts/backfill_fashion_fields.py and any other
callers continue to import + invoke these functions; they just receive
empty results until the LLM extractor lands.

Substring grounding invariant: the validator pattern survives the v2
swap — extracted values must appear verbatim in the source text. The
LLM extractor will reuse the same _validate_substring check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Source enum values. Kept stable across v1 (regex, now no-op) and v2 (LLM)
# so downstream consumers (gateway trust gate in pdpBuilder.pickFashionMeta)
# don't need to know which extractor produced a value.
EXTRACTION_SOURCE_REGEX = "regex_extraction_v1"
EXTRACTION_SOURCE_LLM = "llm_extraction_v1"


@dataclass(frozen=True)
class ExtractionResult:
    """One extracted field with provenance.

    value: extracted string (None until v2 LLM extractor ships)
    confidence: 0.0-1.0; the merchant-facing gate is 0.6 in pivota-agent's
                pdpBuilder.pickFashionMeta. v2 will compute this from
                model self-report × substring-match × category-match;
                today (no-op) confidence is always 0.0.
    source: provenance enum (EXTRACTION_SOURCE_REGEX / EXTRACTION_SOURCE_LLM)
    """
    value: Optional[str]
    confidence: float
    source: str


_DEPRECATED_NOOP = ExtractionResult(value=None, confidence=0.0, source=EXTRACTION_SOURCE_REGEX)


def extract_material(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
) -> ExtractionResult:
    """No-op until the LLM extractor v2 lands. See module docstring for why."""
    del title, description, html_blob  # acknowledged unused
    return _DEPRECATED_NOOP


def extract_care(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
) -> ExtractionResult:
    """No-op until the LLM extractor v2 lands. See module docstring for why."""
    del title, description, html_blob
    return _DEPRECATED_NOOP


def extract_size_guide(
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    html_blob: Optional[str] = None,
) -> ExtractionResult:
    """No-op until the LLM extractor v2 lands. See module docstring for why."""
    del title, description, html_blob
    return _DEPRECATED_NOOP
