"""The Reap agentic CART-LINK lane (mig 229) against REAL Postgres.

Picked up by .github/workflows/postgres-dialect-gate.yml via the `tests/test_*_postgres.py` glob.

TWO HALVES:

  1. EVERY case in tests/reap_cart_link_cases.py, collected again here, so the ledger pairing,
     the validator's whole table through `create_purchase`, the raw-writer CHECKs, the dials and
     the state machine end to end all run on the production engine. On this engine the fixture
     builds the schema from the MIGRATIONS (224 → 225 → 229 → 230).

  2. What only Postgres can show, below: the mig-229 self-heal builds the SAME CATALOG as the
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
    to_pre_229_shape,
)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

# Defined AFTER the star import, so this module's skip — not anything the cases module might
# ever export — is the one that applies.
pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — this is the production-dialect gate",
)

_MIG_229 = MIGRATIONS_DIR / "229_reap_agentic_purchase_item_source.sql"
_MIG_230 = MIGRATIONS_DIR / "230_conversion_click_claims.sql"
assert MIGRATIONS[-2:] == (_MIG_229, _MIG_230)

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


async def test_the_self_heal_builds_the_229_catalog_the_migration_builds():
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
    await to_pre_229_shape()
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
    await apply_migrations((_MIG_229,))
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

    down = MIGRATIONS_DIR / "down/229_reap_agentic_purchase_item_source_down.sql"
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


async def test_the_down_migration_reverses_cleanly_once_drained_and_229_reapplies():
    import db.reap_agentic_ledger as ledger

    row = await mk_cart_row()
    await ledger.transition(row["id"], from_states=["resolving"], to_state="refused")
    await _run_down()
    columns = {c[0] for c in (await _catalog())[0]}
    assert not ({"item_source", "cart_url"} & columns)
    # And forward again: the up migration is what an operator re-runs after a rollback.
    await apply_migrations((_MIG_229,))
    reread = await ledger.get_purchase_internal(row["id"])
    # The drained row comes back as the column DEFAULT says — its URL is gone with the column.
    assert reread["item_source"] == "reap_variant" and reread["cart_url"] is None


async def test_the_down_migration_is_a_no_op_on_a_pre_229_database():
    await to_pre_229_shape()
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


# ── migration 230: the per-click claim, on the production engine ─────────────────────────────


async def _claims_catalog():
    """The claims table's columns, CHECKs, primary key and indexes, as the DATABASE built them."""
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
         WHERE schemaname = 'public' AND tablename = 'conversion_click_claims'
         ORDER BY indexname
        """
    )
    return (
        [tuple(dict(r).values()) for r in columns],
        [(r["conname"], r["contype"], " ".join(r["def"].split())) for r in constraints],
        [(r["indexname"], " ".join(r["indexdef"].split())) for r in indexes],
    )


async def test_the_self_heal_builds_the_230_catalog_the_migration_builds():
    from db.schema_guard import ensure_required_schema_light

    from_migration = await _claims_catalog()
    columns, constraints, indexes = from_migration
    assert [c[0] for c in columns] == ["claimed_at", "claimed_by", "click_id", "external_order_id"]
    assert {c[0] for c in constraints} == {
        "conversion_click_claims_pkey", "ck_conversion_click_claims_claimed_by"
    }
    assert {i[0] for i in indexes} == {"conversion_click_claims_pkey"}

    await drop_tables()
    await ensure_required_schema_light()
    assert await _claims_catalog() == from_migration


async def test_the_230_heal_is_idempotent_and_the_migration_reapplies():
    from db.schema_guard import ensure_required_schema_light

    before = await _claims_catalog()
    await ensure_required_schema_light()
    await apply_migrations((_MIG_230,))
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
    assert "uq_reap_agentic_purchases_cart_link_click" in plan, plan


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


async def _run_down_230():
    from db.database import database
    from db.sql_migrations import split_statements

    down = MIGRATIONS_DIR / "down/230_conversion_click_claims_down.sql"
    for statement in split_statements(down.read_text(encoding="utf-8")):
        await database.execute(statement)


async def test_the_230_down_migration_reverses_cleanly_and_230_reapplies():
    before = await _claims_catalog()
    await _run_down_230()
    columns, constraints, indexes = await _claims_catalog()
    assert columns == [] and constraints == [] and indexes == []
    await apply_migrations((_MIG_230,))
    assert await _claims_catalog() == before


# ── #2214b: one click, one cart-link purchase, and at most ONE edge, across connections ───────


def _positional(sql: str):
    order = []

    def _bind(match):
        name = match.group(1)
        if name not in order:
            order.append(name)
        return f"${order.index(name) + 1}"

    return re.sub(r":([a-zA-Z_][a-zA-Z0-9_]*)", _bind, sql), order


async def test_two_connections_creating_cart_link_purchases_on_one_click_leave_one_row():
    """THE CREATE RACE. Two pods run the ledger's own cart-link INSERT for the same click on
    their own connections at once. `uq_reap_agentic_purchases_cart_link_click` lets exactly one
    commit; the other gets a UniqueViolation, which `start_purchase` maps to
    `cart_link_click_in_use`."""
    import asyncio

    import asyncpg

    import db.reap_agentic_ledger as ledger
    from db.database import database

    sql, order = _positional(ledger._INSERT_CART_LINK_PURCHASE_SQL)
    url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    def _params(purchase_id):
        values = {
            "id": purchase_id, "buyer_ref": f"b_{purchase_id}", "agent_id": "a",
            "agent_user_ref_hash": "h", "enrollment_id": None, "state": "resolving",
            "merchant_domain": "judydoll.com", "product_key": None, "variant_key": None,
            "product_name": None, "variant_title": None, "brand": None, "category": None,
            "quantity": 1, "currency": "USD", "our_price_minor": 2820,
            "click_id": "clk_race_create", "return_url": None, "queries_tried": None,
            "next_poll_at": None, "shipping_address": None, "buyer_email": None,
            "accept_variant_labels": None, "also_accept_domains": None, "market_country": "US",
            "item_source": "cart_link", "cart_url": CART_URL,
        }
        return [values[name] for name in order]

    conns = [await asyncpg.connect(url) for _ in range(2)]
    try:
        results = await asyncio.gather(
            *[conn.fetch(sql, *_params(f"rp_race_{i}")) for i, conn in enumerate(conns)],
            return_exceptions=True,
        )
    finally:
        for conn in conns:
            await conn.close()
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], asyncpg.exceptions.UniqueViolationError)
    assert ledger._is_unique_violation(errors[0])
    row = await database.fetch_one(
        "SELECT COUNT(*) AS n FROM reap_agentic_purchases WHERE click_id = 'clk_race_create'"
    )
    assert row["n"] == 1


async def test_the_cart_link_click_index_is_unique_and_partial_in_both_builds():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    async def _indexdef():
        row = await database.fetch_one(
            "SELECT indexdef FROM pg_indexes WHERE indexname = "
            "'uq_reap_agentic_purchases_cart_link_click'"
        )
        return " ".join(row["indexdef"].split()) if row else None

    from_migration = await _indexdef()
    assert from_migration and "CREATE UNIQUE INDEX" in from_migration
    assert "WHERE (item_source = 'cart_link'::text)" in from_migration
    await drop_tables()
    await ensure_required_schema_light()
    assert await _indexdef() == from_migration


async def _claim_on_its_own_connection(click_id, claimed_by, order_id):
    """A claim taken by ANOTHER POD: the module's own statement, on its own asyncpg connection,
    committed before the service under test runs."""
    import asyncpg

    import services.conversion_click_claims as ccc

    sql, order = _positional(ccc._CLAIM_CLICK_SQL)
    values = {"click_id": click_id, "claimed_by": claimed_by, "external_order_id": order_id}
    conn = await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return await conn.fetch(sql, *[values[name] for name in order])
    finally:
        await conn.close()


async def test_a_webhook_pod_claims_first_then_reap_completes_one_edge_on_two_connections(
    reap, attribution
):
    """Webhook first, on ANOTHER connection: Reap's completion is skipped and says so."""
    from reap_cart_link_cases import CLICK, completed_via_reap, get

    rows = await _claim_on_its_own_connection(CLICK, "merchant_order", "5550001")
    assert len(rows) == 1
    purchase_id = await completed_via_reap(reap)
    assert (await get(purchase_id))["last_error_code"] == "attribution_closed_by_other_channel"
    assert attribution.edges == {}  # the webhook pod wrote its edge elsewhere; Reap wrote none


async def test_reap_claims_first_then_a_webhook_pod_cannot_claim_on_its_own_connection(
    reap, attribution
):
    """Reap first: a merchant claim from another connection gets no row back."""
    from reap_cart_link_cases import CLICK, REAP_ORDER_ID, SHOP, completed_via_reap

    await completed_via_reap(reap)
    assert list(attribution.edges) == [(SHOP, REAP_ORDER_ID)]
    assert await _claim_on_its_own_connection(CLICK, "merchant_order", "5550001") == []


async def test_a_reap_claim_that_crashed_blocks_another_pods_merchant_claim(reap, attribution):
    """The crash-after-claim case across connections: the Reap claim persists (committed; no
    transaction to roll back), so another pod's merchant claim is refused. At most one edge —
    here, zero, which list_claims_without_edge reports."""
    import services.conversion_click_claims as ccc
    from reap_cart_link_cases import CLICK, REAP_ORDER_ID

    assert await ccc.claim_click(CLICK, claimed_by="reap_agentic", external_order_id=REAP_ORDER_ID)
    # ... and the Reap process dies here, before any edge.
    assert await _claim_on_its_own_connection(CLICK, "merchant_order", "5550001") == []
    assert attribution.edges == {}
