"""The canonical row→listing join, and a lint that stops it being retyped.

WHY A LINT AND NOT JUST A HELPER. On 2026-07-31 a new consumer of
`pdp_identity_listing` was written by hand IN A FILE THAT ALREADY IMPORTED the
shared constants it needed. It omitted `merchant_id` (the join fans out, per
ADR-008 `source_listing_ref` = merchant_id:product_id) and the minted-lane CASE
(the whole Path-C lane reads NULL, and `count(DISTINCT)` skips NULLs, so that
lane became invisible to the check). Both defects were found by review, not by
tests, and neither is a syntax error or a test failure — which is exactly why
documentation has not been able to stop this class.

The one defense in this repo that has never drifted is the byte-identical SQL
pin between the two repos. These tests apply the same idea to a join that
identity defects keep landing on: use the helper, or the suite tells you which
helper to use.
"""

from __future__ import annotations

import pathlib
import re

from services.identity_join_sql import (
    MINTED_SOURCE_SYSTEM,
    identity_listing_lateral_sql,
    identity_listing_product_id_sql,
    minted_seed_external_id_sql,
)

_REPO = pathlib.Path(__file__).resolve().parent.parent


def test_the_join_carries_the_merchant_conjunct():
    """`source_listing_ref` is merchant_id:product_id (ADR-008), so product_id
    alone is not unique and the join fans out without this."""
    assert "pil.merchant_id = cp.merchant_id" in identity_listing_lateral_sql("cp")


def test_the_lane_test_routes_minted_rows_through_their_seed():
    """A Path-C row's source_product_id is a name slug, not a seed id. Without
    the CASE the join misses for the entire minted lane."""
    sql = identity_listing_product_id_sql("cp")
    assert f"cp.source_system = '{MINTED_SOURCE_SYSTEM}'" in sql
    assert "attached_product_key = cp.product_key" in sql
    assert "ELSE cp.source_product_id" in sql


def test_the_seed_pick_uses_the_shared_order_constants():
    """Path-C attaches one seed PER OFFER, so the pick is not a formality.
    MINTED_SEED_IDENTITY_LEG prefers a seed that CARRIES a listing — the
    winner's external_product_id IS the join key. A bare `updated_at DESC`
    picks an identity-less sibling (state goes uncounted) or a stale inactive
    seed (clean state counts as broken); both measured 2026-07-31."""
    from services.source_quarantine import MINTED_SEED_IDENTITY_LEG, SEED_PICK_ORDER

    sql = minted_seed_external_id_sql("cp")
    assert MINTED_SEED_IDENTITY_LEG in sql
    assert SEED_PICK_ORDER in sql
    assert "ORDER BY s.updated_at DESC" not in sql


def test_the_lateral_yields_at_most_one_listing():
    """A fanned-out listing corrupts any aggregate over the result, and a LIMIT
    without an ORDER BY is plan-dependent."""
    sql = identity_listing_lateral_sql("cp")
    assert "LEFT JOIN LATERAL" in sql
    assert "ORDER BY pil.source_listing_ref" in sql
    assert "LIMIT 1" in sql


def test_the_alias_is_honoured_without_breaking_correlation():
    sql = identity_listing_lateral_sql("c2", alias="lst")
    assert "lst.merchant_id = c2.merchant_id" in sql
    assert "FROM pdp_identity_listing lst" in sql
    assert "cp." not in sql.replace("catalog_products", "")


# ---------------------------------------------------------------------------
# THE LINT
# ---------------------------------------------------------------------------

# `pil.product_id = <something>.source_product_id` with no merchant conjunct
# nearby. Deliberately narrow: it matches the exact hand-rolled shape that has
# shipped twice, not every mention of the column.
_NAIVE_JOIN = re.compile(r"\.product_id\s*=\s*\w+\.source_product_id")
_MERCHANT_CONJUNCT = re.compile(r"\.merchant_id\s*=\s*\w+\.merchant_id")

# A hand-rolled seed pick, NARROWED to the harmful shape: selecting the attached
# seed's `external_product_id` — the value that BECOMES a join key — ordered by
# recency instead of the shared constants.
#
# Deliberately does NOT fire on picking a seed's CONTENT (seed_data, title):
# index_pipeline_state_service does that on both its Postgres and SQLite paths,
# consistently, and it is a different question. MINTED_SEED_IDENTITY_LEG prefers
# a seed that carries a listing precisely because the winner's
# external_product_id is the pil join key; a content lookup has no such
# constraint and `updated_at DESC` is the right answer there.
_NAIVE_SEED_PICK = re.compile(
    r"SELECT\s+\w+\.external_product_id[\s\S]{0,300}?"
    r"attached_product_key\s*=\s*\w+\.product_key[\s\S]{0,300}?"
    r"ORDER BY\s+\w+\.updated_at")

