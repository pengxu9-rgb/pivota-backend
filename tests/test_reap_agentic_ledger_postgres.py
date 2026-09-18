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

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db/migrations"

# EVERY migration this rail owns, IN ORDER — not just the one that created the tables. 225 is an
# ALTER, so a fixture that applied only 224 would build the pre-225 schema and then compare it
# against a self-heal that does carry the hint columns: the catalog-parity test would fail for
# the wrong reason, and every hint round-trip below would fail on an UndefinedColumn. The list is
# explicit rather than a glob so that the next migration on some OTHER table cannot silently
# join this gate's fixture.
_MIGRATIONS = (
    _MIGRATIONS_DIR / "224_reap_agentic_ledger.sql",
    _MIGRATIONS_DIR / "225_reap_agentic_purchase_hints.sql",
    # 226 adds item_source + cart_url (the cart-link lane). Without it the whole-table parity
    # test compares a self-heal that HAS them against a migration build that does not.
    _MIGRATIONS_DIR / "226_reap_agentic_purchase_item_source.sql",
)
_MIGRATION = _MIGRATIONS[0]

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

    for path in _MIGRATIONS:
        for statement in split_statements(path.read_text(encoding="utf-8")):
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


def _transition_params(purchase_id: str, from_states, to_state: str, holder=None, **fields):
    """Build exactly the parameter dict `ledger.transition` builds — from the module's own field
    list and slot count, so it cannot drift."""
    import db.reap_agentic_ledger as ledger

    sources = list(dict.fromkeys(from_states))
    params = {
        "id": purchase_id,
        "to_state": to_state,
        "to_state_probe": to_state,
        "holder": holder,
        "holder_unfenced": 1 if holder is None else 0,
    }
    for index in range(ledger._FROM_SLOTS):
        params[f"from_{index}"] = sources[index] if index < len(sources) else sources[0]
    for column in ledger._TRANSITION_FIELDS:
        raw = fields.get(column)
        params[column] = (
            ledger._bind_json(raw) if column in ledger._JSON_COLUMNS else ledger._bind_dt(raw)
        )
    return params


def _sweep_params(max_age_seconds: int = 3600, limit: int = 200):
    return {"max_age_seconds": max_age_seconds, "limit": limit}


def _fail_params(max_attempts: int = 5, limit: int = 200, include_processing: int = 1):
    return {"max_attempts": max_attempts, "limit": limit,
            "include_processing": include_processing}


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


# ── FINDING 1: the claim fence, across two connections ───────────────────────────────────────


async def test_a_stalled_worker_cannot_advance_a_row_whose_lease_it_lost_on_postgres():
    """THE INTERLEAVING, with worker B on its OWN CONNECTION — which is what "another pod" means.

    A claims a 'quoting' row and stalls; the lease ages out and is requeued; B (a different
    backend) claims it and advances it to 'awaiting_approval'; A wakes and advances it to
    'processing' with its own checkout id. Before the fence that SUCCEEDED, leaving a row in
    'processing' carrying a checkout minted by a worker that does not own it — a buyer charged
    for the wrong checkout. `from_states` cannot see it: A's view of the STATE is right, its view
    of OWNERSHIP is stale."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="quoting")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)

    await ledger.claim_due_purchases("worker_A", limit=5)
    await _age_claim(purchase["id"], 900)
    assert await ledger.requeue_stale_claims(lease_seconds=300, limit=10) == 1

    conn = await _raw_connection()
    try:
        taken = await _run_on(
            conn, ledger._CLAIM_PURCHASE_SQL, {"id": purchase["id"], "worker_id": "worker_B"}
        )
        assert len(taken) == 1, "worker B, on its own connection, took the lease"
        advanced = await _run_on(
            conn,
            ledger._TRANSITION_SQL,
            _transition_params(
                purchase["id"], ["quoting"], "awaiting_approval",
                holder="worker_B", reap_quote_id="QUOTE_FROM_B",
            ),
        )
        assert len(advanced) == 1
    finally:
        await conn.close()

    lost = await ledger.transition_as_holder(
        purchase["id"], "worker_A", from_states=["awaiting_approval"],
        to_state="processing", reap_checkout_id="CHECKOUT_FROM_A",
    )
    assert lost is None
    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "awaiting_approval"
    assert row["reap_checkout_id"] is None
    assert row["claimed_by"] == "worker_B"


async def test_a_fenced_transition_by_the_true_holder_succeeds_on_postgres():
    """The control — a fence that refused everybody would pass the test above."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="quoting")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    await ledger.claim_due_purchases("worker_A", limit=5)

    moved = await ledger.transition_as_holder(
        purchase["id"], "worker_A", from_states=["quoting"],
        to_state="awaiting_approval", reap_quote_id="q_1",
    )
    assert moved is not None and moved["reap_quote_id"] == "q_1"
    assert moved["claimed_by"] == "worker_A"


async def test_a_terminal_transition_by_the_holder_still_clears_the_claim_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    await ledger.claim_due_purchases("worker_A", limit=5)
    done = await ledger.transition_as_holder(
        purchase["id"], "worker_A", from_states=["awaiting_approval"], to_state="completed"
    )
    assert done is not None
    assert done["claimed_by"] is None and done["buyer_email"] is None


async def test_the_unfenced_form_still_works_for_callers_that_hold_nothing():
    """`holder=None` must leave the statement unfenced — the route that starts a purchase holds
    no lease, and a fence it could not satisfy would make the rail unusable."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="quoting")
    assert await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    ) is not None


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


# Written out here, NOT imported from the module — see the same list in the SQLite arm for why
# an intersection against the module's own set could not notice that set shrinking.
_EXPECTED_PUBLIC_COLUMNS = {
    "id", "state", "merchant_domain", "product_key", "variant_key", "product_name",
    "variant_title", "brand", "category", "quantity", "currency", "our_price_minor",
    "quoted_total_minor", "final_total_minor", "shipping_minor", "tax_minor", "hosted_url",
    "hosted_url_expires_at", "reap_quote_expires_at", "reap_order_id", "refusal_reason",
    "last_error_code", "created_at", "updated_at", "terminal_at",
}


async def test_owner_reads_contain_exactly_the_public_columns_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    got = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    listed = await ledger.list_purchases_for_owner("agent_one", "hash_alice")
    for view in (got, listed[0]):
        assert set(view) == _EXPECTED_PUBLIC_COLUMNS, (
            f"unexpected: {sorted(set(view) - _EXPECTED_PUBLIC_COLUMNS)}; "
            f"missing: {sorted(_EXPECTED_PUBLIC_COLUMNS - set(view))}"
        )
    full = await ledger.get_purchase_for_owner(
        purchase["id"], "agent_one", "hash_alice", include_private=True
    )
    assert full["buyer_email"] == "alice@example.test"


async def test_a_column_added_later_does_not_appear_in_the_view_on_postgres():
    """The allowlist's defining property, through a real ALTER TABLE."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    await database.execute(
        "ALTER TABLE reap_agentic_purchases ADD COLUMN internal_secret_note TEXT"
    )
    await database.execute(
        "UPDATE reap_agentic_purchases SET internal_secret_note = 'do not leak' WHERE id = :i",
        {"i": purchase["id"]},
    )
    internal = await ledger.get_purchase_internal(purchase["id"])
    assert internal["internal_secret_note"] == "do not leak"

    got = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    assert "internal_secret_note" not in got
    assert set(got) == _EXPECTED_PUBLIC_COLUMNS


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
    assert (await ledger.get_purchase_internal(purchase["id"]))["claimed_by"] in\
        ("worker_a", "worker_b")


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
    assert reclaimed[0]["claimed_by"] == "worker_b"


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
            _run_on(conn_a, ledger._EXPIRE_OVERDUE_SQL, _sweep_params()),
            _run_on(conn_b, ledger._EXPIRE_OVERDUE_SQL, _sweep_params()),
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
    assert await ledger.fail_exhausted_purchases(5, include_processing=True) == [purchase["id"]]
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
            _run_on(conn_a, ledger._FAIL_EXHAUSTED_SQL, _fail_params()),
            _run_on(conn_b, ledger._FAIL_EXHAUSTED_SQL, _fail_params()),
        )
    finally:
        await conn_a.close()
        await conn_b.close()
    assert len(rows_a) + len(rows_b) == 1


@pytest.mark.parametrize(
    "sql_name,target",
    [("_EXPIRE_OVERDUE_SQL", "expired"), ("_FAIL_EXHAUSTED_SQL", "failed")],
)
async def test_the_bulk_sweeps_only_use_legal_edges_on_postgres(sql_name, target):
    """FINDING 4. The state list is parsed OUT OF THE FINAL SQL STRING, so this checks the edges
    the database will really take. While the tuple and the SQL were two separate things, adding
    'processing' to the expire sweep's IN list — expiring a purchase the buyer had already
    APPROVED — survived both dialects."""
    import db.reap_agentic_ledger as ledger

    sources = ledger._states_in(getattr(ledger, sql_name), "WHERE state IN (")
    assert sources
    for src in sources:
        assert src in ledger.PURCHASE_STATES
        assert target in ledger.ALLOWED_TRANSITIONS[src], (
            f"{sql_name} would move {src!r} -> {target!r}, which ALLOWED_TRANSITIONS forbids"
        )


