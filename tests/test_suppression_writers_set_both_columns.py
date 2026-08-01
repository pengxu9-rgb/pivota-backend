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
    "scripts/step5_lane3_campaign_clone_dedup.py",
    "scripts/step5_lane4_ownist_twin_cut.py",
    "scripts/step5_sweep_orphan_mirrors.py",
    "scripts/retire_test_rig_merch_efbc.py",
    "scripts/cleanup_niacinamide_test_variants.py",
    "scripts/onboard_external_brand_from_crawl.py",
    "scripts/merge_duplicate_canonicals.py",
    "scripts/repair_orphan_shopify_offers.py",
    # The only RUNTIME writer — everything else here is a one-off ops script.
    "services/catalog_sync_service.py",
    "services/identity_resolution.py",
    # Migrations are executable too, and reachable over HTTP:
    # routes/admin_run_migration_pending.py globs db/migrations/{n}_*.sql.
    "db/migrations/139_tombstone_cross_merchant_redundant_external_seed.sql",
    "db/migrations/146_deactivate_jumiso_niacinamide_dup_seeds.sql",
    "db/migrations/down/139_tombstone_cross_merchant_redundant_external_seed_down.sql",
]

# `services/catalog_sync_service.py` also clears these columns through an ORM
# payload (`_upsert_by_pk` with {"suppression_reason": None, "suppressed_at":
# None}) which no SQL regex can see. It happens to clear BOTH, and
# `_preserve_non_stale_suppression` is what stops a sync tick wiping a live
# tombstone — asserted separately below rather than left to the scan.
_ORM_CLEAR_SITES = "services/catalog_sync_service.py"

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
# The timestamp RHS captures to END OF LINE, not `(\S+)`: the real writers say
# `suppressed_at = COALESCE(suppressed_at, NOW())`, and `(\S+)` stops at the
# space inside the COALESCE, yielding "COALESCE(suppressed_at," — which contains
# no gating token, so every correctly-fixed writer read as broken.
# The REASON side deliberately keeps `(\S+)`: widening it would make
# `suppression_reason = NULL, suppressed_at = NULL` capture the whole tail, and
# the clear-detection would stop recognising it as a NULL.
_ASSIGN_TIMESTAMP = re.compile(
    r"(?<!\w)suppressed_at\s*=\s*([^\n]+)", re.IGNORECASE
)


def _rhs(pattern: "re.Pattern[str]", body: str) -> List[str]:
    """Right-hand sides assigned to a column in one SET body, normalised."""
    return [m.group(1).rstrip(",").strip().upper() for m in pattern.finditer(body)]


def _sets_reason(body: str) -> bool:
    return any(v != "NULL" for v in _rhs(_ASSIGN_REASON, body))


def _clears_reason(body: str) -> bool:
    return any(v == "NULL" for v in _rhs(_ASSIGN_REASON, body))


def _gates_with_timestamp(body: str) -> bool:
    """Assigns suppressed_at a value that actually GATES the row.

    `bool(_rhs(...))` was not enough — it asked only whether the column was
    assigned, so `suppressed_at = NULL` (row written fully un-gated) and
    `suppressed_at = suppressed_at` (a no-op) both passed. Both are realistic:
    every one of these files now carries `SET suppression_reason = NULL,
    suppressed_at = NULL` as a revert recipe ~30 lines above its suppress SQL,
    so copy-paste from the wrong block is the live path.
    """
    # ALLOWLIST, not a denylist. The denylist version excluded only `NULL` and
    # a bare self-assign, so three shapes still passed while leaving the row
    # un-gated or unchanged: `cp.suppressed_at` (a qualified no-op — four of
    # these files use `UPDATE catalog_products cp`), `COALESCE(suppressed_at,
    # NULL)` (a no-op), and `NULL::timestamptz` (actively un-gates). Casts are
    # idiomatic in this repo (`$3::jsonb`), so that last one is not theoretical.
    # Require the RHS to contain something that can actually produce a time.
    gating = ("NOW(", "CURRENT_TIMESTAMP", "$", ":")
    for value in _rhs(_ASSIGN_TIMESTAMP, body):
        if value.lstrip().startswith("NULL"):
            continue  # `NULL`, `NULL::timestamptz` — writes the row un-gated
        if "COALESCE" in value and "NULL" in value and "NOW" not in value:
            continue  # `COALESCE(suppressed_at, NULL)` — a no-op
        if any(token in value for token in gating):
            return True
    return False


def _clears_timestamp(body: str) -> bool:
    """PREFIX check, not equality. The timestamp RHS captures to end of line, so
    `suppressed_at = NULL, suppression_reason = NULL` yields the whole tail —
    equality against "NULL" stopped recognising a real clear. Prefix also
    accepts `NULL::timestamptz`, which clears just as thoroughly."""
    return any(v.lstrip().startswith("NULL") for v in _rhs(_ASSIGN_TIMESTAMP, body))


_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_sql_comments(source: str) -> str:
    """Blank out `--` comments, preserving offsets so line numbers stay true.

    Comments are not part of the statement, and leaving them in breaks the scan
    in both directions: a `;` inside a comment TERMINATES the captured SET body
    (this bit immediately — a comment I added reading "suppressed_at is THE gate
    column; the label alone…" truncated the capture and made a correctly-fixed
    migration look broken), and the word WHERE in a comment would do the same.
    """
    return _LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), source)


