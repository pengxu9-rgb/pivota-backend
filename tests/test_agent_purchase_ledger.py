"""db/agent_purchase_ledger.py + services/payment_orchestration/rails.py, on SQLite.

The Postgres arm (schema parity with migration 264, concurrent writers, jsonb on asyncpg) is
tests/test_agent_purchase_ledger_postgres.py. Tables are built the way production builds them:
through the schema-guard self-heal.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import db.reap_agentic_ledger as reap_ledger  # noqa: E402
from services.payment_orchestration import rails  # noqa: E402

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason="the SQLite arm; the Postgres arm is tests/test_agent_purchase_ledger_postgres.py",
)

_REPO = Path(__file__).resolve().parents[1]

AGENT = "agent_one"
OWNER = "hash_alice"


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchases", "reap_agentic_purchases", "reap_agentic_enrollments"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


async def _reap(agent_id: str = AGENT, owner: str = OWNER, **over) -> str:
    kwargs = dict(
        buyer_ref="bref_alice",
        agent_id=agent_id,
        agent_user_ref_hash=owner,
        merchant_domain="brand.example",
        product_key="pk_1",
        quantity=1,
        currency="USD",
        our_price_minor=4250,
    )
    kwargs.update(over)
    return (await reap_ledger.create_purchase(**kwargs))["id"]


async def _parent_rows():
    return [dict(r) for r in await database.fetch_all("SELECT * FROM agent_purchases ORDER BY id")]


# ── schema and vocabulary ────────────────────────────────────────────────────────────────────


def _check_values(ddl: str, column: str) -> tuple:
    match = re.search(rf"CONSTRAINT ck_agent_purchases_{column} CHECK \({column} IN \(([^)]*)\)\)", ddl)
    assert match, f"no CHECK for {column}"
    return tuple(v.strip().strip("'") for v in match.group(1).split(","))


_RAIL_CHECK_264 = "CHECK (rail IN ('reap'))"


def _migrated_ddl() -> str:
    """Migration 264's DDL with migration 268's widened rail CHECK applied: what the migrations
    leave behind, to compare with the one CREATE TABLE the module holds."""
    m264 = (_REPO / "db/migrations/264_agent_purchases.sql").read_text("utf-8")
    m268 = (_REPO / "db/migrations/268_a2a_rail_ledger.sql").read_text("utf-8")
    widened = re.search(r"ADD CONSTRAINT ck_agent_purchases_rail (CHECK \(rail IN \([^)]*\)\))", m268)
    assert widened and "DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail" in m268
    assert m264.count(_RAIL_CHECK_264) == 1
    return m264.replace(_RAIL_CHECK_264, widened.group(1))


@pytest.mark.parametrize("source", ["module", "migration"])
def test_the_check_vocabularies_are_the_python_tuples(source):
    ddl = purchases._CREATE_TABLE_PG if source == "module" else _migrated_ddl()
    assert _check_values(ddl, "rail") == purchases.RAILS
    assert _check_values(ddl, "executor") == purchases.EXECUTORS


def test_the_migrations_create_the_table_and_indexes_the_module_creates():
    migration = _migrated_ddl()
    squash = lambda text: " ".join(text.split())  # noqa: E731
    for statement in (purchases._CREATE_TABLE_PG, purchases._CREATE_RAIL_INDEX, purchases._CREATE_OWNER_INDEX):
        assert squash(statement) in squash(migration)


def test_the_heal_widens_the_rail_check_to_the_module_vocabulary():
    assert purchases._WIDEN_RAIL_CHECK_PG.count("ADD CONSTRAINT ck_agent_purchases_rail") == 1
    # NOT VALID in the step that holds ACCESS EXCLUSIVE; the scan is the separate VALIDATE step
    assert purchases._WIDEN_RAIL_CHECK_PG.rstrip().endswith("NOT VALID;")
    assert "VALIDATE CONSTRAINT ck_agent_purchases_rail" in purchases._VALIDATE_RAIL_CHECK_PG
    assert "VALIDATE" not in purchases._WIDEN_RAIL_CHECK_PG
    assert _check_values(purchases._WIDEN_RAIL_CHECK_PG.replace("ADD CONSTRAINT", "CONSTRAINT"), "rail") == purchases.RAILS
    for rail in purchases.RAILS:
        assert f"'{rail}'::character varying" in purchases._RAIL_CHECK_PG_DEF


async def test_the_sqlite_heal_rebuilds_a_table_with_the_narrow_check_and_keeps_its_rows():
    await database.execute("DROP TABLE IF EXISTS agent_purchases")
    await database.execute(purchases._CREATE_TABLE_SQLITE.replace("('reap', 'a2a')", "('reap')"))
    await database.execute(
        "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) VALUES ('pp_x', 'reap', 'rail_managed', 'rp_x')"
    )
    await purchases.ensure_agent_purchase_schema()
    await purchases.ensure_agent_purchase_schema()  # idempotent
    await database.execute(
        "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) VALUES ('pp_y', 'a2a', 'rail_managed', 'ap_y')"
    )
    rows = {r["id"]: r["rail"] for r in await database.fetch_all("SELECT id, rail FROM agent_purchases")}
    assert rows == {"pp_x": "reap", "pp_y": "a2a"}
    indexes = {r["name"] for r in await database.fetch_all("PRAGMA index_list(agent_purchases)")}
    assert {"uq_agent_purchases_rail_purchase", "idx_agent_purchases_owner"} <= indexes


async def _narrow_table_with_a_row():
    await database.execute("DROP TABLE IF EXISTS agent_purchases")
    await database.execute("DROP TABLE IF EXISTS agent_purchases_pre268")
    await database.execute(purchases._CREATE_TABLE_SQLITE.replace("('reap', 'a2a')", "('reap')"))
    await database.execute(purchases._CREATE_RAIL_INDEX)
    await database.execute(
        "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) VALUES ('pp_x', 'reap', 'rail_managed', 'rp_x')"
    )


async def _assert_rebuilt():
    rows = {r["id"] for r in await database.fetch_all("SELECT id FROM agent_purchases")}
    assert rows == {"pp_x"}
    tables = {r["name"] for r in await database.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "agent_purchases_pre268" not in tables
    indexes = {r["name"] for r in await database.fetch_all("PRAGMA index_list(agent_purchases)")}
    assert {"uq_agent_purchases_rail_purchase", "idx_agent_purchases_owner"} <= indexes
    assert await purchases.ensure_reap_parent("rp_missing") is None  # ON CONFLICT target exists again


async def test_a_rebuild_interrupted_after_the_rename_is_finished_on_the_next_boot():
    await _narrow_table_with_a_row()
    await database.execute(purchases._SQLITE_RENAME_OLD)  # ...and the process died here
    await purchases.ensure_agent_purchase_schema()
    await _assert_rebuilt()


async def test_a_rebuild_interrupted_after_the_create_is_finished_on_the_next_boot():
    await _narrow_table_with_a_row()
    await database.execute(purchases._SQLITE_RENAME_OLD)
    await database.execute(purchases._CREATE_TABLE_SQLITE)  # ...and the process died here
    await purchases.ensure_agent_purchase_schema()
    await _assert_rebuilt()


async def test_a_rebuild_interrupted_after_the_copy_is_finished_on_the_next_boot():
    await _narrow_table_with_a_row()
    await database.execute(purchases._SQLITE_RENAME_OLD)
    await database.execute(purchases._CREATE_TABLE_SQLITE)
    await database.execute(purchases._SQLITE_COPY_OLD)  # ...and the process died here
    await purchases.ensure_agent_purchase_schema()
    await _assert_rebuilt()


async def test_the_self_heal_builds_the_table_on_sqlite():
    columns = {r["name"] for r in await database.fetch_all("PRAGMA table_info(agent_purchases)")}
    assert columns == {
        "id", "rail", "executor", "rail_purchase_id", "agent_id", "agent_user_ref_hash",
        "routing_plan", "created_at",
    }
    indexes = {r["name"] for r in await database.fetch_all("PRAGMA index_list(agent_purchases)")}
    assert {"uq_agent_purchases_rail_purchase", "idx_agent_purchases_owner"} <= indexes


async def test_the_table_has_no_state_column():
    """The design rule: status is always read from the rail's row, never copied here."""
    columns = {r["name"] for r in await database.fetch_all("PRAGMA table_info(agent_purchases)")}
    assert not {c for c in columns if "state" in c or "status" in c}


