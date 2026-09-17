"""db/reap_agentic_ledger.py against REAL Postgres — the half SQLite structurally cannot test.

Picked up automatically by .github/workflows/postgres-dialect-gate.yml via the
`tests/test_*_postgres.py` glob.

FIVE THINGS HERE THAT THE SQLITE ARM CANNOT PROVE, each one a defect this repo has already
shipped once on some rail or other:

  1. A SECOND BACKEND CONNECTION. The `AND state IN (...)` conjunct is the concurrency guard for
     two POD PROCESSES, not two coroutines. On SQLite databases==0.7.0 serialises everything onto
     one connection, so the SQLite race test proves the guard under interleaving and nothing
     about two connections. Here the losing statement is issued on its own asyncpg connection.

  2. jsonb COMES BACK AS TEXT. No SQLAlchemy result processor runs on a raw statement, so
     asyncpg hands back the jsonb verbatim as a `str`. The test below asserts BOTH that the raw
     value is a str and that the module's decode turned it into a dict — the second assertion
     alone would pass on a driver that never had the problem.

  3. THE SERVER-SIDE CUTOFF, UNDER A NON-UTC CLIENT. asyncpg encodes a naive datetime for a
     timestamptz parameter as local wall time, local to THIS PROCESS — measured seven hours off
     from a Pacific box against a UTC database.

  4. THE SELF-HEAL BUILDS THE SAME SCHEMA AS THE MIGRATION, compared through the CATALOG rather
     than through the source text. `pg_indexes.indexdef` is what the database actually built, so
     a `CREATE UNIQUE INDEX` quietly downgraded to `CREATE INDEX` shows up; a token check on the
     DDL string cannot see it, and a reviewer's mutant proved that by surviving the whole suite.

  5. PREPARE. Every SQL constant in the module is planned against the real schema. SQLite cannot
     see an ambiguous parameter type or a json/jsonb COALESCE that Postgres refuses outright.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp2a_test \\
        .venv/bin/python -m pytest tests/test_reap_agentic_ledger_postgres.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/224_reap_agentic_ledger.sql"

# Same convention as tests/test_agent_issued_cards_postgres.py: this gate DROPS its tables, so it
# must be INCAPABLE of running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

_TABLES = ("reap_agentic_enrollments", "reap_agentic_purchases")
_TERMINALS = ("completed", "failed", "refused", "expired")
_REACHES_TERMINAL = {
    "completed": "awaiting_approval",
    "failed": "resolving",
    "refused": "resolving",
    "expired": "needs_enrollment",
}

_EXPECTED_INDEXES = {
    "idx_reap_agentic_enrollments_buyer_status",
    "uq_reap_agentic_enrollments_reap_id",
    "uq_reap_agentic_enrollments_one_active",
    "idx_reap_agentic_purchases_state_poll",
    "idx_reap_agentic_purchases_buyer_created",
    "idx_reap_agentic_purchases_owner_created",
    "uq_reap_agentic_purchases_checkout",
}
_EXPECTED_UNIQUE_INDEXES = {
    "uq_reap_agentic_enrollments_reap_id",
    "uq_reap_agentic_enrollments_one_active",
    "uq_reap_agentic_purchases_checkout",
}

_PAST = "CURRENT_TIMESTAMP - INTERVAL '120 seconds'"
_FUTURE = "CURRENT_TIMESTAMP + INTERVAL '3600 seconds'"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the agentic-ledger tables in database {dbname!r} — "
            f"throwaway only (e.g. pivota_dialect_check)"
        )


async def _apply_migration():
    from db.database import database
    from db.sql_migrations import split_statements

    for statement in split_statements(_MIGRATION.read_text(encoding="utf-8")):
        await database.execute(statement)


async def _drop_tables():
    from db.database import database

    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Drop first, so a constraint deleted from the migration cannot survive via IF NOT EXISTS
    # and leave this gate testing a schema the repo no longer declares.
    await _drop_tables()
    await _apply_migration()
    yield
    if not was_connected and database.is_connected:
        await database.disconnect()


# ── plumbing for the second-connection tests ─────────────────────────────────────────────────

_BIND_RE = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")


def _to_positional(sql: str):
    """Rewrite `:name` binds to asyncpg's `$n`, keeping the order of first appearance.

    This lets the tests below execute the MODULE'S OWN SQL TEXT on a raw asyncpg connection —
    not a hand-copied paraphrase of it. A paraphrase would keep passing after someone removed
    the guard from the real statement, which is the entire failure mode these tests exist for.
    """
    order = []

    def repl(match):
        name = match.group(1)
        if name not in order:
            order.append(name)
        return f"${order.index(name) + 1}"

    assert "':" not in sql and ":'" not in sql
    return _BIND_RE.sub(repl, sql), order


def _pg_url() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _raw_connection():
    import asyncpg

    return await asyncpg.connect(_pg_url())


async def _run_on(conn, sql: str, params: dict):
    positional, order = _to_positional(sql)
    return await conn.fetch(positional, *[params[name] for name in order])


def _transition_params(purchase_id: str, from_states, to_state: str, **fields):
    """Build exactly the parameter dict `ledger.transition` builds — from the module's own field
    list and slot count, so it cannot drift."""
    import db.reap_agentic_ledger as ledger

    sources = list(dict.fromkeys(from_states))
    params = {"id": purchase_id, "to_state": to_state, "to_state_probe": to_state}
    for index in range(ledger._FROM_SLOTS):
        params[f"from_{index}"] = sources[index] if index < len(sources) else sources[0]
    for column in ledger._TRANSITION_FIELDS:
        raw = fields.get(column)
        params[column] = (
            ledger._bind_json(raw) if column in ledger._JSON_COLUMNS else ledger._bind_dt(raw)
        )
    return params


async def _mk(state: str = "resolving", **over):
    """Create (always 'resolving') and, when a fixture needs another state, move it there with a
    RAW UPDATE — which is explicitly not the code under test, so it cannot mask a defect in
    `transition`."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    kwargs = dict(
        buyer_ref="bref_alice",
        agent_id="agent_one",
        agent_user_ref_hash="hash_alice",
        merchant_domain="brand.example",
        product_key="pk_1",
        currency="USD",
        our_price_minor=4250,
        buyer_email="alice@example.test",
        shipping_address={"line1": "1 Test Way", "city": "Springfield", "zip": "00001"},
    )
    kwargs.update(over)
    purchase = await ledger.create_purchase(**kwargs)
    if state != "resolving":
        await database.execute(
            "UPDATE reap_agentic_purchases SET state = :s WHERE id = :i",
            {"s": state, "i": purchase["id"]},
        )
        purchase = await ledger.get_purchase_internal(purchase["id"])
    return purchase