async def test_the_expire_sweep_never_touches_an_approved_purchase_on_postgres():
    """A purchase in 'processing' has been APPROVED BY THE BUYER and is settling. Expiring it
    abandons a payment in flight."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="processing")
    await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)
    await _set_clock_column(
        purchase["id"], "state_entered_at", "CURRENT_TIMESTAMP - INTERVAL '99999 seconds'"
    )
    assert await ledger.expire_overdue_purchases(max_age_seconds=60) == []
    assert await _state_of(purchase["id"]) == "processing"


# ── P2-1 / P2-2: bounded sweeps and the absolute age fallback ────────────────────────────────


async def test_expire_is_bounded_by_limit_on_postgres():
    """Prod and staging share one Postgres. An unbounded UPDATE on first arming locks every
    qualifying row at once."""
    import db.reap_agentic_ledger as ledger

    ids = []
    for index in range(5):
        purchase = await _mk(state="awaiting_approval", buyer_ref=f"bref_{index}")
        await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)
        ids.append(purchase["id"])

    first = await ledger.expire_overdue_purchases(limit=2)
    assert len(first) == 2
    rest = await ledger.expire_overdue_purchases(limit=50)
    assert len(rest) == 3
    assert sorted(first + rest) == sorted(ids)
    assert await ledger.expire_overdue_purchases(limit=50) == []


async def test_fail_exhausted_is_bounded_by_limit_on_postgres():
    from db.database import database
    import db.reap_agentic_ledger as ledger

    ids = []
    for index in range(4):
        purchase = await _mk(state="processing", buyer_ref=f"bref_{index}")
        await database.execute(
            "UPDATE reap_agentic_purchases SET attempts = 9 WHERE id = :i", {"i": purchase["id"]}
        )
        ids.append(purchase["id"])
    first = await ledger.fail_exhausted_purchases(5, limit=2, include_processing=True)
    assert len(first) == 2
    rest = await ledger.fail_exhausted_purchases(5, limit=50, include_processing=True)
    assert sorted(first + rest) == sorted(ids)


@pytest.mark.parametrize("bad", [0, -1, 501])
async def test_both_sweeps_refuse_an_out_of_range_limit_on_postgres(bad):
    import db.reap_agentic_ledger as ledger

    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(limit=bad)
    with pytest.raises(ValueError):
        await ledger.fail_exhausted_purchases(5, limit=bad)


@pytest.mark.parametrize("state", ["needs_enrollment", "awaiting_approval"])
async def test_a_row_with_no_hosted_deadline_is_expired_on_absolute_age_on_postgres(state):
    """P2-2. Reproduced before the fix: with `hosted_url_expires_at` NULL the row was expired by
    NOTHING and kept the buyer's address and email indefinitely, while the sweep's docstring
    called itself a PII deadline."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state=state)
    assert purchase["hosted_url_expires_at"] is None
    await _set_clock_column(
        purchase["id"], "state_entered_at", "CURRENT_TIMESTAMP - INTERVAL '7200 seconds'"
    )
    assert await ledger.expire_overdue_purchases(max_age_seconds=3600) == [purchase["id"]]
    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None


async def test_a_young_row_with_no_hosted_deadline_is_left_alone_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="awaiting_approval")
    assert await ledger.expire_overdue_purchases(max_age_seconds=3600) == []
    assert await _state_of(purchase["id"]) == "awaiting_approval"


@pytest.mark.parametrize("bad", [0, 59, -1])
async def test_expire_refuses_a_max_age_under_a_minute_on_postgres(bad):
    import db.reap_agentic_ledger as ledger

    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(max_age_seconds=bad)


# ── P2-3 / boundary guards, Postgres twins ───────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["resolving", "quoting", "processing"])
async def test_a_claim_in_a_working_state_increments_attempts_on_postgres(state):
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state=state)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert claimed[0]["attempts"] == 1


@pytest.mark.parametrize("state", ["awaiting_approval", "needs_enrollment"])
async def test_a_claim_in_a_waiting_state_does_not_increment_attempts_on_postgres(state):
    """P2-3. A buyer taking an hour on the partner's page, polled every 30 s, would otherwise
    reach ~120 attempts on a purchase where nothing has gone wrong."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state=state)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    for _ in range(3):
        await ledger.claim_due_purchases("worker_a", limit=5)
        await ledger.release_claim(purchase["id"], "worker_a")
    assert (await ledger.get_purchase_internal(purchase["id"]))["attempts"] == 0


async def test_fail_exhausted_takes_a_row_at_exactly_max_attempts_on_postgres():
    """M22. `>=` vs `>` grants every stuck purchase one extra claim."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    at_bound = await _mk(state="processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 5 WHERE id = :i", {"i": at_bound["id"]}
    )
    assert await ledger.fail_exhausted_purchases(5, include_processing=True) == [at_bound["id"]]


async def test_the_candidate_select_does_not_offer_already_claimed_rows_on_postgres():
    """M38. The claim re-checks it, so dropping it from the SELECT keeps the answer correct and
    makes every worker burn its whole limit on rows somebody else holds."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    held = await _make_due()
    free = await _make_due(buyer_ref="bref_free")
    await _set_clock_column(
        held["id"], "next_poll_at", "CURRENT_TIMESTAMP - INTERVAL '600 seconds'"
    )
    claimed = await ledger.claim_due_purchases("worker_a", limit=1)
    assert [c["id"] for c in claimed] == [held["id"]]

    offered = {
        r["id"]
        for r in await database.fetch_all(ledger._SELECT_DUE_PURCHASES_SQL, {"limit": 50})
    }
    assert free["id"] in offered and held["id"] not in offered


@pytest.mark.parametrize("bad", [0, -1])
async def test_create_refuses_a_non_positive_quantity_on_postgres(bad):
    """M39 — was killed on SQLite only."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    with pytest.raises(ValueError):
        await ledger.create_purchase(
            buyer_ref="b", agent_id="a", agent_user_ref_hash="h", quantity=bad
        )
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


@pytest.mark.parametrize("field", ["buyer_ref", "agent_id", "agent_user_ref_hash"])
@pytest.mark.parametrize("bad", [None, "", "   "])
async def test_create_refuses_blank_ownership_on_postgres(field, bad):
    """M06 — was killed on SQLite only. A row with NULL ownership is invisible to its own owner,
    because the conjunct compares with `=` and NULL = NULL is NULL."""
    import db.reap_agentic_ledger as ledger

    kwargs = dict(buyer_ref="b", agent_id="a", agent_user_ref_hash="h")
    kwargs[field] = bad
    with pytest.raises(ValueError):
        await ledger.create_purchase(**kwargs)


@pytest.mark.parametrize("bad", ["424", "42424", "abcd", ""])
async def test_activation_refuses_a_bad_card_last4_on_postgres(bad):
    """M40 — was killed on SQLite only. card_last4 is the ONLY digits of the card that may ever
    be stored, so the shape of it is not a formatting preference."""
    import db.reap_agentic_ledger as ledger

    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    with pytest.raises(ValueError):
        await ledger.mark_enrollment_active(enrollment["id"], card_last4=bad)


async def test_reactivating_the_already_active_row_does_not_demote_itself_on_postgres():
    """M29 — was killed on SQLite only. Without `id <> :id` the demotion kills the very row it is
    about to promote, and an idempotent retry becomes a de-enrolment."""
    import db.reap_agentic_ledger as ledger

    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    again = await ledger.mark_enrollment_active(live["id"], card_network="visa")
    assert again is not None and again["status"] == "active"
    assert (await ledger.get_active_enrollment("bref_alice"))["id"] == live["id"]


@pytest.mark.parametrize("state", ["needs_enrollment", "awaiting_approval"])
async def test_a_poll_loop_cannot_postpone_the_absolute_expiry_on_postgres(state):
    """ROUND 3, P1. The fallback used to measure `updated_at`, which every claim, release and
    requeue writes — so the poll loop reset the deadline every cycle and at a 30-second cadence
    it NEVER fired, on exactly the rows with no other bound."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    if state == "needs_enrollment":
        moved = await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="needs_enrollment"
        )
    else:
        await ledger.transition(purchase["id"], from_states=["resolving"], to_state="quoting")
        moved = await ledger.transition(
            purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
        )
    assert moved["state"] == state and moved["hosted_url_expires_at"] is None

    await _set_clock_column(
        purchase["id"], "state_entered_at", "CURRENT_TIMESTAMP - INTERVAL '99999 seconds'"
    )
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)

    for _ in range(3):
        await ledger.claim_due_purchases("worker_a", limit=5)
        await ledger.release_claim(purchase["id"], "worker_a")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(purchase["id"], 99999)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)

    assert await ledger.expire_overdue_purchases(max_age_seconds=60) == [purchase["id"]]
    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["terminal_at"] is not None and row["claimed_by"] is None


async def test_the_poll_loop_does_not_touch_state_entered_at_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    before = (await ledger.get_purchase_internal(purchase["id"]))["state_entered_at"]

    await ledger.claim_due_purchases("worker_a", limit=5)
    await ledger.release_claim(purchase["id"], "worker_a")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(purchase["id"], 99999)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)

    after = await ledger.get_purchase_internal(purchase["id"])
    assert after["state_entered_at"] == before
    assert after["updated_at"] > before


async def test_a_transition_resets_state_entered_at_on_postgres():
    """The column is per-STATE, not per-row: a purchase that spent an hour quoting gets a fresh
    deadline the moment it reaches 'awaiting_approval'. This is why it is not `created_at`."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    await _set_clock_column(
        purchase["id"], "state_entered_at", "CURRENT_TIMESTAMP - INTERVAL '99999 seconds'"
    )
    old = (await ledger.get_purchase_internal(purchase["id"]))["state_entered_at"]
    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting"
    )
    assert moved["state_entered_at"] > old

    await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    )
    await _set_clock_column(
        purchase["id"], "created_at", "CURRENT_TIMESTAMP - INTERVAL '99999 seconds'"
    )
    assert await ledger.expire_overdue_purchases(max_age_seconds=60) == []


