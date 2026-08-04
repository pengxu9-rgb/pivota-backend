"""P4 demotion + D3 re-promotion — the full cycle, driven on real Postgres.

THE FILENAME IS LOAD-BEARING (postgres-dialect-gate glob). These tests drive
the REAL functions — `fetch_demotion_candidates` / `demote_row` /
`run_demotion` from the demotion script, and `run_backfill` from the promotion
script — never lifted SQL.

The one property that justifies the whole D1 decision is tested end-to-end
here: a demoted row that later gains a second seller is re-promoted by the
scheduled backfill. If that cycle breaks, the demotion is a one-way door and
P4 should not run.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from services.pdp_scope_classifier import (
    LABEL_SOURCE_ENRICHMENT,
    NON_SELLER_MERCHANT_ID,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"pdp_demotion_test_{os.getpid()}"


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
            " category_label_source text, suppressed_at timestamptz,"
            " pdp_scope varchar(32) NOT NULL DEFAULT 'unverified',"
            " pdp_scope_source varchar(32), pdp_scope_set_at timestamptz)",
            "CREATE TABLE catalog_offers (product_key text, merchant_id text)",
            "CREATE TABLE external_product_seeds (id text,"
            " attached_product_key text, status text, domain text,"
            " external_product_id text)",
            "CREATE TABLE product_group_members (product_group_id text,"
            " merchant_id text, platform text, platform_product_id text)",
        ):
            c.execute(text(ddl))
    yield eng
    with raw.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE"))


def _db():
    import databases

    url = os.environ["DATABASE_URL"]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://")
    return databases.Database(url, server_settings={"search_path": _SCHEMA})


def _drive(modules, coro_factory):
    """Point each module's `database` global at the fixture schema, run, restore."""
    db = _db()

    async def _go():
        await db.connect()
        try:
            return await coro_factory()
        finally:
            await db.disconnect()

    originals = [(m, m.database) for m in modules]
    for m in modules:
        m.database = db
    try:
        return asyncio.run(_go())
    finally:
        for m, original in originals:
            m.database = original


def _seed_rows(conn):
    """One row per demotion-relevant shape. Scopes are set explicitly so the
    starting state is exactly prod's: mislabelled canonical rows."""
    from sqlalchemy import text

    conn.execute(text("TRUNCATE catalog_products, catalog_offers,"
                      " external_product_seeds, product_group_members"))
    rows = [
        # (pk, merchant, scope, label_source, suppressed)
        ("pk_unqualified", NON_SELLER_MERCHANT_ID, "multi_merchant_canonical", None, False),
        ("pk_qualified", NON_SELLER_MERCHANT_ID, "multi_merchant_canonical", None, False),
        ("pk_rule1", NON_SELLER_MERCHANT_ID, "multi_merchant_canonical",
         LABEL_SOURCE_ENRICHMENT, False),
        ("pk_real_merchant", "acme_shop", "multi_merchant_canonical", None, False),
        ("pk_suppressed", NON_SELLER_MERCHANT_ID, "multi_merchant_canonical", None, True),
    ]
    for pk, m, scope, src, sup in rows:
        conn.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform,"
            " source_product_id, category_label_source, pdp_scope,"
            " pdp_scope_source, suppressed_at)"
            " VALUES (:pk, :m, 'external_seed', :pk, :src, :scope,"
            " 'external_product_seeds_mirror_v1',"
            " CASE WHEN :sup THEN now() ELSE NULL END)"),
            {"pk": pk, "m": m, "scope": scope, "src": src, "sup": sup})
    # pk_qualified genuinely has two seed domains.
    for i, dom in enumerate(("a.com", "b.com")):
        conn.execute(text(
            "INSERT INTO external_product_seeds (id, attached_product_key,"
            " status, domain) VALUES (:i, 'pk_qualified', 'active', :d)"),
            {"i": f"q{i}", "d": dom})
    # pk_unqualified has ONE seed — the mislabel shape.
    conn.execute(text(
        "INSERT INTO external_product_seeds (id, attached_product_key, status,"
        " domain) VALUES ('u0', 'pk_unqualified', 'active', 'only.com')"))


def _scopes(conn):
    from sqlalchemy import text

    return dict(conn.execute(text(
        "SELECT product_key, pdp_scope FROM catalog_products")).fetchall())


def test_dry_run_plans_the_right_set_and_writes_nothing(engine):
    import scripts.demote_unqualified_canonical_scopes as mod

    with engine.begin() as c:
        _seed_rows(c)
    report = _drive([mod], lambda: mod.run_demotion(apply=False))

    planned = {p["product_key"] for p in report["previous_values"]}
    assert planned == {"pk_unqualified"}, planned
    # The reversibility record carries the previous state.
    assert report["previous_values"][0]["pdp_scope"] == "multi_merchant_canonical"
    assert report["demoted"] == 0
    with engine.begin() as c:
        assert _scopes(c)["pk_unqualified"] == "multi_merchant_canonical", (
            "dry run wrote")