async def _state_of(purchase_id: str) -> str:
    from db.database import database

    row = await database.fetch_one(
        "SELECT state FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    return row["state"]


async def _set_clock_column(purchase_id: str, column: str, sql_expr: str) -> None:
    """Move a timestamp with a SERVER-SIDE expression — a test that bound its own datetime would
    be exercising precisely the thing the module refuses to do."""
    from db.database import database

    await database.execute(
        f"UPDATE reap_agentic_purchases SET {column} = {sql_expr} WHERE id = :i",
        {"i": purchase_id},
    )


async def _age_claim(purchase_id: str, seconds: int) -> None:
    await _set_clock_column(
        purchase_id, "claimed_at", f"CURRENT_TIMESTAMP - INTERVAL '{int(seconds)} seconds'"
    )


async def _make_due(**over):
    purchase = await _mk(state="awaiting_approval", **over)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    return purchase


# ── 1. the conditional update, across TWO CONNECTIONS ────────────────────────────────────────


async def test_a_second_connection_that_already_advanced_the_row_makes_ours_return_none():
    """THE GUARD, PROVEN ACROSS BACKENDS. The other pod's statement runs on its own asyncpg
    connection and commits; ours then finds the row out of its expected state and is told None.

    Deterministic on purpose. A gathered-coroutine race can pass by accident when the runtime
    happens to serialise the two calls onto one connection — which is exactly what the SQLite
    arm does — so the load-bearing version of this test forces the ordering."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="quoting")
    conn = await _raw_connection()
    try:
        rows = await _run_on(
            conn,
            ledger._TRANSITION_SQL,
            _transition_params(purchase["id"], ["quoting"], "refused"),
        )
        assert len(rows) == 1, "the other connection's advance should have won"
    finally:
        await conn.close()

    lost = await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    )
    assert lost is None, (
        "the row was moved by another connection; the `AND state IN (...)` conjunct is what "
        "turns that into None instead of a second, conflicting advance"
    )
    assert await _state_of(purchase["id"]) == "refused"


async def test_two_connections_advancing_at_once_produce_exactly_one_winner():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="quoting")
    conn_a = await _raw_connection()
    conn_b = await _raw_connection()
    try:
        rows_a, rows_b = await asyncio.gather(
            _run_on(
                conn_a,
                ledger._TRANSITION_SQL,
                _transition_params(purchase["id"], ["quoting"], "awaiting_approval"),
            ),
            _run_on(
                conn_b,
                ledger._TRANSITION_SQL,
                _transition_params(purchase["id"], ["quoting"], "refused"),
            ),
        )
    finally:
        await conn_a.close()
        await conn_b.close()

    assert len(rows_a) + len(rows_b) == 1, (
        f"exactly one advance may win; got {len(rows_a)} + {len(rows_b)}"
    )
    assert await _state_of(purchase["id"]) in ("awaiting_approval", "refused")


async def test_the_module_reports_a_real_advance_as_a_row_not_as_none():
    """The control for every None above, and the defect class in its own right: on this driver
    `execute()` of an UPDATE answers None whether it moved a row or not, which is how
    db/agent_issued_cards.mark_revoked became a constant False."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="quoting")
    moved = await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    )
    assert moved is not None and moved["state"] == "awaiting_approval"


@pytest.mark.parametrize(
    "src,dst",
    [(src, dst) for src, allowed in [
        ("resolving", ("needs_enrollment", "quoting", "refused", "failed")),
        ("needs_enrollment", ("quoting", "expired", "failed")),
        ("quoting", ("awaiting_approval", "refused", "failed")),
        ("awaiting_approval", ("processing", "completed", "failed", "expired")),
        ("processing", ("completed", "failed")),
    ] for dst in allowed],
)
async def test_every_legal_transition_plans_and_runs_on_postgres(src, dst):
    """The pairs are spelled out here rather than read from ALLOWED_TRANSITIONS on purpose: this
    file is the one that would notice the map being edited to permit something the rail does not
    mean to permit. A parametrisation derived from the map cannot."""
    import db.reap_agentic_ledger as ledger

    assert dst in ledger.ALLOWED_TRANSITIONS[src], (
        f"{src} -> {dst} is spelled out in this gate but no longer in ALLOWED_TRANSITIONS"
    )
    purchase = await _mk(state=src)
    moved = await ledger.transition(purchase["id"], from_states=[src], to_state=dst)
    assert moved is not None and moved["state"] == dst