async def test_fail_exhausted_skips_processing_unless_asked_on_postgres():
    """A purchase in 'processing' has been approved by the buyer and its payment is in flight;
    auto-failing it on an attempt counter writes a terminal state over a charge whose outcome we
    do not know."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    processing = await _mk(state="processing")
    quoting = await _mk(state="quoting", buyer_ref="bref_q")
    for row_id in (processing["id"], quoting["id"]):
        await database.execute(
            "UPDATE reap_agentic_purchases SET attempts = 9 WHERE id = :i", {"i": row_id}
        )

    assert await ledger.fail_exhausted_purchases(5) == [quoting["id"]]
    assert await _state_of(processing["id"]) == "processing"
    assert await ledger.fail_exhausted_purchases(5, include_processing=True) == [
        processing["id"]
    ]


@pytest.mark.parametrize("bad", [True, 2.9, "5", None, b"5"])
async def test_sweep_bounds_refuse_non_integers_on_postgres(bad):
    import db.reap_agentic_ledger as ledger

    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(limit=bad)
    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(max_age_seconds=bad)
    with pytest.raises(ValueError):
        await ledger.fail_exhausted_purchases(bad)
    with pytest.raises(ValueError):
        await ledger.requeue_stale_claims(lease_seconds=bad)


@pytest.mark.parametrize("bad", [b"worker", 7, "", "   "])
async def test_worker_ids_must_be_strings_on_postgres(bad):
    """`bytes` reached asyncpg as a DataError naming a parameter index; now it is a ValueError
    naming the parameter."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    with pytest.raises(ValueError):
        await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="quoting", holder=bad
        )
    with pytest.raises(ValueError):
        await ledger.claim_due_purchases(bad, limit=5)


async def test_the_module_opens_no_database_transactions_on_postgres():
    """P1-2's structural half, asserted here too because this is the dialect where the hazard was
    measured. An AST walk, not a grep — the docstrings discuss `database.transaction()` by name."""
    import ast

    tree = ast.parse(
        (Path(__file__).resolve().parent.parent / "db/reap_agentic_ledger.py").read_text("utf-8")
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transaction"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "database"
    ]
    assert not calls, f"database.transaction() at line(s) {[c.lineno for c in calls]}"


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


async def test_a_blocked_activation_resolves_without_raising_at_the_caller():
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

    # WITH (b) RUNNING FIRST THE OUTCOME CHANGED, AND IMPROVED. Ours blocks on the index, the
    # other side commits, ours raises the unique violation — and because that violation PROVES
    # our target is activatable, we demote theirs and retry, which succeeds. "Re-enrolment
    # demotes the previous card" is exactly what this function means, so winning here is correct.
    # What matters is that no driver exception escaped and the invariant held throughout.
    assert result is not None, "a unique violation must be handled, never raised at the caller"
    assert result["id"] == mine["id"] and result["status"] == "active"

    actives = await database.fetch_all(
        "SELECT id FROM reap_agentic_enrollments WHERE buyer_ref = :b AND status = 'active'",
        {"b": "bref_race"},
    )
    assert [r["id"] for r in actives] == [mine["id"]], "exactly one active row, and it is ours"
    theirs = await database.fetch_one(
        "SELECT status FROM reap_agentic_enrollments WHERE id = 're_theirs'"
    )
    assert theirs["status"] == "dead", "the superseded row is demoted, not left active"


# ── FINDING 2: concurrent activation on the SHARED `database` object ─────────────────────────


async def test_two_buyers_activating_at_once_on_the_shared_database_both_succeed():
    """FINDING 2, THE DEFECT IN ITS PUREST FORM. Two UNRELATED buyers, one `asyncio.gather` on
    the app's shared `database` — which is how a FastAPI process actually reaches this code.

    With `database.transaction()` in place this was measured 5 runs out of 5 on real Postgres:
    one task raised TransactionEndedOutOfOrder AND ITS WRITE WAS ROLLED BACK, the other raised
    NoActiveSQLTransactionError for a write that DID commit. databases==0.7.0 scopes its
    connection by ContextVar, so gathered tasks end up inside each other's transaction. Two
    unrelated buyers, and one of them loses an enrollment they completed while the other is told
    an error about a write that worked.

    With no transaction, both are two autocommit statements that interleave harmlessly."""
    import db.reap_agentic_ledger as ledger

    a = await ledger.upsert_pending_enrollment(buyer_ref="buyer_one")
    b = await ledger.upsert_pending_enrollment(buyer_ref="buyer_two")

    async def activate(enrollment_id, ref):
        return await ledger.mark_enrollment_active(enrollment_id, reap_enrollment_id=ref)

    results = await asyncio.gather(
        activate(a["id"], "enr_one"),
        activate(b["id"], "enr_two"),
        return_exceptions=True,
    )
    raised = [r for r in results if isinstance(r, BaseException)]
    assert not raised, f"concurrent activation for two different buyers raised: {raised}"

    assert (await ledger.get_active_enrollment("buyer_one"))["id"] == a["id"]
    assert (await ledger.get_active_enrollment("buyer_two"))["id"] == b["id"]


async def test_same_buyer_two_targets_at_once_on_the_shared_database_leaves_one_active():
    """The same hazard aimed at ONE buyer. Measured with the transaction in place: one call
    raised, the other returned None, and NEITHER row was activated — a buyer who enrolled ending
    up with no active card and no error anyone could act on.

    Now: nothing raises, exactly one row is active, and the loser is still 'pending', which means
    retryable rather than silently lost."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    first = await ledger.upsert_pending_enrollment(buyer_ref="buyer_same")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_second', 'buyer_same', 'pending')"
    )

    results = await asyncio.gather(
        ledger.mark_enrollment_active(first["id"], reap_enrollment_id="enr_a"),
        ledger.mark_enrollment_active("re_second", reap_enrollment_id="enr_b"),
        return_exceptions=True,
    )
    raised = [r for r in results if isinstance(r, BaseException)]
    assert not raised, f"concurrent activation for one buyer raised: {raised}"

    actives = await database.fetch_all(
        "SELECT id FROM reap_agentic_enrollments WHERE buyer_ref = :b AND status = 'active'",
        {"b": "buyer_same"},
    )
    assert len(actives) == 1, f"exactly one active row per buyer; got {[r['id'] for r in actives]}"

    winner = actives[0]["id"]
    loser = "re_second" if winner == first["id"] else first["id"]
    loser_status = await database.fetch_val(
        "SELECT status FROM reap_agentic_enrollments WHERE id = :i", {"i": loser}
    )
    assert loser_status in ("pending", "dead"), (
        f"the loser must be retryable ('pending') or deliberately demoted ('dead'), not "
        f"{loser_status!r}"
    )
    # And whichever call lost answered None rather than raising — asserted above by `not raised`.
    assert None in results or all(r is not None for r in results)


async def test_the_same_target_activated_twice_at_once_returns_the_active_row_both_times():
    """Idempotency under concurrency: a redelivered webhook racing its own original."""
    import db.reap_agentic_ledger as ledger

    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="buyer_dup")
    results = await asyncio.gather(
        ledger.mark_enrollment_active(enrollment["id"], reap_enrollment_id="enr_x"),
        ledger.mark_enrollment_active(enrollment["id"], reap_enrollment_id="enr_x"),
        return_exceptions=True,
    )
    raised = [r for r in results if isinstance(r, BaseException)]
    assert not raised, f"idempotent re-activation raised: {raised}"
    assert all(r is not None and r["status"] == "active" for r in results)
    assert (await ledger.get_active_enrollment("buyer_dup"))["id"] == enrollment["id"]


async def test_activating_a_dead_row_with_no_competitor_stays_dead_on_postgres():
    """FINDING 5. With a competing active row the old test passed for the wrong reason — the
    unique index raised and the except returned None. With NO competitor there is no index to
    save us, and `status IN ('pending','active')` is the only thing between
    `mark_enrollment_dead` and a revoked card coming back."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="buyer_solo")
    await ledger.mark_enrollment_dead(enrollment["id"], reap_status="REVOKED")

    assert await ledger.mark_enrollment_active(
        enrollment["id"], reap_enrollment_id="enr_zombie"
    ) is None

    row = await database.fetch_one(
        "SELECT status, reap_enrollment_id FROM reap_agentic_enrollments WHERE id = :i",
        {"i": enrollment["id"]},
    )
    assert row["status"] == "dead"
    assert row["reap_enrollment_id"] is None
    assert await ledger.get_active_enrollment("buyer_solo") is None


