"""Quarantine enforced at the SHARED seed-retrieval seam, not per consumer.

pivota-backend#1633 gated two lanes by hand. #1635 measured the shape of that:
the same external-seed corpus publishes through ~11 other routes — including
`offers.resolve` and `find_similar_products`, both UNAUTHENTICATED, and
`GET /agent/v1/products/search`, which has its own result cache — and every one
of them read none of the gates. Adding an eighth call site is not convergence.

`fetch_external_seed_rows` is the one place they all go through, so the anti-join
lives there: quarantined seeds are excluded BEFORE ranking, paging and counting,
and every consumer of that function inherits it without knowing.

These tests EXECUTE the SQL rather than matching its text. The predicate's first
version used `now()`, which passed every substring assertion in
tests/test_source_quarantine.py and then failed with `no such function: now` the
moment anything ran it — on SQLite, which is the engine this suite uses.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from services.external_seed_search import (
    _SEED_QUARANTINE_DOMAIN_EXPR,
    build_seed_quarantine_anti_join,
)


def _db(seeds, quarantines=()):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE external_product_seeds (id INTEGER, domain TEXT, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER, match_type TEXT,"
        " match_value TEXT, state TEXT, expires_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO external_product_seeds VALUES (?,?,'active')", seeds
    )
    conn.executemany(
        "INSERT INTO catalog_source_quarantine VALUES (?,?,?,?,?)", quarantines
    )
    return conn


def _surviving(conn):
    sql = (
        "SELECT id FROM external_product_seeds WHERE status='active' "
        + build_seed_quarantine_anti_join()
    )
    return [r[0] for r in conn.execute(sql)]


def test_the_predicate_actually_executes():
    """Not a text match. `now()` read fine and could not run."""
    assert _surviving(_db([(1, "beplain.com")])) == [1]


def test_a_quarantined_domain_is_excluded():
    conn = _db(
        [(1, "mintree.us"), (2, "beplain.com")],
        [(1, "domain", "mintree.us", "active", None)],
    )
    assert _surviving(conn) == [2]


def test_www_prefixed_seeds_are_also_excluded():
    """263 of 11,381 active prod seeds carry a `www.` prefix (none currently
    on a quarantined storefront — so this direction is insurance today).

    The direction that was a REAL bug is the mirror image: stripping only the
    ROW side under-blocked a `www.mintree.us` match_value, which
    `create_quarantine` accepts verbatim. Both sides now go through one
    normaliser; see test_a_www_prefixed_QUARANTINE_value_also_matches.
    """
    conn = _db(
        [(1, "www.mintree.us"), (2, "WWW.Mintree.US"), (3, "beplain.com")],
        [(1, "domain", "mintree.us", "active", None)],
    )
    assert _surviving(conn) == [3]


def test_a_www_prefixed_QUARANTINE_value_also_matches():
    """The asymmetry that was a live under-block.

    `create_quarantine` only `.strip()`s, so an operator can enter
    `www.mintree.us` and the API accepts it. With the strip on the row side
    only, that quarantine matched NOTHING — reporting success and blocking
    nobody, the exact failure this workstream exists to end. It also split the
    SQL gate from the Python matcher, each blocking a pair the other passed.
    """
    conn = _db(
        [(1, "mintree.us"), (2, "www.mintree.us"), (3, "beplain.com")],
        [(1, "domain", "www.mintree.us", "active", None)],
    )
    assert _surviving(conn) == [3]


def test_the_two_gates_agree_on_every_www_combination():
    """SQL anti-join and Python matcher must never disagree about a pair.

    Four combinations of prefixed/bare on each side; both gates must give the
    same verdict for each, or a row's fate depends on which door it arrives at.
    """
    from services.source_quarantine import MATCH_TYPE_DOMAIN, Quarantine, quarantine_matches_source

    combos = [
        ("mintree.us", "mintree.us"),
        ("www.mintree.us", "mintree.us"),
        ("mintree.us", "www.mintree.us"),
        ("www.mintree.us", "www.mintree.us"),
    ]
    for seed_domain, match_value in combos:
        sql_blocks = _surviving(
            _db([(1, seed_domain)], [(1, "domain", match_value, "active", None)])
        ) == []
        q = Quarantine(
            quarantine_id=1, match_type=MATCH_TYPE_DOMAIN, match_value=match_value,
            state="active", reason=None, expires_at=None, created_by="t",
            created_at=None, revoked_at=None, revoked_by=None, metadata=None,
        )
        py_blocks = quarantine_matches_source(
            q, domain=seed_domain, merchant_id=None, platform=None,
            source_system=None, source_ref=None,
        )
        assert sql_blocks is True and py_blocks is True, (
            f"gates disagree or under-block for seed={seed_domain!r} "
            f"match_value={match_value!r}: sql={sql_blocks} python={py_blocks}"
        )


def test_case_is_ignored_on_both_sides():
    conn = _db([(1, "MinTree.US")], [(1, "domain", "MINTREE.us", "active", None)])
    assert _surviving(conn) == []


def test_lookalike_domains_are_kept():
    """Exact match, delegated. Over-blocking deletes real merchants' products."""
    conn = _db(
        [(1, "notmintree.us"), (2, "shop.mintree.us"), (3, "mintree.us.evil.com")],
        [(1, "domain", "mintree.us", "active", None)],
    )
    assert _surviving(conn) == [1, 2, 3]