@pytest.mark.parametrize("src,dst", [("completed", "processing"), ("resolving", "processing"),
                                     ("processing", "refused"), ("expired", "completed")])
async def test_illegal_transitions_raise_before_touching_postgres(src, dst):
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state=src)
    with pytest.raises(ValueError):
        await ledger.transition(purchase["id"], from_states=[src], to_state=dst)
    assert await _state_of(purchase["id"]) == src


# ── FINDING 1: creation is 'resolving' only ──────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["refused", "failed", "completed", "expired", "processing"])
async def test_create_refuses_a_non_resolving_state_on_postgres(state):
    """THE P0. A terminal create kept shipping_address and buyer_email and never stamped
    terminal_at, because the PII rule lives in the transition statement and a create is not a
    transition."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    with pytest.raises(ValueError):
        await ledger.create_purchase(
            buyer_ref="b2",
            agent_id="agent_one",
            agent_user_ref_hash="hash_alice",
            state=state,
            buyer_email="victim@example.test",
            shipping_address={"line1": "1 Test Way"},
        )
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


# ── 2. jsonb ─────────────────────────────────────────────────────────────────────────────────


async def test_jsonb_comes_back_as_text_from_the_driver_and_as_python_from_the_module():
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(
        queries_tried=["serum", "vitamin c serum"],
        shipping_address={"line1": "1 Test Way", "zip": "00001"},
    )
    raw = await database.fetch_val(
        "SELECT queries_tried FROM reap_agentic_purchases WHERE id = :i", {"i": purchase["id"]}
    )
    assert isinstance(raw, str), (
        "a raw-SQL jsonb column is expected to arrive as text on asyncpg — if this ever stops "
        "being true, the module's decode needs re-checking, not deleting"
    )
    assert isinstance(purchase["queries_tried"], list)
    assert purchase["queries_tried"] == ["serum", "vitamin c serum"]
    assert isinstance(purchase["shipping_address"], dict)

    fetched = await ledger.get_purchase_internal(purchase["id"])
    assert isinstance(fetched["queries_tried"], list)
    assert isinstance(fetched["shipping_address"], dict)

    moved = await ledger.transition(
        purchase["id"],
        from_states=["resolving"],
        to_state="quoting",
        queries_tried=[{"q": "serum", "hits": 3}],
    )
    assert isinstance(moved["queries_tried"], list)
    assert moved["queries_tried"][0]["hits"] == 3


async def test_the_json_columns_really_are_jsonb_in_the_shipped_schema():
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT column_name, data_type FROM information_schema.columns
         WHERE table_name = 'reap_agentic_purchases'
           AND column_name IN ('queries_tried', 'shipping_address')
        """
    )
    types = {r["column_name"]: r["data_type"] for r in rows}
    assert types == {"queries_tried": "jsonb", "shipping_address": "jsonb"}, types


# ── the PII rule ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("terminal", _TERMINALS)
async def test_terminal_state_nulls_the_pii_on_postgres(terminal):
    from db.database import database
    import db.reap_agentic_ledger as ledger

    src = _REACHES_TERMINAL[terminal]
    purchase = await _mk(state=src)
    done = await ledger.transition(purchase["id"], from_states=[src], to_state=terminal)
    assert done["buyer_email"] is None
    assert done["shipping_address"] is None
    assert done["terminal_at"] is not None

    row = await database.fetch_one(
        "SELECT buyer_email, shipping_address FROM reap_agentic_purchases WHERE id = :i",
        {"i": purchase["id"]},
    )
    assert row["buyer_email"] is None and row["shipping_address"] is None


async def test_a_non_terminal_transition_keeps_the_pii_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting"
    )
    assert moved["buyer_email"] == "alice@example.test"
    assert moved["shipping_address"]["city"] == "Springfield"
    assert moved["terminal_at"] is None


@pytest.mark.parametrize("terminal", _TERMINALS)
async def test_a_terminal_transition_also_clears_the_claim_on_postgres(terminal):
    from db.database import database
    import db.reap_agentic_ledger as ledger

    src = _REACHES_TERMINAL[terminal]
    purchase = await _mk(state=src)
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = 'w1', "
        "claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase["id"]},
    )
    done = await ledger.transition(purchase["id"], from_states=[src], to_state=terminal)
    assert done["claimed_by"] is None and done["claimed_at"] is None


# ── ownership and redaction ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent_id,user_hash",
    [("agent_two", "hash_alice"), ("agent_one", "hash_bob"), (None, None), ("agent_one", None)],
)
async def test_ownership_conjunct_holds_on_postgres(agent_id, user_hash):
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    assert await ledger.get_purchase_for_owner(purchase["id"], agent_id, user_hash) is None
    assert await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")


async def test_list_ownership_conjunct_holds_on_postgres():
    import db.reap_agentic_ledger as ledger

    mine = await _mk()
    await _mk(agent_id="agent_two", agent_user_ref_hash="hash_alice", buyer_ref="bref_other")
    await _mk(agent_id="agent_one", agent_user_ref_hash="hash_bob", buyer_ref="bref_bob")

    rows = await ledger.list_purchases_for_owner("agent_one", "hash_alice")
    assert [r["id"] for r in rows] == [mine["id"]]
    assert await ledger.list_purchases_for_owner("agent_two", "hash_bob") == []
    assert await ledger.list_purchases_for_owner(None, None) == []


