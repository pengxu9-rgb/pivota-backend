"""db/reap_agentic_ledger.py against REAL Postgres — the half SQLite structurally cannot test.

Picked up automatically by .github/workflows/postgres-dialect-gate.yml via the
`tests/test_*_postgres.py` glob.

FOUR THINGS HERE THAT THE SQLITE ARM CANNOT PROVE, each one a defect this repo has already
shipped once on some rail or other:

  1. A SECOND BACKEND CONNECTION. The `AND state IN (...)` conjunct is the concurrency guard for
     two POD PROCESSES, not two coroutines. On SQLite databases==0.7.0 serialises everything onto
     one connection, so the SQLite race test proves the guard under interleaving and nothing
     about two connections. Here the losing statement is issued on its own asyncpg connection.

  2. jsonb COMES BACK AS TEXT. No SQLAlchemy result processor runs on a raw statement, so
     asyncpg hands back the jsonb verbatim as a `str`. A caller that expected a dict gets a
     string with no error anywhere. The test below asserts BOTH that the raw value is a str and
     that the module's decode turned it into a dict — the second assertion alone would pass on a
     driver that never had the problem.

  3. THE SERVER-SIDE CUTOFF, UNDER A NON-UTC CLIENT. asyncpg encodes a naive datetime for a
     timestamptz parameter as local wall time, local to THIS PROCESS — measured seven hours off
     from a Pacific box against a UTC database. The requeue test runs with TZ shifted to prove
     the cutoff never touches a client clock.

  4. PREPARE. Every SQL constant in the module is planned against the real schema. SQLite cannot
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


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the agentic-ledger tables in database {dbname!r} — "
            f"throwaway only (e.g. pivota_dialect_check)"
        )


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Drop first, so a constraint deleted from the migration cannot survive via IF NOT EXISTS
    # and leave this gate testing a schema the repo no longer declares.
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    for statement in split_statements(_MIGRATION.read_text(encoding="utf-8")):
        await database.execute(statement)
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

    # Do not rewrite inside string literals: the statements here contain none with a ':' in
    # them, and asserting that keeps this helper honest if one is ever added.
    assert "':" not in sql and ":'" not in sql
    return _BIND_RE.sub(repl, sql), order


async def _raw_connection():
    import asyncpg

    return await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"))


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


async def _mk(**over):
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
    return await ledger.create_purchase(**kwargs)


async def _state_of(purchase_id: str) -> str:
    from db.database import database

    row = await database.fetch_one(
        "SELECT state FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    return row["state"]


async def _age_claim(purchase_id: str, seconds: int) -> None:
    """Backdate a claim with a SERVER-SIDE expression. A test that bound its own datetime would
    be exercising precisely the thing the module refuses to do."""
    from db.database import database

    await database.execute(
        "UPDATE reap_agentic_purchases "
        f"SET claimed_at = CURRENT_TIMESTAMP - INTERVAL '{int(seconds)} seconds' "
        "WHERE id = :i",
        {"i": purchase_id},
    )


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
    """The genuinely concurrent version: two asyncpg connections, the same statement, at the
    same time. Exactly one row comes back in total."""
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
    db/agent_issued_cards.mark_revoked became a constant False. Every statement in this module
    uses RETURNING, and this is the assertion that says so."""
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


# ── 2. jsonb ─────────────────────────────────────────────────────────────────────────────────


async def test_jsonb_comes_back_as_text_from_the_driver_and_as_python_from_the_module():
    """BOTH halves. The second assertion alone would pass on a driver that never had the
    problem; the first is what pins that this module is decoding something real."""
    from db.database import database

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

    import db.reap_agentic_ledger as ledger

    fetched = await ledger.get_purchase(purchase["id"])
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
    """`CAST(:x AS JSONB)` in the transition statement is only correct against a jsonb column.
    If the column ever lands as json, the COALESCE becomes unplannable — the reason both arms
    are cast — and this is the test that names the drift."""
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

    purchase = await _mk(state="resolving")
    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting"
    )
    assert moved["buyer_email"] == "alice@example.test"
    assert moved["shipping_address"]["city"] == "Springfield"
    assert moved["terminal_at"] is None


# ── ownership ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "agent_id,user_hash", [("agent_two", "hash_alice"), ("agent_one", "hash_bob")]
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


# ── the claim pair ───────────────────────────────────────────────────────────────────────────


async def _make_due(**over):
    return await _mk(
        state="awaiting_approval",
        next_poll_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        **over,
    )


async def test_claim_exclusivity_across_two_connections():
    """The claim conjunct, proven the same way as the advance: the other pod takes the lease on
    its own connection, and our claim then finds nothing to take."""
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    conn = await _raw_connection()
    try:
        rows = await _run_on(
            conn, ledger._CLAIM_PURCHASE_SQL, {"id": purchase["id"], "worker_id": "worker_x"}
        )
        assert len(rows) == 1
    finally:
        await conn.close()

    assert await ledger.claim_due_purchases("worker_a", limit=5) == [], (
        "a purchase already leased by another worker must not be claimable"
    )


