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
    `database` rebound to it: the build's overlay reads use the global one."""
    import databases

    import db.database as dbmod

    def _run(coro_factory):
        async def go():
            database = databases.Database(db_url.replace("postgresql://", "postgresql+asyncpg://"))
            await database.connect()
            for mod in list(sys.modules.values()):
                if getattr(mod, "database", None) is dbmod.database:
                    monkeypatch.setattr(mod, "database", database)
            try:
                for table in ("agent_pdp_view", "catalog_products", "catalog_offers",
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
                'Public Goods', :domain, 'live',
                CASE WHEN :suppressed THEN NOW() ELSE NULL END, NOW(), NOW())
        """,
        {"pk": pk, "merchant_id": f"m_{pk}", "source_product_id": pk, "ck": ck, "title": title, "description": description, "domain": domain,
         "suppressed": suppressed},
    )


async def _stale_view(db, ck, *, stage="published"):
    from services.agent_pdp_view_assembler import UPSERT_SQL, row_to_upsert_params

    row = {
        "content_key": ck, "pivota_signature_id": None, "product_group_id": None,
        "brand": "Public Goods", "title": "Double Bottle Holder", "description": _STALE,
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
    # The Public Goods shape: one catalog row, on a quarantined storefront, and
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
    assert kept_after_dry is not None
    assert applied["unbuildable"] == 1 and applied["deleted"] == 1
    assert again["unbuildable"] == 0
    assert remaining == ["ck_s_mix", "ck_s_ok", "ck_s_orphan"]
