"""A view row the builder can no longer build must stop being served.

THE DEFECT. `refresh_agent_pdp_view_for_content_key` returned False ("nothing to
build") and wrote nothing when every catalog row under a content_key came from a
quarantined source: `fetch_products_for_key` anti-joins catalog_source_quarantine,
so the build saw no rows. `delete_agent_pdp_view_if_orphaned` did not reap the
view either, because catalog_products rows still referenced the key. And the
reconciler excludes such keys on purpose (its `buildable` gate). So the view kept
whatever it held when the quarantine landed. Found 2026-09-29 on three keys the
HTML-comment cleanup could not rebuild (last written 2026-06-04 by backfill_3a_ii);
the census that followed counted 922 such rows, 553 of them still serving_eligible
or index_eligible.

WHY REAL POSTGRES. The fix is a DELETE guarded by the reconciler's buildability
predicate, including the quarantine anti-join over the full domain chain. Whether
that guard deletes exactly the right rows is a property of the SQL, which no fake
can check. Every case goes through `refresh_agent_pdp_view_for_content_key` itself,
the same call the catalog_sync hook, the seed writer and brand_relabel make.

🚨 PRIVATE DATABASE, as in test_agent_pdp_view_overlay_preservation_postgres.py:
the real catalog tables come from their real Table definitions and migrations,
which must not be built in the database every sibling gate file shares.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_DB_NAME = f"apv_unbuildable_reap_{os.getpid()}"
_BAD_DOMAIN = "quarantined-shop.example.com"
_GOOD_DOMAIN = "clean-shop.example.com"
_STALE = "Stale snapshot text from the June backfill."

# Tables the refresh reads, created from the migrations that define them (verbatim
# statements, in version order). Only tables with no Table() object are listed.
_MIGRATION_TABLES = ("external_product_seeds", "product_group_members", "catalog_source_quarantine")

# The quarantine domain chain also reads these two. Neither has a migration (prod
# creates them from application code), so they get the column shape the chain
# reads, the same stubs tests/test_quarantine_domain_chain_postgres.py uses.
_CHAIN_STUBS = (
    """CREATE TABLE IF NOT EXISTS merchant_stores (
        store_id text PRIMARY KEY, merchant_id text, platform text, domain text,
        status text, is_primary boolean, last_sync timestamptz, created_at timestamptz)""",
    """CREATE TABLE IF NOT EXISTS pdp_identity_listing (
        product_id text, source_listing_ref text, merchant_id text)""",
)

# The FK whose ON DELETE SET NULL makes a reap cost attribution (migration 140's
# column, minus the columns this file never reads).
_LINKS_STUB = """CREATE TABLE IF NOT EXISTS agent_decision_funnel_links (
    link_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    funnel_event_id uuid NOT NULL DEFAULT gen_random_uuid(),
    content_key VARCHAR(40) NULL REFERENCES agent_pdp_view(content_key) ON DELETE SET NULL,
    catalog_offer_id VARCHAR(255) NULL,
    decision_id VARCHAR(64) NULL,
    checkout_decision_id uuid NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""
# The decision layer's own records of the key (migration 140; no FK to the view, so a reap
# leaves them alone): the columns the restore's re-point reads.
_DECISION_STUBS = (
    """CREATE TABLE IF NOT EXISTS checkout_decisions (
        checkout_decision_id uuid PRIMARY KEY, content_key VARCHAR(40) NULL)""",
    """CREATE TABLE IF NOT EXISTS agent_decision_candidates (
        decision_id VARCHAR(64) NOT NULL, content_key VARCHAR(40) NULL, catalog_offer_id VARCHAR(255) NULL)""",
)
_AUDIT = "agent_pdp_view_unbuildable_reap_audit"


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


@pytest.fixture(scope="module")
def db_url():
    import psycopg2
    from sqlalchemy import create_engine
    from sqlalchemy import text as _text

    import db.catalog  # noqa: F401  (registers the catalog tables on the shared MetaData)
    from db.database import metadata

    admin = psycopg2.connect(os.environ["DATABASE_URL"])
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()

    url = _private_url()
    engine = create_engine(url, future=True)
    for name in ("agent_pdp_view", "catalog_products", "catalog_offers", "catalog_skus", "catalog_merchants"):
        metadata.tables[name].create(engine, checkfirst=True)
    migrations = sorted(
        (REPO_ROOT / "db" / "migrations").glob("*.sql"),
        key=lambda p: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)],
    )
    # The repo's own splitter: a naive split(";") breaks on the ";" inside
    # migration 134's header comment and silently never creates the table.
    from db.sql_migrations import split_statements

    for path in migrations:
        for statement in split_statements(path.read_text(encoding="utf-8")):
            if not any(t in statement for t in (*_MIGRATION_TABLES, "agent_pdp_view")):
                continue
            try:
                with engine.begin() as conn:
                    conn.exec_driver_sql(statement)
            except Exception:  # noqa: BLE001 — statements for tables this DB lacks
                pass
    with engine.begin() as conn:
        for ddl in _CHAIN_STUBS:
            conn.execute(_text(ddl))
        conn.execute(_text(_LINKS_STUB))
        for ddl in _DECISION_STUBS:
            conn.execute(_text(ddl))
        assert conn.execute(_text(f"SELECT to_regclass('{_AUDIT}') IS NOT NULL")).scalar(), \
            "migration 272 did not create the audit table"
    engine.dispose()
    try:
        yield url
    finally:
        admin = psycopg2.connect(os.environ["DATABASE_URL"])
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (_DB_NAME,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        admin.close()


@pytest.fixture
def run(db_url, monkeypatch):  # noqa: C901
    """Drive a coroutine against the private DB, with every module-level
    `database` rebound to it: the build's overlay reads use the global one.
    The reap flag is ON here; tests of the OFF default unset it."""
    import databases

    from services import agent_pdp_view_assembler as assembler

    monkeypatch.setenv("AGENT_PDP_VIEW_UNBUILDABLE_REAP", "on")
    # The view-column probes are module globals: a probe this file runs against its private
    # DB must not leak into a later file (it changes which UPSERT the assembler sends).
    assembler.reset_seller_count_probe()
    assembler.reset_market_prices_probe()
    request_cleanup = [assembler.reset_seller_count_probe, assembler.reset_market_prices_probe]

    import db.database as dbmod

    def _run(coro_factory):
        async def go():
            database = databases.Database(db_url.replace("postgresql://", "postgresql+asyncpg://"))
            await database.connect()
            # Every module-level handle, not only the ones still bound to the global: a
            # module first imported while another gate file had rebound the global keeps
            # that file's (closed) handle, and its reads then fail as "best-effort" misses
            # (the overlay read did, and the rebuild preserved the stale text).
            for mod in list(sys.modules.values()):
                handle = getattr(mod, "database", None)
                if handle is dbmod.database or isinstance(handle, databases.Database):
                    monkeypatch.setattr(mod, "database", database)
            try:
                for table in ("agent_decision_funnel_links", "checkout_decisions",
                              "agent_decision_candidates", _AUDIT, "agent_pdp_view",
                              "catalog_products", "catalog_offers",
                              "catalog_source_quarantine", "external_product_seeds"):
                    await database.execute(f"DELETE FROM {table}")
                await database.execute(
                    "INSERT INTO catalog_source_quarantine (match_type, match_value, state, reason, created_by) "
                    "VALUES ('domain', :d, 'active', 'currency mismatch', 'test')",
                    {"d": _BAD_DOMAIN},
                )
                return await coro_factory(database)
            finally:
                await database.disconnect()

        return asyncio.new_event_loop().run_until_complete(go())

    yield _run
    for reset in request_cleanup:
        reset()


async def _product(db, ck, pk, *, domain, title="Double Bottle Holder", description="Fresh copy.",
                   suppressed=False):
    await db.execute(
        """
        INSERT INTO catalog_products
          (product_key, merchant_id, platform, source_product_id, content_key, title, description,
           brand, source_domain, sync_status, suppressed_at, created_at, updated_at)
        VALUES (:pk, :merchant_id, 'external_seed', :source_product_id, :ck, :title, :description,
                'Example Brand', :domain, 'live',
                CASE WHEN :suppressed THEN NOW() ELSE NULL END, NOW(), NOW())
        """,
        {"pk": pk, "merchant_id": f"m_{pk}", "source_product_id": pk, "ck": ck, "title": title, "description": description, "domain": domain,
         "suppressed": suppressed},
    )


async def _stale_view(db, ck, *, stage="published"):
    from services.agent_pdp_view_assembler import UPSERT_SQL, row_to_upsert_params

    row = {
        "content_key": ck, "pivota_signature_id": None, "product_group_id": None,
        "brand": "Example Brand", "title": "Double Bottle Holder", "description": _STALE,
        "image_url": None, "image_urls": None, "currency": None, "price_min": None,
        "price_max": None, "offer_count": 0, "offers": None, "variants": None,
        "variants_count": 0, "gtin13": None, "category_path": None, "taxonomy_tags": None,
        "breadcrumb": None, "pdp_lifecycle_stage": stage, "sync_status": "live",
        "primary_merchant_id": "m1", "material": None, "material_source": None,
        "material_confidence": None, "care": None, "care_source": None, "care_confidence": None,
        "size_guide": None, "size_guide_source": None, "size_guide_confidence": None,
        "rating_value": None, "rating_count": None, "bullet_points": None,
        "usage_scenarios": None, "evidence_profile": None, "required_disclaimers": None,
        "refresh_source": "backfill_3a_ii", "refreshed_by_proposal_id": None,
        "preserve_enrichment": False, "preserve_evidence": False,
    }
    await db.execute(UPSERT_SQL, row_to_upsert_params(row))


async def _view(db, ck):
    row = await db.fetch_one(
        "SELECT description, refresh_source FROM agent_pdp_view WHERE content_key = :ck", {"ck": ck}
    )
    return dict(row) if row else None


async def _refresh(db, ck):
    from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key

    return await refresh_agent_pdp_view_for_content_key(ck, refresh_source="test_refresh", db=db)


def test_a_key_whose_every_row_is_quarantined_loses_its_stale_view(run) -> None:
    # The first census shape: one catalog row, on a quarantined storefront, and
    # suppressed as well. The view says 'published' and serves June's text.
    async def body(db):
        await _product(db, "ck_q", "pk_q", domain=_BAD_DOMAIN, suppressed=True)
        await _stale_view(db, "ck_q")
        built = await _refresh(db, "ck_q")
        return built, await _view(db, "ck_q")

    built, view = run(body)
    assert built is False
    assert view is None


def test_an_unsuppressed_quarantined_row_is_reaped_too(run) -> None:
    # The 553 still-eligible rows: not suppressed, only quarantined.
    async def body(db):
        await _product(db, "ck_q2", "pk_q2", domain="www." + _BAD_DOMAIN)
        await _stale_view(db, "ck_q2", stage="candidate")
        return await _refresh(db, "ck_q2"), await _view(db, "ck_q2")

    built, view = run(body)
    assert built is False
    assert view is None


def test_a_clean_member_keeps_the_key_built_from_it(run) -> None:
    # One quarantined row and one clean row: the build proceeds on the clean
    # row and replaces the stale text. Nothing is deleted.
    async def body(db):
        await _product(db, "ck_mix", "pk_bad", domain=_BAD_DOMAIN, description="Quarantined copy.")
        await _product(db, "ck_mix", "pk_good", domain=_GOOD_DOMAIN, description="Clean storefront copy.")
        await _stale_view(db, "ck_mix")
        return await _refresh(db, "ck_mix"), await _view(db, "ck_mix")

    built, view = run(body)
    assert built is True
    assert view is not None
    assert view["description"] == "Clean storefront copy."
    assert view["refresh_source"] == "test_refresh"


def test_lifting_the_quarantine_builds_the_row_again(run) -> None:
    async def body(db):
        await _product(db, "ck_back", "pk_back", domain=_BAD_DOMAIN, description="Current copy.")
        await _stale_view(db, "ck_back")
        first = await _refresh(db, "ck_back")
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        second = await _refresh(db, "ck_back")
        return first, second, await _view(db, "ck_back")

    first, second, view = run(body)
    assert first is False
    assert second is True
    assert view["description"] == "Current copy."


def test_a_key_whose_rows_all_lack_a_title_loses_its_view(run) -> None:
    async def body(db):
        await _product(db, "ck_untitled", "pk_untitled", domain=_GOOD_DOMAIN, title="   ")
        await _stale_view(db, "ck_untitled")
        return await _refresh(db, "ck_untitled"), await _view(db, "ck_untitled")

    built, view = run(body)
    assert built is False
    assert view is None


def test_an_orphan_is_still_left_to_the_orphan_reaper(run) -> None:
    # No catalog row at all: the refresh does not delete it (callers reap
    # orphans themselves and count them), and the orphan reaper still does.
    async def body(db):
        from services.agent_pdp_view_assembler import delete_agent_pdp_view_if_orphaned

        await _stale_view(db, "ck_orphan")
        built = await _refresh(db, "ck_orphan")
        after_refresh = await _view(db, "ck_orphan")
        reaped = await delete_agent_pdp_view_if_orphaned("ck_orphan", db=db)
        return built, after_refresh, reaped, await _view(db, "ck_orphan")

    built, after_refresh, reaped, final = run(body)
    assert built is False
    assert after_refresh is not None
    assert reaped is True
    assert final is None


def test_the_delete_rechecks_buildability(run) -> None:
    # A caller whose build returned None before a clean row arrived must not
    # delete the view that row now supports.
    async def body(db):
        from services.agent_pdp_view_assembler import delete_agent_pdp_view_if_unbuildable

        await _product(db, "ck_race", "pk_race", domain=_GOOD_DOMAIN)
        await _stale_view(db, "ck_race")
        return await delete_agent_pdp_view_if_unbuildable("ck_race", db=db), await _view(db, "ck_race")

    deleted, view = run(body)
    assert deleted is False
    assert view is not None


def test_the_sweep_finds_exactly_the_unbuildable_rows_and_is_dry_run_by_default(run) -> None:
    async def body(db):
        from services.agent_pdp_view_assembler import reap_unbuildable_agent_pdp_view_rows

        await _product(db, "ck_s_q", "pk_s_q", domain=_BAD_DOMAIN)
        await _product(db, "ck_s_mix", "pk_s_bad", domain=_BAD_DOMAIN)
        await _product(db, "ck_s_mix", "pk_s_good", domain=_GOOD_DOMAIN)
        await _product(db, "ck_s_ok", "pk_s_ok", domain=_GOOD_DOMAIN)
        for ck in ("ck_s_q", "ck_s_mix", "ck_s_ok", "ck_s_orphan"):
            await _stale_view(db, ck)
        dry = await reap_unbuildable_agent_pdp_view_rows(db=db)
        kept_after_dry = await _view(db, "ck_s_q")
        applied = await reap_unbuildable_agent_pdp_view_rows(db=db, dry_run=False)
        again = await reap_unbuildable_agent_pdp_view_rows(db=db, dry_run=False)
        remaining = sorted(
            r["content_key"] for r in await db.fetch_all("SELECT content_key FROM agent_pdp_view")
        )
        return dry, kept_after_dry, applied, again, remaining

    dry, kept_after_dry, applied, again, remaining = run(body)
    assert dry["content_keys"] == ["ck_s_q"] and dry["deleted"] == 0
    assert dry["by_reason"] == {"all_members_quarantined": 1}
    assert kept_after_dry is not None
    assert applied["unbuildable"] == 1 and applied["deleted"] == 1
    assert again["unbuildable"] == 0
    assert remaining == ["ck_s_mix", "ck_s_ok", "ck_s_orphan"]



async def _links(db, ck, n):
    for _ in range(n):
        await db.execute("INSERT INTO agent_decision_funnel_links (content_key) VALUES (:ck)", {"ck": ck})


async def _audit_rows(db):
    return [dict(r) for r in await db.fetch_all(
        f"SELECT content_key, reason, run_id, refresh_source, funnel_link_count, funnel_link_ids, "
        f"row_snapshot, restored_at FROM {_AUDIT} ORDER BY audit_id"
    )]


def test_with_the_flag_off_nothing_is_deleted_by_refresh_or_sweep(run, monkeypatch) -> None:
    async def body(db):
        from services.agent_pdp_view_assembler import reap_unbuildable_agent_pdp_view_rows

        monkeypatch.delenv("AGENT_PDP_VIEW_UNBUILDABLE_REAP", raising=False)
        await _product(db, "ck_off", "pk_off", domain=_BAD_DOMAIN)
        await _stale_view(db, "ck_off")
        built = await _refresh(db, "ck_off")
        dry = await reap_unbuildable_agent_pdp_view_rows(db=db)
        try:
            await reap_unbuildable_agent_pdp_view_rows(db=db, dry_run=False)
            refused = False
        except RuntimeError as exc:
            refused = "AGENT_PDP_VIEW_UNBUILDABLE_REAP" in str(exc)
        return built, dry, refused, await _view(db, "ck_off"), await _audit_rows(db)

    built, dry, refused, view, audit = run(body)
    assert built is False
    assert dry["content_keys"] == ["ck_off"]  # a dry run still reports
    assert refused is True
    assert view is not None
    assert audit == []


def test_the_audit_row_holds_the_deleted_row_and_the_links_it_nulls(run) -> None:
    async def body(db):
        await _product(db, "ck_aud", "pk_aud", domain=_BAD_DOMAIN)
        await _stale_view(db, "ck_aud")
        await _links(db, "ck_aud", 2)
        before = sorted(str(dict(r)["link_id"]) for r in await db.fetch_all(
            "SELECT link_id FROM agent_decision_funnel_links WHERE content_key = 'ck_aud'"))
        built = await _refresh(db, "ck_aud")
        nulled = await db.fetch_val(
            "SELECT count(*) FROM agent_decision_funnel_links WHERE content_key IS NULL")
        return built, before, nulled, await _view(db, "ck_aud"), await _audit_rows(db)

    built, before, nulled, view, audit = run(body)
    assert built is False and view is None
    assert nulled == 2  # the FK action ran: this is the loss the audit makes reversible
    [row] = audit
    assert row["content_key"] == "ck_aud"
    assert row["reason"] == "all_members_quarantined"
    assert row["refresh_source"] == "test_refresh" and row["run_id"].startswith("refresh:")
    assert row["funnel_link_count"] == 2
    ids = row["funnel_link_ids"] if isinstance(row["funnel_link_ids"], list) else __import__("json").loads(row["funnel_link_ids"])
    assert sorted(ids) == before
    snap = row["row_snapshot"] if isinstance(row["row_snapshot"], dict) else __import__("json").loads(row["row_snapshot"])
    assert snap["content_key"] == "ck_aud" and snap["description"] == _STALE
    assert snap["refresh_source"] == "backfill_3a_ii"


def test_a_missing_audit_table_fails_closed(run, db_url) -> None:
    async def body(db):
        await db.execute(f"ALTER TABLE {_AUDIT} RENAME TO {_AUDIT}_parked")
        try:
            from services.agent_pdp_view_assembler import reap_unbuildable_agent_pdp_view_rows

            await _product(db, "ck_noaud", "pk_noaud", domain=_BAD_DOMAIN)
            await _stale_view(db, "ck_noaud")
            built = await _refresh(db, "ck_noaud")
            try:
                await reap_unbuildable_agent_pdp_view_rows(db=db, dry_run=False)
                sweep_refused = False
            except RuntimeError as exc:
                sweep_refused = "migration 272" in str(exc)
            return built, sweep_refused, await _view(db, "ck_noaud")
        finally:
            await db.execute(f"ALTER TABLE {_AUDIT}_parked RENAME TO {_AUDIT}")

    built, sweep_refused, view = run(body)
    assert built is False
    assert sweep_refused is True
    assert view is not None  # no audit, no delete


def test_the_sweep_reports_funnel_links_per_key(run) -> None:
    async def body(db):
        from services.agent_pdp_view_assembler import reap_unbuildable_agent_pdp_view_rows

        await _product(db, "ck_l1", "pk_l1", domain=_BAD_DOMAIN)
        await _product(db, "ck_l0", "pk_l0", domain=_BAD_DOMAIN)
        await _stale_view(db, "ck_l1")
        await _stale_view(db, "ck_l0")
        await _links(db, "ck_l1", 3)
        dry = await reap_unbuildable_agent_pdp_view_rows(db=db)
        applied = await reap_unbuildable_agent_pdp_view_rows(db=db, dry_run=False)
        return dry, applied, await _audit_rows(db)

    dry, applied, audit = run(body)
    assert dry["funnel_links"] == {"ck_l1": 3}
    assert sorted(dry["content_keys"]) == ["ck_l0", "ck_l1"]
    assert applied["deleted"] == 2
    assert {r["content_key"]: r["funnel_link_count"] for r in audit} == {"ck_l0": 0, "ck_l1": 3}
    assert {r["run_id"] for r in audit} == {applied["run_id"]}


def test_a_title_override_keeps_a_titleless_key_out_of_the_sweep(run) -> None:
    # assemble_row serves an enrichment title_override over the storefront title, so a
    # key whose only title is the override is buildable; neither the sweep (which judges
    # by the predicate alone) nor the guarded delete may remove it.
    async def body(db):
        from services.agent_pdp_view_assembler import (
            delete_agent_pdp_view_if_unbuildable,
            reap_unbuildable_agent_pdp_view_rows,
        )

        await db.execute(
            "CREATE TABLE IF NOT EXISTS product_enrichment (merchant_id VARCHAR(100) NOT NULL, "
            "platform VARCHAR(50) NOT NULL, platform_product_id VARCHAR(200) NOT NULL, "
            "geo_code VARCHAR(16) NOT NULL DEFAULT 'default', title_override VARCHAR(500), "
            "PRIMARY KEY (merchant_id, platform, platform_product_id, geo_code))"
        )
        await db.execute("DELETE FROM product_enrichment")
        await _product(db, "ck_ovr", "pk_ovr", domain=_GOOD_DOMAIN, title=" ")
        await _product(db, "ck_noovr", "pk_noovr", domain=_GOOD_DOMAIN, title=" ")
        await _stale_view(db, "ck_ovr")
        await _stale_view(db, "ck_noovr")
        await db.execute(
            "INSERT INTO product_enrichment (merchant_id, platform, platform_product_id, geo_code, title_override) "
            "VALUES ('m_pk_ovr', 'external_seed', 'pk_ovr', 'default', 'Brand Title')"
        )
        dry = await reap_unbuildable_agent_pdp_view_rows(db=db)
        deleted = await delete_agent_pdp_view_if_unbuildable("ck_ovr", db=db)
        await db.execute("DELETE FROM product_enrichment")
        return dry, deleted, await _view(db, "ck_ovr")

    dry, deleted, view = run(body)
    assert dry["content_keys"] == ["ck_noovr"]
    assert deleted is False and view is not None


def test_revoking_the_quarantine_restores_the_row_and_its_links_through_the_reconciler(run) -> None:
    # Restore must not need a hand-called refresh: the reconciler tick rebuilds a reaped
    # key once its quarantine is lifted, and the links the delete nulled point at it again.
    async def body(db):
        from jobs.agent_pdp_view_reconciler_cron import run_agent_pdp_view_reconcile_tick

        await _product(db, "ck_rest", "pk_rest", domain=_BAD_DOMAIN, description="Current copy.")
        await _stale_view(db, "ck_rest")
        await _links(db, "ck_rest", 2)
        assert await _refresh(db, "ck_rest") is False
        # Still quarantined: the tick leaves it reaped.
        await run_agent_pdp_view_reconcile_tick(db=db)
        still_gone = await _view(db, "ck_rest")
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        await run_agent_pdp_view_reconcile_tick(db=db)
        linked = await db.fetch_val(
            "SELECT count(*) FROM agent_decision_funnel_links WHERE content_key = 'ck_rest'")
        return still_gone, await _view(db, "ck_rest"), linked, await _audit_rows(db)

    still_gone, view, linked, audit = run(body)
    assert still_gone is None
    assert view is not None and view["description"] == "Current copy."
    assert linked == 2
    assert [r["restored_at"] is not None for r in audit] == [True]


def test_an_expired_quarantine_restores_on_the_next_tick(run) -> None:
    async def body(db):
        from jobs.agent_pdp_view_reconciler_cron import run_agent_pdp_view_reconcile_tick

        await _product(db, "ck_exp", "pk_exp", domain=_BAD_DOMAIN, description="Current copy.")
        await _stale_view(db, "ck_exp")
        assert await _refresh(db, "ck_exp") is False
        await db.execute("UPDATE catalog_source_quarantine SET expires_at = NOW() - INTERVAL '1 minute'")
        await run_agent_pdp_view_reconcile_tick(db=db)
        return await _view(db, "ck_exp")

    view = run(body)
    assert view is not None and view["description"] == "Current copy."


def test_the_read_only_census_finds_exactly_what_the_sweep_would_reap(run, db_url) -> None:
    # scripts/ops/census_unbuildable_agent_pdp_view.py restates the predicate (it runs on the
    # current prod image, without this code). This pins the two together.
    import json
    import subprocess

    async def body(db):
        from services.agent_pdp_view_assembler import reap_unbuildable_agent_pdp_view_rows

        await db.execute(
            "CREATE TABLE IF NOT EXISTS index_pipeline_state (content_key VARCHAR(40), "
            "serving_eligible BOOLEAN NOT NULL DEFAULT FALSE, index_eligible BOOLEAN NOT NULL DEFAULT FALSE)"
        )
        await db.execute("DELETE FROM index_pipeline_state")
        await _product(db, "ck_c_q", "pk_c_q", domain=_BAD_DOMAIN)
        await _product(db, "ck_c_t", "pk_c_t", domain=_GOOD_DOMAIN, title="")
        await _product(db, "ck_c_ok", "pk_c_ok", domain=_GOOD_DOMAIN)
        for ck in ("ck_c_q", "ck_c_t", "ck_c_ok"):
            await _stale_view(db, ck)
        await db.execute("INSERT INTO index_pipeline_state VALUES ('ck_c_q', TRUE, FALSE), ('ck_c_ok', TRUE, TRUE)")
        await _links(db, "ck_c_q", 2)
        return await reap_unbuildable_agent_pdp_view_rows(db=db)

    dry = run(body)
    env = {k: v for k, v in os.environ.items()}
    env.update({"PIVOTA_ENV": "production", "DATABASE_URL": db_url, "DB_STATEMENT_TIMEOUT_SECONDS": "30"})
    program = (REPO_ROOT / "scripts" / "ops" / "census_unbuildable_agent_pdp_view.py").read_text()
    assert "@" not in program  # run_oneoff_job.sh's --args delimiter set
    out = subprocess.run([sys.executable, "-c", program], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    lines = out.stdout.splitlines()
    assert lines[0] == "GATE_OPEN" and lines[-1] == "CENSUS_DONE"
    census = json.loads(next(l for l in lines if l.startswith("CENSUS "))[len("CENSUS "):])
    assert census["content_keys"] == sorted(dry["content_keys"]) == ["ck_c_q", "ck_c_t"]
    assert census["by_reason"] == {"all_members_quarantined": 1, "no_titled_unquarantined_member": 1}
    assert census["ips_flips_serving_or_index_eligible"] == 1
    assert census["keys_with_funnel_links"] == 1 and census["funnel_links_total"] == 2

    closed = subprocess.run([sys.executable, "-c", program], cwd=REPO_ROOT,
                            env={**env, "PIVOTA_ENV": "staging"}, capture_output=True, text=True, timeout=120)
    assert closed.returncode == 3 and closed.stdout.startswith("GATE_CLOSED")
    assert "GATE_OPEN" not in closed.stdout



async def _quarantine(db, domain, *, expires_sql="NULL"):
    await db.execute(
        "INSERT INTO catalog_source_quarantine (match_type, match_value, state, reason, created_by, expires_at) "
        f"VALUES ('domain', :d, 'active', 'test', 'test', {expires_sql})",
        {"d": domain},
    )


async def _audit(db, ck):
    await db.execute(
        f"INSERT INTO {_AUDIT} (content_key, reason, run_id, row_snapshot, deleted_at) "
        "VALUES (:ck, 'all_members_quarantined', 'test:seed', '{}'::jsonb, NOW() - INTERVAL '1 day')",
        {"ck": ck},
    )


def test_keys_that_cannot_build_never_hold_the_restore_queue(run) -> None:
    # 201 reaped keys still quarantined, sorting first, then one whose quarantine expired:
    # the expired one is restored on the FIRST tick, and the stuck ones cost no rebuild.
    async def body(db):
        from jobs.agent_pdp_view_reconciler_cron import run_agent_pdp_view_reconcile_tick
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        for i in range(201):
            ck = f"ck_a{i:03d}"
            await _product(db, ck, f"pk_a{i:03d}", domain=_BAD_DOMAIN)
            await _audit(db, ck)
        await _quarantine(db, "expired-shop.example.com", expires_sql="NOW() + INTERVAL '1 hour'")
        await _product(db, "ck_zz_expired", "pk_zz_expired", domain="expired-shop.example.com",
                       description="Back after expiry.")
        await _stale_view(db, "ck_zz_expired")
        assert await _refresh(db, "ck_zz_expired") is False
        await db.execute("UPDATE catalog_source_quarantine SET expires_at = NOW() - INTERVAL '1 minute' "
                         "WHERE match_value = 'expired-shop.example.com'")
        await run_agent_pdp_view_reconcile_tick(db=db)
        after_tick = await _view(db, "ck_zz_expired")
        again = await restore_reaped_agent_pdp_view_rows(db=db, limit=50)  # pages, nothing left
        pending = await db.fetch_val(f"SELECT count(DISTINCT content_key) FROM {_AUDIT} WHERE restored_at IS NULL")
        return after_tick, again, pending

    after_tick, again, pending = run(body)
    assert after_tick is not None and after_tick["description"] == "Back after expiry."
    assert again["candidates"] == 0 and again["still_unbuildable"] == 0
    assert pending == 201  # still queued, never rebuilt while they cannot build


def test_a_key_rebuilt_by_another_writer_gets_its_links_back(run) -> None:
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_other", "pk_other", domain=_BAD_DOMAIN, description="Current copy.")
        await _stale_view(db, "ck_other")
        await _links(db, "ck_other", 2)
        assert await _refresh(db, "ck_other") is False
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        assert await _refresh(db, "ck_other") is True  # catalog_sync, the seed writer, ...
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        linked = await db.fetch_val("SELECT count(*) FROM agent_decision_funnel_links WHERE content_key = 'ck_other'")
        return counters, linked, await _audit_rows(db)

    counters, linked, audit = run(body)
    assert counters["relinked"] == 1 and counters["restored"] == 0 and counters["links_restored"] == 2
    assert linked == 2
    assert [r["restored_at"] is not None for r in audit] == [True]


def test_one_failing_key_does_not_stop_the_rest_and_the_tick_says_so(run, caplog) -> None:
    async def body(db):
        from jobs.agent_pdp_view_reconciler_cron import run_agent_pdp_view_reconcile_tick

        await _product(db, "ck_f1", "pk_f1", domain=_BAD_DOMAIN)
        await db.execute("UPDATE catalog_products SET pivota_signature_id = 'sig_f1' WHERE product_key = 'pk_f1'")
        await _stale_view(db, "ck_f1")
        assert await _refresh(db, "ck_f1") is False
        await _product(db, "ck_f2", "pk_f2", domain=_BAD_DOMAIN, description="Second copy.")
        await _stale_view(db, "ck_f2")
        assert await _refresh(db, "ck_f2") is False
        # The documented orphan state: an orphan view row holds ck_f1's signature, so its
        # rebuild hits the signature's unique index.
        await db.execute(
            "INSERT INTO agent_pdp_view (content_key, pivota_signature_id, title, refreshed_at, refresh_source) "
            "VALUES ('ck_orph', 'sig_f1', 'orphan', NOW(), 'test')"
        )
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        with caplog.at_level("WARNING", logger="jobs.agent_pdp_view_reconciler_cron"):
            await run_agent_pdp_view_reconcile_tick(db=db)
        return await _view(db, "ck_f2"), await _view(db, "ck_f1")

    f2, f1 = run(body)
    assert f2 is not None and f2["description"] == "Second copy."
    assert f1 is None
    assert any("1 key(s) failed" in r.getMessage() for r in caplog.records)


def test_the_restored_row_equals_an_in_process_build(run) -> None:
    # Restore runs in-process (the reconciler tick), so the row it writes carries the same
    # overlays as any other refresh: here an enrichment title and description.
    async def body(db):
        from db.product_enrichment import ensure_product_enrichment_table
        from jobs.agent_pdp_view_reconciler_cron import run_agent_pdp_view_reconcile_tick
        from services.agent_pdp_view_assembler import build_agent_pdp_view_row

        await ensure_product_enrichment_table()
        await db.execute("DELETE FROM product_enrichment")
        await _product(db, "ck_ovl", "pk_ovl", domain=_BAD_DOMAIN, description="Storefront copy.")
        await db.execute(
            "INSERT INTO product_enrichment (merchant_id, platform, platform_product_id, geo_code, "
            "title_override, description_markdown) VALUES ('m_pk_ovl', 'external_seed', 'pk_ovl', 'default', "
            "'Brand Title', 'Brand enrichment copy.')"
        )
        await _stale_view(db, "ck_ovl")
        assert await _refresh(db, "ck_ovl") is False
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        await run_agent_pdp_view_reconcile_tick(db=db)
        restored = dict(await db.fetch_one(
            "SELECT title, description, brand, offer_count FROM agent_pdp_view WHERE content_key = 'ck_ovl'"))
        built = await build_agent_pdp_view_row("ck_ovl", refresh_source="control", db=db)
        await db.execute("DELETE FROM product_enrichment")
        return restored, built

    restored, built = run(body)
    assert restored["title"] == "Brand Title" and restored["description"] == "Brand enrichment copy."
    assert {k: restored[k] for k in ("title", "description", "brand", "offer_count")} == {
        k: built[k] for k in ("title", "description", "brand", "offer_count")
    }


def test_links_written_while_the_key_was_reaped_are_repointed_only_when_their_decision_names_it(run) -> None:
    # The ledger writer nulls a content_key whose view row is missing and keeps the link. A link
    # inserted while the key was reaped gets the key back only if the decision layer names it:
    # its checkout decision cites the key, or its offer is the key's AND a candidate of its
    # decision cites the key. The offer alone, or a link older than the reap, is not enough.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_off", "pk_off", domain=_BAD_DOMAIN, description="Current copy.")
        await _product(db, "ck_elsewhere", "pk_elsewhere", domain=_GOOD_DOMAIN)
        for offer, pk in (("offer:mine", "pk_off"), ("offer:theirs", "pk_elsewhere")):
            await _offer(db, offer, pk)
        cd = await _checkout_decision(db, "ck_off")
        await db.execute("INSERT INTO agent_decision_candidates VALUES ('dec_off', 'ck_off', 'offer:mine')")
        await _link(db, offer="offer:mine", decision="dec_off", created_sql="NOW() - INTERVAL '2 days'")  # before the reap
        await _stale_view(db, "ck_off")
        assert await _refresh(db, "ck_off") is False
        await _link(db, offer="offer:mine", decision="dec_off")     # offer + candidate: yes
        await _link(db, offer="offer:mine")                         # offer alone: no
        await _link(db, checkout=cd)                                 # checkout decision: yes
        await _link(db, offer="offer:theirs", decision="dec_off")   # another key's offer: no
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        rows = [dict(r) for r in await db.fetch_all(
            "SELECT tag, content_key FROM (SELECT COALESCE(catalog_offer_id, '-') || '|' || COALESCE(decision_id, '-') || '|' "
            "|| (checkout_decision_id IS NOT NULL)::text || '|' || (created_at < NOW() - INTERVAL '1 day')::text AS tag, "
            "content_key FROM agent_decision_funnel_links) t ORDER BY tag")]
        return counters, rows

    counters, rows = run(body)
    assert counters["restored"] == 1 and counters["links_restored"] == 2
    # Sorted here, not by ORDER BY: CI's Postgres collation orders "-" differently from a local one.
    assert sorted((r["tag"], r["content_key"]) for r in rows) == [
        ("-|-|true|false", "ck_off"),
        ("offer:mine|-|false|false", None),
        ("offer:mine|dec_off|false|false", "ck_off"),
        ("offer:mine|dec_off|false|true", None),
        ("offer:theirs|dec_off|false|false", None),
    ]


def test_a_delete_waiting_on_a_rebuild_keeps_the_fresh_row(run, db_url) -> None:
    # The delete locks the row, then decides on a snapshot taken after the lock: a revoke and
    # rebuild committed meanwhile make the key buildable, so nothing is deleted.
    async def body(db):
        import databases

        from services.agent_pdp_view_assembler import delete_agent_pdp_view_if_unbuildable

        await _product(db, "ck_e", "pk_e", domain=_BAD_DOMAIN)
        await _stale_view(db, "ck_e")
        url = db_url.replace("postgresql://", "postgresql+asyncpg://")
        a, b = databases.Database(url), databases.Database(url)
        await a.connect()
        await b.connect()
        try:
            async with b.connection() as cb:
                tr = await cb.transaction()
                await cb.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
                await cb.execute("UPDATE agent_pdp_view SET description = 'FRESH BUILD', refresh_source = 'rebuilt' "
                                 "WHERE content_key = 'ck_e'")
                task = asyncio.ensure_future(delete_agent_pdp_view_if_unbuildable("ck_e", db=a))
                await asyncio.sleep(0.5)
                blocked = not task.done()
                await tr.commit()
            deleted = await task
        finally:
            await a.disconnect()
            await b.disconnect()
        return blocked, deleted, await _view(db, "ck_e"), await _audit_rows(db)

    blocked, deleted, view, audit = run(body)
    assert blocked is True
    assert deleted is False
    assert view is not None and view["description"] == "FRESH BUILD"
    assert audit == []


def test_restore_stamps_only_the_audit_rows_it_read(run) -> None:
    # A newer reap of the same key (an audit row the pass did not read) is not marked restored.
    async def body(db):
        from services.agent_pdp_view_assembler import _finish_restore

        await _product(db, "ck_st", "pk_st", domain=_GOOD_DOMAIN)
        await _stale_view(db, "ck_st")
        await _audit(db, "ck_st")
        first = await db.fetch_val(f"SELECT audit_id FROM {_AUDIT} WHERE content_key = 'ck_st'")
        await _audit(db, "ck_st")  # the newer row
        since = await db.fetch_val(f"SELECT MIN(deleted_at) FROM {_AUDIT}")
        await _finish_restore(db, {"content_key": "ck_st", "audit_ids": [first], "since": since},
                              present={"links": True, "checkout_decisions": True, "candidates": True})
        return [dict(r) for r in await db.fetch_all(
            f"SELECT audit_id, restored_at IS NOT NULL AS done FROM {_AUDIT} ORDER BY audit_id")], first

    rows, first = run(body)
    assert [(r["audit_id"] == first, r["done"]) for r in rows] == [(True, True), (False, False)]


def test_a_link_repointed_meanwhile_is_left_alone(run) -> None:
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_left", "pk_left", domain=_BAD_DOMAIN)
        await _product(db, "ck_now", "pk_now", domain=_GOOD_DOMAIN)
        await _stale_view(db, "ck_left")
        await _stale_view(db, "ck_now")
        await _links(db, "ck_left", 1)
        assert await _refresh(db, "ck_left") is False
        await db.execute("UPDATE agent_decision_funnel_links SET content_key = 'ck_now'")  # someone fixed it
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        return counters, await db.fetch_val("SELECT content_key FROM agent_decision_funnel_links")

    counters, ck = run(body)
    assert counters["restored"] == 1 and counters["links_restored"] == 0
    assert ck == "ck_now"


def test_restore_runs_with_the_reap_flag_off(run, monkeypatch) -> None:
    # Links must heal while deletes are switched off.
    async def body(db):
        from jobs.agent_pdp_view_reconciler_cron import run_agent_pdp_view_reconcile_tick

        await _product(db, "ck_flagoff", "pk_flagoff", domain=_BAD_DOMAIN, description="Current copy.")
        await _stale_view(db, "ck_flagoff")
        await _links(db, "ck_flagoff", 1)
        assert await _refresh(db, "ck_flagoff") is False
        monkeypatch.delenv("AGENT_PDP_VIEW_UNBUILDABLE_REAP")
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        await run_agent_pdp_view_reconcile_tick(db=db)
        linked = await db.fetch_val("SELECT count(*) FROM agent_decision_funnel_links WHERE content_key = 'ck_flagoff'")
        return await _view(db, "ck_flagoff"), linked

    view, linked = run(body)
    assert view is not None and linked == 1


def test_the_boot_ddl_builds_the_same_audit_table_as_migration_272(run) -> None:
    # Prod gets the table from db/schema_guard.py, every other environment from migration 272.
    import re as _re

    guard_src = (REPO_ROOT / "db" / "schema_guard.py").read_text()
    ddl = _re.search(r"(CREATE TABLE IF NOT EXISTS agent_pdp_view_unbuildable_reap_audit \(.*?\);)",
                     guard_src, _re.S).group(1)
    indexes = _re.findall(r'"(CREATE INDEX IF NOT EXISTS idx_agent_pdp_view_unbuildable_reap_audit_\w+ "\s*"[^"]*)"',
                          guard_src)
    assert len(indexes) == 2

    async def body(db):
        await db.execute("DROP SCHEMA IF EXISTS guard_ddl CASCADE")
        await db.execute("CREATE SCHEMA guard_ddl")
        await db.execute(ddl.replace("agent_pdp_view_unbuildable_reap_audit", "guard_ddl.agent_pdp_view_unbuildable_reap_audit"))
        for idx in indexes:
            sql = _re.sub(r'"\s*"', "", idx)
            await db.execute(sql.replace("ON agent_pdp_view_unbuildable_reap_audit",
                                         "ON guard_ddl.agent_pdp_view_unbuildable_reap_audit"))
        cols = """SELECT table_schema, column_name, data_type, is_nullable, column_default
                  FROM information_schema.columns WHERE table_name = 'agent_pdp_view_unbuildable_reap_audit'
                  ORDER BY ordinal_position"""
        rows = [dict(r) for r in await db.fetch_all(cols)]
        idx = [dict(r) for r in await db.fetch_all(
            "SELECT schemaname, indexname, indexdef FROM pg_indexes "
            "WHERE tablename = 'agent_pdp_view_unbuildable_reap_audit' ORDER BY indexname")]
        await db.execute("DROP SCHEMA guard_ddl CASCADE")
        return rows, idx

    rows, idx = run(body)

    def shape(schema):
        return [(r["column_name"], r["data_type"], r["is_nullable"],
                 (r["column_default"] or "").replace(f"{schema}.", "")) for r in rows if r["table_schema"] == schema]

    def idx_shape(schema):
        return sorted((i["indexname"], i["indexdef"].replace(f"{schema}.", "")) for i in idx if i["schemaname"] == schema)

    assert shape("public") and shape("public") == shape("guard_ddl")
    assert idx_shape("public") == idx_shape("guard_ddl")


def test_a_rebuilt_key_quarantined_again_before_the_tick_still_gets_its_links(run, monkeypatch) -> None:
    # Rebuilt by another writer, then quarantined again while deletes are off: the view row
    # serves, so the links it lost belong on it again, buildable or not.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_back_again", "pk_back_again", domain=_BAD_DOMAIN, description="Current copy.")
        await _stale_view(db, "ck_back_again")
        await _links(db, "ck_back_again", 1)
        assert await _refresh(db, "ck_back_again") is False
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        assert await _refresh(db, "ck_back_again") is True
        monkeypatch.delenv("AGENT_PDP_VIEW_UNBUILDABLE_REAP")
        await _quarantine(db, _BAD_DOMAIN)
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        linked = await db.fetch_val(
            "SELECT count(*) FROM agent_decision_funnel_links WHERE content_key = 'ck_back_again'")
        return counters, linked

    counters, linked = run(body)
    assert counters["relinked"] == 1 and linked == 1


def test_finishing_a_key_whose_row_is_gone_again_stamps_nothing(run) -> None:
    # Reaped again between the rebuild and the stamp: the lock finds no row, nothing is
    # re-pointed or stamped, and the key stays queued.
    async def body(db):
        from services.agent_pdp_view_assembler import _finish_restore

        await _product(db, "ck_gone", "pk_gone", domain=_BAD_DOMAIN)
        await _stale_view(db, "ck_gone")
        await _links(db, "ck_gone", 1)
        assert await _refresh(db, "ck_gone") is False
        row = dict(await db.fetch_one(
            f"SELECT content_key, array_agg(audit_id) AS audit_ids, MIN(deleted_at) AS since "
            f"FROM {_AUDIT} GROUP BY content_key"))
        fixed = await _finish_restore(db, row, present={"links": True, "checkout_decisions": True,
                                                        "candidates": True})
        return fixed, await _audit_rows(db)

    fixed, audit = run(body)
    assert fixed is None
    assert [r["restored_at"] for r in audit] == [None]



async def _offer(db, offer_id, product_key):
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id) VALUES (:o, :o, :pk, 'm1')",
        {"o": offer_id, "pk": product_key},
    )