async def test_owner_reads_are_redacted_by_default_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    got = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    listed = await ledger.list_purchases_for_owner("agent_one", "hash_alice")
    for view in (got, listed[0]):
        leaked = sorted(set(view) & ledger._PRIVATE_PURCHASE_COLUMNS)
        assert not leaked, f"owner read leaked private columns: {leaked}"
    full = await ledger.get_purchase_for_owner(
        purchase["id"], "agent_one", "hash_alice", include_private=True
    )
    assert full["buyer_email"] == "alice@example.test"


async def test_list_limit_and_offset_are_capped_on_postgres():
    """M9. On Postgres a negative OFFSET is a hard error, not a silent zero — so the floor is not
    cosmetic here."""
    import db.reap_agentic_ledger as ledger

    for index in range(3):
        await _mk(buyer_ref=f"bref_{index}")
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", limit=0)) == 1
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", limit=9999)) == 3
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", offset=-3)) == 3


# ── the claim pair ───────────────────────────────────────────────────────────────────────────


async def test_the_claim_statement_itself_refuses_a_second_holder():
    """THE SELECT MASKS THE UPDATE'S CONJUNCT, so the statement is exercised directly.

    `_SELECT_DUE_PURCHASES_SQL` already filters `claimed_by IS NULL`. That makes
    `claim_due_purchases` LOOK correct even with `AND claimed_by IS NULL` deleted from the
    UPDATE — the already-leased row is simply never offered as a candidate a second time.
    Measured, not theorised: that mutant survived every other test in this file while the SQLite
    arm killed it.
    """
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    conn_a = await _raw_connection()
    conn_b = await _raw_connection()
    try:
        rows_a, rows_b = await asyncio.gather(
            _run_on(conn_a, ledger._CLAIM_PURCHASE_SQL,
                    {"id": purchase["id"], "worker_id": "worker_a"}),
            _run_on(conn_b, ledger._CLAIM_PURCHASE_SQL,
                    {"id": purchase["id"], "worker_id": "worker_b"}),
        )
    finally:
        await conn_a.close()
        await conn_b.close()

    assert len(rows_a) + len(rows_b) == 1, (
        f"exactly one worker may take a lease; got {len(rows_a)} + {len(rows_b)}"
    )
    assert (await ledger.get_purchase_internal(purchase["id"]))["attempts"] == 1


async def test_the_claim_statement_refuses_a_row_that_went_terminal_after_the_select():
    """FINDING 2, first half, from a SECOND CONNECTION.

    The interleaving: worker A's SELECT offers a due 'processing' row; worker B — a different
    pod, its own connection — completes it; worker A's claim UPDATE then lands. With only
    `claimed_by IS NULL` that claim SUCCEEDED and handed the poller a finished purchase. It was
    also unrecoverable: the requeue's old state filter made a dead pod's claim on a terminal row
    invisible, so a 99999-second-old claim freed 0 rows. Reproduced on both dialects.
    """
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="processing")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)

    conn = await _raw_connection()
    try:
        moved = await _run_on(
            conn,
            ledger._TRANSITION_SQL,
            _transition_params(purchase["id"], ["processing"], "completed"),
        )
        assert len(moved) == 1
        claimed = await _run_on(
            conn, ledger._CLAIM_PURCHASE_SQL, {"id": purchase["id"], "worker_id": "worker_a"}
        )
    finally:
        await conn.close()

    assert claimed == [], (
        "the claim UPDATE must re-check the state: the row reached a terminal state between the "
        "candidate SELECT and this statement, and a poller must never be handed one"
    )
    assert (await ledger.get_purchase_internal(purchase["id"]))["attempts"] == 0


async def test_create_defaults_next_poll_at_to_now_on_postgres():
    """D5, on this dialect specifically. The default is `COALESCE(:next_poll_at,
    CURRENT_TIMESTAMP)` inside the INSERT, and there is one copy of that per dialect — so a
    mutant that deletes it on the Postgres side is invisible to the SQLite arm. Measured: it was.

    Without the default, a created purchase has next_poll_at NULL and the poller's SELECT can
    never offer it (`next_poll_at IS NOT NULL AND next_poll_at <= CURRENT_TIMESTAMP`), so the row
    sits in 'resolving' forever.
    """
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    assert purchase["next_poll_at"] is not None
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert [c["id"] for c in claimed] == [purchase["id"]]


async def test_claim_then_second_claim_is_empty():
    import db.reap_agentic_ledger as ledger

    await _make_due()
    first = await ledger.claim_due_purchases("worker_a", limit=5)
    second = await ledger.claim_due_purchases("worker_b", limit=5)
    assert len(first) == 1 and second == []
    assert first[0]["attempts"] == 1


@pytest.mark.parametrize("terminal", _TERMINALS)
async def test_a_terminal_purchase_is_never_claimed_on_postgres(terminal):
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state=terminal)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    assert await ledger.claim_due_purchases("worker_a", limit=5) == []


async def test_release_by_the_wrong_worker_does_nothing_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    assert await ledger.release_claim(purchase["id"], "worker_b") is None
    assert (await ledger.get_purchase_internal(purchase["id"]))["claimed_by"] == "worker_a"