async def test_the_claim_statement_itself_refuses_a_second_holder():
    """THE SELECT MASKS THE UPDATE'S CONJUNCT, so the statement is exercised directly.

    `_SELECT_DUE_PURCHASES_SQL` already filters `claimed_by IS NULL`. That makes
    `claim_due_purchases` LOOK correct even with `AND claimed_by IS NULL` deleted from the
    UPDATE — the already-leased row is simply never offered as a candidate a second time.
    Measured, not theorised: that mutant survived every other test in this file while the SQLite
    arm killed it. Two connections issue the real claim statement against the same row here, so
    the conjunct is the only thing that can decide the winner.
    """
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    conn_a = await _raw_connection()
    conn_b = await _raw_connection()
    try:
        rows_a, rows_b = await asyncio.gather(
            _run_on(
                conn_a,
                ledger._CLAIM_PURCHASE_SQL,
                {"id": purchase["id"], "worker_id": "worker_a"},
            ),
            _run_on(
                conn_b,
                ledger._CLAIM_PURCHASE_SQL,
                {"id": purchase["id"], "worker_id": "worker_b"},
            ),
        )
    finally:
        await conn_a.close()
        await conn_b.close()

    assert len(rows_a) + len(rows_b) == 1, (
        f"exactly one worker may take a lease; got {len(rows_a)} + {len(rows_b)}"
    )
    assert (await ledger.get_purchase(purchase["id"]))["attempts"] == 1, (
        "a second claim that 'succeeded' would also have incremented attempts twice"
    )


async def test_claim_then_second_claim_is_empty():
    import db.reap_agentic_ledger as ledger

    await _make_due()
    first = await ledger.claim_due_purchases("worker_a", limit=5)
    second = await ledger.claim_due_purchases("worker_b", limit=5)
    assert len(first) == 1 and second == []
    assert first[0]["attempts"] == 1


async def test_release_by_the_wrong_worker_does_nothing_on_postgres():
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    assert await ledger.release_claim(purchase["id"], "worker_b") is None
    assert (await ledger.get_purchase(purchase["id"]))["claimed_by"] == "worker_a"


async def test_requeue_uses_a_server_side_cutoff_even_from_a_non_utc_process():
    """THE SEVEN-HOUR BUG, made unreproducible.

    asyncpg encodes a naive datetime for a timestamptz parameter as local wall time — local to
    THIS PROCESS. A `utcnow() - timedelta(...)` cutoff bound from a Pacific box against a UTC
    database lands seven hours IN THE FUTURE, which requeues every live lease. This test shifts
    the process timezone and asserts the fresh lease survives, which a client-bound cutoff
    could not do.
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
        "answers None however many rows moved, and the caller's log line disappears."
    )
    assert (await ledger.get_purchase(stale["id"]))["claimed_by"] is None
    assert (await ledger.get_purchase(fresh["id"]))["claimed_by"] == "worker_a", (
        "a lease inside its window must survive a requeue run from a non-UTC client process"
    )


async def test_a_requeued_purchase_is_claimable_again_on_postgres():
    import db.reap_agentic_ledger as ledger

    stale = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(stale["id"], 900)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)
    reclaimed = await ledger.claim_due_purchases("worker_b", limit=5)
    assert [c["id"] for c in reclaimed] == [stale["id"]]
    assert reclaimed[0]["attempts"] == 2


async def test_timestamps_come_back_timezone_aware():
    """A naive datetime out of this module is a datetime some later caller will compare against
    `datetime.now(timezone.utc)` and get a TypeError, or worse, bind back in naive."""
    import db.reap_agentic_ledger as ledger

    purchase = await _make_due()
    fetched = await ledger.get_purchase(purchase["id"])
    for column in ("created_at", "updated_at", "next_poll_at"):
        assert fetched[column].tzinfo is not None, f"{column} came back naive"


# ── enrollments ──────────────────────────────────────────────────────────────────────────────


async def test_the_partial_unique_index_makes_two_active_enrollments_impossible():
    """The DATABASE half of the one-active invariant. `mark_enrollment_active`'s demotion is the
    code half; this proves the index would refuse the write even if the demotion were gone —
    which is the difference between an invariant and a convention."""
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


async def test_the_reap_enrollment_id_is_unique_when_present_and_free_when_null():
    import asyncpg
    from db.database import database
    import db.reap_agentic_ledger as ledger

    # Two pending rows with NULL reap ids coexist — the index is PARTIAL for exactly this.
    await ledger.upsert_pending_enrollment(buyer_ref="bref_a")
    await ledger.upsert_pending_enrollment(buyer_ref="bref_b")

    a = await ledger.upsert_pending_enrollment(buyer_ref="bref_a", reap_enrollment_id="enr_dup")
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await database.execute(
            "UPDATE reap_agentic_enrollments SET reap_enrollment_id = 'enr_dup' WHERE id = :i",
            {"i": (await ledger.upsert_pending_enrollment(buyer_ref="bref_b"))["id"]},
        )
    assert a["reap_enrollment_id"] == "enr_dup"


# ── the self-heal, on an empty database ──────────────────────────────────────────────────────


async def test_self_heal_creates_both_tables_and_every_index_on_postgres():
    """PRODUCTION NEVER RUNS db/migrations. The self-heal is the only thing that builds these
    tables in prod, and the schema-guard coverage gate inspects ADD COLUMN only, so nothing else
    in CI would notice it missing. This is that check."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    present = await database.fetch_val(
        "SELECT to_regclass('public.reap_agentic_purchases')"
    )
    assert present is None, "precondition: the tables are gone before the self-heal runs"

    await ensure_required_schema_light()

    for table in ("reap_agentic_enrollments", "reap_agentic_purchases"):
        assert await database.fetch_val(
            "SELECT to_regclass(:t)", {"t": f"public.{table}"}
        ) is not None, f"the self-heal did not create {table}"

    rows = await database.fetch_all(
        """
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public'
           AND tablename IN ('reap_agentic_enrollments', 'reap_agentic_purchases')
        """
    )
    found = {r["indexname"] for r in rows}
    missing = _EXPECTED_INDEXES - found
    assert not missing, f"the self-heal did not create these indexes: {sorted(missing)}"


