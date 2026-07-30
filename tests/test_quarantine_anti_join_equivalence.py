"""The uncorrelated anti-join must be EXACTLY equivalent to the correlated one.

#1638 replaced a single correlated `NOT EXISTS` with three uncorrelated
`NOT IN` arms, because Postgres planned the former as a Nested Loop Anti Join
rescanned per outer row (68.1 ms vs 17.8 ms on a prod-shaped corpus).

`NOT (A OR B OR C)` ≡ `NOT A AND NOT B AND NOT C` is trivially true in logic and
NOT trivially true in SQL, because `NOT IN` is NULL-propagating in a way
`NOT EXISTS` is not:

    x NOT IN (S)  is NULL — not TRUE — if S contains any NULL,
    and a NULL predicate excludes the row.

So a single NULL in any subquery silently drops the ENTIRE result set, and
`_sql_bare_domain` deliberately produces NULL for a blank match_value. This file
exists to prove the rewrite preserves behaviour on exactly those edges, by
running BOTH forms over a matrix and asserting identical row sets — not
identical counts, which would pass while returning different rows.

The old form is reproduced here verbatim. It is the oracle; if the shipped
builder ever needs to change again, this comparison is what makes that safe.
"""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from services.source_quarantine import _sql_bare_domain, build_quarantine_anti_join_sql


def _legacy_correlated_sql(
    row_domain_expr: str,
    row_merchant_expr: str,
    row_platform_expr: str,
    row_source_system_expr: str,
    row_source_ref_expr: str,
) -> str:
    """The pre-#1638 correlated form, kept as the equivalence oracle."""
    return f"""
AND NOT EXISTS (
  SELECT 1 FROM catalog_source_quarantine q
  WHERE q.state = 'active'
    AND (q.expires_at IS NULL OR q.expires_at > CURRENT_TIMESTAMP)
    AND (
      (q.match_type = 'domain' AND {_sql_bare_domain("q.match_value")} = {_sql_bare_domain(row_domain_expr)})
      OR (q.match_type = 'merchant_platform' AND q.match_value = {row_merchant_expr} || ':' || {row_platform_expr})
      OR (q.match_type = 'source_system_ref' AND q.match_value = {row_source_system_expr} || ':' || {row_source_ref_expr})
    )
)""".strip()


ARGS = ("r.domain", "r.merchant_id", "r.platform", "r.source_system", "r.source_ref")

# Rows chosen to hit every NULL/blank/case/www edge on the ROW side.
#
# Each row is quarantinable on AT MOST ONE axis, so an arm firing is
# attributable. `clean` in particular must be untouchable by every case — it is
# the control that proves a passing test is not passing vacuously, and an
# earlier version of this fixture gave it `sys:ref`, which the source arm
# blocked correctly and made three assertions wrong.
ROWS = [
    # domain axis
    ("bare", "mintree.us", "m_bare", "shopify", "ss_bare", "sr_bare"),
    ("www", "www.mintree.us", "m_www", "shopify", "ss_www", "sr_www"),
    ("upper", "WWW.MinTree.US", "m_up", "shopify", "ss_up", "sr_up"),
    # merchant_platform axis
    ("merchhit", "beplain.com", "m1", "shopify", "ss_mh", "sr_mh"),
    # source_system_ref axis
    ("srchit", "beplain.com", "m_sh", "shopify", "sys", "ref"),
    # the control — matched by nothing in any case
    ("clean", "beplain.com", "m_clean", "shopify", "ss_clean", "sr_clean"),
    # NULL/blank edges
    ("blankdom", "", "m_bd", "shopify", "ss_bd", "sr_bd"),
    ("nulldom", None, "m_nd", "shopify", "ss_nd", "sr_nd"),
    ("nullmerch", "beplain.com", None, "shopify", "ss_nm", "sr_nm"),
    ("nullplat", "beplain.com", "m_np", None, "ss_np", "sr_np"),
    ("nullsrc", "beplain.com", "m_ns", "shopify", None, None),
    ("allnull", None, None, None, None, None),
]

# Quarantine rows chosen to hit every NULL/blank/state/expiry/www edge on the
# MATCH side — including the blank match_value that is the whole NULL trap.
QUARANTINE_CASES = {
    "none": [],
    "domain_bare": [(1, "domain", "mintree.us", "active", None)],
    "domain_www": [(1, "domain", "www.mintree.us", "active", None)],
    "domain_upper": [(1, "domain", "MINTREE.US", "active", None)],
    "domain_blank": [(1, "domain", "", "active", None)],
    "domain_blank_plus_real": [
        (1, "domain", "", "active", None),
        (2, "domain", "mintree.us", "active", None),
    ],
    "domain_revoked": [(1, "domain", "mintree.us", "revoked", None)],
    "domain_expired": [(1, "domain", "mintree.us", "active", "2000-01-01 00:00:00")],
    "domain_future": [(1, "domain", "mintree.us", "active", "2999-01-01 00:00:00")],
    "merchant": [(1, "merchant_platform", "m1:shopify", "active", None)],
    "merchant_blank": [(1, "merchant_platform", "", "active", None)],
    "source": [(1, "source_system_ref", "sys:ref", "active", None)],
    "source_blank": [(1, "source_system_ref", "", "active", None)],
    "many": [
        (1, "domain", "mintree.us", "active", None),
        (2, "domain", "reddane.co.za", "active", None),
        (3, "merchant_platform", "m1:shopify", "active", None),
        (4, "source_system_ref", "sys:ref", "active", None),
        (5, "domain", "", "active", None),
        (6, "domain", "revoked-store.com", "revoked", None),
    ],
}


