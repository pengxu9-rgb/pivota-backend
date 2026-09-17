"""db/reap_agentic_ledger.py — the Reap agentic rail's storage layer, on SQLite.

WHAT THIS FILE IS FOR, AND WHAT IT IS NOT FOR. Row-level semantics belong on SQLite: it is fast,
needs no server, and every guard in the module is expressible here. What CANNOT be tested here is
the driver behaviour that only Postgres has — `execute()` returning no rowcount, jsonb coming
back as text, asyncpg's datetime encoding, and a genuinely concurrent second connection. That is
tests/test_reap_agentic_ledger_postgres.py, and neither file subsumes the other.

EVERY TEST BELOW IS WRITTEN TO DIE WHEN A SPECIFIC GUARD IS REMOVED. The guards are: the
`AND state IN (...)` conjunct in the advance, the ownership conjunct in the get AND in the list,
the PII null on terminal, the `AND claimed_by IS NULL` conjunct in the claim, the
ALLOWED_TRANSITIONS check, and the demotion in mark_enrollment_active. The PR body carries the
mutation table showing which test dies for each.

The tables are SQL-ONLY — no SQLAlchemy `Table`, deliberately, because `metadata.create_all` runs
before db/migrations in main.py and a model would therefore become the source of truth in
production. So this file builds them the way production does: by running the schema-guard
self-heal.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.reap_agentic_ledger as ledger  # noqa: E402

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason=(
        "the SQLite arm of the agentic ledger; the Postgres arm is "
        "tests/test_reap_agentic_ledger_postgres.py"
    ),
)

_TERMINALS = ("completed", "failed", "refused", "expired")

# A from-state that legally reaches each terminal, so the PII test can cover all four.
_REACHES_TERMINAL = {
    "completed": "awaiting_approval",
    "failed": "resolving",
    "refused": "resolving",
    "expired": "needs_enrollment",
}


@pytest.fixture(autouse=True)
async def _db():
    """Build the tables the way PRODUCTION does — through the self-heal, not through a test-only
    CREATE TABLE. A fixture that builds its own schema proves nothing about the schema that
    ships, and on this rail the self-heal IS the only thing that ships (prod never runs
    db/migrations)."""
    if not database.is_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await ensure_required_schema_light()
    yield


async def _mk(**over):
    kwargs = dict(
        buyer_ref="bref_alice",
        agent_id="agent_one",
        agent_user_ref_hash="hash_alice",
        merchant_domain="brand.example",
        product_key="pk_1",
        quantity=1,
        currency="USD",
        our_price_minor=4250,
        buyer_email="alice@example.test",
        shipping_address={"line1": "1 Test Way", "city": "Springfield", "zip": "00001"},
    )
    kwargs.update(over)
    return await ledger.create_purchase(**kwargs)


async def _state_of(purchase_id: str) -> str:
    row = await database.fetch_one(
        "SELECT state FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    return row["state"]


async def _age_claim(purchase_id: str, seconds: int) -> None:
    """Backdate a claim WITHOUT binding a datetime from Python. The whole point of the
    server-side cutoff is that no client clock is involved; a test that bound its own would be
    testing the thing the module refuses to do."""
    await database.execute(
        "UPDATE reap_agentic_purchases "
        f"SET claimed_at = datetime('now', '-{int(seconds)} seconds') WHERE id = :i",
        {"i": purchase_id},
    )


# ── the state machine ────────────────────────────────────────────────────────────────────────

_LEGAL_PAIRS = [
    (src, dst)
    for src, allowed in ledger.ALLOWED_TRANSITIONS.items()
    for dst in sorted(allowed)
]
_ILLEGAL_PAIRS = [
    (src, dst)
    for src in sorted(ledger.PURCHASE_STATES)
    for dst in sorted(ledger.PURCHASE_STATES)
    if dst not in ledger.ALLOWED_TRANSITIONS[src]
]


@pytest.mark.parametrize("src,dst", _LEGAL_PAIRS)
async def test_every_legal_transition_moves_the_row(src, dst):
    purchase = await _mk(state=src)
    moved = await ledger.transition(purchase["id"], from_states=[src], to_state=dst)
    assert moved is not None, f"{src} -> {dst} is declared legal but did not move the row"
    assert moved["state"] == dst
    assert await _state_of(purchase["id"]) == dst


@pytest.mark.parametrize("src,dst", _ILLEGAL_PAIRS)
async def test_every_illegal_transition_raises_before_any_sql(src, dst):
    """A ValueError, not a None. An illegal pair is a caller bug and must be loud; a lost race
    is ordinary and must be quiet. If both answered None, the bug would be retried forever
    instead of fixed."""
    purchase = await _mk(state=src)
    with pytest.raises(ValueError):
        await ledger.transition(purchase["id"], from_states=[src], to_state=dst)
    # And nothing moved — the refusal happened before the statement.
    assert await _state_of(purchase["id"]) == src


async def test_transition_from_a_state_the_row_is_not_in_returns_none():
    """THE CONCURRENCY GUARD, in its simplest form. The pair is legal, so the
    ALLOWED_TRANSITIONS check passes; the row is in another state, so the `AND state IN (...)`
    conjunct is the only thing that can refuse it."""
    purchase = await _mk(state="quoting")
    assert (
        await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="quoting"
        )
        is None
    )
    assert await _state_of(purchase["id"]) == "quoting"


async def test_two_transitions_from_the_same_state_produce_exactly_one_winner():
    """The race. Both callers see 'quoting' and both try to advance it; the conditional UPDATE
    means exactly one row comes back and the other caller is told None.

    On SQLite databases==0.7.0 serialises these onto one connection, so this is the guard under
    interleaving rather than true parallelism — the genuinely concurrent, second-connection
    version is in the Postgres file."""
    purchase = await _mk(state="quoting")

    async def advance(target):
        return await ledger.transition(
            purchase["id"], from_states=["quoting"], to_state=target
        )

    first, second = await asyncio.gather(advance("awaiting_approval"), advance("refused"))
    winners = [r for r in (first, second) if r is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert await _state_of(purchase["id"]) == winners[0]["state"]


async def test_transition_rejects_an_unknown_field_rather_than_dropping_it():
    purchase = await _mk(state="resolving")
    with pytest.raises(TypeError):
        await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="quoting", nonsense="x"
        )


async def test_transition_leaves_unmentioned_columns_alone():
    """The COALESCE contract: None means 'do not touch', which is what lets the statement be a
    single module-level constant instead of an assignment list built at call time."""
    purchase = await _mk(state="resolving", merchant_domain="brand.example")
    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting", reap_quote_id="q_1"
    )
    assert moved["merchant_domain"] == "brand.example"
    assert moved["reap_quote_id"] == "q_1"


# ── the PII rule ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("terminal", _TERMINALS)
async def test_terminal_state_nulls_the_pii_and_stamps_terminal_at(terminal):
    """Every one of the four terminal states, not just the happy one. shipping_address and
    buyer_email are PII the row only needs while the purchase is in flight, and the nulling has
    to happen in the same statement as the state change — otherwise a crash between two
    statements leaves it behind forever."""
    src = _REACHES_TERMINAL[terminal]
    purchase = await _mk(state=src)
    assert purchase["buyer_email"] is not None
    assert purchase["shipping_address"] is not None

    done = await ledger.transition(purchase["id"], from_states=[src], to_state=terminal)
    assert done is not None
    assert done["state"] == terminal
    assert done["buyer_email"] is None
    assert done["shipping_address"] is None
    assert done["terminal_at"] is not None

    # And it is gone from the TABLE, not merely from the returned dict.
    row = await database.fetch_one(
        "SELECT buyer_email, shipping_address, terminal_at "
        "FROM reap_agentic_purchases WHERE id = :i",
        {"i": purchase["id"]},
    )
    assert row["buyer_email"] is None
    assert row["shipping_address"] is None
    assert row["terminal_at"] is not None


async def test_a_non_terminal_transition_keeps_the_pii():
    """The control. Without it, a mutant that NULLs the PII unconditionally would pass every
    assertion above — an absence assertion passes when the mechanism is absent too."""
    purchase = await _mk(state="resolving")
    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting"
    )
    assert moved["buyer_email"] == "alice@example.test"
    assert moved["shipping_address"]["city"] == "Springfield"
    assert moved["terminal_at"] is None


async def test_terminal_transition_can_still_write_its_own_fields():
    purchase = await _mk(state="awaiting_approval")
    done = await ledger.transition(
        purchase["id"],
        from_states=["awaiting_approval"],
        to_state="completed",
        reap_order_id="ord_9",
        final_total_minor=4599,
    )
    assert done["reap_order_id"] == "ord_9"
    assert done["final_total_minor"] == 4599
    assert done["buyer_email"] is None


# ── ownership ────────────────────────────────────────────────────────────────────────────────


async def test_get_for_owner_returns_the_row_for_its_owner():
    purchase = await _mk()
    got = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    assert got is not None and got["id"] == purchase["id"]


@pytest.mark.parametrize(
    "agent_id,user_hash",
    [
        ("agent_two", "hash_alice"),  # another agent, same user hash
        ("agent_one", "hash_bob"),  # same agent, another user
        ("agent_two", "hash_bob"),  # neither
    ],
)
async def test_get_for_owner_refuses_a_non_owner(agent_id, user_hash):
    """BOTH halves of the conjunct. Testing only the agent_id half would leave the
    cross-user leak — one agent's two end users reading each other's purchases — invisible."""
    purchase = await _mk()
    assert await ledger.get_purchase_for_owner(purchase["id"], agent_id, user_hash) is None


async def test_list_for_owner_is_scoped_to_the_same_conjunct():
    """A list endpoint that leaks is a worse leak than a get, and it is the one that gets
    forgotten — so it has its own test rather than riding on the get's."""
    mine = await _mk()
    await _mk(agent_id="agent_two", agent_user_ref_hash="hash_alice", buyer_ref="bref_other")
    await _mk(agent_id="agent_one", agent_user_ref_hash="hash_bob", buyer_ref="bref_bob")

    rows = await ledger.list_purchases_for_owner("agent_one", "hash_alice")
    assert [r["id"] for r in rows] == [mine["id"]]

    assert await ledger.list_purchases_for_owner("agent_two", "hash_bob") == []