def test_apply_demotes_exactly_the_unqualified_bucket_row(engine):
    import scripts.demote_unqualified_canonical_scopes as mod
    from sqlalchemy import text

    with engine.begin() as c:
        _seed_rows(c)
    report = _drive([mod], lambda: mod.run_demotion(apply=True))

    assert report["demoted"] == 1 and report["cas_skipped"] == 0
    with engine.begin() as c:
        scopes = _scopes(c)
        src = c.execute(text(
            "SELECT pdp_scope_source FROM catalog_products"
            " WHERE product_key = 'pk_unqualified'")).scalar()
    assert scopes["pk_unqualified"] == "unverified", (
        "D1: demote to 'unverified', the only state promotion writers act on")
    assert src == mod.DEMOTION_SOURCE
    assert scopes["pk_qualified"] == "multi_merchant_canonical"
    assert scopes["pk_rule1"] == "multi_merchant_canonical"
    assert scopes["pk_real_merchant"] == "multi_merchant_canonical"
    assert scopes["pk_suppressed"] == "multi_merchant_canonical"


def test_cas_skips_a_row_changed_between_select_and_write(engine):
    """The #1663 lesson, driven for real: fetch the plan, change the row
    underneath it, then write — the stale write must be refused."""
    import scripts.demote_unqualified_canonical_scopes as mod
    from sqlalchemy import text

    with engine.begin() as c:
        _seed_rows(c)

    async def race():
        candidates = await mod.fetch_demotion_candidates()
        assert {c["product_key"] for c in candidates} == {"pk_unqualified"}
        # A concurrent writer changes the row after our select.
        await mod.database.execute(
            "UPDATE catalog_products SET pdp_scope = 'merchant_owned'"
            " WHERE product_key = 'pk_unqualified'")
        return await mod.demote_row("pk_unqualified")

    wrote = _drive([mod], race)
    assert wrote is False, "the CAS guard must refuse the stale write"
    with engine.begin() as c:
        assert _scopes(c)["pk_unqualified"] == "merchant_owned", (
            "the concurrent value was clobbered")


def test_the_full_cycle_demote_then_gain_seller_then_cron_repromotes(engine):
    """THE D1 PROPERTY. If this fails, the demotion is a one-way door and P4
    must not run."""
    import scripts.backfill_pdp_scope as backfill
    import scripts.demote_unqualified_canonical_scopes as demotion
    from sqlalchemy import text

    with engine.begin() as c:
        _seed_rows(c)

    _drive([demotion], lambda: demotion.run_demotion(apply=True))
    with engine.begin() as c:
        assert _scopes(c)["pk_unqualified"] == "unverified"
        # The row later gains a second retailer.
        c.execute(text(
            "INSERT INTO external_product_seeds (id, attached_product_key,"
            " status, domain) VALUES ('u1', 'pk_unqualified', 'active',"
            " 'second.com')"))

    result = _drive([backfill], lambda: backfill.run_backfill())
    with engine.begin() as c:
        assert _scopes(c)["pk_unqualified"] == "multi_merchant_canonical", (
            f"re-promotion failed; backfill said {result}")


def test_the_cron_tick_is_env_gated_and_calls_the_shared_loop(monkeypatch):
    """The tick is glue: env gate + connect + run_backfill. The loop's
    semantics are PG-tested above and in test_pdp_scope_backfill_postgres —
    a tick with its own loop copy would be the drift defect itself."""
    import db.database as dbmod
    import jobs.pdp_scope_backfill_cron as cron
    import scripts.backfill_pdp_scope as backfill

    calls = []

    async def fake_run_backfill(**kw):
        calls.append(kw)
        return {"total": 0, "distribution": {}, "dry_run": False}

    class FakeDb:
        is_connected = True

    monkeypatch.setattr(backfill, "run_backfill", fake_run_backfill)
    monkeypatch.setattr(dbmod, "database", FakeDb())

    monkeypatch.setenv("PDP_SCOPE_BACKFILL_ENABLED", "false")
    asyncio.run(cron.run_pdp_scope_backfill_tick())
    assert calls == [], "disabled tick must not run the loop"

    monkeypatch.setenv("PDP_SCOPE_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("PDP_SCOPE_BACKFILL_LIMIT", "777")
    asyncio.run(cron.run_pdp_scope_backfill_tick())
    assert calls == [{"limit": 777}]


def test_the_demotion_rule_is_the_predicate_not_a_copy():
    """Selection must interpolate CANONICAL_SCOPE_PREDICATE — the promotion
    writer's own string — inside NOT(...). A measurement-only copy in a script
    is how the 3,182 estimate drifted from the 2,201 fact."""
    import scripts.demote_unqualified_canonical_scopes as mod
    from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE

    assert CANONICAL_SCOPE_PREDICATE in mod._SELECT_SQL
    assert "NOT (" in mod._SELECT_SQL