def test_every_reap_state_maps_to_exactly_one_unified_state():
    assert set(rails.REAP_STATE_MAP) == set(reap_ledger.PURCHASE_STATES)
    assert set(rails.REAP_STATE_MAP.values()) <= set(rails.UNIFIED_STATES)
    for state in reap_ledger.TERMINAL_STATES:
        assert rails.REAP_STATE_MAP[state] in rails.UNIFIED_TERMINAL_STATES
    for state in set(reap_ledger.PURCHASE_STATES) - set(reap_ledger.TERMINAL_STATES):
        assert rails.REAP_STATE_MAP[state] not in rails.UNIFIED_TERMINAL_STATES


def test_an_unknown_rail_state_maps_to_none_not_a_guess():
    assert rails.unified_state("reap", "teleported") is None
    assert rails.unified_state("other_rail", "completed") is None
    assert rails.unified_state("reap", None) is None


def test_the_executor_map_covers_every_rail_the_ledger_accepts():
    assert set(rails.RAIL_EXECUTOR) == set(purchases.RAILS)
    assert set(rails.RAIL_EXECUTOR.values()) <= set(purchases.EXECUTORS)


def test_the_module_opens_no_database_transactions():
    tree = ast.parse((_REPO / "db/agent_purchase_ledger.py").read_text("utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transaction"
    ]
    assert not calls


@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("ture", False),
    ("1", True), ("true", True), (" ON ", True), ("yes", True),
])
def test_the_dial_is_an_allowlist(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, value)
    assert purchases.is_enabled() is expected


