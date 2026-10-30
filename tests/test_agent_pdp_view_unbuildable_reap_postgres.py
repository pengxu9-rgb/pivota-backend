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
    content_key VARCHAR(40) NULL REFERENCES agent_pdp_view(content_key) ON DELETE SET NULL)"""
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
def run(db_url, monkeypatch):
    """Drive a coroutine against the private DB, with every module-level
    `database` rebound to it: the build's overlay reads use the global one.
    The reap flag is ON here; tests of the OFF default unset it."""
    import databases

    monkeypatch.setenv("AGENT_PDP_VIEW_UNBUILDABLE_REAP", "on")

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
                for table in ("agent_decision_funnel_links", _AUDIT, "agent_pdp_view",
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

    return _run


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
