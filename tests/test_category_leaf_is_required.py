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


# ---------------------------------------------------------------------------------------------
# THE ONE FENCE THAT REACHES EVERY WRITER.
#
# `category_path` is written by at least four committed lanes across two repos and two languages --
# the external-seed mirror, the Ulta retailer mirror, the reviewed-category patch script, the
# enrichment agent -- plus at least one out-of-repo actor: `category_label_source = 'codex_review_v1'`
# appears in prod and in ZERO commits in either repository, i.e. hand-run SQL. No writer-side guard
# can reach that last one. `_classify_product` runs once per row wherever the row came from.

def test_the_serving_predicate_asks_for_a_leaf_not_a_taxonomy_node():
    """USE resolve(), NOT has_category_door(). That distinction IS the bug.

    `ANCESTOR_NODES` is built with `range(1, len(parts))`, so i == 1 is included and every top-level
    domain is a taxonomy node with a "category door". A row parked on a bare domain therefore passes
    every off-taxonomy health check while being unretrievable by category at serving -- which is why
    this cohort survived a taxonomy standardisation pass with nothing measuring it.
    """
    from services.category_path_aliases import has_category_door, resolve
    from services.index_pipeline_state_service import _has_resolvable_leaf_category

    for root in ("beauty", "fashion", "electronics"):
        assert has_category_door(root) is True, (
            f"{root} is a taxonomy node -- this is the trap, and the reason a door check cannot "
            "be used here"
        )
        assert resolve(root) is None
        assert _has_resolvable_leaf_category(root) is False

    for leaf in ("beauty/fragrance/perfume", "beauty/makeup/face/bronzer"):
        assert _has_resolvable_leaf_category(leaf) is True

    assert _has_resolvable_leaf_category(None) is False
    assert _has_resolvable_leaf_category("") is False


def test_the_enforcement_is_off_by_default_because_it_delists_rows():
    """Correct, and unsafe to arm blind.

    A wrongly categorised row still sells; emptying the index to punish bad metadata helps nobody.
    The flag is the moment of enforcement and it waits on a count.
    """
    import os

    from services.index_pipeline_state_service import _leaf_category_required_for_serving

    prior = os.environ.pop("CATEGORY_LEAF_REQUIRED_FOR_SERVING", None)
    try:
        assert _leaf_category_required_for_serving() is False
        for on in ("1", "true", "TRUE", "yes", "on"):
            os.environ["CATEGORY_LEAF_REQUIRED_FOR_SERVING"] = on
            assert _leaf_category_required_for_serving() is True
        for off in ("0", "false", "no", ""):
            os.environ["CATEGORY_LEAF_REQUIRED_FOR_SERVING"] = off
            assert _leaf_category_required_for_serving() is False
    finally:
        os.environ.pop("CATEGORY_LEAF_REQUIRED_FOR_SERVING", None)
        if prior is not None:
            os.environ["CATEGORY_LEAF_REQUIRED_FOR_SERVING"] = prior


def test_the_predicate_reads_a_column_the_query_actually_selects():
    """A gate reading a field the SQL never selects is a gate that always fails closed.

    `cp.category_path` was not in the projection before this change; without it every row would look
    uncategorised the moment the flag is armed.
    """
    src = (REPO_ROOT / "services" / "index_pipeline_state_service.py").read_text(encoding="utf-8")
    assert "cp.category_path," in src, "the projection must select category_path"
    assert 'row.get("category_path")' in src, "and the classifier must read it"