async def test_a_dead_target_between_a_and_b_no_longer_strands_the_buyer():
    """ROUND 3, P2. The reviewer's interleaving, driven through the module's OWN statements.

    With (a) running first, a concurrent `mark_enrollment_dead(target)` landing between the two
    left BOTH rows dead — the buyer with zero active enrollments and no retry able to heal it,
    because the target was now permanently unactivatable. Reproduced on both dialects.

    With (b) first, the same interleaving is inert: (b) returns no row, so the demotion never
    runs and the live enrollment is untouched."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_interleave")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_target', 'bref_interleave', 'pending')"
    )

    # (b) first — and the target dies right before it, which is the worst moment.
    await ledger.mark_enrollment_dead("re_target")
    result = await ledger.mark_enrollment_active("re_target", reap_enrollment_id="enr_target")

    assert result is None
    still = await ledger.get_active_enrollment("bref_interleave")
    assert still is not None and still["id"] == live["id"], (
        "the buyer's live enrollment must survive an activation of a target that died — the "
        "demotion may not run on behalf of a promotion that cannot happen"
    )
    statuses = {
        r["id"]: r["status"]
        for r in await database.fetch_all(
            "SELECT id, status FROM reap_agentic_enrollments WHERE buyer_ref = :b",
            {"b": "bref_interleave"},
        )
    }
    assert statuses[live["id"]] == "active" and statuses["re_target"] == "dead"


class _KillTargetAfterFirstStatement:
    """A `database` stand-in that marks `target` dead right after the FIRST statement
    `mark_enrollment_active` issues, then delegates. The two statements run back-to-back inside
    the function, so the concurrent `mark_enrollment_dead(target)` has to be injected between
    them rather than raced."""

    def __init__(self, real, target):
        self._real = real
        self._target = target
        self._fired = False

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def _kill_once(self):
        if self._fired:
            return
        self._fired = True
        await self._real.execute(
            "UPDATE reap_agentic_enrollments SET status = 'dead' WHERE id = :i",
            {"i": self._target},
        )

    async def fetch_one(self, sql, values=None):
        try:
            return await self._real.fetch_one(sql, values)
        finally:
            await self._kill_once()

    async def fetch_all(self, sql, values=None):
        try:
            return await self._real.fetch_all(sql, values)
        finally:
            await self._kill_once()


async def test_a_concurrent_dead_between_the_two_statements_cannot_strand_the_buyer_on_postgres():
    """ROUND 3, P2 — THE ORDERING, PROVEN BY INTERLEAVING, on the dialect with the real index.

    (a)-first: the demotion sees a still-pending target, passes its gate, kills the buyer's live
    enrollment; the kill lands; (b) finds nothing to activate → ZERO active rows, unrecoverable.
    (b)-first: (b) raises the unique violation, the kill lands, and the demotion's gate now sees a
    DEAD target and demotes nothing → the live enrollment survives.

    Both orders pass every single-call test, because the gate already covers a target that was
    dead BEFORE the call. Only this interleaving separates them."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_interleave2")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_target2', 'bref_interleave2', 'pending')"
    )

    original = ledger.database
    ledger.database = _KillTargetAfterFirstStatement(original, "re_target2")
    try:
        result = await ledger.mark_enrollment_active(
            "re_target2", reap_enrollment_id="enr_target"
        )
    finally:
        ledger.database = original

    assert result is None
    still = await ledger.get_active_enrollment("bref_interleave2")
    assert still is not None and still["id"] == live["id"], (
        "the buyer's live enrollment must survive a target that died mid-flight"
    )
    statuses = {
        r["id"]: r["status"]
        for r in await database.fetch_all(
            "SELECT id, status FROM reap_agentic_enrollments WHERE buyer_ref = :b",
            {"b": "bref_interleave2"},
        )
    }
    assert statuses == {live["id"]: "active", "re_target2": "dead"}, statuses


async def test_the_demotion_does_not_run_when_the_target_is_not_activatable():
    """The mechanism behind the test above, asserted directly: statement (a) is never reached
    when (b) finds nothing to promote."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_gate")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    before = await database.fetch_val(
        "SELECT updated_at FROM reap_agentic_enrollments WHERE id = :i", {"i": live["id"]}
    )

    assert await ledger.mark_enrollment_active("re_does_not_exist") is None

    after = await database.fetch_val(
        "SELECT updated_at FROM reap_agentic_enrollments WHERE id = :i", {"i": live["id"]}
    )
    assert after == before, "the live row was written to on behalf of a target that cannot exist"


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


# ═════════════════════════════════════════════════════════════════════════════════════════════
# WP2c — migration 225, on REAL Postgres
# ═════════════════════════════════════════════════════════════════════════════════════════════
#
# WHAT ONLY THIS ARM CAN PROVE, on top of the row-level semantics the SQLite file covers:
#
#   * the hint columns really are `jsonb` in the shipped schema, and the driver hands their
#     contents back as TEXT (property 2) — so the module's decode is doing real work here and
#     deleting it produces a resolver iterating the characters of a JSON string;
#   * the SELF-HEAL builds the same schema as the migration THROUGH THE CATALOG, now including
#     the three mig-225 columns, on a database built the way production builds it;
#   * the self-heal's ALTER lands on a database that already carries the 224 shape — the state
#     every environment that deployed 224 is actually in, and the one an empty-database test
#     cannot distinguish from the ALTER being deleted;
#   * every new SQL constant PREPAREs. `_SELECT_ENROLLMENT_BY_REAP_ID_SQL` in particular binds a
#     parameter used only in one comparison, which is exactly the shape that has produced
#     AmbiguousParameter on this rail before.


_HINT_COLUMNS = ("accept_variant_labels", "also_accept_domains", "market_country")


async def _purchase_columns() -> set:
    from db.database import database

    rows = await database.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'reap_agentic_purchases'"
    )
    return {r["column_name"] for r in rows}


async def _apply_224_only():
    """Build the table EXACTLY as migration 224 left it — the shape every environment that
    deployed 224 is running — by applying that one file and nothing after it."""
    from db.database import database
    from db.sql_migrations import split_statements

    await _drop_tables()
    for statement in split_statements(
        (_MIGRATIONS_DIR / "224_reap_agentic_ledger.sql").read_text(encoding="utf-8")
    ):
        await database.execute(statement)
    assert database  # the fixture's connection is what ran those


# ── 225.1 the hint columns, through the driver ───────────────────────────────────────────────


async def test_the_hint_columns_really_are_jsonb_and_text_in_the_shipped_schema():
    from db.database import database

    rows = await database.fetch_all(
        """
        SELECT column_name, data_type FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'reap_agentic_purchases'
           AND column_name IN ('accept_variant_labels', 'also_accept_domains', 'market_country')
        """
    )
    types = {r["column_name"]: r["data_type"] for r in rows}
    assert types == {
        "accept_variant_labels": "jsonb",
        "also_accept_domains": "jsonb",
        "market_country": "text",
    }, types


async def test_the_hints_come_back_as_text_from_the_driver_and_as_lists_from_the_module():
    """THE DECODE, WITH A CONTROL, on the engine that actually has the problem. Asserting only
    that the read is a list would pass on a driver that never had it; asserting only that the raw
    value is a str would pass with the decode deleted. Both halves, so dropping these columns from
    `_JSON_COLUMNS` dies here."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(
        accept_variant_labels=["Nude Glow", "nude-glow"],
        also_accept_domains=["shop.brand.example"],
        market_country="US",
    )
    raw = await database.fetch_val(
        "SELECT accept_variant_labels FROM reap_agentic_purchases WHERE id = :i",
        {"i": purchase["id"]},
    )
    assert isinstance(raw, str), (
        "a raw-SQL jsonb column is expected to arrive as text on asyncpg — if that stops being "
        "true the module's decode needs re-checking, not deleting"
    )

    for row in (purchase, await ledger.get_purchase_internal(purchase["id"])):
        assert isinstance(row["accept_variant_labels"], list), (
            "the raw JSON string reached the caller; a resolver iterating it would iterate its "
            "CHARACTERS and accept nothing"
        )
        assert row["accept_variant_labels"] == ["Nude Glow", "nude-glow"]
        assert row["also_accept_domains"] == ["shop.brand.example"]
        assert row["market_country"] == "US"


async def test_hints_default_to_null_on_postgres():
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk()
    read = await ledger.get_purchase_internal(purchase["id"])
    for name in _HINT_COLUMNS:
        assert read[name] is None
        assert await database.fetch_val(
            f"SELECT {name} FROM reap_agentic_purchases WHERE id = :i", {"i": purchase["id"]}
        ) is None