def _db(quarantines):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE rows_under_test (id TEXT, domain TEXT, merchant_id TEXT,"
        " platform TEXT, source_system TEXT, source_ref TEXT)"
    )
    conn.execute(
        "CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER, match_type TEXT,"
        " match_value TEXT, state TEXT, expires_at TEXT)"
    )
    conn.executemany("INSERT INTO rows_under_test VALUES (?,?,?,?,?,?)", ROWS)
    conn.executemany("INSERT INTO catalog_source_quarantine VALUES (?,?,?,?,?)", quarantines)
    return conn


def _survivors(conn, fragment):
    sql = f"SELECT id FROM rows_under_test r WHERE 1=1 {fragment}"
    return sorted(r[0] for r in conn.execute(sql).fetchall())


@pytest.mark.parametrize("case", sorted(QUARANTINE_CASES))
def test_uncorrelated_form_is_row_for_row_identical_to_the_correlated_one(case):
    """Identical ROW SETS, not identical counts.

    A count-only assertion would pass while the two forms disagreed about which
    rows survive, which is the failure that matters.
    """
    conn = _db(QUARANTINE_CASES[case])
    legacy = _survivors(conn, _legacy_correlated_sql(*ARGS))
    shipped = _survivors(conn, build_quarantine_anti_join_sql(*ARGS))
    assert shipped == legacy, (
        f"[{case}] rewrite changed behaviour\n  correlated: {legacy}\n  uncorrelated: {shipped}"
    )


@pytest.mark.parametrize("case", sorted(QUARANTINE_CASES))
def test_the_rewrite_never_empties_the_result_set(case):
    """The specific NULL-trap symptom, asserted directly.

    `x NOT IN (S)` with a NULL in S drops every row. Every case here contains at
    least one row (`clean`) that nothing should ever quarantine — including the
    cases whose whole point is a blank match_value producing a NULL.
    """
    conn = _db(QUARANTINE_CASES[case])
    survivors = _survivors(conn, build_quarantine_anti_join_sql(*ARGS))
    assert survivors, f"[{case}] the gate returned NOTHING — the NULL trap fired"
    assert "clean" in survivors, f"[{case}] an unquarantined row was dropped: {survivors}"


def test_a_blank_match_value_alone_blocks_nobody():
    """The trap in its purest form, on both implementations."""
    conn = _db(QUARANTINE_CASES["domain_blank"])
    assert _survivors(conn, build_quarantine_anti_join_sql(*ARGS)) == sorted(r[0] for r in ROWS)


def test_a_blank_match_value_does_not_disable_a_real_one():
    conn = _db(QUARANTINE_CASES["domain_blank_plus_real"])
    survivors = _survivors(conn, build_quarantine_anti_join_sql(*ARGS))
    for blocked in ("bare", "www", "upper"):
        assert blocked not in survivors
    assert "clean" in survivors


def test_the_three_arms_are_independent():
    """Each arm blocks exactly its own axis, and none suppresses another.

    Every row is quarantinable on at most one axis, so an over- or under-firing
    arm is attributable rather than merely visible.
    """
    conn = _db(QUARANTINE_CASES["many"])
    survivors = set(_survivors(conn, build_quarantine_anti_join_sql(*ARGS)))

    assert not ({"bare", "www", "upper"} & survivors), "domain arm under-fired"
    assert "merchhit" not in survivors, "merchant_platform arm under-fired"
    assert "srchit" not in survivors, "source_system_ref arm under-fired"

    # Everything else must survive: the blank match_value in this case must not
    # eat the NULL/blank rows, and the revoked entry must not block anyone.
    assert {"clean", "blankdom", "nulldom", "nullmerch", "nullplat", "nullsrc", "allnull"} <= survivors, (
        f"an arm over-fired: {sorted(survivors)}"
    )


def test_no_outer_reference_remains_in_any_subquery():
    """The whole point: each subquery must be evaluable once, independently.

    If a row expression leaks into a subquery the planner is back to a
    correlated rescan and the performance win silently disappears — with every
    behavioural test still green.
    """
    frag = build_quarantine_anti_join_sql(*ARGS)
    for chunk in frag.split("NOT IN (")[1:]:
        subquery = chunk.split("\n))")[0]
        assert "r." not in subquery, (
            f"subquery references the outer row — it is still correlated:\n{subquery}"
        )