async def test_get_purchase_unscoped_still_works_for_internal_callers():
    """The poller holds ids from our own storage and has no agent identity to scope by. That is
    why the unscoped read exists, and why it is a separate function rather than a default."""
    purchase = await _mk()
    got = await ledger.get_purchase(purchase["id"])
    assert got is not None and got["id"] == purchase["id"]


# ── json round-trip ──────────────────────────────────────────────────────────────────────────


async def test_json_columns_round_trip_as_python_objects_not_strings():
    """A raw-SQL jsonb column comes back as TEXT on Postgres because no SQLAlchemy result
    processor runs. This asserts the decode on both dialects, so the SQLite run is not quietly
    proving something weaker than the Postgres one."""
    purchase = await _mk(
        queries_tried=["serum", "vitamin c serum"],
        shipping_address={"line1": "1 Test Way", "zip": "00001"},
    )
    assert isinstance(purchase["queries_tried"], list)
    assert purchase["queries_tried"] == ["serum", "vitamin c serum"]
    assert isinstance(purchase["shipping_address"], dict)

    fetched = await ledger.get_purchase(purchase["id"])
    assert isinstance(fetched["queries_tried"], list)
    assert isinstance(fetched["shipping_address"], dict)
    assert fetched["shipping_address"]["zip"] == "00001"

    moved = await ledger.transition(
        purchase["id"],
        from_states=["resolving"],
        to_state="quoting",
        queries_tried=["serum", "vitamin c serum", "ascorbic acid"],
    )
    assert isinstance(moved["queries_tried"], list)
    assert len(moved["queries_tried"]) == 3