@pytest.mark.parametrize("empty", [[], ()])
async def test_an_empty_hint_list_is_null_on_postgres(empty):
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(accept_variant_labels=empty, also_accept_domains=empty)
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] is None
    assert read["also_accept_domains"] is None


async def test_the_stored_hints_are_queryable_as_jsonb_not_as_a_string():
    """A jsonb column that had silently become text would still round-trip through the module's
    own encode/decode. This asks the DATABASE to read the value as JSON, which text cannot do."""
    from db.database import database

    purchase = await _mk(accept_variant_labels=["Nude Glow", "nude-glow"])
    count = await database.fetch_val(
        "SELECT jsonb_array_length(accept_variant_labels) FROM reap_agentic_purchases "
        "WHERE id = :i",
        {"i": purchase["id"]},
    )
    assert count == 2


async def test_domains_are_lowercased_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(
        accept_variant_labels=["Nude Glow"], also_accept_domains=["Shop.BRAND.example"]
    )
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["also_accept_domains"] == ["shop.brand.example"]
    assert read["accept_variant_labels"] == ["Nude Glow"], "labels keep their case"


async def test_the_hints_survive_a_terminal_transition_on_postgres():
    """NOT PII: these name products and hostnames, not the buyer, so the terminal statement leaves
    them alone while it nulls shipping_address and buyer_email."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(
        accept_variant_labels=["Nude Glow"],
        also_accept_domains=["brand.example"],
        market_country="US",
    )
    await ledger.transition(purchase["id"], from_states=["resolving"], to_state="refused")
    done = await ledger.get_purchase_internal(purchase["id"])
    assert done["buyer_email"] is None and done["shipping_address"] is None
    assert done["accept_variant_labels"] == ["Nude Glow"]
    assert done["also_accept_domains"] == ["brand.example"]
    assert done["market_country"] == "US"


async def test_hints_cannot_be_revised_by_a_transition_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(accept_variant_labels=["Nude Glow"])
    for field in _HINT_COLUMNS:
        assert field not in ledger._TRANSITION_FIELDS
        with pytest.raises(TypeError):
            await ledger.transition(
                purchase["id"], from_states=["resolving"], to_state="quoting", **{field: ["x"]}
            )


# ── 225.2 validation, Postgres twins ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["not-a-list", b"bytes", 123, {"a": 1}, [""], ["   "], [None], [123], ["x"] * 33,
     ["x" * 129],
     # Control characters. The NUL case is the one that MATTERS on this dialect: before the
     # check it reached asyncpg as a raw UntranslatableCharacterError naming a parameter index,
     # which is a 500 rather than a refusal and never says "accept_variant_labels".
     ["a\x00b"], ["a\nb"], ["a\rb"], ["a\tb"], ["\x1b[0m"], ["a\x7fb"]],
)
async def test_create_refuses_a_bad_accept_variant_labels_on_postgres(bad):
    with pytest.raises(ValueError):
        await _mk(accept_variant_labels=bad)


@pytest.mark.parametrize(
    "bad",
    ["brand.example", ["https://brand.example"], ["brand.example/path"], ["brand.example:443"],
     ["localhost"], ["brand..example"], ["-brand.example"], ["bra nd.example"], ["brand.123"],
     [""], ["x" * 250 + ".example"], ["a.example"] * 33,
     ["a\x00b.example"], ["a\nb.example"], ["\x1b[0m.example"]],
)
async def test_create_refuses_a_bad_also_accept_domains_on_postgres(bad):
    with pytest.raises(ValueError):
        await _mk(also_accept_domains=bad)


@pytest.mark.parametrize("bad", ["us", "USA", "U", "", "  ", "U5", 12, b"US", "Us", "US\n"])
async def test_create_refuses_a_bad_market_country_on_postgres(bad):
    with pytest.raises(ValueError):
        await _mk(market_country=bad)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"accept_variant_labels": ["", "ok"]},
        {"also_accept_domains": ["https://brand.example"]},
        {"market_country": "us"},
    ],
)
async def test_a_refused_hint_leaves_no_row_behind_on_postgres(kwargs):
    """REFUSED BEFORE THE INSERT. A row created and then rejected would hold the buyer's address
    and email in 'resolving' with nothing scheduled to terminate it — the PII deadline only
    starts once the row exists."""
    from db.database import database

    before = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases")
    with pytest.raises(ValueError):
        await _mk(**kwargs)
    after = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases")
    assert after == before, "a refused hint still wrote a purchase row"


@pytest.mark.parametrize(
    "good", ["brand.example", "shop.brand.example", "a-b.co.uk", "xn--80ak6aa92e.com"]
)
async def test_the_domain_shape_accepts_real_hostnames_on_postgres(good):
    """CONTROL: a validator that refused everything would pass every refusal test above."""
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(also_accept_domains=[good])
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["also_accept_domains"] == [good]


# ── 225.3 release_claim records WHY, on Postgres ─────────────────────────────────────────────


async def _claimed(**over):
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(**over)
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert [r["id"] for r in claimed] == [purchase["id"]], "precondition: the row was claimable"
    return purchase


async def test_release_records_the_error_code_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    released = await ledger.release_claim(
        purchase["id"], "worker_a", last_error_code="transport_error:readtimeout"
    )
    assert released is not None
    assert released["last_error_code"] == "transport_error:readtimeout"
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "transport_error:readtimeout"
    assert read["claimed_by"] is None


async def test_the_release_error_code_projects_through_the_allowlist_only_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="quote_expired")
    row = await ledger.get_purchase_internal(purchase["id"])
    view = ledger.public_purchase_view(row)
    assert set(view) == _EXPECTED_PUBLIC_COLUMNS
    for name in _HINT_COLUMNS:
        assert name not in view


async def test_release_without_a_code_keeps_the_previous_one_on_postgres():
    """COALESCE, not a bare assignment — the poller releases on every ordinary poll, so a bare
    assignment would erase the reason on the next uneventful tick."""
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="quote_expired")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await ledger.release_claim(purchase["id"], "worker_a")
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "quote_expired"


async def test_release_with_an_error_code_does_not_touch_state_entered_at_on_postgres():
    """THE MUTANT: adding `state_entered_at = clock_timestamp()` to the release statements, which
    looks like a consistency tidy-up and makes the PII deadline resettable by the poll loop."""
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed(state="resolving")
    await _set_clock_column(
        purchase["id"], "state_entered_at", "CURRENT_TIMESTAMP - INTERVAL '99999 seconds'"
    )
    before = (await ledger.get_purchase_internal(purchase["id"]))["state_entered_at"]

    await ledger.release_claim(
        purchase["id"], "worker_a", last_error_code="transport_error:readtimeout"
    )

    after = await ledger.get_purchase_internal(purchase["id"])
    assert after["state_entered_at"] == before, (
        "release moved state_entered_at — the PII deadline is now resettable by the poll loop"
    )
    assert after["updated_at"] >= before, "release must still bump updated_at"


async def test_a_release_that_records_a_code_still_lets_the_absolute_expiry_fire_on_postgres():
    """The behavioural half, through the sweep that matters. If the release stamped
    `state_entered_at`, this row would be young again and survive with its PII."""
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed(state="needs_enrollment")
    await _set_clock_column(
        purchase["id"], "state_entered_at", "CURRENT_TIMESTAMP - INTERVAL '99999 seconds'"
    )
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="enrollment_pending")

    moved = await ledger.expire_overdue_purchases(max_age_seconds=60)
    assert purchase["id"] in moved, (
        "the abandoned row survived its absolute deadline — release reset the clock"
    )
    expired = await ledger.get_purchase_internal(purchase["id"])
    assert expired["buyer_email"] is None and expired["shipping_address"] is None


async def test_the_release_statements_do_not_write_state_entered_at():
    """The shape assertion behind the two tests above, on both dialect twins, so the mutant is
    refused by a test that NAMES it."""
    import db.reap_agentic_ledger as ledger

    for name in ("_RELEASE_CLAIM_SQL", "_RELEASE_CLAIM_SQL_SQLITE"):
        sql = getattr(ledger, name)
        assert "state_entered_at" not in sql, f"{name} writes state_entered_at"
        assert "last_error_code = COALESCE(:last_error_code, last_error_code)" in sql
        assert "updated_at =" in sql


@pytest.mark.parametrize(
    "bad",
    ["TRANSPORT_ERROR", "transport error", "transport/error", "x" * 65, "", "   ",
     b"transport_error", 123, ["transport_error"], "transport_error\n"],
)
async def test_release_refuses_a_malformed_error_code_on_postgres(bad):
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    with pytest.raises(ValueError):
        await ledger.release_claim(purchase["id"], "worker_a", last_error_code=bad)


async def test_a_refused_error_code_releases_nothing_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    with pytest.raises(ValueError):
        await ledger.release_claim(purchase["id"], "worker_a", last_error_code="NOT VALID")
    still = await ledger.get_purchase_internal(purchase["id"])
    assert still["claimed_by"] == "worker_a"
    assert still["last_error_code"] is None


@pytest.mark.parametrize(
    "good", ["transport_error:readtimeout", "quote_expired", "a", "x" * 64, "err.4xx"]
)
async def test_the_error_code_shape_accepts_the_rails_vocabulary_on_postgres(good):
    """CONTROL for the refusals above."""
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    released = await ledger.release_claim(purchase["id"], "worker_a", last_error_code=good)
    assert released["last_error_code"] == good


async def test_release_by_the_wrong_worker_records_nothing_on_postgres():
    """The error-code write inherits the release's fence, across the SAME statement — a worker
    that has already lost the lease cannot stamp a reason onto somebody else's step."""
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    assert await ledger.release_claim(
        purchase["id"], "worker_b", last_error_code="not_my_step"
    ) is None
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] is None
    assert read["claimed_by"] == "worker_a"


