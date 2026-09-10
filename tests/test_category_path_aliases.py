"""The alias map, and the membership check it backs.

The defect being repaired is "somebody stored a category_path that is not in the taxonomy". Every
test here is written so that a map which could do the same thing while repairing it fails.
"""

from __future__ import annotations

import pytest

from services.category_path_aliases import (
    ALIASES,
    has_category_door,
    TAXONOMY_GAPS,
    TAXONOMY_LEAVES,
    resolve,
)


# --- the map cannot invent a category ----------------------------------------------------------


def test_every_alias_target_is_a_real_taxonomy_leaf():
    """THE LOAD-BEARING PROPERTY. A map that repairs `tone/toner` into another non-existent path
    has moved the defect, not fixed it. (Also asserted at import, so a bad edit fails collection —
    this pins that the assertion itself is real.)"""
    bad = sorted(set(ALIASES.values()) - TAXONOMY_LEAVES)
    assert not bad, bad


def test_no_alias_hides_a_path_that_is_already_correct():
    """Aliasing a real leaf away would silently re-file working products."""
    assert not sorted(p for p in ALIASES if p in TAXONOMY_LEAVES)


def test_gaps_and_aliases_are_disjoint():
    assert not (set(ALIASES) & set(TAXONOMY_GAPS))


# --- resolve ------------------------------------------------------------------------------------


def test_resolve_snaps_the_measured_near_misses():
    """The real prod cohorts, by row count."""
    assert resolve("beauty/skincare/treat/toner") == "beauty/skincare/tone/toner"
    assert resolve("beauty/skincare/sets") == "beauty/sets/gift-set"                  # 49
    assert resolve("beauty/makeup/lips/lip-balm") == "beauty/makeup/lip/balm"         # 12
    assert resolve("beauty/makeup/lips/lip-gloss") == "beauty/makeup/lip/gloss"       # 9
    assert resolve("beauty/body-care/deodorant") == "beauty/body/care"                # 11


def test_resolve_returns_a_real_leaf_unchanged():
    """The control for the test above: a map that returned a constant would pass it."""
    assert resolve("beauty/makeup/lip/lipstick") == "beauty/makeup/lip/lipstick"
    assert resolve("beauty/skincare/tone/toner") == "beauty/skincare/tone/toner"


def test_resolve_refuses_a_genuine_gap():
    """THE OTHER CONTROL, and the more important one. There is no honest leaf for a nail polish;
    filing it under a blush would make it findable by the WRONG query, which is louder than not
    being findable at all. These must stay off-taxonomy and stay counted."""
    for path in ("beauty/makeup/nails/nail-polish", "wellness/supplements",
                 "beauty/oral-care/toothpaste", "home/fragrance/candle"):
        assert resolve(path) is None, path


def test_resolve_refuses_something_it_has_never_seen():
    assert resolve("beauty/quantum/entanglement") is None
    assert resolve("") is None
    assert resolve(None) is None


def test_resolve_normalises_the_INPUT_because_recall_cannot():
    """Recall's `LIKE` is case-sensitive and does not trim, which is exactly WHY a mis-cased path is
    unreachable — so the repair has to catch what recall cannot. What comes back is always the
    canonical leaf, never a normalised copy of the input."""
    assert resolve("Beauty/Skincare/Treat/Toner") == "beauty/skincare/tone/toner"
    assert resolve("/beauty/makeup/lips/lip-gloss/") == "beauty/makeup/lip/gloss"
    assert resolve("  beauty/skincare/treat/toner  ") == "beauty/skincare/tone/toner"


def test_a_repaired_path_is_actually_REACHABLE_by_recall():
    """The point of the whole exercise, asserted against recall's own prefix builder rather than
    against the map's opinion of itself. A repair that produced another unreachable path would
    satisfy every test above."""
    from services.pdp_category_classifier import category_path_prefix_for_query

    for query, source in (
        ("toner", "beauty/skincare/treat/toner"),
        ("lip gloss", "beauty/makeup/lips/lip-gloss"),
        ("lipstick", "beauty/makeup/lips/lip-balm"),
    ):
        prefix = category_path_prefix_for_query(query)
        assert prefix, query
        repaired = resolve(source)
        assert repaired and repaired.startswith(prefix), (
            "%r -> %r does not match recall's prefix %r for %r"
            % (source, repaired, prefix, query)
        )
        # and the control: the ORIGINAL did not
        assert not source.startswith(prefix), (
            "%r already matched %r; it was not broken" % (source, prefix)
        )


# --- the LLM classifier now checks membership ----------------------------------------------------