async def _checkout_decision(db, ck):
    import uuid

    cd = str(uuid.uuid4())
    await db.execute("INSERT INTO checkout_decisions VALUES (CAST(:cd AS uuid), :ck)", {"cd": cd, "ck": ck})
    return cd


async def _link(db, *, offer=None, decision=None, checkout=None, created_sql="NOW()"):
    await db.execute(
        "INSERT INTO agent_decision_funnel_links (content_key, catalog_offer_id, decision_id, checkout_decision_id, created_at) "
        f"VALUES (NULL, :o, :d, CAST(:cd AS uuid), {created_sql})",
        {"o": offer, "d": decision, "cd": checkout},
    )


async def _link_ck(db, offer_id):
    return await db.fetch_val(
        "SELECT content_key FROM agent_decision_funnel_links WHERE catalog_offer_id = :o", {"o": offer_id})


def test_an_offer_that_moved_keys_does_not_carry_its_link_to_the_new_key(run) -> None:
    # Review probe F5a: both keys reaped, a link for offer:moved (cited for ck_m1), then the offer
    # moves to ck_m2's product (as merge_duplicate_canonicals does). The link must not land on
    # ck_m2. With nothing in the decision layer naming a key it stays NULL; with ck_m1's checkout
    # decision it stays NULL too, because its offer now belongs to another key and the offer and
    # the checkout must not disagree (the safe direction).
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_m1", "pk_m1", domain=_BAD_DOMAIN, description="m1 copy.")
        await _product(db, "ck_m2", "pk_m2", domain=_BAD_DOMAIN, description="m2 copy.")
        await _offer(db, "offer:moved", "pk_m1")
        await _offer(db, "offer:moved2", "pk_m1")
        for ck in ("ck_m1", "ck_m2"):
            await _stale_view(db, ck)
            assert await _refresh(db, ck) is False
        await _link(db, offer="offer:moved")
        await _link(db, offer="offer:moved2", checkout=await _checkout_decision(db, "ck_m1"))
        await db.execute("UPDATE catalog_offers SET product_key = 'pk_m2'")
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        return counters, await _link_ck(db, "offer:moved"), await _link_ck(db, "offer:moved2")

    counters, bare, cited = run(body)
    assert counters["restored"] == 2
    assert bare is None
    assert cited is None