async def test_json_is_not_destroyed_by_a_numeric_cast_on_sqlite():
    """The specific SQLite trap: `CAST(x AS JSONB)` resolves JSONB to NUMERIC affinity and
    silently stores 0. If the Postgres statement were ever used on this path, this is the
    assertion that notices."""
    purchase = await _mk(queries_tried=[{"q": "serum", "hits": 3}])
    raw = await database.fetch_val(
        "SELECT queries_tried FROM reap_agentic_purchases WHERE id = :i", {"i": purchase["id"]}
    )
    assert raw not in (0, "0", 0.0)
    assert json.loads(raw)[0]["q"] == "serum"


# ── the claim pair ───────────────────────────────────────────────────────────────────────────


async def _make_due(**over):
    purchase = await _mk(
        state="awaiting_approval",
        next_poll_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        **over,
    )
    return purchase


async def test_claim_takes_a_due_purchase_and_stamps_the_worker():
    purchase = await _make_due()
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert [c["id"] for c in claimed] == [purchase["id"]]
    assert claimed[0]["claimed_by"] == "worker_a"
    assert claimed[0]["claimed_at"] is not None
    assert claimed[0]["attempts"] == 1


async def test_a_second_claimer_gets_nothing():
    """CLAIM EXCLUSIVITY. The `AND claimed_by IS NULL` conjunct in the UPDATE is the whole
    guard — the SELECT that picks candidates is advisory and can be stale."""
    await _make_due()
    first = await ledger.claim_due_purchases("worker_a", limit=5)
    second = await ledger.claim_due_purchases("worker_b", limit=5)
    assert len(first) == 1
    assert second == [], "a purchase already leased must not be claimable by a second worker"