def _statements(path: str) -> List[Tuple[int, str]]:
    source = _strip_sql_comments((_REPO / path).read_text(encoding="utf-8"))
    return [
        (source[: m.start()].count("\n") + 1, m.group(1))
        for m in _UPDATE.finditer(source)
    ]


def test_the_writer_inventory_is_real():
    """A source scan that matches nothing also passes. Floor both the file list
    and the statements found, so an empty scan fails instead of going green."""
    assert len(_SUPPRESS_WRITERS) >= 15
    for path in _SUPPRESS_WRITERS:
        assert (_REPO / path).is_file(), f"{path} moved or was renamed — update this list"
    total = sum(len(_statements(p)) for p in _SUPPRESS_WRITERS)
    assert total >= 20, f"only {total} UPDATE statements found — the scan is not working"


@pytest.mark.parametrize("path", _SUPPRESS_WRITERS)
def test_setting_a_suppression_reason_also_sets_suppressed_at(path: str):
    """The 2,332-row defect. A writer that sets the label without the timestamp
    mints rows that are tombstoned to the trust policy and clean to every
    serving gate."""
    offenders = [
        line for line, body in _statements(path)
        if _sets_reason(body) and not _gates_with_timestamp(body)
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
        assert _sets_reason(body) and not _gates_with_timestamp(body), f"blind to: {sql}"

    must_pass_suppress = [
        "UPDATE catalog_products SET suppression_reason = $2, "
        "suppressed_at = COALESCE(suppressed_at, NOW()) WHERE x",
        "UPDATE catalog_skus SET suppressed_at = NOW(), suppression_reason = :r WHERE x",
    ]
    for sql in must_pass_suppress:
        body = _UPDATE.search(sql).group(1)
        assert _gates_with_timestamp(body), f"false positive on: {sql}"

    # A suppress that assigns the timestamp a NON-GATING value must still be
    # flagged: `= NULL` writes the row un-gated, `= suppressed_at` is a no-op.
    # Both passed the first version, which only asked "was the column assigned".
    for sql in (
        "UPDATE catalog_products SET suppression_reason = $2, suppressed_at = NULL WHERE x",
        "UPDATE catalog_products SET suppression_reason = $2, "
        "suppressed_at = suppressed_at WHERE x",
        # qualified no-op — four writers use `UPDATE catalog_products cp`
        "UPDATE catalog_products cp SET suppression_reason = $2, "
        "suppressed_at = cp.suppressed_at WHERE x",
        # cast that un-gates; casts are idiomatic here ($3::jsonb)
        "UPDATE catalog_products SET suppression_reason = $2, "
        "suppressed_at = NULL::timestamptz WHERE x",
        # COALESCE to NULL is a no-op
        "UPDATE catalog_products SET suppression_reason = $2, "
        "suppressed_at = COALESCE(suppressed_at, NULL) WHERE x",
    ):
        body = _UPDATE.search(sql).group(1)
        assert _sets_reason(body) and not _gates_with_timestamp(body), (
            f"a non-gating timestamp assignment was accepted: {sql}"
        )

    # A `;` or the word WHERE inside a `--` comment must not truncate the SET
    # body. This bit: a comment reading "…gate column; the label alone…" cut
    # the capture short and made a correctly-fixed migration look broken.
    commented = _strip_sql_comments(
        "UPDATE catalog_products\n"
        "SET suppression_reason = 'x',\n"
        "    -- suppressed_at is THE gate column; the label alone leaves it serving\n"
        "    suppressed_at = COALESCE(suppressed_at, now())\n"
        "WHERE y"
    )
    body = _UPDATE.search(commented).group(1)
    assert _gates_with_timestamp(body), (
        "a semicolon inside a SQL comment truncated the SET body"
    )

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


def test_the_orm_clear_sites_clear_both_columns():
    """`_upsert_by_pk` payload clears in catalog_sync_service are invisible to
    any SQL scan — they are dict literals. The scan CANNOT see them, so assert
    them directly rather than let "every writer is covered" be false."""
    source = (_REPO / _ORM_CLEAR_SITES).read_text(encoding="utf-8")
    reason_clears = source.count('"suppression_reason": None')
    timestamp_clears = source.count('"suppressed_at": None')
    assert reason_clears > 0, "expected ORM payload clears — did the file move?"
    assert timestamp_clears >= reason_clears, (
        f"{_ORM_CLEAR_SITES}: {reason_clears} payload(s) clear suppression_reason "
        f"but only {timestamp_clears} clear suppressed_at. An ORM clear that "
        "drops the label alone leaves the row gated forever."
    )


def test_the_sync_tick_cannot_wipe_a_live_tombstone_on_either_column():
    """`_preserve_non_stale_suppression` is what stops a routine sync tick
    clearing a suppression. It must test BOTH columns — testing the label alone
    would let a tick wipe a timestamp-only tombstone."""
    source = (_REPO / _ORM_CLEAR_SITES).read_text(encoding="utf-8")
    start = source.index("def _preserve_non_stale_suppression")
    body = source[start : source.index("\ndef ", start + 10)]
    assert "suppression_reason" in body and "suppressed_at" in body, (
        "_preserve_non_stale_suppression no longer reads both columns"
    )
