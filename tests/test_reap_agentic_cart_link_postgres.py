"""The Reap agentic CART-LINK lane (mig 226) against REAL Postgres.

Picked up by .github/workflows/postgres-dialect-gate.yml via the `tests/test_*_postgres.py` glob.

TWO HALVES:

  1. EVERY case in tests/reap_cart_link_cases.py, collected again here, so the ledger pairing,
     the validator's whole table through `create_purchase`, the raw-writer CHECKs, the dials and
     the state machine end to end all run on the production engine. On this engine the fixture
     builds the schema from the MIGRATIONS (224 → 225 → 226 → 228).

  2. What only Postgres can show, below: the mig-226 self-heal builds the SAME CATALOG as the
     migration (columns, and every CHECK's `pg_get_constraintdef` BY NAME); healing a 225-shaped
     database reaches that catalog; a second heal adds nothing and duplicates no constraint; the
     down migration refuses while a cart_link row is in flight and reverses cleanly once drained;
     and the new INSERT statement PREPAREs.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_cartlink226_test \\
        .venv/bin/python -m pytest tests/test_reap_agentic_cart_link_postgres.py
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from reap_cart_link_cases import *  # noqa: E402,F401,F403
from reap_cart_link_cases import (  # noqa: E402
    CART_URL,
    MIGRATIONS,
    MIGRATIONS_DIR,
    apply_migrations,
    drop_tables,
    mk_cart_row,
    to_pre_226_shape,
)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

# Defined AFTER the star import, so this module's skip — not anything the cases module might
# ever export — is the one that applies.
pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — this is the production-dialect gate",
)

_MIG_226 = MIGRATIONS_DIR / "226_reap_agentic_purchase_item_source.sql"
_MIG_228 = MIGRATIONS_DIR / "228_conversion_click_claims.sql"
assert MIGRATIONS[-2:] == (_MIG_226, _MIG_228)

_NEW_CONSTRAINTS = (
    "ck_reap_agentic_purchases_item_source",
    "ck_reap_agentic_purchases_cart_url_pairing",
)


async def _catalog():
    """Columns (name, type, nullability, default) and CHECKs (name → definition) of the
    purchases table, as the DATABASE built them."""
    from db.database import database

    columns = await database.fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'reap_agentic_purchases'
         ORDER BY column_name
        """
    )
    checks = await database.fetch_all(
        """
        SELECT c.conname, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE c.contype = 'c' AND t.relname = 'reap_agentic_purchases'
         ORDER BY c.conname
        """
    )
    return (
        [tuple(dict(r).values()) for r in columns],
        [(r["conname"], " ".join(r["def"].split())) for r in checks],
    )


async def test_the_self_heal_builds_the_226_catalog_the_migration_builds():
    from db.schema_guard import ensure_required_schema_light

    columns_m, checks_m = await _catalog()
    named_m = dict(checks_m)
    for name in _NEW_CONSTRAINTS:
        assert name in named_m, f"precondition: the migration built {name}"

    await drop_tables()
    await ensure_required_schema_light()
    columns_s, checks_s = await _catalog()

    assert columns_s == columns_m
    # BY NAME AND DEFINITION. The whole-table parity test in the ledger suite compares CHECK
    # definitions without names; this one pins the two new ones by name as well.
    assert checks_s == checks_m, f"\n migration: {checks_m}\n self-heal: {checks_s}"


async def test_healing_a_225_shaped_database_reaches_the_migrations_catalog():
    from db.schema_guard import ensure_required_schema_light

    from_migration = await _catalog()
    await to_pre_226_shape()
    names = {n for n, _ in (await _catalog())[1]}
    assert not (set(_NEW_CONSTRAINTS) & names), "precondition: the 225 shape"

    await ensure_required_schema_light()
    assert await _catalog() == from_migration