async def test_requeue_uses_a_server_side_cutoff_even_from_a_non_utc_process():
    """THE SEVEN-HOUR BUG, made unreproducible.

    asyncpg encodes a naive datetime for a timestamptz parameter as local wall time — local to
    THIS PROCESS. A `utcnow() - timedelta(...)` cutoff bound from a Pacific box against a UTC
    database lands seven hours IN THE FUTURE, which requeues every live lease.
    """
    import db.reap_agentic_ledger as ledger

    stale = await _make_due()
    fresh = await _make_due(buyer_ref="bref_fresh")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(stale["id"], 900)

    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        moved = await ledger.requeue_stale_claims(lease_seconds=300, limit=10)
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()

    assert moved == 1, (
        "exactly the stale lease moves. The count comes from RETURNING id — without it asyncpg "
        "answers None however many rows moved."
    )
    assert (await ledger.get_purchase_internal(stale["id"]))["claimed_by"] is None
    assert (await ledger.get_purchase_internal(fresh["id"]))["claimed_by"] == "worker_a", (
        "a lease inside its window must survive a requeue run from a non-UTC client process"
    )


async def test_requeue_frees_a_stale_claim_on_a_terminal_row_on_postgres():
    """FINDING 2, second half. The old state filter made a dead pod's claim on a terminal row
    unclearable forever — measured, a 99999-second-old claim freed 0 rows."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'completed' WHERE id = :i",
        {"i": purchase["id"]},
    )
    await _age_claim(purchase["id"], 99999)

    assert await ledger.requeue_stale_claims(lease_seconds=300, limit=10) == 1
    assert (await ledger.get_purchase_internal(purchase["id"]))["claimed_by"] is None
    # ...and clearing it did NOT make it work again.
    assert await ledger.claim_due_purchases("worker_b", limit=5) == []


async def test_a_requeued_purchase_is_claimable_again_on_postgres():
    import db.reap_agentic_ledger as ledger

    stale = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(stale["id"], 900)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)
    reclaimed = await ledger.claim_due_purchases("worker_b", limit=5)
    assert [c["id"] for c in reclaimed] == [stale["id"]]
    assert reclaimed[0]["attempts"] == 2


@pytest.mark.parametrize("bad", [0, 29, -1])
async def test_requeue_refuses_a_short_lease_on_postgres(bad):
    import db.reap_agentic_ledger as ledger

    with pytest.raises(ValueError):
        await ledger.requeue_stale_claims(lease_seconds=bad)


async def test_timestamps_come_back_timezone_aware():
    """A naive datetime out of this module is a datetime some later caller will compare against
    `datetime.now(timezone.utc)` and get a TypeError, or worse, bind back in naive."""
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    fetched = await ledger.get_purchase_internal(purchase["id"])
    for column in ("created_at", "updated_at", "next_poll_at"):
        assert fetched[column].tzinfo is not None, f"{column} came back naive"


# ── FINDING 5: naive datetimes are UTC, not local ────────────────────────────────────────────


@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Asia/Tokyo"])
async def test_a_naive_datetime_is_read_as_utc_not_as_local_time_on_postgres(tz):
    """FINDING 5. `_bind_dt`'s `.replace(tzinfo=utc)` had NO killing test — the reviewer deleted
    it and the whole suite stayed green, even under a shifted TZ, because every other test built
    its datetimes already-aware. asyncpg reads a naive bind for a timestamptz parameter as LOCAL
    time, so the divergence is a real eight-hour shift on next_poll_at and on
    reap_quote_expires_at — a quote we believe is live after Reap has expired it."""
    import db.reap_agentic_ledger as ledger

    previous = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        naive = datetime(2026, 9, 18, 12, 0, 0)
        expected = naive.replace(tzinfo=timezone.utc)

        purchase = await _mk(next_poll_at=naive)
        assert purchase["next_poll_at"] == expected, (
            f"next_poll_at drifted under TZ={tz}: {purchase['next_poll_at']} != {expected}"
        )
        moved = await ledger.transition(
            purchase["id"],
            from_states=["resolving"],
            to_state="quoting",
            reap_quote_expires_at=naive,
            hosted_url_expires_at=naive,
        )
        assert moved["reap_quote_expires_at"] == expected
        assert moved["hosted_url_expires_at"] == expected
        reread = await ledger.get_purchase_internal(purchase["id"])
        assert reread["reap_quote_expires_at"] == expected
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


async def test_an_aware_datetime_in_another_zone_is_stored_as_the_same_instant_on_postgres():
    """The control: the guard must NORMALISE, not blindly stamp UTC onto everything."""
    aware = datetime(2026, 9, 18, 4, 0, 0, tzinfo=timezone(timedelta(hours=-8)))
    purchase = await _mk(next_poll_at=aware)
    assert purchase["next_poll_at"] == datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


# ── the bulk sweeps ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["needs_enrollment", "awaiting_approval"])
async def test_expire_moves_an_overdue_hosted_page_and_scrubs_it_on_postgres(state):
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state=state)
    await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = 'w1', "
        "claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase["id"]},
    )
    assert await ledger.expire_overdue_purchases() == [purchase["id"]]

    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["terminal_at"] is not None
    assert row["claimed_by"] is None and row["claimed_at"] is None
    assert row["last_error_code"] == "hosted_url_expired"


async def test_expire_leaves_live_and_wrong_state_rows_alone_on_postgres():
    import db.reap_agentic_ledger as ledger

    live = await _mk(state="awaiting_approval")
    await _set_clock_column(live["id"], "hosted_url_expires_at", _FUTURE)
    wrong = await _mk(state="quoting", buyer_ref="bref_q")
    await _set_clock_column(wrong["id"], "hosted_url_expires_at", _PAST)
    assert await ledger.expire_overdue_purchases() == []


async def test_two_connections_expiring_at_once_move_the_row_once():
    """The sweep is one conditional UPDATE, so it races the same way the advance does."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)

    conn_a = await _raw_connection()
    conn_b = await _raw_connection()
    try:
        rows_a, rows_b = await asyncio.gather(
            _run_on(conn_a, ledger._EXPIRE_OVERDUE_SQL, {}),
            _run_on(conn_b, ledger._EXPIRE_OVERDUE_SQL, {}),
        )
    finally:
        await conn_a.close()
        await conn_b.close()
    assert len(rows_a) + len(rows_b) == 1
    assert await _state_of(purchase["id"]) == "expired"


