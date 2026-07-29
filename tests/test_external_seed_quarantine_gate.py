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
    """Insurance, and honestly labelled as such.

    Measured on prod 2026-07-29: 263 of 11,381 active seeds carry a `www.`
    prefix, but ZERO of them are on a currently-quarantined storefront — so
    this strip changes nothing today. It is here because the quarantine writer
    normalises `www.` away (storefront_currency.normalize_domain), so the two
    sides would disagree the first time anyone quarantines a store whose seeds
    happen to carry the prefix, and that failure would be silent.
    """
    conn = _db(
        [(1, "www.mintree.us"), (2, "WWW.Mintree.US"), (3, "beplain.com")],
        [(1, "domain", "mintree.us", "active", None)],
    )
    assert _surviving(conn) == [3]


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
    permanently empty tail and reads it as exhausted-but-truncated."""
    import inspect

    import services.external_seed_search as mod

    src = inspect.getsource(mod.fetch_external_seed_rows)
    assert src.count("{quarantine_clause}") == 2, (
        "the anti-join must be interpolated into BOTH query_sql and count_sql"
    )


def test_the_domain_expression_is_portable_sql():
    """No Postgres-only constructs. The suite that runs must be the suite that
    gates — see #1588, where PG-only SQL shipped green and took the feed down
    for 16 minutes."""
    expr = _SEED_QUARANTINE_DOMAIN_EXPR.lower()
    for pg_only in ("regexp_replace", "~*", "::text", "ilike", "now()"):
        assert pg_only not in expr, f"{pg_only} is Postgres-only"