async def test_a_second_heal_changes_nothing_and_duplicates_no_constraint():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await ensure_required_schema_light()
    once = await _catalog()
    await ensure_required_schema_light()
    await ensure_required_schema_light()
    assert await _catalog() == once
    rows = await database.fetch_all(
        "SELECT conname FROM pg_constraint WHERE conname = ANY(:n)",
        {"n": list(_NEW_CONSTRAINTS)},
    )
    assert sorted(r["conname"] for r in rows) == sorted(_NEW_CONSTRAINTS)


async def test_the_migration_is_idempotent_on_its_own():
    before = await _catalog()
    await apply_migrations((_MIG_226,))
    assert await _catalog() == before


async def test_the_pairing_check_raises_a_check_violation_naming_itself():
    import asyncpg
    from db.database import database

    with pytest.raises(asyncpg.exceptions.CheckViolationError) as caught:
        await database.execute(
            "INSERT INTO reap_agentic_purchases (id, buyer_ref, state, item_source) "
            "VALUES ('rp_x', 'b', 'resolving', 'cart_link')"
        )
    assert caught.value.constraint_name == "ck_reap_agentic_purchases_cart_url_pairing"


async def _run_down():
    from db.sql_migrations import split_statements
    from db.database import database

    down = MIGRATIONS_DIR / "down/226_reap_agentic_purchase_item_source_down.sql"
    for statement in split_statements(down.read_text(encoding="utf-8")):
        await database.execute(statement)


async def test_the_down_migration_refuses_while_a_cart_link_row_is_in_flight():
    import asyncpg

    row = await mk_cart_row()
    assert row["state"] == "resolving"
    with pytest.raises(asyncpg.exceptions.RaiseError, match="in-flight cart_link"):
        await _run_down()
    names = {n for n, _ in (await _catalog())[1]}
    assert set(_NEW_CONSTRAINTS) <= names, "a refused down must leave the columns in place"


async def test_the_down_migration_reverses_cleanly_once_drained_and_226_reapplies():
    import db.reap_agentic_ledger as ledger

    row = await mk_cart_row()
    await ledger.transition(row["id"], from_states=["resolving"], to_state="refused")
    await _run_down()
    columns = {c[0] for c in (await _catalog())[0]}
    assert not ({"item_source", "cart_url"} & columns)
    # And forward again: the up migration is what an operator re-runs after a rollback.
    await apply_migrations((_MIG_226,))
    reread = await ledger.get_purchase_internal(row["id"])
    # The drained row comes back as the column DEFAULT says — its URL is gone with the column.
    assert reread["item_source"] == "reap_variant" and reread["cart_url"] is None


async def test_the_down_migration_is_a_no_op_on_a_pre_226_database():
    await to_pre_226_shape()
    await _run_down()


async def test_the_new_insert_statements_prepare_on_postgres():
    import asyncpg
    import db.reap_agentic_ledger as ledger

    order = []

    def _bind(match):
        name = match.group(1)
        if name not in order:
            order.append(name)
        return f"${order.index(name) + 1}"

    positional = re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", _bind, ledger._INSERT_CART_LINK_PURCHASE_SQL)
    conn = await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.prepare(positional)
    finally:
        await conn.close()
    assert {"item_source", "cart_url"} <= set(order)


async def test_the_stored_url_is_text_and_round_trips_verbatim():
    from db.database import database

    row = await mk_cart_row()
    raw = await database.fetch_one(
        "SELECT cart_url, pg_typeof(cart_url)::text AS t FROM reap_agentic_purchases WHERE id = :i",
        {"i": row["id"]},
    )
    assert raw["t"] == "text" and raw["cart_url"] == CART_URL


# ── migration 228: the per-click claim, on the production engine ─────────────────────────────


