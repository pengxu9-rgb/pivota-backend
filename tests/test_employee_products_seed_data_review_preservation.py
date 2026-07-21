"""Tests for the _preserve_seed_data_review_fields helper that protects
codex-curated review/audit fields from being clobbered on re-extraction.

Background: prod audit 2026-05-09 found that `external_product_seeds.seed_data`
contained codex-written review fields on a small fraction of rows (253
with `review_summary`, 12 with `reviewed_ingredient_ids`, 10 with `audit`,
etc.), but the user reported the cycle of "codex reviewed → backfill
wiped review → codex reviewed again" never converged. Root cause:
`_upsert_storefront_referral_seed_candidate` builds `seed_data` from
scratch using only the fresh extraction and then UPDATEs the row,
overwriting the previously-curated review keys with extractor output
that doesn't include them.

This helper preserves the curated keys from the existing row when a
re-extraction lands. See routes/employee_products.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes.employee_products import (  # noqa: E402
    _SEED_DATA_REVIEW_PRESERVE_KEYS,
    _preserve_seed_data_review_fields,
)


def test_preserved_keys_set_matches_known_codex_fields() -> None:
    """Pin the exact set of keys preserved. If a future codex skill
    starts writing a new review-shaped key (e.g. `review_v2`), it
    needs to be added here or it will be silently wiped on the next
    extraction."""
    assert "review_summary" in _SEED_DATA_REVIEW_PRESERVE_KEYS
    assert "reviewed_ingredient_ids" in _SEED_DATA_REVIEW_PRESERVE_KEYS
    assert "reviewed_product_specs_v1" in _SEED_DATA_REVIEW_PRESERVE_KEYS
    assert "review_status" in _SEED_DATA_REVIEW_PRESERVE_KEYS
    assert "audit" in _SEED_DATA_REVIEW_PRESERVE_KEYS
    assert "audit_quarantine" in _SEED_DATA_REVIEW_PRESERVE_KEYS


def test_review_fields_carried_over_when_new_extraction_omits_them() -> None:
    """The trigger: catalog-intelligence re-extracts a product and the
    fresh seed_data has no review_summary / reviewed_ingredient_ids.
    Helper must merge the existing values into the new dict."""
    new = {
        "title": "Glow Bomb Lip Gloss",
        "description": "A glossy lip product",
        "brand": "Fenty Beauty",
    }
    existing = {
        "title": "OLD title (will be replaced)",  # non-review fields don't carry
        "review_summary": {"score": 0.9, "reviewer": "codex_v1"},
        "reviewed_ingredient_ids": ["niacinamide", "hyaluronic_acid"],
        "audit": {"status": "approved", "by": "codex_seed_correction"},
    }
    out = _preserve_seed_data_review_fields(new, existing)
    # Review fields preserved
    assert out["review_summary"] == {"score": 0.9, "reviewer": "codex_v1"}
    assert out["reviewed_ingredient_ids"] == ["niacinamide", "hyaluronic_acid"]
    assert out["audit"] == {"status": "approved", "by": "codex_seed_correction"}
    # Non-review fields use the new extraction (helper doesn't touch them)
    assert out["title"] == "Glow Bomb Lip Gloss"


def test_no_existing_review_fields_is_a_no_op() -> None:
    """First-time mirror of a product: existing seed_data has no review
    keys yet. Helper must not crash and must not invent fields."""
    new = {"title": "First mirror", "description": "..."}
    existing = {"title": "First mirror"}  # no review keys
    out = _preserve_seed_data_review_fields(new, existing)
    assert out == new
    for key in _SEED_DATA_REVIEW_PRESERVE_KEYS:
        assert key not in out


def test_existing_seed_data_none_is_safe() -> None:
    """If the row literally has no existing seed_data (None), the helper
    must return the new payload unchanged. Defensive — avoids a
    surprise None-handling crash during INSERT-style flows."""
    new = {"title": "x"}
    out = _preserve_seed_data_review_fields(new, None)
    assert out is new


def test_existing_seed_data_non_dict_is_safe() -> None:
    """Some legacy rows may have seed_data stored as a JSON string or a
    non-object; helper must not crash."""
    new = {"title": "x"}
    out = _preserve_seed_data_review_fields(new, "not-a-dict")  # type: ignore[arg-type]
    assert out is new

    out2 = _preserve_seed_data_review_fields(new, [])  # type: ignore[arg-type]
    assert out2 is new


def test_explicit_re_review_in_new_payload_wins() -> None:
    """If the new payload already has a non-null review field (rare —
    explicit re-review during the same write), it must NOT be replaced
    by the existing value. Otherwise an explicit fresh review would be
    impossible to land via this code path."""
    new = {
        "title": "x",
        "review_summary": {"score": 1.0, "reviewer": "codex_v2"},  # fresh re-review
    }
    existing = {
        "review_summary": {"score": 0.5, "reviewer": "codex_v1"},  # older review
    }
    out = _preserve_seed_data_review_fields(new, existing)
    assert out["review_summary"]["reviewer"] == "codex_v2"


def test_null_review_field_in_existing_does_not_clobber_new() -> None:
    """Existing has the key but it's null (rare edge — interrupted
    write?). Helper must not propagate null over a non-null new value
    (would make the field disappear from the output payload entirely
    via 'null overwrites null' which downstream sees as missing)."""
    new = {"review_summary": {"score": 0.7}}
    existing = {"review_summary": None}
    out = _preserve_seed_data_review_fields(new, existing)
    assert out["review_summary"] == {"score": 0.7}


def test_helper_mutates_and_returns_same_dict() -> None:
    """Ergonomics pin: callers can use either return value or rely on
    in-place mutation. Both should reflect the merged state."""
    new: dict = {"title": "x"}
    existing = {"review_summary": {"ok": True}}
    out = _preserve_seed_data_review_fields(new, existing)
    assert out is new
    assert new["review_summary"] == {"ok": True}