# ── ensure_reap_parent ───────────────────────────────────────────────────────────────────────


async def test_a_parent_copies_identity_from_the_reap_row():
    rp = await _reap()
    pp = await purchases.ensure_reap_parent(rp)
    assert pp and pp.startswith("pp_")

    [row] = await _parent_rows()
    child = await reap_ledger.get_purchase_internal(rp)
    assert row["id"] == pp
    assert row["rail"] == "reap" and row["executor"] == "rail_managed"
    assert row["rail_purchase_id"] == rp
    assert row["agent_id"] == AGENT and row["agent_user_ref_hash"] == OWNER
    assert str(row["created_at"]) == str(child["created_at"].strftime("%Y-%m-%d %H:%M:%S"))
    assert json.loads(row["routing_plan"]) == {
        "rail": "reap", "executor": "rail_managed", "basis": "agent_selected_rail",
    }


async def test_a_second_call_returns_the_same_parent_and_writes_nothing():
    rp = await _reap()
    first = await purchases.ensure_reap_parent(rp)
    second = await purchases.ensure_reap_parent(rp, basis="backfill")
    assert first == second
    rows = await _parent_rows()
    assert len(rows) == 1
    assert json.loads(rows[0]["routing_plan"])["basis"] == "agent_selected_rail"


async def test_no_reap_row_means_no_parent():
    assert await purchases.ensure_reap_parent("rp_missing") is None
    assert await purchases.ensure_reap_parent("") is None
    assert await _parent_rows() == []


async def test_the_unique_index_refuses_a_second_parent_for_one_rail_purchase():
    rp = await _reap()
    await purchases.ensure_reap_parent(rp)
    with pytest.raises(Exception):
        await database.execute(
            "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) "
            "VALUES ('pp_dupe', 'reap', 'rail_managed', :rp)",
            {"rp": rp},
        )


@pytest.mark.parametrize("column,value", [("rail", "other_rail"), ("executor", "pivota_places")])
async def test_the_checks_refuse_an_unknown_rail_or_executor(column, value):
    values = {"rail": "reap", "executor": "rail_managed"}
    values[column] = value
    with pytest.raises(Exception):
        await database.execute(
            "INSERT INTO agent_purchases (id, rail, executor, rail_purchase_id) "
            "VALUES ('pp_x', :rail, :executor, 'rp_x')",
            values,
        )


# ── owner-scoped reads ───────────────────────────────────────────────────────────────────────


async def test_the_owner_reads_their_parent_by_either_id():
    rp = await _reap()
    pp = await purchases.ensure_reap_parent(rp)
    by_pp = await purchases.get_for_owner(pp, AGENT, OWNER)
    by_rp = await purchases.get_by_rail_id_for_owner("reap", rp, AGENT, OWNER)
    assert by_pp == by_rp
    assert by_pp["routing_plan"]["basis"] == "agent_selected_rail"
    assert by_pp["created_at"].tzinfo is not None


