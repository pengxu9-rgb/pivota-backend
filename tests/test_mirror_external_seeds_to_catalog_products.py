from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.mirror_external_seeds_to_catalog_products import (  # noqa: E402
    CATEGORY_CONFIDENCE_REGEX_AT_MIRROR,
    CATEGORY_LABEL_SOURCE_AT_MIRROR,
    _compute_mirror_lifecycle_stage,
    _extract_tags_from_seed_data,
    resolve_mirror_category_metadata,
)


def test_mirror_insert_classifies_category_path() -> None:
    meta = resolve_mirror_category_metadata(
        category=None,
        product_type="Lip Gloss",
        title="Fenty Beauty Gloss Bomb Lip Gloss",
    )

    assert meta["category_path"] == "beauty/makeup/lip/lipstick"
    assert meta["category_confidence"] == CATEGORY_CONFIDENCE_REGEX_AT_MIRROR
    assert meta["category_label_source"] == CATEGORY_LABEL_SOURCE_AT_MIRROR
    assert meta["category_label"] == "Lipstick"


def test_mirror_insert_leaves_unknown_category_null() -> None:
    meta = resolve_mirror_category_metadata(
        category=None,
        product_type="Accessory",
        title="Travel Makeup Bag",
    )

    assert meta["category_path"] is None
    assert meta["category_confidence"] is None
    assert meta["category_label_source"] is None
    assert meta["category_label"] is None


# ---------------------------------------------------------------------------
# Phase O-1 followup — tag extraction from seed_data JSONB
# ---------------------------------------------------------------------------


def test_extract_tags_from_top_level_list() -> None:
    """Most basic case: scraper put a flat list at seed_data.tags."""
    out = _extract_tags_from_seed_data({"tags": ["matte", "long-wear"]})
    assert out == ["matte", "long-wear"]


def test_extract_tags_from_snapshot_path() -> None:
    """Shopify-style scrape: tags live under seed_data.snapshot.tags."""
    out = _extract_tags_from_seed_data(
        {"snapshot": {"tags": ["vegan", "cruelty-free", "k-beauty"]}}
    )
    assert out == ["vegan", "cruelty-free", "k-beauty"]


def test_extract_tags_from_derived_recall_path() -> None:
    """Pivota-derived recall doc — preferred path; takes priority over
    snapshot/product/top-level."""
    out = _extract_tags_from_seed_data(
        {
            "derived": {"recall": {"tags": ["serum", "ceramide"]}},
            "snapshot": {"tags": ["should-be-ignored"]},
            "tags": ["also-ignored"],
        }
    )
    assert out == ["serum", "ceramide"]


def test_extract_tags_handles_comma_separated_string() -> None:
    """Some Shopify exports flatten tags into a single comma string."""
    out = _extract_tags_from_seed_data({"tags": "matte, long-wear, vegan"})
    assert out == ["matte", "long-wear", "vegan"]


def test_extract_tags_handles_dict_items() -> None:
    """Some scrapers wrap each tag as {name: ...} or {label: ...}."""
    out = _extract_tags_from_seed_data(
        {"tags": [{"name": "matte"}, {"label": "long-wear"}, {"name": ""}, "vegan"]}
    )
    assert out == ["matte", "long-wear", "vegan"]


def test_extract_tags_dedupes_and_strips() -> None:
    out = _extract_tags_from_seed_data(
        {"tags": ["  matte  ", "matte", "long-wear", "", "long-wear"]}
    )
    assert out == ["matte", "long-wear"]


def test_extract_tags_returns_empty_when_no_tag_field() -> None:
    """Same semantic as ingest_standard_products on Path A: empty list,
    not None — meaning "we looked, found nothing"."""
    out = _extract_tags_from_seed_data({"title": "Just a product"})
    assert out == []


def test_extract_tags_returns_empty_for_non_dict() -> None:
    """seed_data may be NULL or non-dict on legacy rows — must not blow up."""
    assert _extract_tags_from_seed_data(None) == []
    assert _extract_tags_from_seed_data([]) == []
    assert _extract_tags_from_seed_data("scrambled string") == []


# ---------------------------------------------------------------------------
# Phase O-4 — lifecycle stage computation on Path B (external seed mirror)
# ---------------------------------------------------------------------------


def test_mirror_lifecycle_validated_when_full_content_and_taxonomy() -> None:
    """Path B reaches validated when title + image + long description
    + category_path + at least one taxonomy signal are all present.
    Path B never reaches published — pdp_scope is NULL in this script."""
    row = {
        "title": "Hydrating Vitamin C Serum",
        "mirrored_description": "Daily-use vegan serum with niacinamide for brightening and hydration support.",
        "image_url": "https://example.com/serum.jpg",
    }
    category_meta = {"category_path": "beauty/skincare/serum"}
    taxonomy = {
        "demographic": "women",
        "use_case_tags": ["daily"],
        "lifestyle_tags": ["vegan"],
    }
    stage = _compute_mirror_lifecycle_stage(
        row, category_meta, ["k-beauty"], taxonomy
    )
    assert stage == "validated"


def test_mirror_lifecycle_candidate_when_no_category_path() -> None:
    """Without category_path the row caps at candidate — same gate as
    the lifecycle module, applied via the script's helper."""
    row = {
        "title": "A Good Product",
        "mirrored_description": "A long enough description to pass the candidate description gate.",
        "image_url": "https://example.com/x.jpg",
    }
    category_meta = {"category_path": None}
    taxonomy = {"demographic": None, "use_case_tags": [], "lifestyle_tags": []}
    stage = _compute_mirror_lifecycle_stage(row, category_meta, [], taxonomy)
    assert stage == "candidate"


def test_mirror_lifecycle_draft_for_thin_content() -> None:
    """Empty seed → draft. Mirror script must still write the column,
    not skip it."""
    row = {"title": None, "mirrored_description": None, "image_url": None}
    category_meta = {"category_path": None}
    taxonomy = {"demographic": None, "use_case_tags": [], "lifestyle_tags": []}
    stage = _compute_mirror_lifecycle_stage(row, category_meta, [], taxonomy)
    assert stage == "draft"
