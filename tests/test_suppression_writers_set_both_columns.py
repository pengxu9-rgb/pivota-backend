"""P1a (#1648) — every suppression writer must set BOTH columns.

`suppressed_at` is THE gate column: every SQL lane reads it, IPS reads it
(`index_pipeline_state_service`: `row_suppressed = suppressed_at is not None`),
and after #1650/#1655/#1657 so do recall, the by-key doors and the quote door.
`suppression_reason` is the LABEL — and `catalog_trust_policy.
_derive_source_lifecycle` tombstones on the label alone.

So a row with the label and no timestamp is simultaneously "withdrawn" to the
trust policy and "clean" to everything that decides serving. That is not a
hypothetical: 2,332 rows across seven cohorts were in exactly that state on
2026-07-30, which is how a retired test rig kept serving on public search, and
the whole reason #1648 exists.

The backfill converged the DATA. These tests pin the WRITERS, in both
directions:

  * suppress must set both columns — eight writers set the label alone;
  * revert must clear both — every revert path cleared the label alone, which
    after the backfill meant the row kept its timestamp and stayed gated
    forever. A revert that silently does not revert.

Read as source, deliberately. These are one-off ops scripts and asyncpg-style
`$1` SQL; standing each up against a live DB to observe the write would cost
more than it proves, and the property worth protecting is a one-line shape of
the statement. The risk of a source-scan — that it silently matches nothing —
is covered by asserting the writer inventory itself is non-empty and by naming
each file, so deleting or renaming one fails here rather than passing quietly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import pytest


_REPO = Path(__file__).resolve().parents[1]

# Every writer that sets catalog_products.suppression_reason. Named explicitly
# rather than globbed: a rename must break this list loudly, and a source scan
# that quietly matches nothing is the failure mode this file most needs to
# avoid.
_SUPPRESS_WRITERS: List[str] = [
    "scripts/step5_lane1_dedup_92sfrj.py",
    "scripts/step5_lane2_same_url_dedup.py",
    "scripts/step5_lane4_ownist_twin_cut.py",
    "scripts/step5_sweep_orphan_mirrors.py",
    "scripts/retire_test_rig_merch_efbc.py",
    "scripts/cleanup_niacinamide_test_variants.py",
    "scripts/onboard_external_brand_from_crawl.py",
    "scripts/merge_duplicate_canonicals.py",
    "scripts/repair_orphan_shopify_offers.py",
    "services/identity_resolution.py",
]

# `UPDATE <table> SET …` up to the WHERE / statement end.
_UPDATE = re.compile(
    r"UPDATE\s+(?:catalog_products|catalog_skus|catalog_offers)\b[^;]*?\bSET\b(.*?)"
    r"(?:\bWHERE\b|\bRETURNING\b|\"\"\"|'''|;|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# CAPTURE the right-hand side rather than using a negative lookahead. The
# obvious `suppression_reason\s*=\s*(?!NULL)` is WRONG: `\s*` backtracks to
# zero width, which puts the lookahead in front of the SPACE before `NULL`, and
# `(?!NULL)` then passes. It flagged every revert recipe as a suppression write.
_ASSIGN_REASON = re.compile(
    r"(?<!\w)suppression_reason\s*=\s*(\S+)", re.IGNORECASE
)
_ASSIGN_TIMESTAMP = re.compile(
    r"(?<!\w)suppressed_at\s*=\s*(\S+)", re.IGNORECASE
)


def _rhs(pattern: "re.Pattern[str]", body: str) -> List[str]:
    """Right-hand sides assigned to a column in one SET body, normalised."""
    return [m.group(1).rstrip(",").strip().upper() for m in pattern.finditer(body)]


def _sets_reason(body: str) -> bool:
    return any(v != "NULL" for v in _rhs(_ASSIGN_REASON, body))


def _clears_reason(body: str) -> bool:
    return any(v == "NULL" for v in _rhs(_ASSIGN_REASON, body))


def _touches_timestamp(body: str) -> bool:
    return bool(_rhs(_ASSIGN_TIMESTAMP, body))


def _clears_timestamp(body: str) -> bool:
    return any(v == "NULL" for v in _rhs(_ASSIGN_TIMESTAMP, body))


def _statements(path: str) -> List[Tuple[int, str]]:
    source = (_REPO / path).read_text(encoding="utf-8")
    return [
        (source[: m.start()].count("\n") + 1, m.group(1))
        for m in _UPDATE.finditer(source)
    ]


def test_the_writer_inventory_is_real():
    """A source scan that matches nothing also passes. Floor both the file list
    and the statements found, so an empty scan fails instead of going green."""
    assert len(_SUPPRESS_WRITERS) >= 10
    for path in _SUPPRESS_WRITERS:
        assert (_REPO / path).is_file(), f"{path} moved or was renamed — update this list"
    total = sum(len(_statements(p)) for p in _SUPPRESS_WRITERS)
    assert total >= 15, f"only {total} UPDATE statements found — the scan is not working"


@pytest.mark.parametrize("path", _SUPPRESS_WRITERS)
def test_setting_a_suppression_reason_also_sets_suppressed_at(path: str):
    """The 2,332-row defect. A writer that sets the label without the timestamp
    mints rows that are tombstoned to the trust policy and clean to every
    serving gate."""
    offenders = [
        line for line, body in _statements(path)
        if _sets_reason(body) and not _touches_timestamp(body)
    ]
    assert offenders == [], (
        f"{path}: UPDATE(s) at line(s) {offenders} set suppression_reason without "
        "touching suppressed_at. suppressed_at is THE gate column — writing the "
        "label alone leaves the row serving. Use "
        "`suppressed_at = COALESCE(suppressed_at, NOW())`."
    )


@pytest.mark.parametrize("path", _SUPPRESS_WRITERS)
def test_clearing_a_suppression_reason_also_clears_suppressed_at(path: str):
    """The mirror, and the one the 2026-07-30 backfill made live: before it,
    reverting by clearing the label worked because these rows had no timestamp.
    After it, the same revert leaves `suppressed_at` set and the row gated
    forever — a revert that silently does not revert."""
    offenders = [
        line for line, body in _statements(path)
        if _clears_reason(body) and not _clears_timestamp(body)
    ]
    assert offenders == [], (
        f"{path}: UPDATE(s) at line(s) {offenders} clear suppression_reason "
        "without clearing suppressed_at, so the row stays gated by every "
        "suppressed_at reader. A revert must clear both."
    )


def test_the_matchers_see_what_they_claim_to():
    """The gate is only as good as its matcher; one that matches nothing makes
    every file above pass. These are the shapes actually present in the writers
    — asyncpg `$1`, named binds, inline literals — plus the backtracking trap
    that made the first version flag every revert recipe as a suppression."""
    must_flag_suppress = [
        "UPDATE catalog_products SET suppression_reason = $2, updated_at = NOW() WHERE x",
        "UPDATE catalog_products SET suppression_reason=:reason, updated_at=NOW() WHERE x",
        "UPDATE catalog_skus SET suppression_reason = 'dup' WHERE x",
    ]
    for sql in must_flag_suppress:
        body = _UPDATE.search(sql).group(1)
        assert _sets_reason(body) and not _touches_timestamp(body), f"blind to: {sql}"

    must_pass_suppress = [
        "UPDATE catalog_products SET suppression_reason = $2, "
        "suppressed_at = COALESCE(suppressed_at, NOW()) WHERE x",
        "UPDATE catalog_skus SET suppressed_at = NOW(), suppression_reason = :r WHERE x",
    ]
    for sql in must_pass_suppress:
        body = _UPDATE.search(sql).group(1)
        assert _touches_timestamp(body), f"false positive on: {sql}"

    # THE TRAP: a label-only revert must read as a CLEAR, never as a SET.
    revert = _UPDATE.search(
        "UPDATE catalog_products SET suppression_reason = NULL, "
        "suppression_metadata = NULL, updated_at = NOW() WHERE x"
    ).group(1)
    assert _clears_reason(revert), "revert-scan blind to a label-only clear"
    assert not _sets_reason(revert), (
        "a NULL clear read as a suppression write — this is the `\\s*` "
        "backtracking hole the helpers exist to avoid"
    )
    assert not _clears_timestamp(revert)

    full_revert = _UPDATE.search(
        "UPDATE catalog_products SET suppression_reason = NULL, "
        "suppressed_at = NULL WHERE x"
    ).group(1)
    assert _clears_reason(full_revert) and _clears_timestamp(full_revert)


def test_the_invariant_checks_cover_both_directions():
    """The runtime half. The source gate stops a writer landing; these two
    invariants catch a row that got into a split state anyway — by a path
    nobody scanned, a manual UPDATE, or a migration."""
    from services.catalog_invariant_checks import _CHECKS

    names = {c["name"] for c in _CHECKS}
    assert "suppression_reason_without_timestamp" in names
    assert "suppression_timestamp_without_reason" in names
    for check in _CHECKS:
        if check["name"].startswith("suppression_"):
            assert check["default_threshold"] == 0, (
                f"{check['name']}: there is no acceptable number of split-column rows"
            )