@pytest.mark.parametrize("agent_id,owner", [("agent_two", OWNER), (AGENT, "hash_bob")])
async def test_another_owner_reads_nothing(agent_id, owner):
    rp = await _reap()
    pp = await purchases.ensure_reap_parent(rp)
    assert await purchases.get_for_owner(pp, agent_id, owner) is None
    assert await purchases.get_by_rail_id_for_owner("reap", rp, agent_id, owner) is None
    assert await purchases.list_for_owner(agent_id, owner) == []


async def test_the_list_is_the_owners_newest_first():
    older = await _reap()
    await database.execute(
        "UPDATE reap_agentic_purchases SET created_at = datetime('now', '-1 day') WHERE id = :i",
        {"i": older},
    )
    newer = await _reap()
    other = await _reap(agent_id="agent_two")
    for rp in (older, newer, other):
        await purchases.ensure_reap_parent(rp)
    listed = await purchases.list_for_owner(AGENT, OWNER)
    assert [p["rail_purchase_id"] for p in listed] == [newer, older]
    assert len(await purchases.list_for_owner(AGENT, OWNER, limit=1)) == 1


# ── healing ──────────────────────────────────────────────────────────────────────────────────


async def test_the_backfill_parents_every_reap_purchase_once():
    ids = [await _reap() for _ in range(5)]
    await purchases.ensure_reap_parent(ids[0])
    assert await purchases.backfill_reap_parents(limit=2) == 2
    assert await purchases.backfill_reap_parents(limit=10) == 2
    assert await purchases.backfill_reap_parents(limit=10) == 0
    rows = await _parent_rows()
    assert sorted(r["rail_purchase_id"] for r in rows) == sorted(ids)
    bases = {r["rail_purchase_id"]: json.loads(r["routing_plan"])["basis"] for r in rows}
    assert bases[ids[0]] == "agent_selected_rail"
    assert {bases[i] for i in ids[1:]} == {"backfill"}


async def test_the_owner_heal_touches_only_that_owner():
    mine = await _reap()
    theirs = await _reap(agent_id="agent_two")
    assert await purchases.heal_reap_parents_for_owner(AGENT, OWNER) == 1
    assert [r["rail_purchase_id"] for r in await _parent_rows()] == [mine]
    assert await purchases.heal_reap_parents_for_owner(AGENT, OWNER) == 0
    assert theirs not in [r["rail_purchase_id"] for r in await _parent_rows()]


async def test_the_owner_heal_takes_the_newest_first_the_order_the_list_pages_in():
    ids = []
    for days in (3, 2, 1):
        rp = await _reap()
        await database.execute(
            f"UPDATE reap_agentic_purchases SET created_at = datetime('now', '-{days} day') WHERE id = :i",
            {"i": rp},
        )
        ids.append(rp)
    assert await purchases.heal_reap_parents_for_owner(AGENT, OWNER, limit=1) == 1
    assert [r["rail_purchase_id"] for r in await _parent_rows()] == [ids[-1]]


async def test_the_dry_run_count_matches_what_the_backfill_parents():
    for _ in range(3):
        await _reap()
    assert await purchases.count_missing_reap_parents() == 3
    assert await purchases.backfill_reap_parents() == 3
    assert await purchases.count_missing_reap_parents() == 0


async def test_the_backfill_never_writes_a_reap_row():
    rp = await _reap()
    before = dict(await database.fetch_one("SELECT * FROM reap_agentic_purchases WHERE id = :i", {"i": rp}))
    await purchases.backfill_reap_parents()
    after = dict(await database.fetch_one("SELECT * FROM reap_agentic_purchases WHERE id = :i", {"i": rp}))
    assert before == after


def test_the_prepare_gate_collects_the_postgres_statements_of_this_module():
    """Driver property 5 (db/reap_agentic_ledger.py): a statement the PREPARE gate cannot see ships
    unplanned. The write and the DDL were once chosen by a conditional expression, which hid both."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_prepare_gate", _REPO / "tests/test_repo_sql_prepare_postgres.py"
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    collected = [" ".join(str(sql).split()) for _where, sql in gate.collect_statements()]
    for statement in (
        purchases._INSERT_REAP_PARENT_SQL,
        purchases._CREATE_TABLE_PG,
        purchases._SELECT_FOR_OWNER_SQL,
        purchases._LIST_FOR_OWNER_SQL,
        purchases._COUNT_REAP_MISSING_PARENTS_SQL,
    ):
        assert " ".join(statement.split()) in collected, statement.split("\n")[1][:60]