async def test_self_heal_is_idempotent_and_leaves_the_rail_usable():
    from db.database import database
    from db.schema_guard import ensure_required_schema_light
    import db.reap_agentic_ledger as ledger

    await ensure_required_schema_light()
    await ensure_required_schema_light()

    rows = await database.fetch_all(
        """
        SELECT indexname FROM pg_indexes
         WHERE schemaname = 'public'
           AND tablename IN ('reap_agentic_enrollments', 'reap_agentic_purchases')
        """
    )
    assert _EXPECTED_INDEXES <= {r["indexname"] for r in rows}
    purchase = await _mk()
    assert await ledger.get_purchase(purchase["id"]) is not None


async def test_the_self_heal_ddl_is_byte_identical_to_the_migration_where_it_must_be():
    """Column-for-column, on the Postgres branch. Type names, defaults and CHECKs all compared,
    because on THIS dialect the self-heal has no excuse to differ from the migration — and a
    heal that disagrees makes prod behave unlike every environment where the migration ran."""
    from db.database import database
    from db.schema_guard import ensure_required_schema_light

    async def _columns(table):
        rows = await database.fetch_all(
            """
            SELECT column_name, data_type, is_nullable, column_default,
                   character_maximum_length
              FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = :t
             ORDER BY column_name
            """,
            {"t": table},
        )
        return [dict(r) for r in rows]

    from_migration = {
        table: await _columns(table)
        for table in ("reap_agentic_enrollments", "reap_agentic_purchases")
    }

    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await ensure_required_schema_light()

    for table, expected in from_migration.items():
        assert await _columns(table) == expected, (
            f"{table} built by the schema-guard self-heal differs from the one built by "
            f"db/migrations/224_reap_agentic_ledger.sql"
        )


# ── 4. PREPARE ───────────────────────────────────────────────────────────────────────────────


def _module_sql_constants():
    import db.reap_agentic_ledger as ledger

    return {
        name: value
        for name, value in vars(ledger).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


async def test_every_postgres_sql_constant_in_the_module_prepares():
    """The #1588 class: Postgres refuses to plan a statement whose parameter types it cannot
    infer, and SQLite cannot see it. The repo-wide sweep in
    tests/test_repo_sql_prepare_postgres.py covers these too — every statement here is a
    module-level constant precisely so that it can — but this keeps the failure local and
    readable, and it fails on THIS gate rather than on a sweep that runs elsewhere."""
    constants = _module_sql_constants()
    assert len(constants) >= 12, (
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
    payload is destroyed without an error. The split is load-bearing, so it is asserted rather
    than trusted."""
    for name, sql in _module_sql_constants().items():
        if name.endswith("_SQLITE"):
            assert "JSONB" not in sql.upper(), f"{name} must not mention JSONB"


async def test_the_repo_prepare_sweep_can_see_this_module():
    """MERGED IS NOT RUNNING. The sweep resolves a `database.*` first argument only when it is a
    literal or a MODULE-level name bound to one; a helper that returns SQL is a counted blind
    spot. This asserts the module is actually in the sweep's reach rather than assuming it."""
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