async def test_a_second_connection_that_stole_the_lease_beats_our_release_on_postgres():
    """The fence across TWO BACKEND CONNECTIONS, which is the only place it really means
    anything: the SQLite arm serialises everything onto one connection. Worker A's lease ages
    out and is requeued, worker B claims the row — A's release, error code and all, must write
    NOTHING."""
    import db.reap_agentic_ledger as ledger

    purchase = await _claimed()
    await _age_claim(purchase["id"], 99999)
    assert await ledger.requeue_stale_claims(lease_seconds=300, limit=10) == 1

    conn = await _raw_connection()
    try:
        row = await _run_on(
            conn,
            ledger._CLAIM_PURCHASE_SQL,
            {"id": purchase["id"], "worker_id": "worker_b"},
        )
        assert row is not None, "precondition: the other connection took the lease"
    finally:
        await conn.close()

    assert await ledger.release_claim(
        purchase["id"], "worker_a", last_error_code="stale_worker"
    ) is None
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["claimed_by"] == "worker_b"
    assert read["last_error_code"] is None


async def test_release_claim_has_no_attempts_delta_on_postgres():
    import inspect

    import db.reap_agentic_ledger as ledger

    params = inspect.signature(ledger.release_claim).parameters
    assert "attempts_delta" not in params
    assert set(params) == {"purchase_id", "worker_id", "next_poll_at", "last_error_code"}

    purchase = await _claimed()
    before = (await ledger.get_purchase_internal(purchase["id"]))["attempts"]
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="quote_expired")
    assert (await ledger.get_purchase_internal(purchase["id"]))["attempts"] == before


# ── 225.4 the two enrollment reads, on Postgres ──────────────────────────────────────────────


_ENROLLMENT_PROJECTION = {
    "id", "buyer_ref", "reap_enrollment_id", "status", "hosted_url", "hosted_url_expires_at",
    "card_network", "card_last4",
}
_ENROLLMENT_NEVER_PROJECTED = {"agent_id", "reap_status", "created_at", "updated_at"}


async def test_get_enrollment_internal_reads_a_pending_row_on_postgres():
    import db.reap_agentic_ledger as ledger

    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", reap_enrollment_id="enr_1", hosted_url="https://hosted.example/a"
    )
    read = await ledger.get_enrollment_internal(created["id"])
    assert read is not None
    assert read["id"] == created["id"]
    assert read["status"] == "pending"
    assert read["reap_enrollment_id"] == "enr_1"


async def test_get_enrollment_internal_writes_nothing_on_postgres():
    """The whole reason it exists: the workaround it replaces is `upsert_pending_enrollment`, a
    WRITE, which bumps `updated_at` and can mint a stray pending row."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    created = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    before = await database.fetch_all(
        "SELECT id, updated_at FROM reap_agentic_enrollments ORDER BY id"
    )
    await ledger.get_enrollment_internal(created["id"])
    await ledger.get_enrollment_internal("re_missing")
    after = await database.fetch_all(
        "SELECT id, updated_at FROM reap_agentic_enrollments ORDER BY id"
    )
    assert [dict(r) for r in after] == [dict(r) for r in before]


@pytest.mark.parametrize("status", ["pending", "active", "dead"])
async def test_get_enrollment_internal_reads_every_status_on_postgres(status):
    import db.reap_agentic_ledger as ledger

    created = await ledger.upsert_pending_enrollment(buyer_ref=f"bref_{status}")
    if status == "active":
        await ledger.mark_enrollment_active(created["id"], card_network="visa", card_last4="4242")
    elif status == "dead":
        await ledger.mark_enrollment_dead(created["id"])
    read = await ledger.get_enrollment_internal(created["id"])
    assert read["status"] == status


async def test_the_enrollment_reads_carry_exactly_the_projection_on_postgres():
    """An EQUALITY against a literal written in this file — `>=` would pass a projection that
    quietly grew `agent_id` or the partner's raw `reap_status`."""
    import db.reap_agentic_ledger as ledger

    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice",
        agent_id="agent_one",
        reap_enrollment_id="enr_p",
        reap_status="upstream_words",
    )
    for row in (
        await ledger.get_enrollment_internal(created["id"]),
        await ledger.get_enrollment_by_reap_id("enr_p"),
    ):
        assert set(row) == _ENROLLMENT_PROJECTION, (
            f"unexpected: {sorted(set(row) - _ENROLLMENT_PROJECTION)}; "
            f"missing: {sorted(_ENROLLMENT_PROJECTION - set(row))}"
        )
        assert not (set(row) & _ENROLLMENT_NEVER_PROJECTED)


async def test_get_enrollment_by_reap_id_finds_the_row_on_postgres():
    import db.reap_agentic_ledger as ledger

    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", reap_enrollment_id="enr_webhook"
    )
    read = await ledger.get_enrollment_by_reap_id("enr_webhook")
    assert read is not None and read["id"] == created["id"]