def test_wholesale_subdomain_quarantine_does_not_catch_the_parent():
    """`wholesale.publicgoods.com` is quarantined; `publicgoods.com` is not."""
    conn = _db(
        [(1, "wholesale.publicgoods.com"), (2, "publicgoods.com")],
        [(1, "domain", "wholesale.publicgoods.com", "active", None)],
    )
    assert _surviving(conn) == [2]


def test_revoked_and_expired_quarantines_release_the_store():
    conn = _db(
        [(1, "mintree.us")],
        [
            (1, "domain", "mintree.us", "revoked", None),
            (2, "domain", "mintree.us", "expired", None),
            (3, "domain", "mintree.us", "active", "2000-01-01 00:00:00"),
        ],
    )
    assert _surviving(conn) == [1], "a non-active quarantine must not block"


def test_a_future_expiry_still_blocks():
    conn = _db(
        [(1, "mintree.us")],
        [(1, "domain", "mintree.us", "active", "2999-01-01 00:00:00")],
    )
    assert _surviving(conn) == []


def test_a_blank_match_value_does_not_eat_every_domainless_seed():
    """`match_value TEXT NOT NULL` has no non-empty CHECK, and these rows are
    inserted by direct SQL ops. Without `nullif`, one blank row would delete
    every domain-less seed — 1,779 of 11,381 on prod — from every consumer of
    this function at once, with nothing but a smaller result to notice by."""
    conn = _db(
        [(1, ""), (2, None), (3, "beplain.com")],
        [(1, "domain", "", "active", None)],
    )
    assert _surviving(conn) == [1, 2, 3]


def test_a_blank_quarantine_does_not_disable_a_real_one():
    conn = _db(
        [(1, "mintree.us"), (2, "beplain.com")],
        [
            (1, "domain", "", "active", None),
            (2, "domain", "mintree.us", "active", None),
        ],
    )
    assert _surviving(conn) == [2]


def test_every_active_quarantine_is_enforced_not_just_one():
    """Prod holds 15 active domain quarantines."""
    conn = _db(
        [(1, "mintree.us"), (2, "reddane.co.za"), (3, "bijin-shop.com"), (4, "beplain.com")],
        [
            (1, "domain", "mintree.us", "active", None),
            (2, "domain", "reddane.co.za", "active", None),
            (3, "domain", "bijin-shop.com", "active", None),
        ],
    )
    assert _surviving(conn) == [4]


