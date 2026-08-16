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

A NOTE ON HOW THIS FILE IS MAINTAINED. Every assertion here has been checked by
MUTATION — break the source, confirm the test goes red. That is not ceremony:
the first draft of this file shipped two assertions that could not fail (the
lateral's `LIMIT 1`, and the upserter agreement check), and reading them did not
reveal it. If you add a test here, mutate the thing it claims to protect and
watch it fail before you trust it.
"""

from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

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
    the identity leg prefers a seed that CARRIES a listing — the
    winner's external_product_id IS the join key. A bare `updated_at DESC`
    picks an identity-less sibling (state goes uncounted) or a stale inactive
    seed (clean state counts as broken); both measured 2026-07-31."""
    from services.source_quarantine import (
        SEED_PICK_ORDER,
        minted_seed_identity_leg_sql,
    )

    sql = minted_seed_external_id_sql("cp")
    # The leg must be tested against THIS row's merchant (#1665).
    assert minted_seed_identity_leg_sql("cp.merchant_id") in sql
    assert "spl.merchant_id = cp.merchant_id" in sql
    assert SEED_PICK_ORDER in sql
    assert "ORDER BY s.updated_at DESC" not in sql


def test_the_lateral_yields_at_most_one_listing():
    """A fanned-out listing corrupts any aggregate over the result, and a LIMIT
    without an ORDER BY is plan-dependent.

    THE LATERAL'S OWN `LIMIT 1` NEEDS ITS OWN ASSERTION. A bare
    `assert "LIMIT 1" in sql` is satisfied by the SEED SUBSELECT's limit, so it
    stays green while the lateral's is deleted — measured, and the deletion is
    not cosmetic: with two listings sharing merchant_id+product_id but differing
    on sellable_item_group_id, dropping it turns a NON-fragmented content_key
    into a false positive.
    """
    sql = identity_listing_lateral_sql("cp")
    assert "LEFT JOIN LATERAL" in sql
    assert "ORDER BY pil.source_listing_ref" in sql
    # Two distinct limits: the seed subselect's, and the lateral's own.
    assert sql.count("LIMIT 1") == 2
    assert sql.rstrip().endswith("LIMIT 1\n            ) pil ON TRUE")


def test_the_alias_is_honoured_without_breaking_correlation():
    """EVERY reference must move with the alias, including the select-list.

    The first version of this test asserted only `"cp." not in sql`, which was
    true while the emitted SQL was
    `SELECT pil.sellable_item_group_id FROM pdp_identity_listing lst` — an
    invalid reference, and the same right-value/wrong-alias shape this module
    exists to prevent. Assert on the DEFAULT-columns path specifically: an
    explicit `columns=` argument would mask the bug that shipped.
    """
    sql = identity_listing_lateral_sql("c2", alias="lst")
    assert "lst.merchant_id = c2.merchant_id" in sql
    assert "FROM pdp_identity_listing lst" in sql
    assert "SELECT lst.sellable_item_group_id" in sql
    assert "ORDER BY lst.source_listing_ref" in sql
    # No reference to the DEFAULT alias may survive an alias override.
    assert "pil." not in sql
    assert "cp." not in sql.replace("catalog_products", "")


def test_multiple_columns_are_each_qualified():
    sql = identity_listing_lateral_sql("cp", columns="sellable_item_group_id, product_id")
    assert "SELECT pil.sellable_item_group_id, pil.product_id" in sql


def test_an_alias_that_would_shadow_the_seed_subquery_is_rejected():
    """`s` and `spl` are baked into SEED_PICK_ORDER / the identity leg,
    which are byte-pinned across two repos and so cannot be renamed. Passing
    `cp_alias="s"` therefore emits `WHERE s.attached_product_key = s.product_key`
    — the seeds row compared to ITSELF, correlation silently gone.

    Production would at least error (`external_product_seeds` has no bare
    `product_key`; migration 044 defines only `attached_product_key`), but the
    gate DB's schema DOES have that column, so a test could go green on SQL that
    breaks in production. Reject the alias instead of relying on either.
    """
    import pytest

    for bad in ("s", "spl"):
        with pytest.raises(ValueError, match="shadow"):
            identity_listing_lateral_sql(bad)
        with pytest.raises(ValueError, match="shadow"):
            identity_listing_lateral_sql("cp", alias=bad)


# ---------------------------------------------------------------------------
# THE LINT
# ---------------------------------------------------------------------------

# Capture BOTH aliases, so the merchant conjunct we then demand must belong to
# THE SAME JOIN. An earlier version demanded only that *some* merchant conjunct
# appear within the window, which a neighbouring `JOIN merchant_stores ms ON
# ms.merchant_id = cp.merchant_id` satisfies — and identity queries routinely
# join merchant_stores (see STORE_PICK_ORDER). That excused the exact defect
# that shipped. `[\w{}]+` rather than `\w+` so a `.format()`/f-string template
# (`{a}.product_id = {c}.source_product_id`) cannot slip through as "not code".
_NAIVE_JOIN = re.compile(
    r"([\w{}]+)\.product_id\s*=\s*([\w{}]+)\.source_product_id"
    r"|([\w{}]+)\.source_product_id\s*=\s*([\w{}]+)\.product_id"
)

# The minted lane must be HANDLED or explicitly EXCLUDED — either way the author
# has to have thought about it. Without this the lint covered only one of the
# two defects its own docstring names, and the uncovered one is the worse of the
# pair: it yields silent NULLs that `count(DISTINCT)` then skips.
_MINTED_LANE = re.compile(r"catalog_enrichment_agent_v1|MINTED_SOURCE_SYSTEM")

# A hand-rolled seed pick, NARROWED to the harmful shape: selecting the attached
# seed's `external_product_id` — the value that BECOMES a join key — ordered by
# recency instead of the shared constants.
#
# Deliberately does NOT fire on picking a seed's CONTENT (seed_data, title):
# index_pipeline_state_service does that on both its Postgres and SQLite paths,
# and a content lookup has no identity constraint to honour.
#
# Those two paths are NOT trivially equivalent, though. `ORDER BY updated_at
# DESC` puts NULLs FIRST in Postgres and LAST in SQLite, so on a NULL date they
# pick opposite seeds. What makes them agree today is that production's
# `external_product_seeds.updated_at` is `NOT NULL DEFAULT NOW()` (migration
# 044) — NOT the ORDER BY. The gate DB's copy of that table IS nullable, so the
# divergence is reachable in tests even though it is not in production. If that
# column ever becomes nullable for real, these two paths diverge silently.
_NAIVE_SEED_PICK = re.compile(
    r"SELECT\s+\w+\.external_product_id[\s\S]{0,300}?"
    r"attached_product_key\s*=\s*\w+\.product_key[\s\S]{0,300}?"
    r"ORDER BY\s+\w+\.updated_at")

_SCAN_ROOTS = ("services", "jobs", "routes", "scripts")

# Files that legitimately contain the canonical form. Each entry is a decision,
# not a mute button — add one only with a reason.
#
# `catalog_row_trust_upserter.py` is deliberately NOT here. It owns the
# page-scan `minted_seed_one` shape, so exempting it looks reasonable — but it
# is the single file this lint most needs to watch, the exemption bought nothing
# (its `pil.product_id = CASE…` never matched _NAIVE_JOIN anyway), and while it
# was exempt, three mutations to its real join went undetected by the whole
# suite. test_the_upserter_join_still_agrees below now checks it directly.
_ALLOWED = {
    # The helper itself.
    "services/identity_join_sql.py",
    # This file.
    "tests/test_identity_join_sql.py",
    # READ-ONLY orphan recon (ADR-009). Its listing subqueries are cross-merchant
    # ON PURPOSE: a listing left under the retired sentinel is classified by
    # counting the catalog rows that carry its product_id under ANY merchant
    # (`n_products`, `merchants[]`) — the fan-out this lint forbids in a serving
    # join is the measurement here. The minted lane is read through its own
    # seed subquery (`n_seeds_attached_live`), never through source_product_id.
    # Nothing it emits is served or written.
    "scripts/recon_sentinel_orphans.py",
}


def _strip_prose(source: str) -> str:
    """Blank comments and docstrings, preserving line numbers and offsets.

    A lint that fires on the comment WARNING about a bug is a lint people mute,
    and the modules documenting this defect quote the bad pattern verbatim. The
    first version blanked only whole lines starting with `--`/`#`, which left
    docstring prose and trailing comments live — both of which fire.

    This can only ever REMOVE text, so it cannot hide a real offender; the worst
    case is that it deletes an excusing conjunct and the lint fires more.
    """
    lines = source.split("\n")

    def blank(lineno: int, col: int, end_lineno: int, end_col: int) -> None:
        for ln in range(lineno, end_lineno + 1):
            i = ln - 1
            if not (0 <= i < len(lines)):
                continue
            start = col if ln == lineno else 0
            end = end_col if ln == end_lineno else len(lines[i])
            end = min(end, len(lines[i]))
            if end > start:
                lines[i] = lines[i][:start] + " " * (end - start) + lines[i][end:]

    try:
        tree = ast.parse(source)
    except SyntaxError:
        tree = None
    if tree is not None:
        holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        for node in ast.walk(tree):
            if not isinstance(node, holders):
                continue
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                blank(first.lineno, first.col_offset,
                      first.end_lineno, first.end_col_offset)

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                blank(tok.start[0], tok.start[1], tok.end[0], tok.end[1])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass

    # SQL comments, including trailing ones, inside the surviving strings.
    return "\n".join(re.sub(r"--.*$", "", ln) for ln in lines)


def _python_sources():
    for base in _SCAN_ROOTS:
        for path in (_REPO / base).rglob("*.py"):
            rel = path.relative_to(_REPO).as_posix()
            if rel not in _ALLOWED:
                yield rel, _strip_prose(
                    path.read_text(encoding="utf-8", errors="ignore"))


def _join_offenders(text: str, rel: str = "<text>"):
    """Every hand-rolled listing join missing a same-join merchant conjunct or
    the minted-lane test."""
    out = []
    for m in _NAIVE_JOIN.finditer(text):
        listing, cp = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        window = text[max(0, m.start() - 400): m.end() + 400]
        same_join = re.compile(
            rf"{re.escape(listing)}\.merchant_id\s*=\s*{re.escape(cp)}\.merchant_id"
            rf"|{re.escape(cp)}\.merchant_id\s*=\s*{re.escape(listing)}\.merchant_id")
        missing = []
        if not same_join.search(window):
            missing.append("merchant_id conjunct on THIS join")
        if not _MINTED_LANE.search(window):
            missing.append("minted-lane CASE")
        if missing:
            line = text[: m.start()].count("\n") + 1
            out.append(f"{rel}:{line}  {m.group(0)}  — missing: {', '.join(missing)}")
    return out


def test_no_hand_rolled_identity_join():
    """Both shipped defects, not just the fan-out one.

    Use services.identity_join_sql.identity_listing_lateral_sql.
    """
    offenders = []
    for rel, text in _python_sources():
        offenders.extend(_join_offenders(text, rel))
    assert not offenders, (
        "hand-rolled identity join — use "
        "services.identity_join_sql.identity_listing_lateral_sql:\n  "
        + "\n  ".join(offenders))


def test_no_hand_rolled_minted_seed_pick():
    """The other half: picking the attached seed by `updated_at` instead of
    the identity leg + SEED_PICK_ORDER. Diverges from the upserter in
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


def test_the_scan_is_actually_live():
    """A lint pointed at nothing passes silently and reads as coverage.

    Both `_strip_prose` returning "" for every line and `_SCAN_ROOTS` set to ()
    left the suite green — the regexes were pinned, the SCAN was not. This
    routes a synthetic offender through the REAL scan path rather than asserting
    against the regex directly.
    """
    files = dict(_python_sources())
    assert len(files) > 100, f"scan collapsed to {len(files)} files"
    assert sum(len(t) for t in files.values()) > 1_000_000, "scan text collapsed"

    # A known file with known LIVE code must survive the prose-stripping. Note
    # this file no longer contains the literal `pdp_identity_listing` — the
    # whole point of this PR is that it interpolates the helper instead — so the
    # canary is a call that is still really there.
    key = "services/catalog_invariant_checks.py"
    assert key in files, f"{key} not scanned"
    assert "identity_listing_lateral_sql" in files[key], "prose-stripping ate live code"
    assert "catalog_products" in files[key], "prose-stripping ate live SQL"

    # Some scanned file must still contain a real listing join, or the lint is
    # guarding a table nothing references any more.
    assert any("pdp_identity_listing" in t for t in files.values()), \
        "no scanned file joins pdp_identity_listing"

    # And the scan must actually flag a planted offender.
    planted = ("LEFT JOIN pdp_identity_listing pil "
               "ON pil.product_id = cp.source_product_id")
    assert _join_offenders(planted), "the scan path cannot see an offender"


def test_the_lint_catches_both_shipped_defects():
    """Pin the exact strings, so the lint cannot decay into matching nothing."""
    # 1. No merchant conjunct at all — the fan-out defect.
    bare = "LEFT JOIN pdp_identity_listing pil ON pil.product_id = cp.source_product_id"
    assert _join_offenders(bare)

    # 2. Merchant conjunct present, minted-lane CASE absent — the silent-NULL
    #    defect, which the first version of this lint waved through.
    half = ("LEFT JOIN pdp_identity_listing pil ON pil.merchant_id = cp.merchant_id "
            "AND pil.product_id = cp.source_product_id")
    assert _join_offenders(half), "the minted-lane half is unlinted"

    # 3. A NEIGHBOURING join's merchant conjunct must not excuse it. This case
    #    SATISFIES the minted-lane requirement (it excludes the lane explicitly),
    #    so the only thing left to flag is the missing same-join merchant
    #    conjunct — otherwise the case passes for the wrong reason and cannot
    #    detect a regression in the alias-matching at all.
    neighbour = ("WHERE cp.source_system <> 'catalog_enrichment_agent_v1' "
                 "JOIN merchant_stores ms ON ms.merchant_id = cp.merchant_id "
                 "LEFT JOIN pdp_identity_listing pil ON pil.product_id = cp.source_product_id")
    assert _MINTED_LANE.search(neighbour), "case 3 must not pass for the lane reason"
    assert _join_offenders(neighbour), "an unrelated merchant conjunct excused it"

    # 4. Reversed operands, and templated aliases.
    assert _join_offenders("ON cp.source_product_id = pil.product_id")
    assert _join_offenders("ON {a}.product_id = {c}.source_product_id")

    # 5. The canonical form is clean.
    assert not _join_offenders(identity_listing_lateral_sql("cp"))

    # 6. Seed pick: identity key by recency is flagged; a CONTENT lookup is not.
    assert _NAIVE_SEED_PICK.search(
        "SELECT s.external_product_id FROM external_product_seeds s "
        "WHERE s.attached_product_key = cp.product_key "
        "ORDER BY s.updated_at DESC LIMIT 1")
    assert not _NAIVE_SEED_PICK.search(minted_seed_external_id_sql("cp"))
    assert not _NAIVE_SEED_PICK.search(
        "SELECT eps.seed_data FROM external_product_seeds eps "
        "WHERE eps.attached_product_key = cp.product_key "
        "ORDER BY eps.updated_at DESC LIMIT 1")


def test_prose_is_stripped_but_live_sql_is_not():
    """The modules documenting this bug quote the bad pattern verbatim, in all
    three shapes: line comments, TRAILING comments, and docstring prose."""
    src = (
        '"""A docstring saying pil.product_id = cp.source_product_id is wrong."""\n'
        "# pil.product_id = cp.source_product_id in a line comment\n"
        "SQL = 'x'  # pil.product_id = cp.source_product_id trailing\n"
        "LIVE = '''\n"
        "  -- pil.product_id = cp.source_product_id in a SQL comment\n"
        "  LEFT JOIN pdp_identity_listing pil ON pil.product_id = cp.source_product_id\n"
        "'''\n"
    )
    stripped = _strip_prose(src)
    # Exactly one survivor: the live SQL on line 6.
    hits = _join_offenders(stripped)
    assert len(hits) == 1, f"expected only the live SQL, got {hits}"
    assert ":6" in hits[0], f"line numbers not preserved: {hits[0]}"


def test_the_upserter_join_still_agrees():
    """The upserter's `minted_seed_one` CTE and this module's correlated form
    are different SQL for the same decision. Neither can be expressed in the
    other's context, so they cannot be de-duplicated — but they MUST agree.

    ASSERT ON THE IDENTITY JOIN, NOT THE FILE. The first version checked bare
    substrings against the whole module, and every one was satisfied by
    unrelated text: the merchant conjunct matched the `merchant_store_one` join,
    MINTED_SOURCE_SYSTEM matched a comment, and the two order constants matched
    the import line. All three documented defects could be introduced into the
    real join with the entire suite staying green.
    """
    upserter = (_REPO / "services" / "catalog_row_trust_upserter.py").read_text()

    # The FIRST `pdp_identity_listing` in this file is the seed-pick's identity
    # leg (`spl`), not the row->listing join. Anchor on the `pil` join itself.
    start = upserter.find("LEFT JOIN pdp_identity_listing pil")
    assert start != -1, "the upserter no longer joins pdp_identity_listing as pil"
    join_block = upserter[start: start + 700]

    # The merchant conjunct must be on THIS join, not a neighbouring one.
    assert re.search(r"pil\.merchant_id\s*=\s*cp\.merchant_id", join_block), join_block
    # The lane test must route minted rows through their seed.
    assert MINTED_SOURCE_SYSTEM in join_block, join_block
    assert "epm.external_product_id" in join_block or "external_product_id" in join_block
    # And the seed pick must use the shared constants, not a hand-rolled order.
    assert "ORDER BY s.updated_at DESC" not in join_block

    # The lint must agree, applied to the same block.
    assert not _join_offenders(join_block), "the upserter's own join is off-contract"

    # THE SEED PICK IS A SEPARATE BLOCK. It lives in the `minted_seed_one` CTE,
    # ~150 lines above the pil join, so the window anchored on the join never
    # saw it and `"ORDER BY s.updated_at DESC" not in join_block` was vacuous.
    cte = upserter.find("minted_seed_one AS (")
    assert cte != -1, "the upserter no longer has a minted_seed_one CTE"
    # Terminate on the CTE's own closing paren at ITS indentation, not the first
    # `),` anywhere: a `),` inside a COMMENT truncated this block and silently
    # shrank what the assertions below could see.
    end = upserter.find("\n  ),", cte)
    assert end != -1, "cannot find the end of the minted_seed_one CTE"
    cte_block = upserter[cte:end]
    assert "SEED_PICK_ORDER" in cte_block, "block extraction truncated the CTE"
    assert "{_MINTED_SEED_IDENTITY_LEG}" in cte_block, cte_block
    assert "{SEED_PICK_ORDER}" in cte_block, cte_block
    assert "s.updated_at DESC" not in cte_block, cte_block
