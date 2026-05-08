"""PDP onboarding lifecycle stage — Phase O-4.

Pure functions. Computes the stage column at ingest time across all
three onboarding paths so the gateway (Phase O-5) can filter on
validated|published without rescanning content quality on every
recall query.

Stage progression:

  draft      → identity present, content insufficient
  candidate  → +title +image +description ≥ 50 chars
  validated  → +category_path +≥1 taxonomy signal
  published  → +canonical evidence (multi_merchant_canonical, agent
               curation, or manual approval)

  hold       → reserved (manual quality flag, future)
  archived   → reserved (source delisted, future)

The four `is_*_ready` predicates are independent monotonic gates.
`compute_lifecycle_stage` returns the highest stage the row
currently qualifies for. Re-running on the same row is idempotent;
re-running after content changes (e.g. agent fills tags) progresses
the stage automatically.

See docs/PDP_ONBOARDING_PLAYBOOK.md.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional


# Vocabulary
STAGE_DRAFT = "draft"
STAGE_CANDIDATE = "candidate"
STAGE_VALIDATED = "validated"
STAGE_PUBLISHED = "published"
STAGE_HOLD = "hold"
STAGE_ARCHIVED = "archived"

LIFECYCLE_VOCAB = (
    STAGE_DRAFT,
    STAGE_CANDIDATE,
    STAGE_VALIDATED,
    STAGE_PUBLISHED,
    STAGE_HOLD,
    STAGE_ARCHIVED,
)

# Live stages that surface in recall (Phase O-5 will filter on this).
LIVE_STAGES = frozenset({STAGE_VALIDATED, STAGE_PUBLISHED})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


CANDIDATE_DESCRIPTION_MIN_LEN = 50


def _nonempty_str(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        return bool(value)
    return bool(value.strip())


def _has_nonempty_list(value: Any) -> bool:
    """The taxonomy / tags fields may arrive as Python list, JSON-
    encoded string (asyncpg sometimes returns JSONB columns as
    strings), or comma-separated string. Treat all as "list-shaped"
    and return True iff there's at least one non-empty entry.

    Empty list / empty JSON-array / NULL all count as "no signal" —
    this matches the Phase O-1 / O-2 semantic where `[]` means
    "ingest looked, no tags" (still a signal that we *checked*, but
    not a signal usable as a lifecycle gate)."""
    if value is None:
        return False
    if isinstance(value, list):
        return any(_nonempty_str(item) for item in value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return False
        # Try JSON first (catalog_products columns serialize as JSON
        # strings on read in some drivers).
        try:
            decoded = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            # Fall back to comma split — a few legacy paths shove
            # tags through as "vegan,cruelty-free".
            return any(part.strip() for part in s.split(","))
        if isinstance(decoded, list):
            return any(_nonempty_str(item) for item in decoded)
        # Decoded but not a list → ignore.
        return False
    return False


# ---------------------------------------------------------------------------
# Stage gates
# ---------------------------------------------------------------------------


def is_candidate_ready(row: Mapping[str, Any]) -> bool:
    """Has identity + content the recall side can show: title, image,
    description (≥ CANDIDATE_DESCRIPTION_MIN_LEN chars). Identity
    (merchant_id + platform + source_product_id) is assumed to be
    present — without it, no row would have been written at all."""
    if not _nonempty_str(row.get("title")):
        return False
    if not _nonempty_str(row.get("image_url")):
        return False
    description = row.get("description") or ""
    if not isinstance(description, str):
        return False
    if len(description.strip()) < CANDIDATE_DESCRIPTION_MIN_LEN:
        return False
    return True


def is_validated_ready(row: Mapping[str, Any]) -> bool:
    """Candidate + category_path + ≥1 taxonomy signal. The taxonomy
    signal can come from any of: free-form tags, demographic, use_case_tags,
    lifestyle_tags. Only one is required — surfacing in recall just
    needs *some* discriminator beyond raw title text."""
    if not is_candidate_ready(row):
        return False
    if not _nonempty_str(row.get("category_path")):
        return False
    if (
        _has_nonempty_list(row.get("tags"))
        or _nonempty_str(row.get("demographic"))
        or _has_nonempty_list(row.get("use_case_tags"))
        or _has_nonempty_list(row.get("lifestyle_tags"))
    ):
        return True
    return False


def is_published_ready(row: Mapping[str, Any]) -> bool:
    """Validated + canonical evidence. Three independent triggers:

    1. pdp_scope='multi_merchant_canonical' — Phase 6 governance
       confirmed multi-merchant evidence; this is the strongest
       signal.
    2. source_system='catalog_enrichment_agent_v1' — the
       hand-curated + Gemini-validated Phase 4 path. By construction
       these have validator-confirmed URLs and content.
    3. (future) manual approval flag.

    LabelAgent fills (category_label_source='label_agent_v1') do
    NOT auto-publish in v1 because per-row confidence is not
    persisted on the row. That means a row that ONLY has agent-
    inferred taxonomy stays at validated until it earns one of the
    triggers above. Conservative on purpose."""
    if not is_validated_ready(row):
        return False
    if row.get("pdp_scope") == "multi_merchant_canonical":
        return True
    if row.get("source_system") == "catalog_enrichment_agent_v1":
        return True
    return False


def compute_lifecycle_stage(row: Mapping[str, Any]) -> str:
    """Return the highest stage the row currently qualifies for.

    Idempotent: re-running on a row in any state always returns the
    same answer for the same input. Safe to call at every ingest +
    every UPDATE that touches content / taxonomy fields. Never
    returns hold or archived in v1 — those require explicit signals
    that don't exist yet."""
    if is_published_ready(row):
        return STAGE_PUBLISHED
    if is_validated_ready(row):
        return STAGE_VALIDATED
    if is_candidate_ready(row):
        return STAGE_CANDIDATE
    return STAGE_DRAFT