async def test_fail_exhausted_moves_a_stuck_row_and_scrubs_it_on_postgres():
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 7, claimed_by = 'w1', "
        "claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase["id"]},
    )
    assert await ledger.fail_exhausted_purchases(5) == [purchase["id"]]
    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "failed"
    assert row["last_error_code"] == "attempts_exhausted"
    assert row["buyer_email"] is None and row["terminal_at"] is not None
    assert row["claimed_by"] is None


async def test_fail_exhausted_leaves_rows_under_the_bound_alone_on_postgres():
    from db.database import database
    import db.reap_agentic_ledger as ledger

    under = await _mk(state="processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 4 WHERE id = :i", {"i": under["id"]}
    )
    assert await ledger.fail_exhausted_purchases(5) == []


async def test_two_connections_failing_exhausted_at_once_move_the_row_once():
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 9 WHERE id = :i", {"i": purchase["id"]}
    )
    conn_a = await _raw_connection()
    conn_b = await _raw_connection()
    try:
        rows_a, rows_b = await asyncio.gather(
            _run_on(conn_a, ledger._FAIL_EXHAUSTED_SQL, {"max_attempts": 5}),
            _run_on(conn_b, ledger._FAIL_EXHAUSTED_SQL, {"max_attempts": 5}),
        )
    finally:
        await conn_a.close()
        await conn_b.close()
    assert len(rows_a) + len(rows_b) == 1


async def test_the_bulk_sweeps_only_use_legal_edges_on_postgres():
    import db.reap_agentic_ledger as ledger

    for src in ledger._EXPIRE_SOURCE_STATES:
        assert "expired" in ledger.ALLOWED_TRANSITIONS[src]
    for src in ledger._FAIL_EXHAUSTED_SOURCE_STATES:
        assert "failed" in ledger.ALLOWED_TRANSITIONS[src]


# ── enrollments ──────────────────────────────────────────────────────────────────────────────


async def test_the_partial_unique_index_makes_two_active_enrollments_impossible():
    import asyncpg
    from db.database import database
    import db.reap_agentic_ledger as ledger

    first = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(first["id"], reap_enrollment_id="enr_1")
    second = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "UPDATE reap_agentic_enrollments SET status = 'active' WHERE id = :i",
            {"i": second["id"]},
        )


async def test_activation_demotes_the_previous_active_row_on_postgres():
    from db.database import database
    import db.reap_agentic_ledger as ledger

    first = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(first["id"], reap_enrollment_id="enr_1")
    second = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    activated = await ledger.mark_enrollment_active(second["id"], reap_enrollment_id="enr_2")
    assert activated is not None and activated["status"] == "active"

    actives = await database.fetch_all(
        "SELECT id FROM reap_agentic_enrollments WHERE buyer_ref = :b AND status = 'active'",
        {"b": "bref_alice"},
    )
    assert [r["id"] for r in actives] == [second["id"]]
    assert (await ledger.get_active_enrollment("bref_alice"))["id"] == second["id"]


async def test_activating_a_DEAD_row_leaves_the_live_enrollment_alone_on_postgres():
    """FINDING 3. The demotion used to run before the promotion could fail, leaving the buyer
    with NO active card — a worse state than they started in, reached by a call that answered
    None as though nothing had happened."""
    import db.reap_agentic_ledger as ledger

    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    dead = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_dead(dead["id"])

    assert await ledger.mark_enrollment_active(dead["id"]) is None
    still = await ledger.get_active_enrollment("bref_alice")
    assert still is not None and still["id"] == live["id"]


async def test_a_lost_activation_race_returns_none_not_a_driver_exception():
    """E4, FROM TWO REAL CONNECTIONS, deterministically.

    A second connection opens a transaction and promotes one of the buyer's pending rows without
    committing. The module then tries to promote the OTHER one: its promotion blocks on the
    partial unique index, the other side commits, and ours comes back a UniqueViolationError.

    Two things must hold. The call must answer None — every other lost race in this module does,
    and a caller handed a raw asyncpg exception cannot tell "you lost" from "the database is
    broken". And the loser's DEMOTION must be rolled back, which is why the except sits OUTSIDE
    the `async with database.transaction()` rather than inside it: catching inside would COMMIT a
    demotion whose promotion never happened, which is the finding-3 outcome by another route.
    """
    from db.database import database
    import db.reap_agentic_ledger as ledger

    mine = await ledger.upsert_pending_enrollment(buyer_ref="bref_race")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_theirs', 'bref_race', 'pending')"
    )

    conn = await _raw_connection()
    transaction = conn.transaction()
    await transaction.start()
    await conn.execute(
        "UPDATE reap_agentic_enrollments SET status = 'active' WHERE id = 're_theirs'"
    )

    async def ours():
        return await ledger.mark_enrollment_active(mine["id"], reap_enrollment_id="enr_mine")

    task = asyncio.ensure_future(ours())
    try:
        # Let ours reach the index and block on the uncommitted row, then let the other side win.
        await asyncio.sleep(0.4)
        await transaction.commit()
        result = await asyncio.wait_for(task, timeout=10)
    finally:
        await conn.close()

    assert result is None, (
        "a lost activation race must answer None, like every other lost race in this module"
    )
    actives = await database.fetch_all(
        "SELECT id FROM reap_agentic_enrollments WHERE buyer_ref = :b AND status = 'active'",
        {"b": "bref_race"},
    )
    assert [r["id"] for r in actives] == ["re_theirs"]
    loser = await database.fetch_one(
        "SELECT status FROM reap_agentic_enrollments WHERE id = :i", {"i": mine["id"]}
    )
    assert loser["status"] == "pending", "the loser's row must be untouched, not half-written"


