"""db/agent_purchase_ledger.py + services/payment_orchestration/rails.py, on SQLite.

The Postgres arm (schema parity with migration 264, concurrent writers, jsonb on asyncpg) is
tests/test_agent_purchase_ledger_postgres.py. Tables are built the way production builds them:
through the schema-guard self-heal.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sys
from collections import Counter
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


def _transaction_calls(tree):
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transaction"
    ]


def test_the_module_opens_no_database_transactions():
    """It never opens one. The single `transaction()` is the heal's per-row SAVEPOINT, taken only when
    the caller already holds a transaction (so it nests: a SAVEPOINT, never a BEGIN)."""
    tree = ast.parse((_REPO / "db/agent_purchase_ledger.py").read_text("utf-8"))
    [heal_rows] = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_heal_rows"]
    [guard] = [n for n in ast.walk(heal_rows) if isinstance(n, ast.If) and _transaction_calls(n)]
    assert ast.unparse(guard.test) == "savepoint"
    assert _transaction_calls(ast.Module(body=guard.body, type_ignores=[])) and not _transaction_calls(
        ast.Module(body=guard.orelse, type_ignores=[]))
    [assign] = [n for n in heal_rows.body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "savepoint"]
    assert ast.unparse(assign.value) == "_in_transaction()"
    assert len(_transaction_calls(tree)) == 1


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


# ── healing, row by row ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def heal_clock(monkeypatch):
    """A clock the test moves, and an empty retry memory (it is per process)."""
    now = [1000.0]
    monkeypatch.setattr(purchases, "_clock", lambda: now[0])
    monkeypatch.setattr(purchases, "_heal_deferred_until", {})
    return now


async def _reaps_in_order(n: int) -> list:
    """`n` Reap purchases, oldest first (created a day apart, so the heal order is fixed)."""
    ids = []
    for days in range(n, 0, -1):
        rp = await _reap()
        await database.execute(
            "UPDATE reap_agentic_purchases SET created_at = datetime('now', '-' || :d || ' days') WHERE id = :i",
            {"d": days, "i": rp},
        )
        ids.append(rp)
    return ids


def _failing_for(monkeypatch, bad, tried=None):
    real = purchases.ensure_reap_parent

    async def flaky(rail_purchase_id, **kw):
        if tried is not None:
            tried.append(rail_purchase_id)
        if rail_purchase_id in bad:
            raise RuntimeError("constraint violation")
        return await real(rail_purchase_id, **kw)

    monkeypatch.setattr(purchases, "ensure_reap_parent", flaky)
    return real


async def _heal(how: str, **kw) -> int:
    if how == "backfill":
        return await purchases.backfill_reap_parents(**kw)
    return await purchases.heal_reap_parents_for_owner(AGENT, OWNER, **kw)


@pytest.mark.parametrize("how", ["backfill", "owner"])
async def test_a_reap_row_whose_parent_cannot_be_written_does_not_stop_the_rest(how, heal_clock, monkeypatch, caplog):
    older, newer = await _reaps_in_order(2)
    bad, good = (older, newer) if how == "backfill" else (newer, older)  # the bad row is tried first
    real = _failing_for(monkeypatch, {bad})
    with caplog.at_level(logging.WARNING, logger=purchases.__name__):
        assert await _heal(how) == 1
    assert [r["rail_purchase_id"] for r in await _parent_rows()] == [good]
    [warning] = [r.getMessage() for r in caplog.records if "parent heal failed" in r.getMessage()]
    assert "reap parent heal failed" in warning and bad in warning and "RuntimeError" in warning
    # Left alone while it waits, even once it could be healed; tried again after the wait.
    monkeypatch.setattr(purchases, "ensure_reap_parent", real)
    assert await _heal(how) == 0
    heal_clock[0] += purchases.HEAL_RETRY_AFTER_SECONDS - 1
    assert await _heal(how) == 0
    heal_clock[0] += 1
    assert await _heal(how) == 1
    assert sorted(r["rail_purchase_id"] for r in await _parent_rows()) == sorted([bad, good])


@pytest.mark.parametrize("how", ["backfill", "owner"])
async def test_a_row_waiting_to_retry_does_not_use_up_the_limit(how, heal_clock, monkeypatch):
    older, newer = await _reaps_in_order(2)
    bad, good = (older, newer) if how == "backfill" else (newer, older)
    _failing_for(monkeypatch, {bad})
    assert await _heal(how, limit=1) == 0  # the bad row is first and takes the run
    tally = Counter()
    if how == "backfill":
        assert await purchases.backfill_reap_parents(limit=1, tally=tally) == 1
        assert tally == {"deferred": 1}
    else:
        assert await _heal(how, limit=1) == 1
    assert [r["rail_purchase_id"] for r in await _parent_rows()] == [good]


async def test_failures_in_a_row_end_the_run_and_the_next_run_starts_after_them(heal_clock, monkeypatch, caplog):
    stop = purchases.HEAL_STOP_AFTER_FAILURES_IN_A_ROW
    ids = await _reaps_in_order(stop + 2)
    tried = []
    _failing_for(monkeypatch, set(ids), tried)
    tally = Counter()
    with caplog.at_level(logging.WARNING, logger=purchases.__name__):
        assert await purchases.backfill_reap_parents(tally=tally) == 0
    assert tally == {"failed": stop, "stopped": 1}
    assert tried == ids[:stop]
    lines = [r.getMessage() for r in caplog.records if r.name == purchases.__name__]
    assert len(lines) == stop + 1 and f"stopped after {stop} failures in a row" in lines[-1]
    tried.clear()
    tally = Counter()
    assert await purchases.backfill_reap_parents(tally=tally) == 0
    assert tally == {"failed": 2, "deferred": stop}
    assert tried == ids[stop:]


async def test_a_success_resets_the_failures_in_a_row(heal_clock, monkeypatch):
    stop = purchases.HEAL_STOP_AFTER_FAILURES_IN_A_ROW
    ids = await _reaps_in_order(2 * stop)
    good = {ids[stop - 1], ids[-1]}  # stop-1 failures, a success, stop-1 failures, a success
    _failing_for(monkeypatch, set(ids) - good)
    tally = Counter()
    assert await purchases.backfill_reap_parents(tally=tally) == 2
    assert tally == {"failed": 2 * (stop - 1)}
    assert {r["rail_purchase_id"] for r in await _parent_rows()} == good


def test_the_retry_memory_is_bounded(heal_clock, monkeypatch):
    monkeypatch.setattr(purchases, "_HEAL_DEFERRED_MAX", 3)
    for n in range(5):
        purchases._defer("reap", f"rp_{n}")
    assert purchases._deferred_ids("reap") == {"rp_2", "rp_3", "rp_4"}
    heal_clock[0] += purchases.HEAL_RETRY_AFTER_SECONDS
    assert purchases._deferred_ids("reap") == frozenset() and purchases._heal_deferred_until == {}


async def test_the_transaction_check_reads_this_connections_transaction_stack():
    """`_in_transaction` reads a private of databases 0.7.0; this fails if an upgrade moves it."""
    assert purchases._in_transaction() is False
    async with database.transaction():
        assert purchases._in_transaction() is True
    assert purchases._in_transaction() is False


async def test_inside_a_callers_transaction_each_row_is_healed_in_a_savepoint(heal_clock, monkeypatch):
    """On Postgres this is what keeps a failed row from aborting the caller's transaction
    (tests/test_agent_purchase_events_postgres.py); here, that the nesting itself works."""
    bad, good = await _reaps_in_order(2)
    _failing_for(monkeypatch, {bad})
    async with database.transaction():
        assert await purchases.backfill_reap_parents() == 1
    assert [r["rail_purchase_id"] for r in await _parent_rows()] == [good]


async def test_the_backfill_script_prints_failures_and_stops_when_a_batch_parents_nothing(monkeypatch, capsys):
    import argparse

    import scripts.backfill_agent_purchases as script

    batches = iter([(2, {}), (0, {"failed": 1, "deferred": 2})])
    calls = []

    async def backfill(*, limit, tally):
        parented, outcome = next(batches)
        tally.update(outcome)
        calls.append(limit)
        return parented

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(purchases, "backfill_reap_parents", backfill)
    monkeypatch.setattr(purchases, "ensure_agent_purchase_schema", nothing)
    monkeypatch.setattr(database, "connect", nothing)
    monkeypatch.setattr(database, "disconnect", nothing)
    assert await script.run(argparse.Namespace(dry_run=False, batch=7, max_batches=10)) == 0
    assert calls == [7, 7]
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "batch=0 parented=2 total=2 failed=0 skipped_after_failure=0 stopped=0",
        "batch=1 parented=0 total=2 failed=1 skipped_after_failure=2 stopped=0",
    ]


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