async def _claims_catalog():
    """The claims table's columns, CHECKs, primary key and indexes, plus the purchases click_id
    index, as the DATABASE built them."""
    from db.database import database

    columns = await database.fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'conversion_click_claims'
         ORDER BY column_name
        """
    )
    constraints = await database.fetch_all(
        """
        SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = 'conversion_click_claims'
         ORDER BY c.conname
        """
    )
    indexes = await database.fetch_all(
        """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public'
           AND (tablename = 'conversion_click_claims'
                OR indexname = 'idx_reap_agentic_purchases_click_id')
         ORDER BY indexname
        """
    )
    return (
        [tuple(dict(r).values()) for r in columns],
        [(r["conname"], r["contype"], " ".join(r["def"].split())) for r in constraints],
        [(r["indexname"], " ".join(r["indexdef"].split())) for r in indexes],
    )


async def test_the_self_heal_builds_the_228_catalog_the_migration_builds():
    from db.schema_guard import ensure_required_schema_light

    from_migration = await _claims_catalog()
    columns, constraints, indexes = from_migration
    assert [c[0] for c in columns] == ["claimed_at", "claimed_by", "click_id", "external_order_id"]
    assert {c[0] for c in constraints} == {
        "conversion_click_claims_pkey", "ck_conversion_click_claims_claimed_by"
    }
    assert "idx_reap_agentic_purchases_click_id" in {i[0] for i in indexes}

    await drop_tables()
    await ensure_required_schema_light()
    assert await _claims_catalog() == from_migration


async def test_the_228_heal_is_idempotent_and_the_migration_reapplies():
    from db.schema_guard import ensure_required_schema_light

    before = await _claims_catalog()
    await ensure_required_schema_light()
    await apply_migrations((_MIG_228,))
    assert await _claims_catalog() == before


async def test_the_scope_lookup_uses_the_click_id_index():
    from db.database import database

    await database.execute("SET enable_seqscan = off")
    try:
        rows = await database.fetch_all(
            "EXPLAIN SELECT 1 FROM reap_agentic_purchases "
            "WHERE click_id = 'clk_x' AND item_source = 'cart_link' LIMIT 1"
        )
    finally:
        await database.execute("SET enable_seqscan = on")
    plan = " ".join(str(dict(r).get("QUERY PLAN", "")) for r in rows)
    assert "idx_reap_agentic_purchases_click_id" in plan, plan


async def test_two_connections_claiming_at_once_produce_exactly_one_winner():
    """THE GUARD, ACROSS BACKENDS. Two pods — a webhook worker and a Reap poller — claim the same
    click on their own asyncpg connections at the same moment. The PRIMARY KEY lets exactly one
    INSERT return a row. Repeated, with the claimants swapped, so neither side wins by order."""
    import asyncio

    import asyncpg

    import services.conversion_click_claims as ccc

    url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    sql = re.sub(r":(click_id|claimed_by|external_order_id)", lambda m: {
        "click_id": "$1", "claimed_by": "$2", "external_order_id": "$3"}[m.group(1)],
        ccc._CLAIM_CLICK_SQL)
    for round_no in range(10):
        conns = [await asyncpg.connect(url) for _ in range(4)]
        try:
            claimants = ["reap_agentic", "merchant_order"] * 2
            if round_no % 2:
                claimants.reverse()
            click = f"clk_race_{round_no}"
            results = await asyncio.gather(*[
                conn.fetch(sql, click, who, f"ord_{i}")
                for i, (conn, who) in enumerate(zip(conns, claimants))
            ])
        finally:
            for conn in conns:
                await conn.close()
        assert sum(len(r) for r in results) == 1, (round_no, results)


async def _run_down_228():
    from db.database import database
    from db.sql_migrations import split_statements

    down = MIGRATIONS_DIR / "down/228_conversion_click_claims_down.sql"
    for statement in split_statements(down.read_text(encoding="utf-8")):
        await database.execute(statement)


async def test_the_228_down_migration_reverses_cleanly_and_228_reapplies():
    before = await _claims_catalog()
    await _run_down_228()
    columns, constraints, indexes = await _claims_catalog()
    assert columns == [] and constraints == [] and indexes == []
    await apply_migrations((_MIG_228,))
    assert await _claims_catalog() == before
