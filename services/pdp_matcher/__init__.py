"""PDP matcher — links external_product_seeds rows to catalog_products PDPs.

Phase 3A of the PDP-as-canonical recall migration (see
~/.claude/plans/shimmying-soaring-ember.md).

Three deterministic matchers in priority order:
  1. exact source_product_id match (high confidence)
  2. canonical URL normalization match (high confidence)
  3. title trigram match within the same brand (medium confidence)

Phase 3B (separate PR) will use Claude API for the ~15% of seeds that
none of these match.
"""

from services.pdp_matcher.deterministic import (
    canonical_url_match,
    matches_for_seed,
    normalize_canonical_url,
    source_product_id_match,
    title_brand_match,
)
from services.pdp_matcher.llm_match import llm_match_seed

__all__ = [
    "canonical_url_match",
    "llm_match_seed",
    "matches_for_seed",
    "normalize_canonical_url",
    "source_product_id_match",
    "title_brand_match",
]