async def test_two_workers_racing_the_same_candidate_split_it_one_to_one():
    a, b = await asyncio.gather(
        ledger.claim_due_purchases("worker_a", limit=5),
        ledger.claim_due_purchases("worker_b", limit=5),
    )
    assert len(a) + len(b) == 0  # nothing due yet in this test's fresh table

    await _make_due()
    a, b = await asyncio.gather(
        ledger.claim_due_purchases("worker_a", limit=5),
        ledger.claim_due_purchases("worker_b", limit=5),
    )
    assert len(a) + len(b) == 1, "exactly one worker may hold a lease on one purchase"


async def test_a_purchase_whose_poll_is_not_due_is_not_claimed():
    await _mk(
        state="awaiting_approval",
        next_poll_at=datetime.now(timezone.utc) + timedelta(seconds=600),
    )
    assert await ledger.claim_due_purchases("worker_a", limit=5) == []


async def test_a_terminal_purchase_is_never_claimed():
    await _mk(
        state="completed",
        next_poll_at=datetime.now(timezone.utc) - timedelta(seconds=600),
    )
    assert await ledger.claim_due_purchases("worker_a", limit=5) == []


async def test_release_returns_the_lease_and_can_reschedule():
    purchase = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    released = await ledger.release_claim(
        purchase["id"], "worker_a", next_poll_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert released is not None
    assert released["claimed_by"] is None
    assert released["claimed_at"] is None
    # Rescheduled into the future, so it is no longer due.
    assert await ledger.claim_due_purchases("worker_b", limit=5) == []


async def test_release_by_the_wrong_worker_does_nothing():
    """A worker whose lease was already requeued and handed on must not be able to clear the new
    holder's claim on its way out."""
    purchase = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    assert await ledger.release_claim(purchase["id"], "worker_b") is None
    still = await ledger.get_purchase(purchase["id"])
    assert still["claimed_by"] == "worker_a"


async def test_requeue_frees_a_stale_lease_and_leaves_a_fresh_one_alone():
    stale = await _make_due()
    fresh = await _make_due(buyer_ref="bref_fresh")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(stale["id"], 900)

    moved = await ledger.requeue_stale_claims(lease_seconds=300, limit=10)
    assert moved == 1, "the count must come from RETURNING, not from the driver's rowcount"

    assert (await ledger.get_purchase(stale["id"]))["claimed_by"] is None
    assert (await ledger.get_purchase(fresh["id"]))["claimed_by"] == "worker_a"


async def test_a_requeued_purchase_is_claimable_again():
    stale = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(stale["id"], 900)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)

    reclaimed = await ledger.claim_due_purchases("worker_b", limit=5)
    assert [c["id"] for c in reclaimed] == [stale["id"]]
    assert reclaimed[0]["attempts"] == 2, "attempts counts claims, so a requeue+reclaim is two"


