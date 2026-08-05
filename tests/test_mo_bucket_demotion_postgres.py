"""The merchant_owned-bucket remediation (round-14 finding) — driven on real
Postgres against the REAL script functions, never lifted SQL.

THE FILENAME IS LOAD-BEARING (postgres-dialect-gate glob).

What must hold, each pinned below and mutation-checked:
  * selection = ALL merchant_owned bucket rows (suppressed INCLUDED — the
    deliberate delta from P4), and nothing else: real-merchant merchant_owned,
    canonical bucket, unverified bucket all untouched;
  * the group-existence RAIL: a group-less row is held (reported, never
    written) — demotion must be provably serving-safe per row, not by cohort
    assumption; the rail is re-checked at WRITE time (round-11 lesson: only
    the outer quals survive EvalPlanQual), so a row de-grouped between select
    and write is skipped;
  * CAS: a row whose scope changed between select and write is skipped;
  * the cycle closes: a demoted row that genuinely qualifies is promoted by
    the REAL D3 cron tick at the next pass — re-qualification working is the
    point of the remediation, and the dry run forecasts it
    (`would_promote_next_tick` via the shared predicate, not a retype).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from services.pdp_scope_classifier import NON_SELLER_MERCHANT_ID

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_SCHEMA = f"mo_bucket_test_{os.getpid()}"


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
    """One row per remediation-relevant shape."""
    from sqlalchemy import text

    conn.execute(text("TRUNCATE catalog_products, catalog_offers,"
                      " external_product_seeds, product_group_members"))
    rows = [
        # (pk, merchant, scope, source, suppressed, grouped)
        # THE COHORT: merchant_owned bucket rows.
        ("pk_mo_single", NON_SELLER_MERCHANT_ID, "merchant_owned",
         "backfill_2026_05", False, True),          # demote; stays unverified
        ("pk_mo_qualified", NON_SELLER_MERCHANT_ID, "merchant_owned",
         "external_brand_crawl", False, True),      # demote; next tick promotes
        ("pk_mo_suppressed", NON_SELLER_MERCHANT_ID, "merchant_owned",
         "external_brand_crawl", True, True),       # demote (delta from P4)
        ("pk_mo_nogroup", NON_SELLER_MERCHANT_ID, "merchant_owned",
         "backfill_2026_05", False, False),         # HELD — the serving rail
        # NOT THE COHORT.
        ("pk_real_mo", "acme_shop", "merchant_owned",
         "backfill_2026_05", False, True),          # real merchant — untouched
        ("pk_bucket_canon", NON_SELLER_MERCHANT_ID, "multi_merchant_canonical",
         "external_product_seeds_mirror_v1", False, True),  # P4's domain
        ("pk_bucket_unv", NON_SELLER_MERCHANT_ID, "unverified",
         None, False, True),                        # already correct
    ]
    for pk, m, scope, src, sup, grouped in rows:
        conn.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform,"
            " source_product_id, pdp_scope, pdp_scope_source, pdp_scope_set_at,"
            " suppressed_at) VALUES (:pk, :m, 'external_seed', :pk, :scope,"
            " :src, now() - interval '30 days',"
            " CASE WHEN :sup THEN now() ELSE NULL END)"),
            {"pk": pk, "m": m, "scope": scope, "src": src, "sup": sup})
        if grouped:
            conn.execute(text(
                "INSERT INTO product_group_members VALUES"
                " (:g, :m, 'external_seed', :pk)"),
                {"g": f"pg_{pk}", "m": m, "pk": pk})
    # pk_mo_qualified genuinely has two active seed domains -> predicate-true.
    for i, dom in enumerate(("a.com", "b.com")):
        conn.execute(text(
            "INSERT INTO external_product_seeds (id, attached_product_key,"
            " status, domain) VALUES (:i, 'pk_mo_qualified', 'active', :d)"),
            {"i": f"q{i}", "d": dom})
    # pk_mo_single has one seed — single-seller, must NOT promote.
    conn.execute(text(
        "INSERT INTO external_product_seeds (id, attached_product_key, status,"
        " domain) VALUES ('s0', 'pk_mo_single', 'active', 'only.com')"))


def _scopes(conn):
    from sqlalchemy import text

    return dict(conn.execute(text(
        "SELECT product_key, pdp_scope FROM catalog_products")).fetchall())


def test_dry_run_plans_the_cohort_holds_the_groupless_and_writes_nothing(engine):
    import scripts.demote_merchant_owned_bucket_rows as mod

    with engine.begin() as c:
        _seed_rows(c)
    report = _drive([mod], lambda: mod.run_demotion(apply=False))

    planned = {p["product_key"] for p in report["previous_values"]}
    assert planned == {"pk_mo_single", "pk_mo_qualified", "pk_mo_suppressed"}, planned
    assert report["held_no_group"] == ["pk_mo_nogroup"], (
        "the group-less row must be HELD and reported, not planned")
    assert report["suppressed_in_plan"] == 1, (
        "suppressed merchant_owned bucket rows are IN scope (delta from P4)")
    assert report["would_promote_next_tick"] == 1, (
        "the forecast must count exactly the genuinely-qualified row")
    assert report["by_source"] == {"backfill_2026_05": 2,
                                   "external_brand_crawl": 2}
    assert report["demoted"] == 0
    with engine.begin() as c:
        scopes = _scopes(c)
    assert scopes["pk_mo_single"] == "merchant_owned", "dry run wrote"


def test_apply_demotes_exactly_the_grouped_cohort(engine):
    import scripts.demote_merchant_owned_bucket_rows as mod
    from sqlalchemy import text

    with engine.begin() as c:
        _seed_rows(c)
    report = _drive([mod], lambda: mod.run_demotion(apply=True))

    assert report["demoted"] == 3 and report["cas_skipped"] == 0
    assert report["cas_skipped_keys"] == []
    # THE REVERSIBILITY RECORD IS THE PRE-IMAGE — round-15 caught that only
    # the dry-run test pinned this, so a refactor that rebuilds the record
    # AFTER the writes (recording 'unverified'/'mo_bucket_demotion' — a
    # worthless restore) survived the whole suite. Pin the apply-mode record
    # to the values the rows held BEFORE the run.
    recorded = {p["product_key"]: p for p in report["previous_values"]}
    assert set(recorded) == {"pk_mo_single", "pk_mo_qualified",
                             "pk_mo_suppressed"}
    for pk, prev in recorded.items():
        assert prev["pdp_scope"] == "merchant_owned", (
            f"{pk}: record holds {prev['pdp_scope']!r} — built after the "
            "write, not before; restore from it would be a no-op lie")
        assert prev["pdp_scope_source"] != mod.DEMOTION_SOURCE, (
            f"{pk}: record holds this run's own marker — post-image")
    assert recorded["pk_mo_single"]["pdp_scope_source"] == "backfill_2026_05"
    assert recorded["pk_mo_qualified"]["pdp_scope_source"] == "external_brand_crawl"
    with engine.begin() as c:
        scopes = _scopes(c)
        srcs = dict(c.execute(text(
            "SELECT product_key, pdp_scope_source FROM catalog_products")).fetchall())
    for pk in ("pk_mo_single", "pk_mo_qualified", "pk_mo_suppressed"):
        assert scopes[pk] == "unverified", pk
        assert srcs[pk] == mod.DEMOTION_SOURCE, pk
    assert scopes["pk_mo_nogroup"] == "merchant_owned", (
        "the group-less row was demoted — it would drop out of serving "
        "(identity_resolved = group OR resolved scope)")
    assert scopes["pk_real_mo"] == "merchant_owned", (
        "a REAL merchant's resolution was demoted — bucket filter lost")
    assert scopes["pk_bucket_canon"] == "multi_merchant_canonical", (
        "canonical rows are P4's domain, not this script's")
    assert scopes["pk_bucket_unv"] == "unverified"


def test_cas_skips_a_row_changed_or_degrouped_between_select_and_write(engine):
    """Both write-time rails, driven: (a) scope flipped after the select —
    refused; (b) group deleted after the select — refused (the serving rail is
    re-checked AT THE WRITE, where EvalPlanQual actually looks)."""
    import scripts.demote_merchant_owned_bucket_rows as mod
    from sqlalchemy import text

    with engine.begin() as c:
        _seed_rows(c)

    async def race():
        candidates = await mod.fetch_demotion_candidates()
        assert {c["product_key"] for c in candidates} >= {"pk_mo_single",
                                                          "pk_mo_qualified"}
        await mod.database.execute(
            "UPDATE catalog_products SET pdp_scope = 'multi_merchant_canonical'"
            " WHERE product_key = 'pk_mo_single'")
        flipped = await mod.demote_row("pk_mo_single")
        await mod.database.execute(
            "DELETE FROM product_group_members"
            " WHERE platform_product_id = 'pk_mo_qualified'")
        degrouped = await mod.demote_row("pk_mo_qualified")
        return flipped, degrouped

    flipped, degrouped = _drive([mod], race)
    assert flipped is False, "the CAS guard must refuse the stale write"
    assert degrouped is False, (
        "the group rail must be part of the WRITE-TIME quals — a row "
        "de-grouped mid-run would otherwise be demoted and unserved")
    with engine.begin() as c:
        scopes = _scopes(c)
    assert scopes["pk_mo_single"] == "multi_merchant_canonical"
    assert scopes["pk_mo_qualified"] == "merchant_owned"


def test_a_cas_skipped_row_is_attributed_in_the_report(engine, monkeypatch):
    """Round-15 finding 2: `previous_values` lists every PLANNED row, so
    without per-row attribution a naive restore would write merchant_owned
    back onto a row the script never touched (cron-promoted mid-run). The
    report must name the skipped keys; the restore guard is
    `WHERE pdp_scope_source = 'mo_bucket_demotion'` (module docstring).
    Drives the REAL run_demotion accounting over a genuinely stale plan."""
    import scripts.demote_merchant_owned_bucket_rows as mod
    from sqlalchemy import text

    with engine.begin() as c:
        _seed_rows(c)

    async def run():
        candidates = await mod.fetch_demotion_candidates()
        # A concurrent writer promotes one planned row after the fetch —
        # the run's plan is now stale for exactly that row.
        await mod.database.execute(
            "UPDATE catalog_products SET pdp_scope='multi_merchant_canonical'"
            " WHERE product_key='pk_mo_single'")

        async def stale_fetch(limit=10_000):
            return candidates

        monkeypatch.setattr(mod, "fetch_demotion_candidates", stale_fetch)
        return await mod.run_demotion(apply=True)

    report = _drive([mod], run)
    assert report["demoted"] == 2 and report["cas_skipped"] == 1
    assert report["cas_skipped_keys"] == ["pk_mo_single"], (
        "the skipped row must be NAMED — counts alone cannot guard a restore")
    assert {p["product_key"] for p in report["previous_values"]} == {
        "pk_mo_single", "pk_mo_qualified", "pk_mo_suppressed"}, (
        "the record still lists every planned row — attribution, not "
        "omission, is what distinguishes written from skipped")
    with engine.begin() as c:
        assert _scopes(c)["pk_mo_single"] == "multi_merchant_canonical", (
            "the concurrently-promoted row was clobbered")


def test_the_cycle_closes_demote_then_the_real_cron_promotes_the_qualified(engine, monkeypatch):
    """After the apply, the demoted cohort is back inside continuous
    re-qualification: the REAL D3 tick promotes exactly the genuinely
    multi-seller row (matching the dry run's forecast), leaves the
    single-seller at 'unverified', and never touches the suppressed one."""
    import jobs.pdp_scope_backfill_cron as cron
    import scripts.demote_merchant_owned_bucket_rows as mod
    from sqlalchemy import text

    monkeypatch.setenv("PDP_SCOPE_BACKFILL_ENABLED", "true")

    with engine.begin() as c:
        _seed_rows(c)
    _drive([mod], lambda: mod.run_demotion(apply=True))
    _drive([cron], lambda: cron.run_pdp_scope_backfill_tick())

    with engine.begin() as c:
        scopes = _scopes(c)
        src = c.execute(text(
            "SELECT pdp_scope_source FROM catalog_products"
            " WHERE product_key = 'pk_mo_qualified'")).scalar()
    assert scopes["pk_mo_qualified"] == "multi_merchant_canonical", (
        "the qualified demoted row must be re-promoted by the next tick — "
        "that is the remediation's purpose")
    assert src == cron.PROMOTION_SOURCE
    assert scopes["pk_mo_single"] == "unverified", (
        "single-seller must STAY unverified — re-checkable, not resolved")
    assert scopes["pk_mo_suppressed"] == "unverified", (
        "the tick promoted a SUPPRESSED row")


def test_the_forecast_and_selection_are_the_shared_predicate_not_a_copy():
    """`would_promote` must interpolate CANONICAL_SCOPE_PREDICATE — the
    promotion writer's own string. A retyped forecast is the drift shape this
    workstream removes (P4's 3,182-vs-2,201 lesson). Also pins: the marker
    fits varchar(32); the write-time quals carry scope + bucket + the group
    rail; selection carries no suppressed_at filter (suppressed rows are IN
    scope, by design)."""
    import inspect

    import scripts.demote_merchant_owned_bucket_rows as mod
    from services.pdp_identity_recovery import CANONICAL_SCOPE_PREDICATE

    assert CANONICAL_SCOPE_PREDICATE in mod._SELECT_SQL
    assert len(mod.DEMOTION_SOURCE) <= 32
    src = inspect.getsource(mod.demote_row)
    assert "pdp_scope = 'merchant_owned'" in src
    assert ":bucket" in src and "_HAS_GROUP_SQL" in src
    assert "suppressed_at" not in mod._SELECT_SQL.split("would_promote")[1], (
        "selection must not filter suppressed rows out of the cohort")