# Files that legitimately contain the canonical form, or a differently-shaped
# resolution that the reviewer confirmed is correct. Each entry is a decision,
# not a mute button — add one only with a reason.
_ALLOWED = {
    # Owns the canonical page-scan form (the `minted_seed_one` CTE). The shared
    # helper is the correlated one-row-at-a-time shape; both are pinned to agree
    # by test_the_two_shapes_agree_on_the_lane_decision below.
    "services/catalog_row_trust_upserter.py",
    # The helper itself.
    "services/identity_join_sql.py",
    # This file.
    "tests/test_identity_join_sql.py",
}


def _strip_comments(text: str) -> str:
    """Blank out comment lines, preserving line numbers.

    Without this the lint matches its own explanation: the modules that document
    this defect QUOTE the bad pattern to describe it, and a lint that fires on
    the comment warning about a bug is a lint people mute.
    """
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        out.append("" if stripped.startswith(("--", "#")) else line)
    return "\n".join(out)


def _python_sources():
    for base in ("services", "jobs", "routes", "scripts"):
        for path in (_REPO / base).rglob("*.py"):
            rel = path.relative_to(_REPO).as_posix()
            if rel not in _ALLOWED:
                yield rel, _strip_comments(
                    path.read_text(encoding="utf-8", errors="ignore"))


def test_no_hand_rolled_identity_join_without_the_merchant_conjunct():
    """The defect that shipped: `pil.product_id = cp.source_product_id` alone.

    `source_listing_ref` is merchant_id:product_id, so product_id is not unique;
    the join fans out and injects a foreign merchant's group into the aggregate.
    Use services.identity_join_sql.identity_listing_lateral_sql.
    """
    offenders = []
    for rel, text in _python_sources():
        for m in _NAIVE_JOIN.finditer(text):
            window = text[max(0, m.start() - 400): m.end() + 400]
            if not _MERCHANT_CONJUNCT.search(window):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{rel}:{line}  {m.group(0)}")
    assert not offenders, (
        "hand-rolled identity join without a merchant_id conjunct — use "
        "services.identity_join_sql.identity_listing_lateral_sql:\n  "
        + "\n  ".join(offenders))


def test_no_hand_rolled_minted_seed_pick():
    """The other half: picking the attached seed by `updated_at` instead of
    MINTED_SEED_IDENTITY_LEG + SEED_PICK_ORDER. Diverges from the upserter in
    BOTH directions — measured 2026-07-31."""
    offenders = []
    for rel, text in _python_sources():
        for m in _NAIVE_SEED_PICK.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "hand-rolled minted-seed pick — use "
        "services.identity_join_sql.minted_seed_external_id_sql:\n  "
        + "\n  ".join(offenders))


def test_the_lint_actually_matches_the_shape_it_claims():
    """A lint that matches nothing is worse than no lint — it reads as coverage.
    Pin it against the exact string that shipped, and against the fixed form."""
    bad = "LEFT JOIN pdp_identity_listing pil ON pil.product_id = cp.source_product_id"
    assert _NAIVE_JOIN.search(bad)
    assert not _MERCHANT_CONJUNCT.search(bad)

    good = identity_listing_lateral_sql("cp")
    assert _MERCHANT_CONJUNCT.search(good), "the canonical form must satisfy the lint"

    bad_pick = ("SELECT s.external_product_id FROM external_product_seeds s "
                "WHERE s.attached_product_key = cp.product_key "
                "ORDER BY s.updated_at DESC LIMIT 1")
    assert _NAIVE_SEED_PICK.search(bad_pick)
    assert not _NAIVE_SEED_PICK.search(minted_seed_external_id_sql("cp"))

    # And it must NOT fire on a CONTENT lookup, which is a different question.
    content_pick = ("SELECT eps.seed_data FROM external_product_seeds eps "
                    "WHERE eps.attached_product_key = cp.product_key "
                    "ORDER BY eps.updated_at DESC LIMIT 1")
    assert not _NAIVE_SEED_PICK.search(content_pick)

    # Comment prose describing the defect must not trip the join lint — the
    # modules that document this bug quote the bad pattern verbatim.
    assert not _NAIVE_JOIN.search(_strip_comments(
        "-- the naive `pil.product_id = cp.source_product_id` is wrong"))


def test_the_two_shapes_agree_on_the_lane_decision():
    """The upserter's `minted_seed_one` CTE and this module's correlated form
    are different SQL for the same decision. Neither can be expressed in the
    other's context, so they cannot be de-duplicated — but they MUST agree, and
    nothing but this test says so."""
    upserter = (_REPO / "services" / "catalog_row_trust_upserter.py").read_text()

    # Same lane marker.
    assert MINTED_SOURCE_SYSTEM in upserter
    # Same merchant conjunct on the listing join.
    assert _MERCHANT_CONJUNCT.search(upserter)
    # Same seed-pick constants (the CTE inlines them via SEED_PICK_ORDER etc).
    assert "MINTED_SEED_IDENTITY_LEG" in upserter
    assert "SEED_PICK_ORDER" in upserter