async def test_requeue_never_binds_a_python_datetime_for_its_cutoff():
    """The cutoff is `datetime('now', ...)` / `CURRENT_TIMESTAMP - interval`, computed by the
    SERVER. A Python-bound cutoff was measured seven hours wrong on this driver (asyncpg encodes
    a naive datetime for a timestamptz parameter in the CLIENT PROCESS's timezone).

    A shape assertion is weak on its own, which is why the behavioural tests above exist too —
    but this one names the specific defect so a future edit that reintroduces it is refused
    here rather than five files away."""
    for sql in (ledger._REQUEUE_STALE_CLAIMS_SQL, ledger._REQUEUE_STALE_CLAIMS_SQL_SQLITE):
        assert ":cutoff" not in sql and ":stale_before" not in sql
    assert "CURRENT_TIMESTAMP - (:lease_seconds * INTERVAL '1 second')" in (
        ledger._REQUEUE_STALE_CLAIMS_SQL
    )
    assert "datetime('now', :lease_window)" in ledger._REQUEUE_STALE_CLAIMS_SQL_SQLITE


# ── enrollments ──────────────────────────────────────────────────────────────────────────────


async def test_pending_enrollment_is_refreshed_not_duplicated():
    first = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", agent_id="agent_one", hosted_url="https://reap.test/a"
    )
    second = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", agent_id="agent_one", hosted_url="https://reap.test/b"
    )
    assert second["id"] == first["id"], "a re-open must refresh the pending row, not add one"
    assert second["hosted_url"] == "https://reap.test/b"
    count = await database.fetch_val(
        "SELECT COUNT(*) FROM reap_agentic_enrollments WHERE buyer_ref = :b",
        {"b": "bref_alice"},
    )
    assert count == 1


async def test_activation_demotes_any_other_active_row_for_that_buyer():
    """THE ONE-ACTIVE INVARIANT. With two active rows, 'which card did this buyer authorize?'
    has no answer and the purchase path picks one arbitrarily."""
    first = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(first["id"], reap_enrollment_id="enr_1")

    second = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    activated = await ledger.mark_enrollment_active(second["id"], reap_enrollment_id="enr_2")
    assert activated is not None and activated["status"] == "active"

    actives = await database.fetch_all(
        "SELECT id, status FROM reap_agentic_enrollments "
        "WHERE buyer_ref = :b AND status = 'active'",
        {"b": "bref_alice"},
    )
    assert [r["id"] for r in actives] == [second["id"]], (
        "exactly one active enrollment per buyer_ref — the older one must be demoted to 'dead' "
        "in the same transaction that promotes the new one"
    )
    demoted = await database.fetch_one(
        "SELECT status FROM reap_agentic_enrollments WHERE id = :i", {"i": first["id"]}
    )
    assert demoted["status"] == "dead"


async def test_activation_does_not_touch_another_buyers_active_row():
    """The control for the demotion: scoped to buyer_ref, not global."""
    alice = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(alice["id"], reap_enrollment_id="enr_a")
    bob = await ledger.upsert_pending_enrollment(buyer_ref="bref_bob")
    await ledger.mark_enrollment_active(bob["id"], reap_enrollment_id="enr_b")

    assert (await ledger.get_active_enrollment("bref_alice"))["id"] == alice["id"]
    assert (await ledger.get_active_enrollment("bref_bob"))["id"] == bob["id"]


async def test_get_active_enrollment_is_none_while_only_pending_exists():
    await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    assert await ledger.get_active_enrollment("bref_alice") is None


async def test_mark_dead_retires_the_enrollment_and_is_idempotent():
    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(enrollment["id"], reap_enrollment_id="enr_1")

    dead = await ledger.mark_enrollment_dead(enrollment["id"], reap_status="REVOKED")
    assert dead is not None and dead["status"] == "dead"
    assert await ledger.get_active_enrollment("bref_alice") is None
    # Second call: already dead, so nothing moved. None, not an error.
    assert await ledger.mark_enrollment_dead(enrollment["id"]) is None


async def test_activation_stores_only_the_two_permitted_card_fields():
    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    active = await ledger.mark_enrollment_active(
        enrollment["id"], card_network="visa", card_last4="4242"
    )
    assert active["card_network"] == "visa"
    assert active["card_last4"] == "4242"
    # The table has no column that could hold anything more than this.
    columns = {
        r["name"]
        for r in await database.fetch_all("PRAGMA table_info(reap_agentic_enrollments);")
    }
    forbidden = {"card_number", "pan", "cvv", "cvc", "card_expiry", "exp_month", "exp_year"}
    assert not (columns & forbidden)


