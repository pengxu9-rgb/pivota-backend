"""The own-merchant seller term: one spelling, three writers, bucket-aware.

`seller_count` (services/pdp_scope_classifier) includes the row's own merchant —
but `merchant_id='external_seed'` is a BUCKET, not a seller: many real merchants
collapsed into one id (ADR-009 bans it). Counting it as "1 own merchant" is how
3,182 single-seller mirror rows earned `multi_merchant_canonical` and its +200
rank term.

Three writers spell the term in SQL. Before this change each had its own
spelling: the recovery predicate carried the CASE inline,
`scripts/backfill_pdp_scope.py` had an unconditional `1 +` (the live defect —
bucket row + one seed = "2 sellers"), and
`scripts/onboard_external_brand_from_crawl.py` had another unconditional `1 +`
guarded only by a docstring claiming its cohort never includes bucket rows. All
three now call `own_merchant_seller_term_sql`.

THE FILENAME IS LOAD-BEARING: `postgres-dialect-gate.yml` discovers
`tests/test_*_postgres.py` by glob. These tests drive the REAL functions
(monkeypatching each module's `database` global), not lifted SQL — a lifted copy
tests the copy, which is the failure that let the original defect ship.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from services.pdp_scope_classifier import (
    NON_SELLER_MERCHANT_ID,
    SCOPE_CANONICAL,
    SCOPE_MERCHANT_OWNED,
    own_merchant_seller_term_sql,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"pdp_backfill_test_{os.getpid()}"


@pytest.fixture(scope="module")
def engine():
    from sqlalchemy import create_engine, text

    eng = create_engine(
        os.environ["DATABASE_URL"], future=True,
        connect_args={"options": f"-csearch_path={_SCHEMA}"})
    raw = create_engine(os.environ["DATABASE_URL"], future=True)
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {_SCHEMA}"))
    with eng.begin() as c:
        for ddl in (
            "CREATE TABLE catalog_products (product_key text PRIMARY KEY,"
            " merchant_id text, platform text, source_product_id text,"
            " category_label_source text,"
            " pdp_scope varchar(32) NOT NULL DEFAULT 'unverified',"
            " pdp_scope_source varchar(32), pdp_scope_set_at timestamptz)",
            "CREATE TABLE catalog_offers (product_key text, merchant_id text)",
            "CREATE TABLE external_product_seeds (id text,"
            " attached_product_key text, status text, domain text)",
        ):
            c.execute(text(ddl))
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


def _db_for_schema():
    import databases

    url = os.environ["DATABASE_URL"]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://")
    return databases.Database(url, server_settings={"search_path": _SCHEMA})


def _drive(module, coro_factory):
    """Run an async callable from `module` with its `database` global pointed at
    the fixture schema. try/finally restores — the module global is shared."""
    db = _db_for_schema()

    async def _go():
        await db.connect()
        try:
            return await coro_factory()
        finally:
            await db.disconnect()

    original = module.database
    module.database = db
    try:
        return asyncio.run(_go())
    finally:
        module.database = original


def _seed(conn, *, pk, merchant, offers=(), seeds=(), platform="external_seed",
          spid=None):
    from sqlalchemy import text

    conn.execute(text(
        "INSERT INTO catalog_products (product_key, merchant_id, platform,"
        " source_product_id) VALUES (:pk, :m, :pl, :sp)"),
        {"pk": pk, "m": merchant, "pl": platform, "sp": spid or pk})
    for i, m in enumerate(offers):
        conn.execute(text("INSERT INTO catalog_offers VALUES (:pk, :m)"),
                     {"pk": pk, "m": m})
    for i, (attached, dom) in enumerate(seeds):
        conn.execute(text("INSERT INTO external_product_seeds VALUES"
                          " (:i, :a, 'active', :d)"),
                     {"i": f"{pk}_s{i}", "a": attached, "d": dom})


def test_the_term_itself(engine):
    """Bucket -> 0, real merchant -> 1, NULL -> 0 — executed, not read."""
    from sqlalchemy import text

    with engine.begin() as c:
        for expr, param, expected in (
            (":m", {"m": NON_SELLER_MERCHANT_ID}, 0),
            (":m", {"m": "acme_shop"}, 1),
            (":m", {"m": None}, 0),
        ):
            got = c.execute(text(
                f"SELECT {own_merchant_seller_term_sql(expr)}"), param).scalar()
            assert got == expected, (param, got)

    with pytest.raises(ValueError):
        own_merchant_seller_term_sql("")


def test_backfill_excludes_bucket_rows_entirely(engine):
    """PR #1680 review, F1/F2: this writer's terminal states are canonical /
    merchant_owned, and 'merchant_owned' is an AFFIRMED state a bucket row must
    never receive — it exits the `WHERE pdp_scope='unverified'` promotion gate
    forever. Bucket lifecycle belongs to the P4 demote / D3 promote-only cron
    pair, so bucket rows must not even be FETCHED here — including the
    genuinely two-domain one (the D3 cron promotes it instead). Real merchants
    keep the corrected own-merchant arithmetic. Drives the real
    `_fetch_batch`."""
    import scripts.backfill_pdp_scope as mod

    with engine.begin() as c:
        c.execute(__import__("sqlalchemy").text(
            "TRUNCATE catalog_products, catalog_offers, external_product_seeds"))
        _seed(c, pk="pk_bucket1", merchant=NON_SELLER_MERCHANT_ID,
              seeds=[("pk_bucket1", "zappos.com")])
        _seed(c, pk="pk_real1", merchant="acme_shop",
              seeds=[("pk_real1", "zappos.com")])
        _seed(c, pk="pk_bucket2", merchant=NON_SELLER_MERCHANT_ID,
              seeds=[("pk_bucket2", "zappos.com"), ("pk_bucket2", "nordstrom.com")])

    rows = _drive(mod, lambda: mod._fetch_batch(50))
    by_key = {r["product_key"]: r["seller_count"] for r in rows}
    assert "pk_bucket1" not in by_key, (
        "a bucket row reached the classify() loop — it would be resolved to "
        "'merchant_owned' and parked outside every promotion path")
    assert "pk_bucket2" not in by_key, (
        "even a qualifying bucket row is not this writer's to resolve")
    assert by_key["pk_real1"] == 2, "a real merchant IS a seller: own + seed = 2"


def test_backfill_still_matches_the_composite_attachment_form(engine):
    """The backfill matches seeds attached by the pipe-composite key as well as
    the product_key. Formerly a pinned divergence — the recovery predicate did
    NOT match the composite form — CLOSED 2026-08-04 (prod cohort 0): both
    sides now render `seed_attachment_keys_sql`, one spelling."""
    import scripts.backfill_pdp_scope as mod

    with engine.begin() as c:
        c.execute(__import__("sqlalchemy").text(
            "TRUNCATE catalog_products, catalog_offers, external_product_seeds"))
        _seed(c, pk="pk_comp", merchant="acme_shop", platform="shopify",
              spid="sp9", seeds=[("acme_shop|shopify|sp9", "zappos.com")])

    rows = _drive(mod, lambda: mod._fetch_batch(50))
    assert rows and rows[0]["seller_count"] == 2


def test_onboard_leaves_a_bucket_row_unverified(engine):
    """The onboard script's docstring claims its cohort never includes bucket
    rows; nothing enforced it, and an earlier version of THIS TEST pinned the
    opposite behavior (bucket -> merchant_owned) as correct. Under the shipped
    doctrine (PR #1680, F1/F2: 'merchant_owned' is an AFFIRMED state a bucket
    row must never receive — it exits the promotion gate forever) the lane now
    EXCLUDES bucket rows: they stay 'unverified', re-checkable, unstamped.
    Real-merchant rows keep the normal classification. Drives the real
    `_resolve_pdp_scope`."""
    import scripts.onboard_external_brand_from_crawl as mod
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text(
            "TRUNCATE catalog_products, catalog_offers, external_product_seeds"))
        _seed(c, pk="pk_ob_bucket", merchant=NON_SELLER_MERCHANT_ID,
              seeds=[("pk_ob_bucket", "zappos.com")])
        _seed(c, pk="pk_ob_real", merchant="acme_shop",
              seeds=[("pk_ob_real", "zappos.com")])

    _drive(mod, lambda: mod._resolve_pdp_scope(
        {}, "pk_ob_bucket", NON_SELLER_MERCHANT_ID))
    _drive(mod, lambda: mod._resolve_pdp_scope({}, "pk_ob_real", "acme_shop"))

    with engine.begin() as c:
        got = dict(c.execute(text(
            "SELECT product_key, pdp_scope FROM catalog_products")).fetchall())
        bucket_src = c.execute(text(
            "SELECT pdp_scope_source FROM catalog_products"
            " WHERE product_key = 'pk_ob_bucket'")).scalar()
    assert got["pk_ob_bucket"] == "unverified", (
        "the onboard lane wrote an affirmed scope to a bucket row — the "
        "bucket exclusion regressed; 'merchant_owned' here is a one-way door")
    assert bucket_src is None, "the excluded bucket row must stay unstamped"
    assert got["pk_ob_real"] == SCOPE_CANONICAL


def test_the_bucket_constant_is_pinned_to_seller_identity():
    """NON_SELLER_MERCHANT_ID duplicates seller_identity's constant BY VALUE —
    a runtime import would drag db + sync-service into the pure classifier
    module. This pin is the price of that choice."""
    from services.seller_identity import BANNED_BUCKET_MERCHANT_ID

    assert NON_SELLER_MERCHANT_ID == BANNED_BUCKET_MERCHANT_ID


def test_all_three_writers_call_the_shared_term():
    """AST, not grep: a retyped-but-identical CASE is behaviourally invisible,
    and stopping the retype IS the point. Inspects every module that computes a
    seller count in SQL."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ("services/pdp_identity_recovery.py",
                "scripts/backfill_pdp_scope.py",
                "scripts/onboard_external_brand_from_crawl.py"):
        tree = ast.parse((root / rel).read_text())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and ((isinstance(n.func, ast.Name)
                       and n.func.id == "own_merchant_seller_term_sql")
                      or (isinstance(n.func, ast.Attribute)
                          and n.func.attr == "own_merchant_seller_term_sql"))]
        assert calls, f"{rel} no longer calls own_merchant_seller_term_sql"


def test_both_seed_counters_render_the_shared_attachment_keys():
    """#1676's pinned divergence, closed: the recovery predicate and the
    backfill's seller count both match seeds attached by product_key AND the
    pipe-composite form, via `seed_attachment_keys_sql` — one spelling. The
    rendered helper output must appear in the predicate (the predicate is a
    formatted module constant, so the rendered form IS the shipped string),
    and the backfill must call the helper (AST, same standard as above)."""
    import ast
    import pathlib

    from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE
    from services.pdp_scope_classifier import seed_attachment_keys_sql

    assert seed_attachment_keys_sql("cp") in CANONICAL_SCOPE_PREDICATE, (
        "the predicate no longer matches the composite attachment form — "
        "the #1676 divergence is back")

    root = pathlib.Path(__file__).resolve().parent.parent
    tree = ast.parse((root / "scripts/backfill_pdp_scope.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Name)
                   and n.func.id == "seed_attachment_keys_sql")
                  or (isinstance(n.func, ast.Attribute)
                      and n.func.attr == "seed_attachment_keys_sql"))]
    assert calls, "backfill_pdp_scope no longer calls seed_attachment_keys_sql"
