"""Tests for services/pdp_lifecycle.py (Phase O-4).

The module is called by all three onboarding paths to compute the
lifecycle stage at write time. Pure-function gates make stage
progression independent of the path — Path A and Path B with the
same content + taxonomy yield the same stage.
"""

from __future__ import annotations

import json

import pytest

from services.pdp_lifecycle import (
    CANDIDATE_DESCRIPTION_MIN_LEN,
    LIFECYCLE_VOCAB,
    LIVE_STAGES,
    STAGE_CANDIDATE,
    STAGE_DRAFT,
    STAGE_PUBLISHED,
    STAGE_VALIDATED,
    compute_lifecycle_stage,
    is_candidate_ready,
    is_published_ready,
    is_validated_ready,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal rows at each stage
# ---------------------------------------------------------------------------


def _draft_row(**overrides):
    """Has identity but content insufficient (no title or no image
    or short description)."""
    base = {
        "title": None,  # missing → draft
        "image_url": None,
        "description": None,
        "category_path": None,
        "tags": None,
        "demographic": None,
        "use_case_tags": None,
        "lifestyle_tags": None,
        "pdp_scope": None,
        "source_system": None,
    }
    base.update(overrides)
    return base


def _candidate_row(**overrides):
    """Title + image + description ≥ 50 chars, no taxonomy yet."""
    base = _draft_row(
        title="A Real Product",
        image_url="https://example.com/img.jpg",
        description="A long enough description for the candidate gate to fire on this row." + " " * 5,
        category_path=None,
        tags=None,
    )
    base.update(overrides)
    return base


def _validated_row(**overrides):
    """Candidate + category_path + tag signal."""
    base = _candidate_row(
        category_path="beauty/skincare/treat/serum",
        tags=["k-beauty"],
    )
    base.update(overrides)
    return base


def _published_row(**overrides):
    """Validated + canonical evidence."""
    base = _validated_row(pdp_scope="multi_merchant_canonical")
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# is_candidate_ready
# ---------------------------------------------------------------------------


def test_candidate_ready_requires_title():
    row = _candidate_row(title=None)
    assert is_candidate_ready(row) is False


def test_candidate_ready_requires_image_url():
    row = _candidate_row(image_url=None)
    assert is_candidate_ready(row) is False
    row = _candidate_row(image_url="")
    assert is_candidate_ready(row) is False


def test_candidate_ready_requires_description_min_length():
    row = _candidate_row(description="too short")
    assert is_candidate_ready(row) is False
    # Edge case: exactly 49 chars fails, 50 passes
    row = _candidate_row(description="x" * (CANDIDATE_DESCRIPTION_MIN_LEN - 1))
    assert is_candidate_ready(row) is False
    row = _candidate_row(description="x" * CANDIDATE_DESCRIPTION_MIN_LEN)
    assert is_candidate_ready(row) is True


def test_candidate_ready_strips_whitespace_for_length_check():
    """Padding alone shouldn't satisfy the gate — it's a content check."""
    row = _candidate_row(description="  short  " + " " * 100)
    assert is_candidate_ready(row) is False


def test_candidate_ready_passes_with_full_content():
    assert is_candidate_ready(_candidate_row()) is True


# ---------------------------------------------------------------------------
# is_validated_ready
# ---------------------------------------------------------------------------


def test_validated_ready_blocked_when_not_candidate():
    """Even with category_path + tags, if the candidate gate fails,
    validated must too. Monotonic gates."""
    row = _draft_row(category_path="beauty/x", tags=["vegan"])
    assert is_validated_ready(row) is False


def test_validated_ready_requires_category_path():
    row = _candidate_row(tags=["vegan"])  # has tag, no category_path
    assert is_validated_ready(row) is False


def test_validated_ready_passes_with_any_taxonomy_signal():
    """Each of tags, demographic, use_case_tags, lifestyle_tags
    independently satisfies the taxonomy requirement."""
    base = _candidate_row(category_path="beauty/x")
    assert is_validated_ready({**base, "tags": ["k-beauty"]}) is True
    assert is_validated_ready({**base, "demographic": "women"}) is True
    assert is_validated_ready({**base, "use_case_tags": ["daily"]}) is True
    assert is_validated_ready({**base, "lifestyle_tags": ["vegan"]}) is True


def test_validated_ready_blocked_with_only_empty_taxonomy():
    """[] across the typed fields and missing tags = "we looked, found
    nothing" — taxonomy gate is not satisfied. Caller has to fill
    something (LabelAgent or merchant data) before the row promotes."""
    row = _candidate_row(
        category_path="beauty/x",
        tags=[],
        demographic=None,
        use_case_tags=[],
        lifestyle_tags=[],
    )
    assert is_validated_ready(row) is False


def test_validated_ready_handles_jsonb_string_form():
    """Some drivers return JSONB columns as JSON-encoded strings.
    The gate must handle both list and str forms or it'll be
    silently broken on prod reads."""
    base = _candidate_row(category_path="beauty/x")
    assert is_validated_ready({**base, "tags": '["vegan", "k-beauty"]'}) is True
    assert is_validated_ready({**base, "use_case_tags": '["daily"]'}) is True
    assert is_validated_ready({**base, "tags": "[]"}) is False
    # Comma-separated string fallback (some legacy ingest path)
    assert is_validated_ready({**base, "tags": "vegan, k-beauty"}) is True


# ---------------------------------------------------------------------------
# is_published_ready
# ---------------------------------------------------------------------------


def test_published_ready_blocked_when_not_validated():
    """Published requires validated. Even with multi_merchant_canonical
    scope, a row missing taxonomy is not published."""
    row = _candidate_row(pdp_scope="multi_merchant_canonical")
    assert is_published_ready(row) is False


def test_published_ready_via_multi_merchant_scope():
    row = _validated_row(pdp_scope="multi_merchant_canonical")
    assert is_published_ready(row) is True


def test_published_ready_via_agent_curation():
    """source_system='catalog_enrichment_agent_v1' → Phase 4 hand-curated
    + Gemini-validated path. Counts as canonical evidence."""
    row = _validated_row(source_system="catalog_enrichment_agent_v1")
    assert is_published_ready(row) is True


def test_published_ready_blocked_for_merchant_owned():
    """Merchant_owned + LabelAgent fills do NOT auto-publish in v1.
    Per-row LabelAgent confidence isn't persisted, so we conservative
    on the gate."""
    row = _validated_row(pdp_scope="merchant_owned")
    assert is_published_ready(row) is False


def test_published_ready_blocked_for_unverified():
    row = _validated_row(pdp_scope="unverified")
    assert is_published_ready(row) is False


# ---------------------------------------------------------------------------
# compute_lifecycle_stage — top-level helper
# ---------------------------------------------------------------------------


def test_compute_returns_draft_for_empty_row():
    assert compute_lifecycle_stage(_draft_row()) == STAGE_DRAFT


def test_compute_returns_candidate_when_no_taxonomy():
    assert compute_lifecycle_stage(_candidate_row()) == STAGE_CANDIDATE


def test_compute_returns_validated_with_taxonomy_no_canonical():
    assert compute_lifecycle_stage(_validated_row()) == STAGE_VALIDATED


def test_compute_returns_published_with_canonical_scope():
    assert compute_lifecycle_stage(_published_row()) == STAGE_PUBLISHED


def test_compute_returns_published_with_agent_curation():
    row = _validated_row(source_system="catalog_enrichment_agent_v1")
    assert compute_lifecycle_stage(row) == STAGE_PUBLISHED


def test_compute_idempotent_for_progressive_fills():
    """A row gradually accumulating fields traverses the gates
    monotonically. Re-computing at any point returns the same
    answer for the same input."""
    row = _draft_row()
    assert compute_lifecycle_stage(row) == STAGE_DRAFT

    row["title"] = "Vitamin C Serum"
    row["image_url"] = "https://x/y.jpg"
    row["description"] = "x" * 60
    assert compute_lifecycle_stage(row) == STAGE_CANDIDATE
    # idempotent
    assert compute_lifecycle_stage(row) == STAGE_CANDIDATE

    row["category_path"] = "beauty/skincare/treat/serum"
    row["use_case_tags"] = ["daily"]
    assert compute_lifecycle_stage(row) == STAGE_VALIDATED

    row["pdp_scope"] = "multi_merchant_canonical"
    assert compute_lifecycle_stage(row) == STAGE_PUBLISHED


def test_live_stages_constant_matches_recall_filter_intent():
    """Phase O-5 will filter recall on this set. Pin it so a future
    refactor doesn't accidentally surface draft/hold/archived in
    recall."""
    assert LIVE_STAGES == frozenset({STAGE_VALIDATED, STAGE_PUBLISHED})


def test_lifecycle_vocab_covers_all_documented_stages():
    """Catches a stage being added to the docstring without being
    added to the vocab tuple — they must stay in sync."""
    assert "draft" in LIFECYCLE_VOCAB
    assert "candidate" in LIFECYCLE_VOCAB
    assert "validated" in LIFECYCLE_VOCAB
    assert "published" in LIFECYCLE_VOCAB
    assert "hold" in LIFECYCLE_VOCAB
    assert "archived" in LIFECYCLE_VOCAB