@pytest.mark.parametrize("bad", ["424", "42424", "abcd", ""])
async def test_activation_refuses_a_card_last4_that_is_not_four_digits(bad):
    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    with pytest.raises(ValueError):
        await ledger.mark_enrollment_active(enrollment["id"], card_last4=bad)


# ── the self-heal ────────────────────────────────────────────────────────────────────────────

_EXPECTED_INDEXES = {
    "idx_reap_agentic_enrollments_buyer_status",
    "uq_reap_agentic_enrollments_reap_id",
    "uq_reap_agentic_enrollments_one_active",
    "idx_reap_agentic_purchases_state_poll",
    "idx_reap_agentic_purchases_buyer_created",
    "idx_reap_agentic_purchases_owner_created",
    "uq_reap_agentic_purchases_checkout",
}


async def _sqlite_objects():
    rows = await database.fetch_all(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index')"
    )
    return {(r["type"], r["name"]) for r in rows}


async def test_self_heal_creates_both_tables_and_every_index_on_an_empty_database():
    """NOTHING ELSE IN CI CHECKS THIS. tests/test_schema_guard_migration_coverage.py inspects
    ADD COLUMN only, so a missing CREATE TABLE self-heal is invisible to it — and production
    never runs db/migrations, so a table that exists only in a migration does not exist in
    production at all."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    objects = await _sqlite_objects()
    assert ("table", "reap_agentic_purchases") not in objects

    await ensure_required_schema_light()

    objects = await _sqlite_objects()
    assert ("table", "reap_agentic_enrollments") in objects
    assert ("table", "reap_agentic_purchases") in objects
    missing = _EXPECTED_INDEXES - {name for kind, name in objects if kind == "index"}
    assert not missing, f"self-heal did not create these indexes: {sorted(missing)}"


async def test_self_heal_is_idempotent_on_a_second_run():
    """It runs on EVERY startup. A second run that errors takes the whole best-effort block with
    it — including the self-heals that come after this one in the same branch."""
    before = await _sqlite_objects()
    await ensure_required_schema_light()
    await ensure_required_schema_light()
    assert await _sqlite_objects() == before
    # Still usable afterwards — "no error" is not the same as "still works".
    purchase = await _mk()
    assert await ledger.get_purchase(purchase["id"]) is not None


async def test_the_tables_are_not_in_sqlalchemy_metadata():
    """SQL-ONLY, and this is the test that keeps it that way. `metadata.create_all` runs BEFORE
    db/migrations in main.py, so declaring a `Table` for either of these would make the model —
    not the migration and not the self-heal — the thing that builds them in production."""
    from db.database import metadata

    assert "reap_agentic_purchases" not in metadata.tables
    assert "reap_agentic_enrollments" not in metadata.tables


async def test_the_self_heal_ddl_matches_the_migrations_vocabulary():
    """A heal that disagrees with its migration is worse than no heal: /health goes green, the
    table exists, and the behaviour is silently different from every environment where the
    migration ran. Scope here is the parts that carry MEANING — the CHECK vocabularies — since
    the SQLite twin legitimately differs on type names and `now()`."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1] / "db/migrations/224_reap_agentic_ledger.sql"
    ).read_text(encoding="utf-8")
    guard = (
        Path(__file__).resolve().parents[1] / "db/schema_guard.py"
    ).read_text(encoding="utf-8")

    for token in (
        "CHECK (status IN ('pending', 'active', 'dead'))",
        "'processing', 'completed', 'failed', 'refused', 'expired'",
        "CHECK (quantity > 0)",
        "WHERE status = 'active'",
        "WHERE reap_checkout_id IS NOT NULL",
    ):
        assert token in migration, f"migration 224 no longer contains {token!r}"
        assert guard.count(token) >= 2, (
            f"{token!r} appears in migration 224 but not in BOTH schema_guard self-heal "
            "branches — the Postgres and SQLite twins have drifted from the migration"
        )


async def test_the_state_vocabulary_in_the_check_matches_allowed_transitions():
    """Two declarations of the same vocabulary: the CHECK constraint and the Python map. Nothing
    reconciles them automatically, so a state added to one and not the other would be a row the
    database refuses for a transition the code believes is legal."""
    for state in sorted(ledger.PURCHASE_STATES):
        purchase = await _mk(state=state)
        assert await _state_of(purchase["id"]) == state