def test_a_null_link_written_after_the_view_returned_is_not_claimed(run) -> None:
    # Review probe F5b: reaped, rebuilt by another writer, THEN a link is written with content_key
    # NULL (a writer that named no valid key). It is outside the reap window, so even a checkout
    # decision citing the key does not claim it; the never-reaped control stays NULL too.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_nn", "pk_nn", domain=_BAD_DOMAIN, description="nn copy.")
        await _offer(db, "offer:nn", "pk_nn")
        await _stale_view(db, "ck_nn")
        assert await _refresh(db, "ck_nn") is False
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        assert await _refresh(db, "ck_nn") is True
        await _link(db, offer="offer:nn", checkout=await _checkout_decision(db, "ck_nn"))
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        ck = await _link_ck(db, "offer:nn")
        await _product(db, "ck_ctrl", "pk_ctrl", domain=_GOOD_DOMAIN)
        await _offer(db, "offer:ctrl", "pk_ctrl")
        assert await _refresh(db, "ck_ctrl") is True
        await _link(db, offer="offer:ctrl")
        await restore_reaped_agent_pdp_view_rows(db=db)
        return counters, ck, await _link_ck(db, "offer:ctrl")

    counters, ck, ctrl = run(body)
    assert counters["relinked"] == 1 and counters["links_restored"] == 0
    assert ck is None
    assert ctrl is None


