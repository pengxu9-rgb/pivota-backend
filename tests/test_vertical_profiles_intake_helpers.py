"""Fix Plan B — unit tests for the shared intake structure helpers in
services.vertical_profiles (T3 unresolved accounting + T4 category normalization).

These are the pure functions both catalog_products write sites
(ingest_standard_products + the external-seed mirror) share, so pinning them here
keeps the two lanes from drifting. NULL-signal cases are covered explicitly —
the resolver never returns NULL (it returns 'other'), so "no structure at all"
must be detected from the signal fields, not the resolved value.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.vertical_profiles import (  # noqa: E402
    DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD,
    is_vertical_unresolved,
    normalize_category,
    summarize_unresolved_vertical,
)


# ---------------------------------------------------------------------------
# T4 — normalize_category: case/trim ONLY, no semantic renames
# ---------------------------------------------------------------------------


def test_normalize_category_lowercases_and_trims() -> None:
    assert normalize_category("  HairCare ") == "haircare"
    assert normalize_category("Skincare") == "skincare"
    assert normalize_category("BEAUTY PRODUCT") == "beauty product"


def test_normalize_category_collapses_internal_whitespace() -> None:
    assert normalize_category("Hair   Care") == "hair care"
    assert normalize_category("Beauty\tProduct") == "beauty product"


def test_normalize_category_blank_and_null_stay_null() -> None:
    """Empty / whitespace-only input must return None so a blank column stays
    NULL rather than an empty string."""
    assert normalize_category("") is None
    assert normalize_category("   ") is None
    assert normalize_category(None) is None


def test_normalize_category_does_not_rename_values() -> None:
    """Sanity: normalization is case/trim only. 'skincare' and 'Haircare' must
    NOT be collapsed into one another or into a canonical synonym."""
    assert normalize_category("skincare") == "skincare"
    assert normalize_category("Haircare") == "haircare"
    assert normalize_category("skincare") != normalize_category("Haircare")


# ---------------------------------------------------------------------------
# T3 — is_vertical_unresolved: 'other' AND no category/product_type/category_path
# ---------------------------------------------------------------------------


def test_unresolved_true_when_other_and_all_signals_null() -> None:
    """The core NULL-signal case: resolved 'other' with every structure field
    None -> the row carried NO machine-readable vertical at all."""
    assert is_vertical_unresolved(
        "other", {"product_type": None, "category": None, "category_path": None}
    ) is True


def test_unresolved_true_when_other_and_all_signals_blank_strings() -> None:
    """Whitespace-only / empty-string signals count as absent too."""
    assert is_vertical_unresolved(
        "other", {"product_type": "  ", "category": "", "category_path": None}
    ) is True
    # a product mapping missing the keys entirely is also unresolved
    assert is_vertical_unresolved("other", {}) is True


def test_unresolved_false_when_other_but_a_signal_present() -> None:
    """Resolved 'other' but carried category text = a lexicon GAP, not a
    structure gap. Must NOT count toward the brake (else the brake trips on
    genuinely-categorized-but-uncovered products)."""
    assert is_vertical_unresolved(
        "other", {"product_type": "Novelty Widget", "category": None, "category_path": None}
    ) is False
    assert is_vertical_unresolved(
        "other", {"product_type": None, "category": "gadgets", "category_path": None}
    ) is False
    assert is_vertical_unresolved(
        "other", {"product_type": None, "category": None, "category_path": "misc/other"}
    ) is False


def test_unresolved_false_for_any_resolved_vertical() -> None:
    """A row that resolved to a real vertical is never unresolved, regardless of
    signals (including the NULL-signal shape)."""
    for v in ("beauty", "fashion", "electronics"):
        assert is_vertical_unresolved(
            v, {"product_type": None, "category": None, "category_path": None}
        ) is False


def test_unresolved_false_when_resolved_is_none_or_empty() -> None:
    """Defensive: a None/empty resolved value is not the string 'other', so it
    is not counted as unresolved by this predicate."""
    assert is_vertical_unresolved(None, {}) is False
    assert is_vertical_unresolved("", {}) is False


# ---------------------------------------------------------------------------
# T3 — summarize_unresolved_vertical: brake verdict
# ---------------------------------------------------------------------------


def test_summary_fails_when_share_strictly_exceeds_threshold() -> None:
    s = summarize_unresolved_vertical(3, 10)  # 30% > 20% default
    assert s["unresolved_vertical"] == 3
    assert s["total"] == 10
    assert abs(s["share"] - 0.3) < 1e-9
    assert s["threshold"] == DEFAULT_UNRESOLVED_VERTICAL_FAIL_THRESHOLD
    assert s["should_fail"] is True
    assert s["summary"] == "unresolved_vertical: 3/10 (30.0%)"


def test_summary_passes_at_or_below_threshold() -> None:
    assert summarize_unresolved_vertical(2, 10)["should_fail"] is False  # 20% == threshold
    assert summarize_unresolved_vertical(1, 10)["should_fail"] is False


def test_summary_empty_run_never_fails() -> None:
    s = summarize_unresolved_vertical(0, 0)
    assert s["share"] == 0.0
    assert s["should_fail"] is False
    assert s["summary"] == "unresolved_vertical: 0/0 (0.0%)"


def test_summary_respects_custom_threshold() -> None:
    # 30% share, custom 50% threshold -> passes
    assert summarize_unresolved_vertical(3, 10, threshold=0.5)["should_fail"] is False
    # 30% share, custom 10% threshold -> fails
    assert summarize_unresolved_vertical(3, 10, threshold=0.1)["should_fail"] is True