async def test_the_by_reap_id_STATEMENT_does_not_wildcard_on_a_null_bind_on_postgres():
    """EVERY pending row has a NULL `reap_enrollment_id`, and on Postgres `NULL = NULL` is NULL
    rather than false — which is what makes this a no-match instead of an arbitrary buyer's
    enrollment handed to a webhook receiver.

    RUN AGAINST THE STATEMENT, not through the function: `_require_lookup_id` now refuses None
    before the query, so a test that called the function would pass with the SQL rewritten to
    `(:reap_enrollment_id IS NULL OR …)` — the Python guard would hide the SQL one."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    await ledger.upsert_pending_enrollment(buyer_ref="bref_one")
    await ledger.upsert_pending_enrollment(buyer_ref="bref_two")
    rows = await database.fetch_all(
        ledger._SELECT_ENROLLMENT_BY_REAP_ID_SQL, {"reap_enrollment_id": None}
    )
    assert rows == [], (
        "the by-reap-id statement matched rows on a NULL bind — every pending enrollment in the "
        "table is reachable by anyone who can reach this read"
    )


async def test_get_enrollment_by_reap_id_is_none_for_an_unknown_id_on_postgres():
    import db.reap_agentic_ledger as ledger

    await ledger.upsert_pending_enrollment(buyer_ref="bref_one")
    assert await ledger.get_enrollment_by_reap_id("enr_unknown") is None


@pytest.mark.parametrize("bad", [None, "", "   ", 123, b"re_1", 1.5, ["re_1"]])
@pytest.mark.parametrize("reader", ["get_enrollment_internal", "get_enrollment_by_reap_id"])
async def test_the_enrollment_reads_refuse_a_non_string_or_blank_id_on_postgres(reader, bad):
    """On THIS dialect the unguarded versions raised a raw asyncpg DataError naming a parameter
    index — a 500 that never says which argument was wrong. One ValueError beats it."""
    import db.reap_agentic_ledger as ledger

    with pytest.raises(ValueError):
        await getattr(ledger, reader)(bad)


async def test_the_enrollment_reads_are_both_off_the_advertised_surface_on_postgres():
    """Both new reads are UNSCOPED and both hand back `hosted_url` — a live page on which a card
    can be enrolled. `get_enrollment_by_reap_id` is keyed on an identifier from OUTSIDE this
    system, so its caller must have authenticated that id first."""
    import db.reap_agentic_ledger as ledger

    assert "get_enrollment_internal" not in ledger.__all__
    assert "get_enrollment_by_reap_id" not in ledger.__all__
    assert "get_active_enrollment" in ledger.__all__


async def test_get_enrollment_by_reap_id_uses_the_unique_index_on_postgres():
    """At most one row can match, and the reason is an INDEX rather than a LIMIT: the statement
    has neither an ORDER BY nor a LIMIT, so a database that lost
    uq_reap_agentic_enrollments_reap_id would be visible rather than papered over."""
    import asyncpg
    from db.database import database
    import db.reap_agentic_ledger as ledger

    await ledger.upsert_pending_enrollment(buyer_ref="bref_a", reap_enrollment_id="enr_u")
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status, reap_enrollment_id) "
            "VALUES ('re_dup', 'bref_b', 'pending', 'enr_u')"
        )
    sql = ledger._SELECT_ENROLLMENT_BY_REAP_ID_SQL
    assert "LIMIT" not in sql.upper() and "ORDER BY" not in sql.upper()


async def test_get_enrollment_by_reap_id_survives_the_row_going_dead_on_postgres():
    import db.reap_agentic_ledger as ledger

    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", reap_enrollment_id="enr_dead"
    )
    await ledger.mark_enrollment_dead(created["id"])
    read = await ledger.get_enrollment_by_reap_id("enr_dead")
    assert read is not None and read["status"] == "dead"


# ── 225.5 the self-heal, through the catalog ─────────────────────────────────────────────────


async def test_the_parity_fingerprint_actually_covers_the_new_columns():
    """GUARD THE GUARD. `test_the_self_heal_builds_the_same_schema_as_the_migration` compares two
    fingerprints — and it would compare two fingerprints that BOTH omitted these columns just as
    happily. This asserts the columns are IN the thing being compared, so the parity test's pass
    is evidence about them rather than about their absence."""
    columns, _indexes, _checks = await _schema_fingerprint()
    named = {row[1] for row in columns if row[0] == "reap_agentic_purchases"}
    assert set(_HINT_COLUMNS) <= named, (
        f"the fingerprint does not mention {sorted(set(_HINT_COLUMNS) - named)} — the parity "
        "test cannot be proving anything about them"
    )


async def test_the_self_heal_builds_the_hint_columns_identically_to_the_migration():
    """The mig-225 slice of the catalog parity test, stated so a failure NAMES these columns
    rather than arriving as one line in a whole-table diff. THE MUTANT THIS KILLS: deleting the
    mig-225 ALTER from ONE schema_guard branch. On this dialect that is the Postgres branch, and
    the columns then exist in the migration build and not in the self-heal build."""
    from db.schema_guard import ensure_required_schema_light

    columns_m, _i, _c = await _schema_fingerprint()
    from_migration = {r[1]: r for r in columns_m if r[0] == "reap_agentic_purchases"}
    assert set(_HINT_COLUMNS) <= set(from_migration), "precondition: 225 is in the fixture"

    await _drop_tables()
    await ensure_required_schema_light()
    columns_s, _i, _c = await _schema_fingerprint()
    from_self_heal = {r[1]: r for r in columns_s if r[0] == "reap_agentic_purchases"}

    missing = set(_HINT_COLUMNS) - set(from_self_heal)
    assert not missing, (
        f"the self-heal did not build {sorted(missing)} — production, which never runs "
        "db/migrations, would not have them at all"
    )
    for name in _HINT_COLUMNS:
        assert from_self_heal[name] == from_migration[name], (
            f"{name} is built differently by the self-heal:\n"
            f"  migration: {from_migration[name]}\n  self-heal: {from_self_heal[name]}"
        )


async def test_the_self_heal_adds_the_hint_columns_to_a_224_shaped_database():
    """PATH TWO, AND THE ONE THAT MATTERS IN PRODUCTION. Every environment that deployed
    migration 224 already HAS this table, so `CREATE TABLE IF NOT EXISTS` is a no-op there and
    the ALTER is the whole of the heal. A test that only covered an empty database would pass
    with the ALTER deleted entirely."""
    from db.schema_guard import ensure_required_schema_light
    import db.reap_agentic_ledger as ledger

    await _apply_224_only()
    before = await _purchase_columns()
    assert not (set(_HINT_COLUMNS) & before), "precondition: this is the 224 shape"

    await ensure_required_schema_light()

    after = await _purchase_columns()
    # A 224-shaped database is also pre-226, so the same heal lands mig 226's two columns.
    assert after - before == set(_HINT_COLUMNS) | {"item_source", "cart_url"}, (
        f"the heal on a 224-shaped database added {sorted(after - before)}"
    )
    purchase = await _mk(accept_variant_labels=["Nude Glow"], market_country="US")
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] == ["Nude Glow"] and read["market_country"] == "US"


async def test_healing_a_224_shaped_database_reaches_the_migrations_own_column_types():
    """Not just "the columns exist" — the same TYPES. A heal that landed `accept_variant_labels`
    as text would round-trip through the module's own encode/decode and be invisible until
    somebody wrote a jsonb query against it."""
    from db.schema_guard import ensure_required_schema_light

    from_migration = {
        r[1]: r for r in (await _schema_fingerprint())[0] if r[0] == "reap_agentic_purchases"
    }
    await _apply_224_only()
    await ensure_required_schema_light()
    healed = {
        r[1]: r for r in (await _schema_fingerprint())[0] if r[0] == "reap_agentic_purchases"
    }
    for name in _HINT_COLUMNS:
        assert healed[name] == from_migration[name], (
            f"{name} healed onto a 224-shaped database differently from the migration:\n"
            f"  migration: {from_migration[name]}\n  healed:    {healed[name]}"
        )


async def test_the_hint_self_heal_is_idempotent_on_a_224_shaped_database():
    """It runs on EVERY startup. A second run must add nothing and must not raise — a raise here
    takes the whole best-effort block with it, including the self-heals that follow."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await _apply_224_only()
    await ensure_required_schema_light()
    once = await _schema_fingerprint()

    await ensure_required_schema_light()
    await ensure_required_schema_light()

    assert await _schema_fingerprint() == once, "a later run changed the schema"
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


async def test_the_migration_and_the_postgres_self_heal_run_the_same_alter():
    """The migration's ALTER and the Postgres branch's ALTER must be the SAME STATEMENT, because
    the two build the same production schema by two routes and only one of them ever runs in
    prod.

    COMPARED ON NORMALISED WHITESPACE, and the name of this test says so rather than saying
    "byte-identical" — the two differ in leading indentation and nothing asserts otherwise, so
    claiming bytes would be claiming more than is checked. What is checked is every token the
    database acts on."""
    root = Path(__file__).resolve().parent.parent
    migration = (
        root / "db/migrations/225_reap_agentic_purchase_hints.sql"
    ).read_text(encoding="utf-8")
    guard = (root / "db/schema_guard.py").read_text(encoding="utf-8")

    def _alter(text: str) -> str:
        start = text.index("ALTER TABLE IF EXISTS reap_agentic_purchases")
        return " ".join(text[start: text.index(";", start) + 1].split())

    assert _alter(migration) == _alter(guard), (
        "the mig-225 ALTER in db/schema_guard.py is not the one in the migration:\n"
        f"  migration:   {_alter(migration)}\n  schema_guard: {_alter(guard)}"
    )


async def test_the_new_columns_are_partitioned_between_public_and_never_public():
    """GUARD THE GUARD, the Postgres twin of the SQLite partition test: a column that is in
    neither list is a decision nobody made. The never-public set is written out here rather than
    imported, so a shrinking definition of private cannot satisfy it."""
    never_public = {
        "buyer_ref", "agent_id", "agent_user_ref_hash", "buyer_email", "shipping_address",
        "enrollment_id", "click_id", "return_url", "reap_product_id", "reap_variant_id",
        "reap_quote_id", "reap_checkout_id", "queries_tried", "attempts", "next_poll_at",
        "claimed_by", "claimed_at", "state_entered_at",
        "accept_variant_labels", "also_accept_domains", "market_country",
        # mig 226 — see the SQLite twin of this list for why neither is public.
        "item_source", "cart_url",
    }
    columns = await _purchase_columns()
    unclassified = columns - _EXPECTED_PUBLIC_COLUMNS - never_public
    assert not unclassified, (
        f"these columns are in neither list: {sorted(unclassified)} — decide which, here AND in "
        "db/reap_agentic_ledger.PUBLIC_PURCHASE_COLUMNS"
    )
    assert not (_EXPECTED_PUBLIC_COLUMNS & never_public), "the two lists overlap"


async def test_the_new_sql_constants_are_module_level_and_prepare():
    """`test_every_postgres_sql_constant_in_the_module_prepares` sweeps whatever it FINDS, so it
    would stay green if these statements had been built at call time and were therefore invisible
    to it — the #1588 blind spot. This names them."""
    constants = _module_sql_constants()
    for name in ("_SELECT_ENROLLMENT_BY_ID_SQL", "_SELECT_ENROLLMENT_BY_REAP_ID_SQL"):
        assert name in constants, f"{name} is not a module-level SQL constant"

    conn = await _raw_connection()
    try:
        for name in sorted(constants):
            if name.endswith("_SQLITE"):
                continue
            positional, _order = _to_positional(constants[name])
            await conn.prepare(positional)
    finally:
        await conn.close()


# ── 225.6 a failing sibling statement must not starve the hint columns ───────────────────────
#
# THE REVIEWER'S P1, REPRODUCED. The mig-225 ALTER used to be the LAST statement inside the
# mig-224 try, and `CREATE UNIQUE INDEX uq_reap_agentic_enrollments_one_active` FAILS on a
# 224-shaped database that already holds two 'active' enrollments for one buyer_ref. That raise
# abandoned every statement after it, so the three hint columns never landed — and because
# production never runs db/migrations there was no other route: `create_purchase` then raised
# UndefinedColumnError on every call, forever, on exactly the databases that were already unwell.
#
# This is the dialect the defect was found on, and the one where it matters: on Postgres the
# duplicate rows make a REAL index build fail.