def test_the_window_starts_at_the_first_reap(run) -> None:
    # Two reaps of one key (two audit rows): a link written between them is inside the window.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_two", "pk_two", domain=_GOOD_DOMAIN)
        for days in (3, 1):
            await db.execute(
                f"INSERT INTO {_AUDIT} (content_key, reason, run_id, row_snapshot, deleted_at) "
                f"VALUES ('ck_two', 'all_members_quarantined', 'test:seed', '{{}}'::jsonb, NOW() - INTERVAL '{days} days')")
        await _link(db, checkout=await _checkout_decision(db, "ck_two"), created_sql="NOW() - INTERVAL '2 days'")
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        return counters, await db.fetch_val("SELECT content_key FROM agent_decision_funnel_links")

    counters, ck = run(body)
    assert counters["restored"] == 1 and counters["links_restored"] == 1
    assert ck == "ck_two"


def test_a_pass_stops_at_its_budget_and_the_next_one_finishes(run, monkeypatch) -> None:
    async def body(db):
        from jobs.agent_pdp_view_reconciler_cron import run_agent_pdp_view_reconcile_tick
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        for i in range(5):
            await _product(db, f"ck_b{i}", f"pk_b{i}", domain=_GOOD_DOMAIN)
            await _audit(db, f"ck_b{i}")
        first = await restore_reaped_agent_pdp_view_rows(db=db, limit=2, max_keys=2)
        monkeypatch.setenv("AGENT_PDP_VIEW_RESTORE_MAX_KEYS", "1")
        await run_agent_pdp_view_reconcile_tick(db=db)
        after_tick = await db.fetch_val(f"SELECT count(*) FROM {_AUDIT} WHERE restored_at IS NOT NULL")
        rest = await restore_reaped_agent_pdp_view_rows(db=db, limit=2)
        return first, after_tick, rest

    first, after_tick, rest = run(body)
    assert first["restored"] == 2 and first["budget_exhausted"] == 1
    assert after_tick == 3
    assert rest["restored"] == 2 and rest["budget_exhausted"] == 0


