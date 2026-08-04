"""P4 demotion + D3 re-promotion — the full cycle, driven on real Postgres.

THE FILENAME IS LOAD-BEARING (postgres-dialect-gate glob). These tests drive
the REAL functions — `fetch_demotion_candidates` / `demote_row` /
`run_demotion` from the demotion script, and the REAL
`run_pdp_scope_backfill_tick` from the promote-only cron — never lifted SQL.

The one property that justifies the whole D1 decision is tested end-to-end
here, in the REALISTIC ordering (review F1 on PR #1680 broke the lucky
ordering the first version used): demote -> a cron tick runs BEFORE the row
gains its second seller and must NO-OP for it (the shipped v1 resolved it to
'merchant_owned' here, exiting the promotion gate forever) -> the row gains a
second seller -> the next tick promotes. If that cycle breaks, the demotion is
a one-way door and P4 should not run.
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
        # Unverified AND suppressed AND genuinely two-domain: the promote-only
        # cron must SKIP it (suppressed_at IS NULL gate).
        ("pk_supp_unv", NON_SELLER_MERCHANT_ID, "unverified", None, True),
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
    # pk_supp_unv would qualify on two seed domains — but it is suppressed.
    for i, dom in enumerate(("s1.com", "s2.com")):
        conn.execute(text(
            "INSERT INTO external_product_seeds (id, attached_product_key,"
            " status, domain) VALUES (:i, 'pk_supp_unv', 'active', :d)"),
            {"i": f"sv{i}", "d": dom})


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


def test_the_full_cycle_demote_tick_noops_gain_seller_tick_promotes(engine, monkeypatch):
    """THE D1 PROPERTY, in the REALISTIC ordering. The first version inserted
    the second seller BEFORE the tick — the one lucky ordering under which the
    shipped v1 (classify() loop on a schedule) looked correct. Review drove
    the real sequence on Postgres: demote -> tick -> 'merchant_owned' -> gains
    seller -> tick -> {'total': 0}, never re-promoted (F1). This test pins the
    real sequence against the REAL cron tick."""
    import jobs.pdp_scope_backfill_cron as cron
    import scripts.demote_unqualified_canonical_scopes as demotion
    from sqlalchemy import text

    monkeypatch.setenv("PDP_SCOPE_BACKFILL_ENABLED", "true")

    with engine.begin() as c:
        _seed_rows(c)

    _drive([demotion], lambda: demotion.run_demotion(apply=True))
    with engine.begin() as c:
        assert _scopes(c)["pk_unqualified"] == "unverified"

    # Tick 1 — BEFORE the row gains its second seller. MUST no-op for it:
    # the row is single-seller, so it does not qualify, and the promote-only
    # tick leaves non-qualifying rows at 'unverified' — re-checkable, not
    # resolved. 'merchant_owned' here is the F1 one-way door.
    _drive([cron], lambda: cron.run_pdp_scope_backfill_tick())
    with engine.begin() as c:
        scopes = _scopes(c)
    assert scopes["pk_unqualified"] == "unverified", (
        f"F1 regression: the first tick resolved a not-yet-qualified demoted "
        f"row to {scopes['pk_unqualified']!r} — it must STAY 'unverified' "
        "(inside the promotion gate) until it actually qualifies")
    assert scopes["pk_supp_unv"] == "unverified", (
        "the tick promoted a SUPPRESSED row")

    # Only now does the row gain a second retailer.
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO external_product_seeds (id, attached_product_key,"
            " status, domain) VALUES ('u1', 'pk_unqualified', 'active',"
            " 'second.com')"))

    # Tick 2 — promotes it, stamped with the cron's own source marker.
    _drive([cron], lambda: cron.run_pdp_scope_backfill_tick())
    with engine.begin() as c:
        scopes = _scopes(c)
        src = c.execute(text(
            "SELECT pdp_scope_source FROM catalog_products"
            " WHERE product_key = 'pk_unqualified'")).scalar()
    assert scopes["pk_unqualified"] == "multi_merchant_canonical", (
        "re-promotion failed — the demotion is a one-way door, P4 must not run")
    assert src == cron.PROMOTION_SOURCE
    assert scopes["pk_supp_unv"] == "unverified", (
        "the tick promoted a SUPPRESSED row")


def test_a_row_suppressed_mid_tick_is_skipped_not_promoted(engine):
    """PR #1680 round-11 finding, driven with REAL concurrency: a second
    connection suppresses a qualified row while holding the row lock in an
    open transaction; the promotion UPDATE blocks on that lock; the suppressor
    commits. Under READ COMMITTED Postgres then re-evaluates only the OUTER
    quals (EvalPlanQual) — with only the pdp_scope re-check there, the
    suppressed row WAS promoted, and a suppressed-canonical row escapes the P4
    demotion selection (`suppressed_at IS NULL`) forever. The outer WHERE must
    therefore repeat the suppressed_at gate. This is not a simulation: the
    lock wait is observed via pg_stat_activity before the suppressor commits,
    so the write-time re-check — not the subselect — is what the assertion
    exercises."""
    import jobs.pdp_scope_backfill_cron as cron
    from sqlalchemy import text

    with engine.begin() as c:
        _seed_rows(c)
        # pk_qualified already has two active seed domains; start it inside
        # the promotion gate.
        c.execute(text(
            "UPDATE catalog_products SET pdp_scope = 'unverified'"
            " WHERE product_key = 'pk_qualified'"))

    async def race():
        suppressor = _db()
        await suppressor.connect()
        try:
            tx = suppressor.transaction()
            await tx.start()
            await suppressor.execute(
                "UPDATE catalog_products SET suppressed_at = now()"
                " WHERE product_key = 'pk_qualified'")
            promotion = asyncio.create_task(cron.run_promotion_pass(limit=100))
            # The race is real only if the promotion reaches the row lock
            # BEFORE the suppressor commits — otherwise its subselect never
            # saw the row and the outer re-check goes unexercised.
            blocked = False
            for _ in range(200):
                await asyncio.sleep(0.05)
                if promotion.done():
                    break
                blocked = bool(await suppressor.fetch_val(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE wait_event_type = 'Lock'"))
                if blocked:
                    break
            assert blocked and not promotion.done(), (
                "the promotion pass never blocked on the suppressor's row "
                "lock — the race did not happen, the test proves nothing")
            await tx.commit()
            return await promotion
        finally:
            await suppressor.disconnect()

    result = _drive([cron], race)
    assert result == {"promoted": 0}, (
        "a row suppressed between subselect and write was promoted — the "
        "outer write-time re-check is missing the suppressed_at gate")
    with engine.begin() as c:
        row = c.execute(text(
            "SELECT pdp_scope, suppressed_at FROM catalog_products"
            " WHERE product_key = 'pk_qualified'")).one()
    assert row.pdp_scope == "unverified", (
        "the suppressed row must stay 'unverified' (re-checkable), "
        f"got {row.pdp_scope!r}")
    assert row.suppressed_at is not None, "the concurrent suppression was lost"


def test_the_cron_tick_is_disabled_by_default_and_promote_only(monkeypatch):
    """The tick is glue: env gate + connect + ONE promote-only pass. Two
    load-bearing properties (both from PR #1680 review):
      * DEFAULT DISABLED — a stated deviation from house style. The first
        enabled tick promotes a serving-visible cohort (F2); enabling is an
        operator step, not a deploy side-effect.
      * the call shape is run_promotion_pass, NOT run_backfill — the classify()
        loop on a schedule resolves every unverified row (F1)."""
    import jobs.pdp_scope_backfill_cron as cron

    calls = []

    async def fake_pass(**kw):
        calls.append(kw)
        return {"promoted": 0}

    class FakeDb:
        is_connected = True

    monkeypatch.setattr(cron, "run_promotion_pass", fake_pass)
    monkeypatch.setattr(cron, "database", FakeDb())

    monkeypatch.delenv("PDP_SCOPE_BACKFILL_ENABLED", raising=False)
    asyncio.run(cron.run_pdp_scope_backfill_tick())
    assert calls == [], (
        "the DEFAULT must be disabled — enabling is an operator step")

    monkeypatch.setenv("PDP_SCOPE_BACKFILL_ENABLED", "false")
    asyncio.run(cron.run_pdp_scope_backfill_tick())
    assert calls == [], "disabled tick must not run the pass"

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


def test_the_cron_rule_is_the_predicate_not_a_copy_or_a_loop():
    """The cron's UPDATE must interpolate the SAME predicate string — a second
    spelling of the rule in the cron is the drift shape this workstream
    removes, and a classify() loop is F1."""
    import inspect

    import jobs.pdp_scope_backfill_cron as cron
    from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE

    assert CANONICAL_SCOPE_PREDICATE in cron._PROMOTE_SQL
    assert cron._PROMOTE_SQL.count("suppressed_at IS NULL") == 2, (
        "the suppressed_at gate must appear in BOTH the subselect and the "
        "outer write-time re-check — EvalPlanQual re-evaluates only the outer "
        "quals, so a subselect-only gate does not survive a concurrent "
        "suppression (PR #1680 round-11 finding)")
    src = inspect.getsource(cron)
    assert "from scripts.backfill_pdp_scope import" not in src, (
        "the cron must not import the one-shot classify() loop (F1)")
    assert "run_backfill(" not in src, (
        "the cron must not call the one-shot classify() loop (F1)")