def test_the_llm_validator_rejects_a_shape_valid_invention_UNDER_AN_INDEXED_ROOT():
    """`_PATH_RE` accepts `beauty/skincare/treat/toner`: right casing, right separators, allowed
    root. The old validator returned it and the caller stored it. Shape is not membership.

    The rejection is scoped to roots recall actually INDEXES, which is the narrow case where a
    plausible-but-fake path is harmful: it looks classified while having no door."""
    from services.category_classifier_llm import _validate_path

    assert _validate_path("beauty/skincare/treat/toner") == "beauty/skincare/tone/toner"
    assert _validate_path("beauty/makeup/nails/nail-polish") is None


def test_the_llm_validator_does_NOT_police_roots_recall_does_not_index():
    """THE REGRESSION AN EARLIER VERSION OF THIS CHANGE SHIPPED, caught by five pre-existing tests.

    The classifier serves eleven `_ALLOWED_ROOTS`; the recall taxonomy covers three
    (beauty, electronics, fashion). Requiring taxonomy membership everywhere turned a yoga mat into
    None — deleting a working classification to fix a beauty problem. `sports/yoga/mat` has no
    prefix in recall either way; that is not this function's business."""
    from services.category_classifier_llm import _validate_path

    assert _validate_path("sports/yoga/mat") == "sports/yoga/mat"
    assert _validate_path("wellness/supplements") == "wellness/supplements"
    assert _validate_path("food/snacks/bar") == "food/snacks/bar"


def test_a_path_can_be_REACHABLE_without_being_a_leaf():
    """`electronics` is the parent of the `electronics/ereader` leaf, so it is a live query prefix
    and `electronics/laptops/gaming` matches `LIKE 'electronics/%'` directly. Rejecting it for not
    being a leaf would throw away a classification that works."""
    from services.category_classifier_llm import _validate_path
    from services.category_path_aliases import has_category_door

    assert has_category_door("electronics/laptops/gaming")
    assert _validate_path("electronics/laptops/gaming") == "electronics/laptops/gaming"
    # ...and the control: `beauty` is NOT a leaf-parent, so the same shape under it has no door
    assert not has_category_door("beauty/quantum/entanglement")
    assert _validate_path("beauty/quantum/entanglement") is None


def test_has_category_door_agrees_with_recalls_own_prefix_builder():
    """Asserted against `category_path_prefix_for_query`, not against this module's opinion."""
    from services.pdp_category_classifier import category_path_prefix_for_query

    prefix = category_path_prefix_for_query("lipstick")
    assert prefix == "beauty/makeup/lip/"
    assert has_category_door("beauty/makeup/lip/lipstick")
    assert has_category_door("beauty/makeup/lip")          # ancestor: #2122 can admit it
    assert not has_category_door("beauty/makeup/lips/lip-gloss")   # sibling typo: no door


def test_the_llm_validator_still_accepts_a_real_leaf():
    """The control. A validator that rejected everything passes the test above."""
    from services.category_classifier_llm import _validate_path

    assert _validate_path("beauty/makeup/lip/lipstick") == "beauty/makeup/lip/lipstick"
    assert _validate_path("BEAUTY/MAKEUP/LIP/LIPSTICK") == "beauty/makeup/lip/lipstick"


def test_the_llm_validator_still_rejects_a_malformed_string():
    from services.category_classifier_llm import _validate_path

    assert _validate_path("not a path at all!") is None
    assert _validate_path(None) is None
    assert _validate_path(12) is None


# --- the map matches the cohort it was built for -------------------------------------------------


def test_the_map_covers_the_measured_production_cohort():
    """117 distinct off-taxonomy paths were measured on prod 2026-09-09. Each is either aliased or
    declared a gap; a path in neither would be silently left broken by a map that claims to have
    considered it. The counts are pinned so that trimming the map is a visible decision."""
    assert len(ALIASES) == 91, "alias count changed; re-measure before editing the expectation"
    assert len(TAXONOMY_GAPS) == 26
    assert len(ALIASES) + len(TAXONOMY_GAPS) == 117


@pytest.mark.parametrize(
    "path,rows",
    [
        ("beauty/skincare/treat/toner", 316),
        ("beauty/skincare/sets", 49),
        ("beauty/makeup/nails/nail-polish", 37),
        ("beauty/skincare/eye-care", 31),
    ],
)
def test_the_biggest_cohorts_are_each_decided(path, rows):
    """Named individually so the four largest cannot fall out of the map unnoticed."""
    assert path in ALIASES or path in TAXONOMY_GAPS, (path, rows)