def test_a_key_the_builder_declines_is_backed_off_not_rebuilt_every_pass(run) -> None:
    # The predicate calls an NBSP-only title buildable (PG trim strips spaces only); the builder
    # does not. It is tried, backed off, and skipped until its next_attempt_at.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_nbsp", "pk_nbsp", domain=_GOOD_DOMAIN, title="  ")
        await _audit(db, "ck_nbsp")
        first = await restore_reaped_agent_pdp_view_rows(db=db)
        second = await restore_reaped_agent_pdp_view_rows(db=db)
        state1 = dict(await db.fetch_one(
            f"SELECT attempts, next_attempt_at > NOW() + INTERVAL '50 minutes' AS later FROM {_AUDIT}"))
        await db.execute(f"UPDATE {_AUDIT} SET next_attempt_at = NOW() - INTERVAL '1 second'")
        third = await restore_reaped_agent_pdp_view_rows(db=db)
        state2 = dict(await db.fetch_one(
            f"SELECT attempts, next_attempt_at > NOW() + INTERVAL '110 minutes' AS later FROM {_AUDIT}"))
        return first, second, state1, third, state2

    first, second, state1, third, state2 = run(body)
    assert first["still_unbuildable"] == 1
    assert second["candidates"] == 0
    assert state1 == {"attempts": 1, "later": True}
    assert third["still_unbuildable"] == 1
    assert state2 == {"attempts": 2, "later": True}