def test_non_domain_match_types_cannot_match_here():
    """The seed table has no merchant/platform/source columns, so those three
    types are passed NULL. They must match NOTHING rather than everything."""
    conn = _db(
        [(1, "beplain.com"), (2, "")],
        [
            (1, "merchant_platform", "merch_x:shopify", "active", None),
            (2, "source_system_ref", "external_product_seeds:1", "active", None),
        ],
    )
    assert _surviving(conn) == [1, 2]


def test_no_quarantines_is_a_no_op():
    conn = _db([(1, "mintree.us"), (2, "beplain.com")])
    assert _surviving(conn) == [1, 2]


# ---------------------------------------------------------------------------
# the wiring -- the clause must reach BOTH statements
# ---------------------------------------------------------------------------

def test_the_clause_is_applied_to_the_page_query_and_the_count():
    """A quarantine clause on the page but not the count makes `total_count`
    advertise rows the page can never contain, so a consumer pages into a
    permanently empty tail and reads it as exhausted-but-truncated.

    Kept as a cheap structural canary; the real assertion is
    test_REAL_fetch_excludes_quarantined_seeds below, which executes.
    """
    import inspect

    import services.external_seed_search as mod

    src = inspect.getsource(mod.fetch_external_seed_rows)
    assert src.count("{quarantine_clause}") == 2, (
        "the anti-join must be interpolated into BOTH query_sql and count_sql"
    )


# ---------------------------------------------------------------------------
# THE REAL FUNCTION -- everything above tests the BUILDER
# ---------------------------------------------------------------------------
#
# Review found the wrong-LAYER hole: `quarantine_clause = ""` in
# fetch_external_seed_rows -- disabling the gate entirely while leaving the
# builder perfect -- passed 67 tests across nine seed/quarantine files. So did
# `"" if lean_where_applied else build_seed_quarantine_anti_join()`, a plausible
# perf tweak that turns the gate off on exactly the hot path.
#
# Nothing executed the real query. The one test that calls fetch_external_seed_rows
# uses a fake database that records the SQL and returns []; it cannot execute it,
# because the real WHERE emits Postgres-only JSON operators.
#
# This harness runs the REAL function against SQLite by neutralising those
# operators at the connection boundary -- the quarantine clause itself is
# untouched and portable, which is the whole point of writing it that way.

class _SqliteBackedDatabase:
    """Enough of the `databases` interface for fetch_external_seed_rows.

    Reports a sqlite URL so the function takes its non-Postgres path (no
    `SET LOCAL statement_timeout`), and rewrites the PG-only JSON operators the
    text clauses emit. The quarantine fragment passes through verbatim.
    """

    url = "sqlite:///:memory:"

    def __init__(self, seeds, quarantines=()):
        self._conn = sqlite3.connect(":memory:")
        self._conn.execute(
            "CREATE TABLE external_product_seeds ("
            " id TEXT, external_product_id TEXT, market TEXT, tool TEXT, utm_template TEXT,"
            " partner_type TEXT, disclosure_text TEXT, destination_url TEXT, canonical_url TEXT,"
            " domain TEXT, title TEXT, image_url TEXT, price_amount REAL, price_currency TEXT,"
            " availability TEXT, seed_data TEXT, status TEXT, notes TEXT,"
            " created_by_employee_id TEXT, attached_product_key TEXT, attached_variant_id TEXT,"
            " seller_ref TEXT, seed_kind TEXT, created_at TEXT, updated_at TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE catalog_source_quarantine (quarantine_id INTEGER, match_type TEXT,"
            " match_value TEXT, state TEXT, expires_at TEXT)"
        )
        for sid, domain, title in seeds:
            self._conn.execute(
                "INSERT INTO external_product_seeds (id, external_product_id, domain, title,"
                " status, market, seed_data, destination_url, created_at, updated_at)"
                " VALUES (?,?,?,?,'active','US','{}','https://x/y','2026-01-01','2026-01-01')",
                (sid, sid, domain, title),
            )
        for q in quarantines:
            self._conn.execute(
                "INSERT INTO catalog_source_quarantine VALUES (?,?,?,?,?)", q
            )

    @staticmethod
    def _portable(sql: str) -> str:
        # The seed_data JSON probes are Postgres-only and orthogonal to the
        # quarantine clause; collapse them to a literal that never matches.
        sql = re.sub(r"seed_data\s*#>>\s*'\{[^}]*\}'", "''", sql)
        sql = re.sub(r"seed_data\s*(->>?\s*'[^']*'\s*)+", "''", sql)
        return sql

    def _run(self, query, values):
        sql = self._portable(str(query))
        for key in sorted((values or {}), key=len, reverse=True):
            sql = sql.replace(f":{key}", "?")
        ordered = re.findall(r":(\w+)", str(query))
        params = [(values or {}).get(k) for k in ordered]
        return self._conn.execute(sql, params)

    async def fetch_all(self, query, values=None):
        cur = self._run(query, values)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    async def fetch_one(self, query, values=None):
        rows = await self.fetch_all(query, values)
        return rows[0] if rows else None