async def test_the_reap_enrollment_id_is_unique_when_present_and_free_when_null():
    import asyncpg
    from db.database import database
    import db.reap_agentic_ledger as ledger

    await ledger.upsert_pending_enrollment(buyer_ref="bref_a")
    await ledger.upsert_pending_enrollment(buyer_ref="bref_b")
    a = await ledger.upsert_pending_enrollment(buyer_ref="bref_a", reap_enrollment_id="enr_dup")
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "UPDATE reap_agentic_enrollments SET reap_enrollment_id = 'enr_dup' WHERE id = :i",
            {"i": (await ledger.upsert_pending_enrollment(buyer_ref="bref_b"))["id"]},
        )
    assert a["reap_enrollment_id"] == "enr_dup"


# ── FINDING 4: the self-heal builds the SAME schema as the migration ─────────────────────────


async def _schema_fingerprint():
    """What the DATABASE actually built, read out of the catalog.

    Three parts, because each catches something the others cannot:
      columns  — name, type, nullability, default, length;
      indexes  — `pg_indexes.indexdef`, which is the reconstructed DDL. This is the part that
                 catches UNIQUE silently becoming non-unique, and a partial predicate being
                 dropped. A token check on the SOURCE text cannot: the reviewer changed
                 CREATE UNIQUE INDEX to CREATE INDEX in both self-heal branches and the entire
                 suite stayed green.
      checks   — `pg_get_constraintdef` for every CHECK, which is where the state and status
                 vocabularies and `quantity > 0` actually live.
    """
    from db.database import database

    columns = await database.fetch_all(
        """
        SELECT table_name, column_name, data_type, is_nullable, column_default,
               character_maximum_length
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = ANY(:tables)
         ORDER BY table_name, column_name
        """,
        {"tables": list(_TABLES)},
    )
    indexes = await database.fetch_all(
        """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = ANY(:tables)
         ORDER BY indexname
        """,
        {"tables": list(_TABLES)},
    )
    checks = await database.fetch_all(
        """
        SELECT pg_get_constraintdef(c.oid) AS def
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public' AND c.contype = 'c' AND t.relname = ANY(:tables)
        """,
        {"tables": list(_TABLES)},
    )

    def _norm(text: str) -> str:
        # Whitespace only. The schema qualifier is identical on both builds (both land in
        # `public`), so normalising it away would weaken the comparison for nothing.
        return " ".join((text or "").split())

    return (
        [tuple(dict(r).values()) for r in columns],
        {r["indexname"]: _norm(r["indexdef"]) for r in indexes},
        sorted(_norm(r["def"]) for r in checks),
    )


async def test_the_self_heal_builds_the_same_schema_as_the_migration():
    """FINDING 4. Production never runs db/migrations, so the self-heal IS the production schema
    — and until now nothing compared the two beyond a token search of the source text.

    Built sequentially in this one throwaway database rather than in two databases: `CREATE
    DATABASE` needs a privilege the CI gate's role may not have, and dropping between the two
    builds gives the same comparison. Both halves start from the same empty state.
    """
    from db.schema_guard import ensure_required_schema_light

    # The fixture already applied the migration.
    from_migration = await _schema_fingerprint()
    assert from_migration[1], "precondition: the migration built some indexes"
    assert from_migration[2], "precondition: the migration built some CHECK constraints"

    await _drop_tables()
    await ensure_required_schema_light()
    from_self_heal = await _schema_fingerprint()

    columns_m, indexes_m, checks_m = from_migration
    columns_s, indexes_s, checks_s = from_self_heal

    assert columns_s == columns_m, "columns differ between the self-heal and the migration"
    assert set(indexes_s) == set(indexes_m), (
        f"index NAMES differ: only in self-heal {sorted(set(indexes_s) - set(indexes_m))}, "
        f"only in migration {sorted(set(indexes_m) - set(indexes_s))}"
    )
    for name in sorted(indexes_m):
        assert indexes_s[name] == indexes_m[name], (
            f"{name} is built differently by the self-heal:\n"
            f"  migration: {indexes_m[name]}\n"
            f"  self-heal: {indexes_s[name]}"
        )
    assert checks_s == checks_m, (
        "CHECK constraints differ between the self-heal and the migration:\n"
        f"  migration: {checks_m}\n  self-heal: {checks_s}"
    )