def test_one_pass_pages_through_every_finishable_key(run) -> None:
    async def body(db):
        from services import agent_pdp_view_assembler as apv

        for i in range(5):
            await _product(db, f"ck_p{i}", f"pk_p{i}", domain=_GOOD_DOMAIN)
            await _audit(db, f"ck_p{i}")
        reads = []

        class Counting:
            def __getattr__(self, name):
                return getattr(db, name)

            async def fetch_all(self, query, values=None):
                if "array_agg(a.audit_id" in query:
                    reads.append(values["after"])
                return await db.fetch_all(query, values)

        counters = await apv.restore_reaped_agent_pdp_view_rows(db=Counting(), limit=2)
        return counters, reads

    counters, reads = run(body)
    assert counters["restored"] == 5
    assert reads == ["", "ck_p1", "ck_p3"]


def test_a_full_page_of_keys_that_do_not_build_still_ends_the_pass(run) -> None:
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        for i in range(4):
            await _product(db, f"ck_s{i}", f"pk_s{i}", domain=_GOOD_DOMAIN, title=" ")
            await _audit(db, f"ck_s{i}")
        await _product(db, "ck_s9", "pk_s9", domain=_GOOD_DOMAIN)
        await _audit(db, "ck_s9")
        return await asyncio.wait_for(restore_reaped_agent_pdp_view_rows(db=db, limit=2), timeout=60)

    counters = run(body)
    assert counters["still_unbuildable"] == 4 and counters["restored"] == 1


class _LockProbe:
    """Delegates to `db`; right after a FOR UPDATE read returns, checks from a second
    connection whether that row is still locked (NOWAIT), i.e. whether the lock is held
    across the work that follows it."""

    def __init__(self, db, other, ck):
        self._db, self._other, self._ck, self.held = db, other, ck, []

    def __getattr__(self, name):
        return getattr(self._db, name)

    def transaction(self):
        return self._db.transaction()

    async def fetch_one(self, query, values=None):
        row = await self._db.fetch_one(query, values)
        if "FOR UPDATE" in query:
            try:
                async with self._other.transaction():
                    await self._other.fetch_one(
                        "SELECT 1 FROM agent_pdp_view WHERE content_key = :ck FOR UPDATE NOWAIT", {"ck": self._ck})
                self.held.append(False)
            except Exception as exc:  # noqa: BLE001 — LockNotAvailable: still held
                self.held.append("could not obtain lock" in str(exc) or "LockNotAvailable" in type(exc).__name__)
        return row


def test_the_delete_holds_its_row_lock_until_the_delete(run, db_url) -> None:
    async def body(db):
        import databases

        from services.agent_pdp_view_assembler import delete_agent_pdp_view_if_unbuildable

        await _product(db, "ck_lk", "pk_lk", domain=_BAD_DOMAIN)
        await _stale_view(db, "ck_lk")
        other = databases.Database(db_url.replace("postgresql://", "postgresql+asyncpg://"))
        await other.connect()
        try:
            probe = _LockProbe(db, other, "ck_lk")
            deleted = await delete_agent_pdp_view_if_unbuildable("ck_lk", db=probe)
        finally:
            await other.disconnect()
        return deleted, probe.held

    deleted, held = run(body)
    assert deleted is True
    assert held == [True]


def test_finishing_a_restore_holds_its_row_lock(run, db_url) -> None:
    async def body(db):
        import databases

        from services.agent_pdp_view_assembler import _finish_restore

        await _product(db, "ck_fl", "pk_fl", domain=_GOOD_DOMAIN)
        await _stale_view(db, "ck_fl")
        await _audit(db, "ck_fl")
        row = dict(await db.fetch_one(
            f"SELECT content_key, array_agg(audit_id) AS audit_ids, MIN(deleted_at) AS since FROM {_AUDIT} GROUP BY content_key"))
        other = databases.Database(db_url.replace("postgresql://", "postgresql+asyncpg://"))
        await other.connect()
        try:
            probe = _LockProbe(db, other, "ck_fl")
            fixed = await _finish_restore(probe, row, present={"links": True, "checkout_decisions": True, "candidates": True})
        finally:
            await other.disconnect()
        return fixed, probe.held

    fixed, held = run(body)
    assert fixed == 0
    assert held == [True]