def _real_fetch(seeds, quarantines=(), query="serum"):
    import asyncio

    from services.external_seed_search import fetch_external_seed_rows

    db = _SqliteBackedDatabase(seeds, quarantines)
    return asyncio.run(
        fetch_external_seed_rows(
            database=db,
            market="US",
            query=query,
            limit=50,
            only_unattached=False,
            include_total_count=True,
        )
    )


def test_REAL_fetch_excludes_quarantined_seeds():
    """Drives the actual function. Kills `quarantine_clause = ""`."""
    result = _real_fetch(
        seeds=[("s1", "mintree.us", "serum a"), ("s2", "beplain.com", "serum b")],
        quarantines=[(1, "domain", "mintree.us", "active", None)],
    )
    ids = [r["id"] for r in result["rows"]]
    assert ids == ["s2"], f"the real query did not apply the gate: {ids}"


def test_REAL_fetch_excludes_from_the_total_count_too():
    """Kills a clause applied to the page query only."""
    result = _real_fetch(
        seeds=[("s1", "mintree.us", "serum a"), ("s2", "beplain.com", "serum b")],
        quarantines=[(1, "domain", "mintree.us", "active", None)],
    )
    assert result["total_count"] == 1, (
        f"total_count advertises rows the page cannot contain: {result['total_count']}"
    )


def test_REAL_fetch_is_gated_on_the_lean_path_too():
    """Kills `"" if lean_where_applied else ...` — a plausible perf tweak that
    disables the gate on precisely the hot multi-term path."""
    import asyncio

    from services.external_seed_search import fetch_external_seed_rows

    db = _SqliteBackedDatabase(
        [("s1", "mintree.us", "vitamin c serum brightening"), ("s2", "beplain.com", "vitamin c serum brightening")],
        [(1, "domain", "mintree.us", "active", None)],
    )
    result = asyncio.run(
        fetch_external_seed_rows(
            database=db,
            market="US",
            query="vitamin c serum brightening",
            limit=50,
            only_unattached=False,
            include_total_count=True,
            lean_where_min_tokens=2,  # force the lean branch
        )
    )
    ids = [r["id"] for r in result["rows"]]
    assert ids == ["s2"], f"the lean path is ungated: {ids}"


def test_REAL_fetch_is_a_no_op_with_no_quarantines():
    result = _real_fetch(
        seeds=[("s1", "mintree.us", "serum a"), ("s2", "beplain.com", "serum b")],
    )
    assert sorted(r["id"] for r in result["rows"]) == ["s1", "s2"]
    assert result["total_count"] == 2


def test_the_domain_expression_is_portable_sql():
    """No Postgres-only constructs. The suite that runs must be the suite that
    gates — see #1588, where PG-only SQL shipped green and took the feed down
    for 16 minutes."""
    expr = _SEED_QUARANTINE_DOMAIN_EXPR.lower()
    for pg_only in ("regexp_replace", "~*", "::text", "ilike", "now()"):
        assert pg_only not in expr, f"{pg_only} is Postgres-only"
