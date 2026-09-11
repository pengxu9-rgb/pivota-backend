"""A bare domain is not a category, and the system must not accept one as an answer.

THE DEFECT THIS PINS. `beauty` is a namespace, not an answer to "what is this". Treating it as an
answer broke the same cohort in two directions at once:

  - SERVING dropped those rows as `category_mismatch`, because the gate's per-category text rescue
    only ran when a path was ABSENT -- so a useless path was strictly worse than none.
  - THE BACKFILL never revisited them, because `WHERE category_path IS NULL` reads a useless path as
    already done.

Measured on the live index: of 50 rows returned for "eau de parfum", 19 are not on a fragrance path
and 16 sit on bare `beauty` -- the entire Ariana Grande line (Cloud, Ari, God Is A Woman, r.e.m.),
Cosmic Kylie Jenner, every PixiPerfume. The live agent door declines a citrus EDT saying the
candidates it was shown are "rose or amber/gourmand-leaning", which is the small correctly-pathed
subset it could see.

AND IT EXPLAINS WHY A STANDARDISATION PASS MISSED IT. `beauty` IS on the taxonomy, so an
off-taxonomy health check counts it healthy. A detector that cannot see the failure is why this
survived. These tests are the replacement: they make a non-leaf path a failure in the places that
can produce one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from services.pdp_category_classifier import (
    CATEGORY_PATTERNS,
    MIN_CATEGORISED_PATH_SEGMENTS,
    is_categorised_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKFILL = REPO_ROOT / "scripts" / "backfill_pdp_category_path.py"


@pytest.mark.parametrize(
    "path",
    ["beauty", "beauty/", "/beauty/", "  beauty  ", "", "   ", None, "fashion"],
)
def test_a_top_level_domain_is_not_a_categorisation(path):
    assert is_categorised_path(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "beauty/fragrance",
        "beauty/fragrance/perfume",
        "beauty/skincare/treat/serum",
        "beauty/makeup/face/blush",
        "/beauty/fragrance/perfume/",
    ],
)
def test_a_path_that_names_something_is_a_categorisation(path):
    assert is_categorised_path(path) is True


def test_the_classifier_can_never_emit_a_non_leaf_path():
    """The pattern table is the one thing in this repo that MINTS category paths.

    If it could emit a bare domain, every consumer downstream would inherit the defect. 72 paths
    today; the two shortest are `electronics/ereader` and `fashion/shoes`, both two segments.
    """
    offenders = [
        (label, path)
        for label, path, _pattern in CATEGORY_PATTERNS
        if not is_categorised_path(path)
    ]
    assert offenders == [], f"CATEGORY_PATTERNS would mint uncategorised paths: {offenders}"


def test_the_backfill_revisits_non_leaf_rows_not_just_nulls():
    """`WHERE category_path IS NULL` is why the cohort is permanently stranded.

    A row stamped `beauty` looks done to the backfill and unservable to search at the same time.
    Pinned on the SQL because there is no test database here to drive the script against, and the
    predicate is the whole fix.
    """
    src = BACKFILL.read_text(encoding="utf-8")
    select_and_update = re.findall(
        r"WHERE[^\"]*?category_path[^\"]*?(?=\n\s*\"\"\"|\n\s*ORDER BY|\n\s*LIMIT)",
        src,
        re.DOTALL,
    )
    assert select_and_update, "the backfill must still filter on category_path"
    for clause in select_and_update:
        assert "POSITION('/' IN category_path) = 0" in clause, (
            "both the SELECT and the UPDATE must treat a single-segment path as uncategorised; "
            f"found: {clause.strip()!r}"
        )
    # And the UPDATE must keep the same guard, or a concurrent writer's real path can be clobbered
    # between the read and the write.
    assert src.count("POSITION('/' IN category_path) = 0") >= 2


def test_the_rule_is_stated_once():
    """One definition, so the backfill, the serving gate and any future writer cannot disagree.

    PIVOTA-Agent carries the serving-side twin as `categoryPathIsCategorised` in src/server.js. If
    this constant moves, that one has to move with it -- they are the same rule about the same rows.
    """
    assert MIN_CATEGORISED_PATH_SEGMENTS == 2
    assert is_categorised_path("a/b") is True
    assert is_categorised_path("a") is False