async def test_the_self_heal_built_indexes_are_actually_unique():
    """The direct reading of finding 4's mutant, so the failure names itself rather than arriving
    as a diff. `indexdef` is the DDL the database reconstructed, not the DDL the source says."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await _drop_tables()
    await ensure_required_schema_light()

    rows = await database.fetch_all(
        """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = ANY(:tables)
        """,
        {"tables": list(_TABLES)},
    )
    built = {r["indexname"]: " ".join(r["indexdef"].split()) for r in rows}

    missing = _EXPECTED_INDEXES - set(built)
    assert not missing, f"the self-heal did not create these indexes: {sorted(missing)}"

    for name in sorted(_EXPECTED_UNIQUE_INDEXES):
        assert "CREATE UNIQUE INDEX" in built[name].upper(), (
            f"{name} was built as a NON-unique index: {built[name]!r}. The invariant it carries "
            "is the whole reason it exists."
        )
        assert " WHERE " in built[name].upper(), (
            f"{name} lost its partial predicate: {built[name]!r}"
        )
    for name in sorted(_EXPECTED_INDEXES - _EXPECTED_UNIQUE_INDEXES):
        assert "UNIQUE" not in built[name].upper(), f"{name} is not meant to be unique"


async def test_uniqueness_holds_BEHAVIOURALLY_on_a_self_heal_built_schema():
    """The other half of finding 4: the behavioural uniqueness tests used to run only against the
    MIGRATION-built schema, which production never has. These run against the schema production
    actually gets."""
    import asyncpg
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await _drop_tables()
    await ensure_required_schema_light()

    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_a1', 'bref_x', 'active')"
    )
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_a2', 'bref_x', 'pending')"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "UPDATE reap_agentic_enrollments SET status = 'active' WHERE id = 're_a2'"
        )

    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status, reap_enrollment_id) "
        "VALUES ('re_d1', 'bref_y', 'pending', 'enr_dup')"
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments "
            "(id, buyer_ref, status, reap_enrollment_id) "
            "VALUES ('re_d2', 'bref_z', 'pending', 'enr_dup')"
        )

    import db.reap_agentic_ledger as ledger

    a = await _mk()
    b = await _mk(buyer_ref="bref_b")
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_dup' WHERE id = :i",
        {"i": a["id"]},
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_dup' WHERE id = :i",
            {"i": b["id"]},
        )
    assert ledger  # the module is what put those rows there


async def test_the_state_check_on_a_self_heal_built_schema_refuses_an_unknown_state():
    """The CHECK vocabulary, behaviourally, on the production-shaped schema."""
    import asyncpg
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await _drop_tables()
    await ensure_required_schema_light()
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await database.execute(
            "INSERT INTO reap_agentic_purchases (id, buyer_ref, state) "
            "VALUES ('rp_bad', 'b', 'not_a_state')"
        )


async def test_self_heal_is_idempotent_and_leaves_the_rail_usable():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light
    import db.reap_agentic_ledger as ledger

    await ensure_required_schema_light()
    await ensure_required_schema_light()

    rows = await database.fetch_all(
        """
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public' AND tablename = ANY(:tables)
        """,
        {"tables": list(_TABLES)},
    )
    assert _EXPECTED_INDEXES <= {r["indexname"] for r in rows}
    purchase = await _mk()
    assert await ledger.get_purchase_internal(purchase["id"]) is not None


# ── 5. PREPARE ───────────────────────────────────────────────────────────────────────────────


def _module_sql_constants():
    import db.reap_agentic_ledger as ledger

    return {
        name: value
        for name, value in vars(ledger).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


async def test_every_postgres_sql_constant_in_the_module_prepares():
    """The #1588 class: Postgres refuses to plan a statement whose parameter types it cannot
    infer, and SQLite cannot see it."""
    constants = _module_sql_constants()
    assert len(constants) >= 14, (
        f"only found {len(constants)} SQL constants — has the module started building SQL at "
        "call time? That shape is invisible to the repo's PREPARE sweep."
    )

    conn = await _raw_connection()
    failures = []
    try:
        for name, sql in sorted(constants.items()):
            if name.endswith("_SQLITE"):
                continue
            positional, _order = _to_positional(sql)
            try:
                await conn.prepare(positional)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
    finally:
        await conn.close()
    assert not failures, "these statements cannot be planned by Postgres:\n  " + "\n  ".join(
        failures
    )


async def test_the_sqlite_twins_carry_no_jsonb_cast():
    """`CAST(x AS JSONB)` on SQLite resolves to NUMERIC affinity and silently stores 0 — the
    payload is destroyed without an error."""
    for name, sql in _module_sql_constants().items():
        if name.endswith("_SQLITE"):
            assert "JSONB" not in sql.upper(), f"{name} must not mention JSONB"


async def test_the_repo_prepare_sweep_can_see_this_module():
    """MERGED IS NOT RUNNING. The sweep resolves a `database.*` first argument only when it is a
    literal or a MODULE-level name bound to one; a helper that returns SQL is a counted blind
    spot, and so is a local bound to `A if IS_POSTGRES else B`."""
    import ast

    source = (
        Path(__file__).resolve().parent.parent / "db/reap_agentic_ledger.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_level = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }

    unresolvable = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id != "database" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            continue
        if isinstance(first, ast.Name) and first.id in module_level:
            continue
        unresolvable.append(ast.dump(first)[:120])

    assert not unresolvable, (
        "these database.* call sites hand SQL the repo's PREPARE sweep cannot follow, so the "
        "statements would ship unplanned:\n  " + "\n  ".join(unresolvable)
    )
