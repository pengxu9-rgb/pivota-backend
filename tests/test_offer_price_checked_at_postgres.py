"""`catalog_offers.price_checked_at` (migration 246) on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_offer_price_checked_at_postgres.py

The gateway publishes this column as the served price's `price_as_of` (PIVOTA-Agent #2322), so it
must date exactly the price on the row:
  * the trigger forgets it when any writer moves the price or currency without a read;
  * the mirror upsert stamps it only when its caller vouches for a read;
  * schema_guard installs the column and the SAME trigger on a boot where the migration never ran.
The attached lane's stamp is pinned in test_external_offer_attached_listing_postgres.py.

ISOLATION. The dialect gate runs every file against ONE database, and a trigger on the shared
catalog_offers would change every other file's writes. So this file never touches `public`: a
per-process scratch schema, the real migrations applied through a connection whose search_path is
that schema ALONE, dropped at teardown.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_BASE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "202_external_seed_content_freshness.sql",
    "058_catalog_core.sql",
    "132_catalog_offer_suppression_writer_audit.sql",
    "133_catalog_source_domain.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    "149_catalog_offers_agent_decision_fields.sql",
)
_PRICE_CHECK_MIGRATION = "246_catalog_offers_price_checked_at.sql"
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"offer_price_checked_at_test_{os.getpid()}"

PK = "prod::external_seed::merch_obs_price_check::ext_price_check"
READ_AT = datetime(2026, 9, 1, 5, 15, tzinfo=timezone.utc)


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


async def _apply(db, name: str) -> None:
    from db.sql_migrations import split_statements

    for statement in split_statements((_MIGRATIONS / name).read_text()):
        if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
            continue
        await db.execute(statement)


async def _scratch(with_price_check: bool):
    import databases

    _assert_throwaway_database()
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    for name in _BASE_MIGRATIONS:
        await _apply(scoped, name)
    if with_price_check:
        await _apply(scoped, _PRICE_CHECK_MIGRATION)
    return admin, scoped


async def _teardown(admin, scoped) -> None:
    await scoped.disconnect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.disconnect()


@pytest.fixture()
async def scoped_db():
    admin, scoped = await _scratch(with_price_check=True)
    try:
        yield scoped
    finally:
        await _teardown(admin, scoped)


@pytest.fixture()
async def unmigrated_db():
    """catalog_offers as prod has it today: no price_checked_at, no trigger."""
    admin, scoped = await _scratch(with_price_check=False)
    try:
        yield scoped
    finally:
        await _teardown(admin, scoped)


async def _offer(db, *, checked_at=READ_AT):
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, availability,"
        " currency, list_price, merchant_effective_price, price_checked_at)"
        " VALUES ('of_1', 'sku_1', :pk, 'merch_obs_price_check', 'in_stock', 'SGD', 28.20, 28.20,"
        "         :checked)",
        {"pk": PK, "checked": checked_at},
    )


async def _checked(db, offer_id="of_1"):
    row = await db.fetch_one(
        "SELECT price_checked_at, price_checked_at = :read AS original"
        " FROM catalog_offers WHERE offer_id = :oid",
        {"oid": offer_id, "read": READ_AT},
    )
    return dict(row)


# ── the trigger ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "assignment",
    [
        "list_price = 30.00",
        "merchant_effective_price = 30.00",
        "currency = 'USD'",
        "list_price = NULL, merchant_effective_price = NULL",
    ],
)
async def test_a_price_moved_without_a_read_forgets_its_stamp(scoped_db, assignment):
    # The currency backfills, the repair scripts, an ingest re-upsert: none read the page.
    await _offer(scoped_db)
    await scoped_db.execute(f"UPDATE catalog_offers SET {assignment}, updated_at = NOW() WHERE offer_id = 'of_1'")
    assert (await _checked(scoped_db))["price_checked_at"] is None


@pytest.mark.parametrize(
    "assignment",
    [
        # suppression-style and vocabulary-style writes: updated_at moves, the price does not
        "availability = 'out_of_stock', updated_at = NOW()",
        "readiness_tier = 'referral_only', updated_at = NOW()",
        # rewriting the SAME price (a no-op re-upsert) still dates it
        "list_price = 28.20, merchant_effective_price = 28.20, currency = 'SGD'",
    ],
)
async def test_a_write_that_leaves_the_price_alone_keeps_its_stamp(scoped_db, assignment):
    await _offer(scoped_db)
    await scoped_db.execute(f"UPDATE catalog_offers SET {assignment} WHERE offer_id = 'of_1'")
    assert (await _checked(scoped_db))["original"] is True


async def test_a_read_that_moves_the_price_keeps_its_own_new_stamp(scoped_db):
    await _offer(scoped_db)
    await scoped_db.execute(
        "UPDATE catalog_offers SET list_price = 30.00, merchant_effective_price = 30.00,"
        " price_checked_at = NOW() WHERE offer_id = 'of_1'"
    )
    row = await _checked(scoped_db)
    assert row["price_checked_at"] is not None and row["original"] is False


async def test_the_trigger_fires_on_the_upsert_path_too(scoped_db):
    # INSERT ... ON CONFLICT DO UPDATE is how the ingest writers move prices.
    await _offer(scoped_db)
    await scoped_db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency,"
        " list_price, merchant_effective_price) VALUES ('of_1', 'sku_1', :pk, 'm', 'SGD', 31, 31)"
        " ON CONFLICT (offer_id) DO UPDATE SET list_price = EXCLUDED.list_price,"
        " merchant_effective_price = EXCLUDED.merchant_effective_price",
        {"pk": PK},
    )
    assert (await _checked(scoped_db))["price_checked_at"] is None


# ── the mirror upsert ───────────────────────────────────────────────────────────────────────────


def _seed(price):
    return {
        "id": "seed_price_check", "external_product_id": "ext_price_check",
        "destination_url": "https://jsmbeauty.sg/products/serum-gloss",
        "domain": "jsmbeauty.sg", "price_amount": price, "price_currency": "SGD",
        "availability": "in_stock", "market": "SG",
    }


async def _mirror(monkeypatch, db, price, *, price_read):
    from services import external_offer_dual_write as mod

    monkeypatch.setattr(mod, "database", db)
    await mod.upsert_catalog_offer_from_seed_row(
        PK, _seed(price), merchant_id="merch_obs_price_check", price_read=price_read
    )
    row = await db.fetch_one(
        "SELECT list_price, price_checked_at FROM catalog_offers WHERE offer_id = :oid",
        {"oid": mod.derive_mirror_offer_id(PK)},
    )
    return dict(row)


async def test_the_mirror_upsert_stamps_only_a_vouched_read(scoped_db, monkeypatch):
    unread = await _mirror(monkeypatch, scoped_db, 28.2, price_read=False)
    assert unread["price_checked_at"] is None  # an unvouched INSERT claims nothing

    read = await _mirror(monkeypatch, scoped_db, 28.2, price_read=True)
    assert read["price_checked_at"] is not None

    # seed_data_writer's merge re-projects the SAME price: the read still dates it.
    same = await _mirror(monkeypatch, scoped_db, 28.2, price_read=False)
    assert same["price_checked_at"] == read["price_checked_at"]

    # ...and a DIFFERENT price nobody read: the stamp no longer describes it.
    moved = await _mirror(monkeypatch, scoped_db, 30.0, price_read=False)
    assert float(moved["list_price"]) == 30.0 and moved["price_checked_at"] is None

    reread = await _mirror(monkeypatch, scoped_db, 30.0, price_read=True)
    assert reread["price_checked_at"] is not None


# ── schema_guard on a boot where migration 246 never ran ────────────────────────────────────────


async def _trigger_oid(db):
    row = await db.fetch_one(
        "SELECT oid FROM pg_trigger WHERE tgrelid = to_regclass('catalog_offers')"
        " AND tgname = 'trg_catalog_offers_forget_unread_price_check' AND NOT tgisinternal"
    )
    return row["oid"] if row else None


async def test_schema_guard_heals_the_column_and_installs_the_migrations_trigger(unmigrated_db, monkeypatch):
    from db import schema_guard

    monkeypatch.setattr(schema_guard, "database", unmigrated_db)
    assert await _trigger_oid(unmigrated_db) is None

    # Column missing: the trigger heal waits for it rather than install a function that reads a
    # column that is not there.
    await schema_guard._heal_price_check_trigger()
    assert await _trigger_oid(unmigrated_db) is None

    await schema_guard._heal_add_columns(
        """
        ALTER TABLE IF EXISTS catalog_offers
          ADD COLUMN IF NOT EXISTS price_checked_at TIMESTAMPTZ NULL;
        """
    )
    await schema_guard._heal_price_check_trigger()
    installed = await _trigger_oid(unmigrated_db)
    assert installed is not None

    # A later boot changes nothing: the guard reads the catalog and skips the DDL.
    await schema_guard._heal_price_check_trigger()
    assert await _trigger_oid(unmigrated_db) == installed

    # And the healed trigger behaves as the migration's does.
    await _offer(unmigrated_db)
    await unmigrated_db.execute("UPDATE catalog_offers SET list_price = 30.00 WHERE offer_id = 'of_1'")
    assert (await _checked(unmigrated_db))["price_checked_at"] is None


def test_the_heal_runs_exactly_the_migrations_trigger_statements():
    from db import schema_guard

    ddl = schema_guard.price_check_trigger_ddl()
    migration = (_MIGRATIONS / _PRICE_CHECK_MIGRATION).read_text()
    for statement in filter(None, (s.strip() for s in ddl.split(";\n"))):
        head = statement.splitlines()[0]
        assert head in migration, head
    assert "CREATE OR REPLACE FUNCTION catalog_offers_forget_unread_price_check()" in ddl
    assert "DROP TRIGGER IF EXISTS trg_catalog_offers_forget_unread_price_check" in ddl
    assert "CREATE TRIGGER trg_catalog_offers_forget_unread_price_check" in ddl
    # The column add stays a plain column heal (the schema-guard-coverage gate reads it there).
    assert "ADD COLUMN" not in ddl


# ── the backfill, end to end ────────────────────────────────────────────────────────────────────


async def test_the_backfill_refuses_before_migration_246(unmigrated_db):
    from scripts.backfill_offer_price_checked_at import run_backfill

    summary = await run_backfill(unmigrated_db, apply=True)
    assert summary["refused"] == "migration_246_not_applied"


async def _backfill_fixture(db):
    import json

    dest = "https://jsmbeauty.sg/products/serum-gloss"
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url, domain,"
        " title, price_amount, price_currency, status, attached_product_key, seed_data, last_crawled_at)"
        " VALUES ('seed_bf', 'ext_bf', 'SG', :dest, 'jsmbeauty.sg', 'Serum Gloss', 28.20, 'SGD', 'active',"
        "         :pk, CAST(:sd AS jsonb), :read)",
        {"dest": dest, "pk": PK, "sd": json.dumps({"snapshot": {"price_amount": 28.2}}), "read": READ_AT},
    )
    for oid, sku, price in (("of_listing", f"{PK}::canonical", 28.20), ("of_variant", f"{PK}::v:1", 28.20)):
        await db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, currency, list_price,"
            " merchant_effective_price, source_ref, offer_payload, updated_at)"
            " VALUES (:oid, :sku, :pk, 'agent_seed::jsm', 'SGD', :p, :p, :dest, CAST(:payload AS jsonb),"
            "         TIMESTAMP '2026-09-28 06:07:00')",
            {"oid": oid, "sku": sku, "pk": PK, "p": price, "dest": dest,
             "payload": json.dumps({"destination_url": dest})},
        )


async def test_the_backfill_dates_only_the_read_price_and_is_fill_only(scoped_db):
    from scripts.backfill_offer_price_checked_at import run_backfill

    await _backfill_fixture(scoped_db)
    dry = await run_backfill(scoped_db, apply=False)
    assert (dry["seeds"], dry["planned"], dry["written"]) == (1, 1, 0)
    assert dry["skips"] == {"variant_row": 1}
    assert (await _checked(scoped_db, "of_listing"))["price_checked_at"] is None

    applied = await run_backfill(scoped_db, apply=True)
    assert applied["written"] == 1
    # dated with the READ, not with now and not with updated_at
    assert (await _checked(scoped_db, "of_listing"))["original"] is True
    assert (await _checked(scoped_db, "of_variant"))["price_checked_at"] is None
    updated = await scoped_db.fetch_one("SELECT updated_at FROM catalog_offers WHERE offer_id = 'of_listing'")
    assert str(updated["updated_at"]) == "2026-09-28 06:07:00"  # offer truth untouched

    again = await run_backfill(scoped_db, apply=True)
    assert again["written"] == 0 and again["skips"].get("already_stamped") == 1


async def test_the_stamp_refuses_a_row_whose_price_moved_after_the_plan(scoped_db):
    from scripts.backfill_offer_price_checked_at import STAMP_SQL

    await _backfill_fixture(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET merchant_effective_price = 30 WHERE offer_id = 'of_listing'")
    write = {"offer_id": "of_listing", "checked_at": READ_AT, "price": 28.2, "currency": "SGD"}
    assert await scoped_db.fetch_all(STAMP_SQL, write) == []
    assert (await _checked(scoped_db, "of_listing"))["price_checked_at"] is None


async def test_the_stamp_never_overwrites_a_later_read(scoped_db):
    # The plan skips stamped rows, but a refresh can stamp one between the plan and the UPDATE.
    from scripts.backfill_offer_price_checked_at import STAMP_SQL

    await _backfill_fixture(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET price_checked_at = NOW() WHERE offer_id = 'of_listing'")
    write = {"offer_id": "of_listing", "checked_at": READ_AT, "price": 28.2, "currency": "SGD"}
    assert await scoped_db.fetch_all(STAMP_SQL, write) == []
    assert (await _checked(scoped_db, "of_listing"))["original"] is False
