"""A seed attached to a suppressed catalog product is not served by the seed lane.

`catalog_products.suppressed_at` is the gate column (#1648). `fetch_external_seed_rows` -- the seam
the find_products_multi seed lane, the agent_api seed lanes and pivot_query_service all read
through -- filtered status, currency and domain quarantine but never joined catalog_products, so a
product the catalog had withdrawn kept serving through its seed. Measured prod 2026-09-27: 682
active seeds attached to suppressed products, 334 of them past the quarantine anti-join and priced
USD (athiscosmetics.com 140 wrong_brand_namesake, vmintree.in 108, headandshoulders.com 76
placeholder_price_store, ...).

These tests EXECUTE the real function on SQLite (the harness of
tests/test_external_seed_quarantine_gate.py); tests/test_external_seed_serving_currency_postgres.py
runs the same clause on Postgres.
"""

from __future__ import annotations

import asyncio

from services.external_seed_search import (
    SEED_SUPPRESSED_PRODUCT_ANTI_JOIN,
    fetch_external_seed_rows,
)
from tests.test_external_seed_quarantine_gate import _SqliteBackedDatabase

# (seed id, attached_product_key)
SEEDS = [
    ("s_suppressed", "prod::athis::1"),   # attached to a withdrawn product -> refused
    ("s_live", "prod::brand::2"),         # attached to a live product -> served
    ("s_unattached", None),               # no attachment -> served (the gate needs none)
    ("s_dangling", "prod::gone::3"),      # key names no catalog row -> served
    ("s_unsuppressed", "prod::back::4"),  # suppression lifted (suppressed_at NULL) -> served
]
PRODUCTS = [
    ("prod::athis::1", "2026-07-18 00:00:00"),
    ("prod::brand::2", None),
    ("prod::back::4", None),
]
SERVED = ["s_dangling", "s_live", "s_unattached", "s_unsuppressed"]


def _db(seeds=SEEDS, products=PRODUCTS, quarantines=()):
    db = _SqliteBackedDatabase(
        [(sid, f"{sid}.example", "hydrating serum brightening") for sid, _ in seeds], quarantines
    )
    for sid, key in seeds:
        db._conn.execute(
            "UPDATE external_product_seeds SET attached_product_key = ? WHERE id = ?", (key, sid)
        )
    db._conn.executemany("INSERT INTO catalog_products VALUES (?, ?)", products)
    return db


def _fetch(db, **kw):
    kw.setdefault("market", None)
    kw.setdefault("serving_market", "US")
    kw.setdefault("query", "serum")
    kw.setdefault("limit", 50)
    kw.setdefault("only_unattached", False)
    kw.setdefault("include_total_count", True)
    return asyncio.run(fetch_external_seed_rows(database=db, **kw))


def _ids(result):
    return sorted(r["id"] for r in result["rows"])


def test_a_seed_on_a_suppressed_product_is_refused_and_every_other_shape_is_served():
    result = _fetch(_db())
    assert result["table_missing"] is False
    assert _ids(result) == SERVED


def test_the_count_is_filtered_like_the_page():
    """A clause on the page but not the count advertises rows no page can contain."""
    result = _fetch(_db())
    assert result["total_count"] == len(SERVED)


def test_the_lean_where_path_is_gated_too():
    """Stage A's hot path for long queries on the find_products_multi lane."""
    result = _fetch(
        _db(), query="hydrating serum brightening", fast_multiterm=True, lean_where_min_tokens=2
    )
    assert result["lean_where_applied"] is True
    assert _ids(result) == SERVED


def test_the_stage_b_text_scan_shape_is_gated_too():
    result = _fetch(_db(), include_seed_data_text_match=True)
    assert _ids(result) == SERVED


def test_an_only_unattached_read_is_unchanged():
    """The default read already excludes every attached seed; the anti-join must not widen it."""
    result = _fetch(_db(), only_unattached=True)
    assert _ids(result) == ["s_unattached"]


def test_it_composes_with_the_quarantine_anti_join():
    result = _fetch(
        _db(quarantines=[(1, "domain", "s_live.example", "active", None)])
    )
    assert _ids(result) == ["s_dangling", "s_unattached", "s_unsuppressed"]


def test_every_seed_of_a_suppressed_product_is_refused_not_just_one():
    """athiscosmetics.com alone is 140 seeds; several seeds can share one product."""
    seeds = [("a", "prod::x"), ("b", "prod::x"), ("c", "prod::y")]
    result = _fetch(_db(seeds=seeds, products=[("prod::x", "2026-07-18"), ("prod::y", None)]))
    assert _ids(result) == ["c"]
    assert result["total_count"] == 1


def test_a_missing_catalog_products_table_degrades_to_empty_not_a_500():
    """The classifier's contract for this query's tables (a fresh DB without migration 058)."""
    db = _db()
    db._conn.execute("DROP TABLE catalog_products")
    result = _fetch(db)
    assert result["rows"] == [] and result["table_missing"] is True


def test_the_clause_is_portable_sql():
    """No Postgres-only constructs -- the suite that executes it runs on SQLite (#1588)."""
    clause = SEED_SUPPRESSED_PRODUCT_ANTI_JOIN.lower()
    for pg_only in ("::", "ilike", "now()", "~*", "regexp_replace"):
        assert pg_only not in clause, f"{pg_only} is Postgres-only"


def test_a_null_key_in_the_suppressed_set_does_not_blank_the_lane():
    """The NOT IN trap: one NULL in the subquery makes `x NOT IN (...)` NULL for EVERY row, a silent
    total blackout. product_key is the primary key in prod, so this guards the construction, not
    today's data."""
    result = _fetch(_db(products=PRODUCTS + [(None, "2026-07-18 00:00:00")]))
    assert _ids(result) == SERVED