def test_the_boot_heal_builds_the_same_index_as_migration_273(run) -> None:
    import re as _re

    guard_src = (REPO_ROOT / "db" / "schema_guard.py").read_text()
    m = _re.search(r'"(CREATE INDEX IF NOT EXISTS idx_agent_decision_funnel_links_content_key "\s*"[^"]*)"', guard_src)
    assert m, "schema_guard.py lost the migration 273 heal"
    heal = _re.sub(r'"\s*"', "", m.group(1))
    mig = (REPO_ROOT / "db" / "migrations" / "273_agent_decision_funnel_links_content_key_index.sql").read_text()
    mig_sql = _re.search(r"(CREATE INDEX CONCURRENTLY IF NOT EXISTS .*?;)", mig, _re.S).group(1)

    async def body(db):
        out = {}
        for schema, sql in (("heal273", heal), ("mig273", mig_sql.replace(" CONCURRENTLY", ""))):
            await db.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await db.execute(f"CREATE SCHEMA {schema}")
            await db.execute(f"CREATE TABLE {schema}.agent_decision_funnel_links (LIKE public.agent_decision_funnel_links)")
            await db.execute(sql.replace("ON agent_decision_funnel_links", f"ON {schema}.agent_decision_funnel_links"))
            out[schema] = [dict(r)["indexdef"].replace(f"{schema}.", "") for r in await db.fetch_all(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = :s", {"s": schema})]
            await db.execute(f"DROP SCHEMA {schema} CASCADE")
        return out

    out = run(body)
    assert len(out["mig273"]) == 1 and "WHERE (content_key IS NOT NULL)" in out["mig273"][0]
    assert out["heal273"] == out["mig273"]


def test_the_boot_heal_runs_and_creates_the_273_index(run) -> None:
    # Prod skips migrations: this heal is the index's only automatic path. It must be called
    # from the boot guard, and calling it must build the index.
    import ast

    import db.schema_guard as guard

    tree = ast.parse((REPO_ROOT / "db" / "schema_guard.py").read_text())
    boot = next(f for f in ast.walk(tree)
                if isinstance(f, ast.AsyncFunctionDef) and f.name == "ensure_required_schema_light")
    calls = [n for n in ast.walk(boot) if isinstance(n, ast.Await) and isinstance(n.value, ast.Call)
             and getattr(n.value.func, "id", "") == "_heal_unbuildable_reap_schema"]
    assert len(calls) == 1

    async def body(db):
        await db.execute("DROP INDEX IF EXISTS idx_agent_decision_funnel_links_content_key")
        await guard._heal_unbuildable_reap_schema()
        return await db.fetch_val(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_agent_decision_funnel_links_content_key'")

    indexdef = run(body)
    assert indexdef and "(content_key)" in indexdef and "WHERE (content_key IS NOT NULL)" in indexdef



def test_a_checkout_decision_citing_another_key_does_not_claim_the_link(run) -> None:
    # Mutant r7 (the checkout arm accepting any key) must fail here.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_r7", "pk_r7", domain=_BAD_DOMAIN, description="r7 copy.")
        await _product(db, "ck_r7_other", "pk_r7_other", domain=_GOOD_DOMAIN)
        await _stale_view(db, "ck_r7")
        assert await _refresh(db, "ck_r7") is False
        await _link(db, checkout=await _checkout_decision(db, "ck_r7_other"))
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        return counters, await db.fetch_val("SELECT content_key FROM agent_decision_funnel_links")

    counters, ck = run(body)
    assert counters["restored"] == 1 and counters["links_restored"] == 0
    assert ck is None


def test_a_checkout_decision_and_an_offer_that_disagree_do_not_claim_the_link(run) -> None:
    # Review N2: the checkout decision cites the key, but the link's own offer is another key's.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_n2", "pk_n2", domain=_BAD_DOMAIN, description="n2 copy.")
        await _product(db, "ck_n2_b", "pk_n2_b", domain=_GOOD_DOMAIN)
        await _offer(db, "offer:n2_mine", "pk_n2")
        await _offer(db, "offer:n2_theirs", "pk_n2_b")
        await _stale_view(db, "ck_n2")
        assert await _refresh(db, "ck_n2") is False
        await _link(db, offer="offer:n2_theirs", checkout=await _checkout_decision(db, "ck_n2"))
        await _link(db, offer="offer:n2_mine", checkout=await _checkout_decision(db, "ck_n2"))
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        return counters, await _link_ck(db, "offer:n2_theirs"), await _link_ck(db, "offer:n2_mine")

    counters, theirs, mine = run(body)
    assert counters["links_restored"] == 1
    assert theirs is None and mine == "ck_n2"


def test_twins_in_one_slate_and_a_moved_offer_go_to_the_key_the_decision_cited(run) -> None:
    # Review N1: one decision's candidates cite both twins, each with its own offer. A link for
    # ck_a's offer is written while both are reaped; the offer then moves to ck_b's product. The
    # candidate row records what the decision saw (ck_a with that offer), so the link goes to
    # ck_a, never to the offer's new key. A decision citing one offer under two keys proves
    # nothing and claims nothing.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_ta", "pk_ta", domain=_BAD_DOMAIN, description="a copy.")
        await _product(db, "ck_tb", "pk_tb", domain=_BAD_DOMAIN, description="b copy.")
        await _offer(db, "offer:ta", "pk_ta")
        await _offer(db, "offer:tb", "pk_tb")
        await _offer(db, "offer:amb", "pk_ta")
        await db.execute(
            "INSERT INTO agent_decision_candidates VALUES ('dec_twins', 'ck_ta', 'offer:ta'), "
            "('dec_twins', 'ck_tb', 'offer:tb'), ('dec_amb', 'ck_ta', 'offer:amb'), ('dec_amb', 'ck_tb', 'offer:amb')")
        for ck in ("ck_ta", "ck_tb"):
            await _stale_view(db, ck)
            assert await _refresh(db, ck) is False
        await _link(db, offer="offer:ta", decision="dec_twins")
        await _link(db, offer="offer:amb", decision="dec_amb")
        await db.execute("UPDATE catalog_offers SET product_key = 'pk_tb' WHERE offer_id = 'offer:ta'")
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        counters = await restore_reaped_agent_pdp_view_rows(db=db)
        return counters, await _link_ck(db, "offer:ta"), await _link_ck(db, "offer:amb")

    counters, moved, amb = run(body)
    assert counters["restored"] == 2 and counters["links_restored"] == 1
    assert moved == "ck_ta"
    assert amb is None


def test_retry_backoff_restores_a_backed_off_key_now(run) -> None:
    # After a fixed outage: a key backed off for days is skipped by a plain pass, and restored
    # by `reap_orphaned_agent_pdp_view --restore --retry-backoff` (which drives the same pass).
    async def body(db):
        import argparse

        from scripts.reap_orphaned_agent_pdp_view import _drive
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_bk", "pk_bk", domain=_GOOD_DOMAIN)
        await _audit(db, "ck_bk")
        await db.execute(f"UPDATE {_AUDIT} SET attempts = 6, next_attempt_at = NOW() + INTERVAL '5 days'")
        plain = await restore_reaped_agent_pdp_view_rows(db=db)
        args = argparse.Namespace(restore=True, retry_backoff=False, unbuildable=False, apply=False, limit=0)
        script_plain = await _drive(args, db=db)
        args.retry_backoff = True
        script_retry = await _drive(args, db=db)
        return plain, script_plain, script_retry, await _view(db, "ck_bk")

    plain, script_plain, script_retry, view = run(body)
    assert plain["candidates"] == 0 and script_plain["candidates"] == 0
    assert script_retry["restored"] == 1
    assert view is not None


def test_the_heal_upgrades_an_audit_table_from_an_earlier_build(run) -> None:
    # A database that ran an earlier build has the audit table without the backoff columns;
    # CREATE TABLE IF NOT EXISTS leaves it so, and the restore's queue read would fail. The heal
    # adds them, and restore then works on the old rows.
    import db.schema_guard as guard

    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await db.execute(f"ALTER TABLE {_AUDIT} RENAME TO {_AUDIT}_current")
        try:
            await db.execute(
                f"CREATE TABLE {_AUDIT} (audit_id BIGSERIAL PRIMARY KEY, content_key TEXT NOT NULL, "
                "pivota_signature_id TEXT, reason TEXT NOT NULL, run_id TEXT NOT NULL, refresh_source TEXT, "
                "funnel_link_count INTEGER NOT NULL DEFAULT 0, funnel_link_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "row_snapshot JSONB NOT NULL, deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), restored_at TIMESTAMPTZ)")
            await _product(db, "ck_old", "pk_old", domain=_GOOD_DOMAIN)
            await db.execute(
                f"INSERT INTO {_AUDIT} (content_key, reason, run_id, row_snapshot) "
                "VALUES ('ck_old', 'all_members_quarantined', 'old:1', '{}'::jsonb)")
            try:
                await restore_reaped_agent_pdp_view_rows(db=db)
                before = "ok"
            except Exception as exc:  # noqa: BLE001
                before = type(exc).__name__
            await guard._heal_unbuildable_reap_schema()
            cols = sorted(dict(r)["column_name"] for r in await db.fetch_all(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :t "
                "AND column_name IN ('attempts', 'next_attempt_at')", {"t": _AUDIT}))
            after = await restore_reaped_agent_pdp_view_rows(db=db)
            return before, cols, after
        finally:
            await db.execute(f"DROP TABLE IF EXISTS {_AUDIT}")
            await db.execute(f"ALTER TABLE {_AUDIT}_current RENAME TO {_AUDIT}")

    before, cols, after = run(body)
    assert before == "UndefinedColumnError"
    assert cols == ["attempts", "next_attempt_at"]
    assert after["restored"] == 1


# ---- be#2681 review round: cross-arm precedence, NULL semantics, retry and heal pins ----------

async def _mklink(db, *, offer=None, decision=None, checkout=None):
    return str(await db.fetch_val(
        "INSERT INTO agent_decision_funnel_links (content_key, catalog_offer_id, decision_id, checkout_decision_id) "
        "VALUES (NULL, :o, :d, CAST(:cd AS uuid)) RETURNING link_id",
        {"o": offer, "d": decision, "cd": checkout}))


async def _ck_of(db, link_id):
    return await db.fetch_val(
        "SELECT content_key FROM agent_decision_funnel_links WHERE link_id = CAST(:l AS uuid)", {"l": link_id})


async def _cand(db, decision, ck, offer):
    await db.execute("INSERT INTO agent_decision_candidates VALUES (:d, :ck, :o)", {"d": decision, "ck": ck, "o": offer})


async def _reap(db, *pairs):
    for ck, pk in pairs:
        await _product(db, ck, pk, domain=_BAD_DOMAIN, description=f"{ck} copy.")
    for ck, _ in pairs:
        await _stale_view(db, ck)
        assert await _refresh(db, ck) is False


async def _revoke_restore(db, **kw):
    from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

    await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
    return await restore_reaped_agent_pdp_view_rows(db=db, **kw)


def test_twins_citing_one_offer_in_different_decisions_each_get_their_own_key(run) -> None:
    # Review p1: the ambiguity check is per decision, not across decisions.
    async def body(db):
        await _reap(db, ("ck_p1a", "pk_p1a"), ("ck_p1b", "pk_p1b"))
        await _offer(db, "offer:p1", "pk_p1a")
        await _cand(db, "dec_p1_1", "ck_p1a", "offer:p1")
        await _cand(db, "dec_p1_2", "ck_p1b", "offer:p1")
        l1 = await _mklink(db, offer="offer:p1", decision="dec_p1_1")
        l2 = await _mklink(db, offer="offer:p1", decision="dec_p1_2")
        await _revoke_restore(db)
        return await _ck_of(db, l1), await _ck_of(db, l2)

    assert run(body) == ("ck_p1a", "ck_p1b")


@pytest.mark.parametrize("cand_key,checkout_key", [("ck_x1", "ck_x2"), ("ck_x2", "ck_x1")])
def test_a_checkout_naming_another_key_overrides_the_candidate_arm(run, cand_key, checkout_key) -> None:
    # Review p6: the slate saw the offer under cand_key, a twin merge moved it to checkout_key's
    # product, and the order cites checkout_key with it (consistent). Both reaped, one pass. The
    # checkout (the purchase-time record) decides, whichever key restores first.
    async def body(db):
        pk = {"ck_x1": "pk_x1", "ck_x2": "pk_x2"}
        await _reap(db, ("ck_x1", "pk_x1"), ("ck_x2", "pk_x2"))
        await _offer(db, "offer:x", pk[cand_key])
        await _cand(db, "dec_x", cand_key, "offer:x")
        await db.execute("UPDATE catalog_offers SET product_key = :pk WHERE offer_id = 'offer:x'",
                         {"pk": pk[checkout_key]})
        link = await _mklink(db, offer="offer:x", decision="dec_x", checkout=await _checkout_decision(db, checkout_key))
        await _revoke_restore(db)
        return await _ck_of(db, link)

    assert run(body) == checkout_key


def test_a_checkout_and_a_candidate_that_disagree_leave_the_link_null(run) -> None:
    # Review p6b: the offer is ck_b's throughout and the slate cited (ck_b, offer); the checkout
    # cites ck_a. The checkout arm refuses (offer vs checkout), and the candidate arm defers to
    # the checkout, so the link stays NULL.
    async def body(db):
        await _reap(db, ("ck_y_a", "pk_y_a"), ("ck_y_b", "pk_y_b"))
        await _offer(db, "offer:y", "pk_y_b")
        await _cand(db, "dec_y", "ck_y_b", "offer:y")
        link = await _mklink(db, offer="offer:y", decision="dec_y", checkout=await _checkout_decision(db, "ck_y_a"))
        await _revoke_restore(db)
        return await _ck_of(db, link)

    assert run(body) is None


def test_the_checkout_key_wins_even_when_the_candidate_key_restores_first(run) -> None:
    # Review p6c: the checkout key is still quarantined when the candidate key restores; the
    # candidate arm must not take the link meanwhile. It goes to the checkout key once that restores.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_z1", "pk_z1", domain="other-bad.example.com", description="z1")
        await _product(db, "ck_z2", "pk_z2", domain=_BAD_DOMAIN, description="z2")
        await db.execute(
            "INSERT INTO catalog_source_quarantine (match_type, match_value, state, reason, created_by) "
            "VALUES ('domain', 'other-bad.example.com', 'active', 't', 't')")
        for ck in ("ck_z1", "ck_z2"):
            await _stale_view(db, ck)
            assert await _refresh(db, ck) is False
        await _offer(db, "offer:z", "pk_z2")
        await _cand(db, "dec_z", "ck_z2", "offer:z")
        await db.execute("UPDATE catalog_offers SET product_key = 'pk_z1' WHERE offer_id = 'offer:z'")
        link = await _mklink(db, offer="offer:z", decision="dec_z", checkout=await _checkout_decision(db, "ck_z1"))
        await db.execute(f"UPDATE catalog_source_quarantine SET state = 'revoked' WHERE match_value = '{_BAD_DOMAIN}'")
        first = await restore_reaped_agent_pdp_view_rows(db=db)
        between = await _ck_of(db, link)
        await db.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        second = await restore_reaped_agent_pdp_view_rows(db=db)
        return first, between, second, await _ck_of(db, link)

    first, between, second, final = run(body)
    assert first["restored"] == 1 and second["restored"] == 1
    assert between is None
    assert final == "ck_z1"


def test_null_keys_are_unknown_never_a_match_and_never_a_contradiction(run) -> None:
    # Pinned on purpose (review p7, p8, m11): a NULL-key candidate claims nothing and does not make
    # an offer ambiguous; an offer whose product has a NULL content_key does not disagree with a
    # checkout.
    async def body(db):
        await _reap(db, ("ck_n7", "pk_n7"), ("ck_n8", "pk_n8"), ("ck_n9", "pk_n9"))
        await _offer(db, "offer:n7", "pk_n7")
        await _offer(db, "offer:n8", "pk_n8")
        await _product(db, "ck_null_owner", "pk_null_owner", domain=_GOOD_DOMAIN)
        await db.execute("UPDATE catalog_products SET content_key = NULL WHERE product_key = 'pk_null_owner'")
        await _offer(db, "offer:n9", "pk_null_owner")
        await _cand(db, "dec_n7", "ck_n7", "offer:n7")
        await _cand(db, "dec_n7", None, "offer:n7")      # NULL-key twin: not ambiguity
        await _cand(db, "dec_n8", None, "offer:n8")      # NULL-key candidate alone: no claim
        l7 = await _mklink(db, offer="offer:n7", decision="dec_n7")
        l8 = await _mklink(db, offer="offer:n8", decision="dec_n8")
        l9 = await _mklink(db, offer="offer:n9", checkout=await _checkout_decision(db, "ck_n9"))
        await _revoke_restore(db)
        return await _ck_of(db, l7), await _ck_of(db, l8), await _ck_of(db, l9)

    assert run(body) == ("ck_n7", None, "ck_n9")


def test_retry_backoff_tries_a_failing_key_once_per_run_and_backs_it_off_again(run, monkeypatch) -> None:
    # Review r1: under --retry-backoff a key whose rebuild raises is tried once, backed off again,
    # and skipped by a plain pass in between.
    from services import agent_pdp_view_assembler as apv

    calls = []

    async def boom(ck, **kw):
        calls.append(ck)
        raise RuntimeError("still broken")

    async def body(db):
        await _product(db, "ck_r1", "pk_r1", domain=_GOOD_DOMAIN)
        await _audit(db, "ck_r1")
        monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", boom)
        out = []
        for _ in range(3):
            out.append(await apv.restore_reaped_agent_pdp_view_rows(db=db, ignore_backoff=True))
            out.append(await apv.restore_reaped_agent_pdp_view_rows(db=db))
        st = dict(await db.fetch_one(f"SELECT attempts, next_attempt_at > NOW() AS later FROM {_AUDIT}"))
        return out, st

    out, st = run(body)
    assert [o["errors"] for o in out] == [1, 0, 1, 0, 1, 0]
    assert len(calls) == 3
    assert st == {"attempts": 3, "later": True}


def test_retry_backoff_backs_off_a_key_the_builder_still_declines(run) -> None:
    # Review m4: the decline branch re-backs-off under --retry-backoff too.
    async def body(db):
        from services.agent_pdp_view_assembler import restore_reaped_agent_pdp_view_rows

        await _product(db, "ck_dec", "pk_dec", domain=_GOOD_DOMAIN, title=" ")
        await _audit(db, "ck_dec")
        await db.execute(f"UPDATE {_AUDIT} SET attempts = 3, next_attempt_at = NOW() + INTERVAL '2 days'")
        retry = await restore_reaped_agent_pdp_view_rows(db=db, ignore_backoff=True)
        st = dict(await db.fetch_one(
            f"SELECT attempts, next_attempt_at > NOW() + INTERVAL '7 hours' AS later FROM {_AUDIT}"))
        return retry, st

    retry, st = run(body)
    assert retry["still_unbuildable"] == 1
    assert st == {"attempts": 4, "later": True}


def test_the_tick_never_ignores_backoff(run) -> None:
    # Review r3.
    async def body(db):
        from jobs.agent_pdp_view_reconciler_cron import _restore_reaped_rows

        await _product(db, "ck_r3", "pk_r3", domain=_GOOD_DOMAIN)
        await _audit(db, "ck_r3")
        await db.execute(f"UPDATE {_AUDIT} SET next_attempt_at = NOW() + INTERVAL '1 day'")
        await _restore_reaped_rows(db)
        return await db.fetch_val(f"SELECT restored_at FROM {_AUDIT}")

    assert run(body) is None


def test_retry_backoff_pages_past_the_first_page(run) -> None:
    # Review r4.
    from services import agent_pdp_view_assembler as apv

    async def body(db):
        for i in range(3):
            await _product(db, f"ck_r4_{i}", f"pk_r4_{i}", domain=_GOOD_DOMAIN)
            await _audit(db, f"ck_r4_{i}")
        await db.execute(f"UPDATE {_AUDIT} SET attempts = 4, next_attempt_at = NOW() + INTERVAL '3 days'")
        return await apv.restore_reaped_agent_pdp_view_rows(db=db, ignore_backoff=True, limit=1)

    assert run(body)["restored"] == 3


def test_the_heal_gives_up_on_a_busy_old_table_then_converges_not_null_default_zero(run, db_url, caplog) -> None:
    # Review h2: on an earlier build's table with a writer mid-transaction, the heal waits at
    # most its lock_timeout, adds nothing, and converges on the next call with attempts NOT NULL
    # DEFAULT 0 on the old rows (a nullable attempts would leave the backoff NULL forever).
    import time

    import asyncpg

    import db.schema_guard as guard

    async def body(db):
        await db.execute(f"ALTER TABLE {_AUDIT} RENAME TO {_AUDIT}_cur")
        try:
            await db.execute(
                f"CREATE TABLE {_AUDIT} (audit_id BIGSERIAL PRIMARY KEY, content_key TEXT NOT NULL, "
                "pivota_signature_id TEXT, reason TEXT NOT NULL, run_id TEXT NOT NULL, refresh_source TEXT, "
                "funnel_link_count INTEGER NOT NULL DEFAULT 0, funnel_link_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "row_snapshot JSONB NOT NULL, deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), restored_at TIMESTAMPTZ)")
            await db.execute(f"INSERT INTO {_AUDIT} (content_key, reason, run_id, row_snapshot) "
                             "VALUES ('ck_h2', 'r', 'x', '{}'::jsonb)")
            other = await asyncpg.connect(db_url)
            try:
                tr = other.transaction()
                await tr.start()
                await other.execute(f"UPDATE {_AUDIT} SET reason = 'r2'")
                t0 = time.monotonic()
                with caplog.at_level("WARNING"):
                    await asyncio.wait_for(guard._heal_unbuildable_reap_schema(), 10)
                dt = time.monotonic() - t0
                cols_busy = await db.fetch_val(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name = :t "
                    "AND column_name IN ('attempts', 'next_attempt_at')", {"t": _AUDIT})
                await tr.commit()
            finally:
                await other.close()
            await guard._heal_unbuildable_reap_schema()
            cols = [dict(r) for r in await db.fetch_all(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns WHERE table_name = :t "
                "AND column_name IN ('attempts', 'next_attempt_at') ORDER BY 1", {"t": _AUDIT})]
            att = await db.fetch_val(f"SELECT attempts FROM {_AUDIT}")
            return dt, cols_busy, cols, att
        finally:
            await db.execute(f"DROP TABLE IF EXISTS {_AUDIT}")
            await db.execute(f"ALTER TABLE {_AUDIT}_cur RENAME TO {_AUDIT}")

    dt, cols_busy, cols, att = run(body)
    assert dt < 5.0, dt
    assert cols_busy == 0
    assert [c["column_name"] for c in cols] == ["attempts", "next_attempt_at"]
    assert cols[0]["is_nullable"] == "NO" and cols[0]["column_default"] == "0" and att == 0
    assert any("column heal deferred" in r.getMessage() for r in caplog.records)