async def _seed_two_active_enrollments_for_one_buyer():
    """Put the database in the state that makes the unique-index creation fail.

    Raw INSERTs against a table built WITHOUT the index, because that is the only way this state
    is reachable — `mark_enrollment_active` cannot produce it, which is the whole point of the
    index. It is reachable in production the way any pre-index duplicate is: rows written before
    the index existed, or an index dropped by hand during an incident.
    """
    from db.database import database

    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await database.execute(
        """
        CREATE TABLE reap_agentic_enrollments (
            id VARCHAR(64) PRIMARY KEY,
            buyer_ref VARCHAR(128) NOT NULL,
            agent_id VARCHAR(128),
            reap_enrollment_id VARCHAR(128),
            status VARCHAR(16) NOT NULL
                CHECK (status IN ('pending', 'active', 'dead')),
            reap_status VARCHAR(64),
            card_network VARCHAR(32),
            card_last4 VARCHAR(4)
                CHECK (card_last4 IS NULL OR length(card_last4) = 4),
            hosted_url TEXT,
            hosted_url_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    for row_id in ("re_dup_a", "re_dup_b"):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
            "VALUES (:i, 'bref_dup', 'active')",
            {"i": row_id},
        )


async def test_the_repro_really_does_break_the_unique_index_on_postgres():
    """CONTROL, AND IT IS NOT OPTIONAL. The tests below assert that something SURVIVES a failure
    — and an assertion that a mechanism survives a failure passes just as happily when THERE WAS
    NO FAILURE. If these duplicate rows stopped making the index build fail, the starvation tests
    would stay green while testing nothing at all.

    So: prove the failure is real, by attempting the statement the self-heal attempts."""
    import asyncpg
    from db.database import database

    await _seed_two_active_enrollments_for_one_buyer()
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_enrollments_one_active "
            "ON reap_agentic_enrollments (buyer_ref) WHERE status = 'active';"
        )


async def test_a_failing_sibling_statement_does_not_starve_the_hint_columns_on_postgres():
    """THE P1 REGRESSION GUARD, on the dialect it was found on."""
    from db.schema_guard import ensure_required_schema_light

    await _apply_224_only()
    await _seed_two_active_enrollments_for_one_buyer()
    before = await _purchase_columns()
    assert not (set(_HINT_COLUMNS) & before), "precondition: the 224 shape, no hint columns"

    await ensure_required_schema_light()

    after = await _purchase_columns()
    assert set(_HINT_COLUMNS) <= after, (
        f"a failing sibling statement starved the mig-225 columns: missing "
        f"{sorted(set(_HINT_COLUMNS) - after)}. Production never runs db/migrations, so these "
        "columns would not exist there at all and create_purchase would raise on every call."
    )


async def test_the_rail_still_opens_a_purchase_after_a_failing_sibling_on_postgres():
    """The behavioural half. `create_purchase` is the call that raised UndefinedColumnError in
    the reviewer's repro — not a schema query, the actual thing a request does."""
    from db.schema_guard import ensure_required_schema_light
    import db.reap_agentic_ledger as ledger

    await _apply_224_only()
    await _seed_two_active_enrollments_for_one_buyer()
    await ensure_required_schema_light()

    purchase = await _mk(
        accept_variant_labels=["Nude Glow"],
        also_accept_domains=["brand.example"],
        market_country="US",
    )
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] == ["Nude Glow"]
    assert read["also_accept_domains"] == ["brand.example"]
    assert read["market_country"] == "US"


async def test_the_healed_hint_columns_have_the_right_types_after_a_failing_sibling():
    """Not just "they exist" — the same catalog shape the migration builds. A heal that limped
    to the finish with `text` columns would round-trip through the module's own encode/decode
    and stay invisible."""
    from db.schema_guard import ensure_required_schema_light

    from_migration = {
        r[1]: r for r in (await _schema_fingerprint())[0] if r[0] == "reap_agentic_purchases"
    }
    await _apply_224_only()
    await _seed_two_active_enrollments_for_one_buyer()
    await ensure_required_schema_light()
    healed = {
        r[1]: r for r in (await _schema_fingerprint())[0] if r[0] == "reap_agentic_purchases"
    }
    for name in _HINT_COLUMNS:
        assert healed[name] == from_migration[name], (
            f"{name} healed differently after a failing sibling:\n"
            f"  migration: {from_migration[name]}\n  healed:    {healed[name]}"
        )


# ── 225.7 a terminal transition CLEARS a stale last_error_code ───────────────────────────────
#
# FROM THE #2204 RE-REVIEW. With an unconditional COALESCE, a retry that eventually SUCCEEDS
# carried the failed attempt's code into a 'completed' row. On THIS dialect the change also has
# to survive planning: `:last_error_code` now appears twice in one statement, which is the shape
# that produced AmbiguousParameter on this rail before — `test_every_postgres_sql_constant_in_
# the_module_prepares` is what confirms it does not here.


async def _with_stored_error_code(state: str, code: str = "completed_without_order_id", **over):
    """A purchase sitting in `state` with `code` already on the row, put there by a RAW UPDATE —
    a claim/release cycle would also move claimed_by, attempts and next_poll_at, any of which
    could mask or explain a result here."""
    from db.database import database
    import db.reap_agentic_ledger as ledger

    purchase = await _mk(state=state, **over)
    await database.execute(
        "UPDATE reap_agentic_purchases SET last_error_code = :c WHERE id = :i",
        {"c": code, "i": purchase["id"]},
    )
    assert (await ledger.get_purchase_internal(purchase["id"]))["last_error_code"] == code
    return purchase


async def test_a_clean_completion_clears_a_stale_error_code_on_postgres():
    """THE CASE THE RE-REVIEW FOUND."""
    import db.reap_agentic_ledger as ledger

    purchase = await _with_stored_error_code("processing")
    moved = await ledger.transition(
        purchase["id"], from_states=["processing"], to_state="completed"
    )
    assert moved is not None and moved["state"] == "completed"
    assert moved["last_error_code"] is None, (
        "a clean completion kept the failed attempt's code; every reader of this row would "
        "believe the order had a problem it did not have"
    )
    assert (await ledger.get_purchase_internal(purchase["id"]))["last_error_code"] is None


@pytest.mark.parametrize("terminal", _TERMINALS)
async def test_every_terminal_target_clears_a_stale_code_on_postgres(terminal):
    import db.reap_agentic_ledger as ledger

    purchase = await _with_stored_error_code(
        _REACHES_TERMINAL[terminal], buyer_ref=f"bref_{terminal}"
    )
    moved = await ledger.transition(
        purchase["id"], from_states=[_REACHES_TERMINAL[terminal]], to_state=terminal
    )
    assert moved["state"] == terminal
    assert moved["last_error_code"] is None


async def test_a_terminal_transition_stores_an_explicit_code_on_postgres():
    """The half that lets 'failed' and 'refused' speak at all."""
    import db.reap_agentic_ledger as ledger

    purchase = await _with_stored_error_code("processing")
    moved = await ledger.transition(
        purchase["id"],
        from_states=["processing"],
        to_state="failed",
        last_error_code="checkout_declined",
    )
    assert moved["last_error_code"] == "checkout_declined"
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "checkout_declined"


async def test_a_non_terminal_transition_still_keeps_the_previous_code_on_postgres():
    """THE COALESCE HALF, AND THE CONTROL FOR THE WHOLE RULE. Without it, replacing the CASE with
    a bare assignment on BOTH arms would pass every test above."""
    import db.reap_agentic_ledger as ledger

    purchase = await _with_stored_error_code("quoting", code="quote_expired")
    moved = await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    )
    assert moved["state"] == "awaiting_approval"
    assert moved["last_error_code"] == "quote_expired", (
        "a non-terminal advance erased the previous error code — a row in flight would forget "
        "why it last stalled on every uneventful step"
    )


async def test_a_terminal_transition_clears_the_code_and_the_pii_together_on_postgres():
    """The terminal write is ONE statement, so the code clearing and the PII nulling cannot come
    apart — a crash between them is not a state this rail can reach."""
    import db.reap_agentic_ledger as ledger

    purchase = await _with_stored_error_code("processing")
    moved = await ledger.transition(
        purchase["id"], from_states=["processing"], to_state="completed"
    )
    assert moved["last_error_code"] is None
    assert moved["buyer_email"] is None and moved["shipping_address"] is None
    assert moved["terminal_at"] is not None
    assert moved["claimed_by"] is None


async def test_release_claim_still_coalesces_on_postgres():
    """The rule is about TERMINAL writes; a release is never terminal. Pinned so that "make them
    consistent" is a change somebody has to argue for."""
    import db.reap_agentic_ledger as ledger

    purchase = await _with_stored_error_code("quoting", code="quote_expired")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await ledger.release_claim(purchase["id"], "worker_a")
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "quote_expired"


async def test_the_terminal_clear_survives_planning_on_postgres():
    """`:last_error_code` now appears TWICE in one statement. Both occurrences are value
    positions of the same column type, so Postgres deduces one type — but that is an argument,
    and this is the check. A statement it cannot plan is the #1588 class."""
    import db.reap_agentic_ledger as ledger

    conn = await _raw_connection()
    try:
        for name in ("_TRANSITION_SQL",):
            positional, _order = _to_positional(getattr(ledger, name))
            await conn.prepare(positional)
    finally:
        await conn.close()
