"""db/reap_agentic_ledger.py — the Reap agentic rail's storage layer, on SQLite.

WHAT THIS FILE IS FOR, AND WHAT IT IS NOT FOR. Row-level semantics belong on SQLite: it is fast,
needs no server, and every guard in the module is expressible here. What CANNOT be tested here is
the driver behaviour that only Postgres has — `execute()` returning no rowcount, jsonb coming
back as text, asyncpg's datetime encoding, `pg_indexes.indexdef`, and a genuinely concurrent
second connection. That is tests/test_reap_agentic_ledger_postgres.py, and neither file subsumes
the other.

EVERY TEST BELOW IS WRITTEN TO DIE WHEN A SPECIFIC GUARD IS REMOVED, and the PR body carries the
mutation table proving it. The guards are: the `AND state IN (…)` conjunct in the advance, the
state conjunct in the CLAIM, the ownership conjunct in the get AND in the list, the PII null on
terminal, the claim-clearing on terminal, the `claimed_by IS NULL` claim conjunct, the
ALLOWED_TRANSITIONS check, the create-state restriction, the demotion gate in
mark_enrollment_active, the naive→UTC normalisation in `_bind_dt`, and the redaction default on
both owner reads.

The tables are SQL-ONLY — no SQLAlchemy `Table`, deliberately, because `metadata.create_all` runs
before db/migrations in main.py and a model would therefore become the source of truth in
production. So this file builds them the way production does: by running the schema-guard
self-heal.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
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

_PAST = "datetime('now', '-120 seconds')"
_FUTURE = "datetime('now', '+3600 seconds')"


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


async def _mk(state: str = "resolving", **over):
    """Create a purchase and, when a fixture needs some other state, move it there with a RAW
    UPDATE.

    `create_purchase` only makes 'resolving' rows now — deliberately, see
    `test_create_refuses_every_state_except_resolving` — so a test that needs a row sitting in
    'quoting' has to put it there out of band. The raw UPDATE is the right tool: it is explicitly
    NOT the code under test, so it cannot mask a defect in `transition`.
    """
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
    purchase = await ledger.create_purchase(**kwargs)
    if state != "resolving":
        await database.execute(
            "UPDATE reap_agentic_purchases SET state = :s WHERE id = :i",
            {"s": state, "i": purchase["id"]},
        )
        purchase = await ledger.get_purchase_internal(purchase["id"])
    return purchase


async def _state_of(purchase_id: str) -> str:
    row = await database.fetch_one(
        "SELECT state FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    return row["state"]


async def _set_clock_column(purchase_id: str, column: str, sql_expr: str) -> None:
    """Move a timestamp column with a SERVER-SIDE expression. A test that bound its own datetime
    would be exercising precisely the thing the module refuses to do."""
    await database.execute(
        f"UPDATE reap_agentic_purchases SET {column} = {sql_expr} WHERE id = :i",
        {"i": purchase_id},
    )


async def _age_claim(purchase_id: str, seconds: int) -> None:
    await _set_clock_column(
        purchase_id, "claimed_at", f"datetime('now', '-{int(seconds)} seconds')"
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
    purchase = await _mk()
    with pytest.raises(TypeError):
        await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="quoting", nonsense="x"
        )


async def test_transition_leaves_unmentioned_columns_alone():
    """The COALESCE contract: None means 'do not touch', which is what lets the statement be a
    single module-level constant instead of an assignment list built at call time."""
    purchase = await _mk(merchant_domain="brand.example")
    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting", reap_quote_id="q_1"
    )
    assert moved["merchant_domain"] == "brand.example"
    assert moved["reap_quote_id"] == "q_1"


# ── FINDING 1 (P0): a purchase may only be created in 'resolving' ────────────────────────────


@pytest.mark.parametrize(
    "state", sorted(ledger.PURCHASE_STATES - {"resolving"})
)
async def test_create_refuses_every_state_except_resolving(state):
    """THE P0. `create_purchase(state='refused', buyer_email=..., shipping_address=...)` used to
    succeed and return a row that KEPT both and never stamped terminal_at — because the PII rule
    lives in the transition statement and a create is not a transition. "The resolve failed
    instantly, record it and stop" is exactly what the next package will want to do, and it would
    have written the buyer's address into a permanently-terminal row.

    Parametrised over all eight non-'resolving' states, not just the four terminals: the guard is
    "only resolving", and a guard tested only against the cases someone thought of is a guard
    that drifts.
    """
    with pytest.raises(ValueError):
        await ledger.create_purchase(
            buyer_ref="b2",
            agent_id="agent_one",
            agent_user_ref_hash="hash_alice",
            state=state,
            buyer_email="victim@example.test",
            shipping_address={"line1": "1 Test Way"},
        )
    count = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases")
    assert count == 0, "the refusal must happen before the INSERT, not after it"


async def test_an_instant_refusal_is_create_then_transition_and_it_nulls_the_pii():
    """The replacement path, end to end — so the refusal above is not merely a wall but a wall
    with a door next to it."""
    purchase = await _mk()
    refused = await ledger.transition(
        purchase["id"],
        from_states=["resolving"],
        to_state="refused",
        refusal_reason="no_match",
    )
    assert refused["state"] == "refused"
    assert refused["buyer_email"] is None
    assert refused["shipping_address"] is None
    assert refused["terminal_at"] is not None
    assert refused["refusal_reason"] == "no_match"


# ── C2: ownership and buyer_ref must be real ─────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["buyer_ref", "agent_id", "agent_user_ref_hash"])
@pytest.mark.parametrize("bad", [None, "", "   "])
async def test_create_refuses_blank_ownership(field, bad):
    """A row whose agent_id or agent_user_ref_hash is NULL is invisible to its own owner — the
    ownership conjunct compares with `=`, and NULL = NULL is NULL — so it could never be read
    back. And "" is a bucket two unrelated callers would silently share."""
    kwargs = dict(
        buyer_ref="bref_alice", agent_id="agent_one", agent_user_ref_hash="hash_alice"
    )
    kwargs[field] = bad
    with pytest.raises(ValueError):
        await ledger.create_purchase(**kwargs)


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
    purchase = await _mk()
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


@pytest.mark.parametrize("terminal", _TERMINALS)
async def test_a_terminal_transition_also_clears_the_claim(terminal):
    """FINDING 2, third half. A terminal row that still carries a claim is a lease nobody will
    ever release through the normal path, and it is the state that made the unclearable-claim bug
    reachable at all. Cleared in the same UPDATE, for the same reason the PII is."""
    src = _REACHES_TERMINAL[terminal]
    purchase = await _mk(state=src)
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = 'w1', "
        "claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase["id"]},
    )
    done = await ledger.transition(purchase["id"], from_states=[src], to_state=terminal)
    assert done["claimed_by"] is None
    assert done["claimed_at"] is None


async def test_a_non_terminal_transition_leaves_a_live_claim_alone():
    """The control for the clearing above: a worker mid-poll advancing 'awaiting_approval' to
    'processing' must not drop its own lease."""
    purchase = await _mk(state="awaiting_approval")
    await database.execute(
        "UPDATE reap_agentic_purchases SET claimed_by = 'w1', "
        "claimed_at = CURRENT_TIMESTAMP WHERE id = :i",
        {"i": purchase["id"]},
    )
    moved = await ledger.transition(
        purchase["id"], from_states=["awaiting_approval"], to_state="processing"
    )
    assert moved["claimed_by"] == "w1"
    assert moved["claimed_at"] is not None


# ── ownership, and B2's redaction ────────────────────────────────────────────────────────────


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
        (None, None),  # M3: no identity at all
        (None, "hash_alice"),
        ("agent_one", None),
    ],
)
async def test_get_for_owner_refuses_a_non_owner(agent_id, user_hash):
    """BOTH halves of the conjunct, plus the None cases.

    The None rows are not decoration: the natural "make it optional" refactor is
    `(:agent_id IS NULL OR agent_id = :agent_id)`, which turns a missing identity into a WILDCARD
    and hands any caller anyone's purchase. With the conjunct as written, NULL = NULL is NULL and
    nothing matches. The Postgres arm had these cases and this one did not; now both do.
    """
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
    assert await ledger.list_purchases_for_owner(None, None) == []


async def test_owner_reads_are_redacted_by_default():
    """B2. The owner reads are what a route returns, and a route returns what it is given. The
    PII columns are NULLed on terminal precisely so they cannot leak — handing them out on every
    read before then is the same leak through a different door."""
    purchase = await _mk()
    got = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    listed = await ledger.list_purchases_for_owner("agent_one", "hash_alice")

    for view in (got, listed[0]):
        leaked = sorted(set(view) & ledger._PRIVATE_PURCHASE_COLUMNS)
        assert not leaked, f"owner read leaked private columns: {leaked}"
        # Still useful: the things a caller actually needs are all there.
        for kept in ("id", "state", "merchant_domain", "product_key", "currency", "created_at"):
            assert kept in view


async def test_include_private_is_available_and_explicit():
    """The escape hatch exists — an internal caller may ask — and asking is greppable."""
    purchase = await _mk()
    full = await ledger.get_purchase_for_owner(
        purchase["id"], "agent_one", "hash_alice", include_private=True
    )
    assert full["buyer_email"] == "alice@example.test"
    assert full["shipping_address"]["city"] == "Springfield"

    listed = await ledger.list_purchases_for_owner(
        "agent_one", "hash_alice", include_private=True
    )
    assert listed[0]["buyer_email"] == "alice@example.test"


async def test_public_view_survives_a_new_column_being_added():
    """`public_purchase_view` removes a denylist rather than keeping an allowlist, so a column
    added to the table later is VISIBLE (and reviewable) instead of silently vanishing."""
    view = ledger.public_purchase_view({"id": "rp_1", "state": "resolving", "brand_new": 1})
    assert view["brand_new"] == 1
    assert ledger.public_purchase_view(None) is None


async def test_get_purchase_internal_is_named_internal_and_is_not_exported():
    """C3. The unscoped, unredacted read is the one a route author must not reach for by
    accident, so it carries the suffix and stays out of `__all__`."""
    assert "get_purchase_internal" not in ledger.__all__
    assert not hasattr(ledger, "get_purchase"), (
        "the old un-suffixed name is the trap; it must not come back"
    )
    purchase = await _mk()
    got = await ledger.get_purchase_internal(purchase["id"])
    assert got["buyer_email"] == "alice@example.test"


async def test_list_limit_and_offset_are_capped_and_floored():
    """M9. `limit` reaches a LIMIT clause and `offset` an OFFSET; an unbounded limit is a way to
    ask for the whole table in one request, and a negative offset is a driver error on one engine
    and a silent zero on the other."""
    for index in range(3):
        await _mk(buyer_ref=f"bref_{index}")

    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", limit=1)) == 1
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", limit=0)) == 1
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", limit=-5)) == 1
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", limit=9999)) == 3
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", offset=-3)) == 3
    assert len(await ledger.list_purchases_for_owner("agent_one", "hash_alice", offset=2)) == 1


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

    fetched = await ledger.get_purchase_internal(purchase["id"])
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


# ── FINDING 5: naive datetimes are UTC, not local ────────────────────────────────────────────


@pytest.mark.parametrize("tz", ["America/Los_Angeles", "Asia/Tokyo"])
async def test_a_naive_datetime_is_read_as_utc_not_as_local_time(tz):
    """FINDING 5. `_bind_dt`'s `.replace(tzinfo=utc)` had NO killing test — the reviewer deleted
    it and the whole suite stayed green, even under a shifted TZ, because every other test built
    its datetimes already-aware.

    The divergence is not cosmetic: read as local time, a naive 12:00 written from a Pacific box
    is stored as 19:00 or 20:00 UTC. On next_poll_at that is a poll eight hours late; on
    reap_quote_expires_at it is a quote we believe is live after Reap has expired it.

    Two timezones, one either side of UTC, so a mutant cannot pass by coincidence of sign.
    """
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


async def test_an_aware_datetime_in_another_zone_is_stored_as_the_same_instant():
    """The control: the guard must NORMALISE, not blindly stamp UTC onto everything. An aware
    04:00-08:00 is 12:00 UTC and must come back as 12:00 UTC."""
    aware = datetime(2026, 9, 18, 4, 0, 0, tzinfo=timezone(timedelta(hours=-8)))
    purchase = await _mk(next_poll_at=aware)
    assert purchase["next_poll_at"] == datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


# ── the claim pair ───────────────────────────────────────────────────────────────────────────


async def _make_due(**over):
    """A purchase sitting in 'awaiting_approval' and due to be polled."""
    purchase = await _mk(state="awaiting_approval", **over)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    return purchase


async def test_create_defaults_next_poll_at_to_now_so_a_new_row_is_immediately_due():
    """D5. A created purchase with next_poll_at NULL is a row the poller's SELECT can never
    offer — `next_poll_at IS NOT NULL AND next_poll_at <= CURRENT_TIMESTAMP` — so it would sit in
    'resolving' forever. Defaulted SERVER-SIDE, so no client clock decides it."""
    purchase = await _mk()
    assert purchase["next_poll_at"] is not None
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert [c["id"] for c in claimed] == [purchase["id"]]


async def test_claim_takes_a_due_purchase_and_stamps_the_worker():
    purchase = await _make_due()
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert [c["id"] for c in claimed] == [purchase["id"]]
    assert claimed[0]["claimed_by"] == "worker_a"
    assert claimed[0]["claimed_at"] is not None
    assert claimed[0]["attempts"] == 1


async def test_a_second_claimer_gets_nothing():
    """CLAIM EXCLUSIVITY. The `AND claimed_by IS NULL` conjunct in the UPDATE is the guard — the
    SELECT that picks candidates is advisory and can be stale."""
    await _make_due()
    first = await ledger.claim_due_purchases("worker_a", limit=5)
    second = await ledger.claim_due_purchases("worker_b", limit=5)
    assert len(first) == 1
    assert second == [], "a purchase already leased must not be claimable by a second worker"


async def test_two_workers_racing_the_same_candidate_split_it_one_to_one():
    await _make_due()
    a, b = await asyncio.gather(
        ledger.claim_due_purchases("worker_a", limit=5),
        ledger.claim_due_purchases("worker_b", limit=5),
    )
    assert len(a) + len(b) == 1, "exactly one worker may hold a lease on one purchase"


async def test_the_claim_statement_refuses_a_row_that_went_terminal_after_the_select():
    """FINDING 2, first half — and the SELECT cannot prove it, so the STATEMENT is exercised
    directly.

    The interleaving: worker A's SELECT offers a due 'processing' row; worker B completes it;
    worker A's claim UPDATE then lands. With only `claimed_by IS NULL` that claim SUCCEEDS and
    the poller is handed a finished purchase to poll. Reproduced on both dialects before the fix.
    """
    purchase = await _mk(state="processing")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)

    # The SELECT has offered it. Now it goes terminal underneath us.
    await ledger.transition(
        purchase["id"], from_states=["processing"], to_state="completed"
    )

    row = await database.fetch_one(
        ledger._CLAIM_PURCHASE_SQL, {"id": purchase["id"], "worker_id": "worker_a"}
    )
    assert row is None, (
        "the claim UPDATE must re-check the state: between the candidate SELECT and this "
        "statement the row reached a terminal state, and a poller must never be handed one"
    )
    assert (await ledger.get_purchase_internal(purchase["id"]))["attempts"] == 0


async def test_a_purchase_whose_poll_is_not_due_is_not_claimed():
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "next_poll_at", _FUTURE)
    assert await ledger.claim_due_purchases("worker_a", limit=5) == []


@pytest.mark.parametrize("terminal", _TERMINALS)
async def test_a_terminal_purchase_is_never_claimed(terminal):
    purchase = await _mk(state=terminal)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
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
    assert await ledger.claim_due_purchases("worker_b", limit=5) == []


async def test_release_by_the_wrong_worker_does_nothing():
    """A worker whose lease was already requeued and handed on must not be able to clear the new
    holder's claim on its way out."""
    purchase = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    assert await ledger.release_claim(purchase["id"], "worker_b") is None
    still = await ledger.get_purchase_internal(purchase["id"])
    assert still["claimed_by"] == "worker_a"


async def test_claim_due_purchases_has_no_lease_seconds_parameter():
    """D4. It accepted one, documented it as "what requeue_stale_claims measures against", and
    connected it to nothing — a caller passing 60 here and taking the default 300 there had a
    300-second lease and no way to tell."""
    import inspect

    params = inspect.signature(ledger.claim_due_purchases).parameters
    assert "lease_seconds" not in params, (
        "the lease length belongs to requeue_stale_claims alone; a parameter that reads as "
        "configuration and configures nothing is worse than no parameter"
    )


async def test_requeue_frees_a_stale_lease_and_leaves_a_fresh_one_alone():
    stale = await _make_due()
    fresh = await _make_due(buyer_ref="bref_fresh")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(stale["id"], 900)

    moved = await ledger.requeue_stale_claims(lease_seconds=300, limit=10)
    assert moved == 1, "the count must come from RETURNING, not from the driver's rowcount"

    assert (await ledger.get_purchase_internal(stale["id"]))["claimed_by"] is None
    assert (await ledger.get_purchase_internal(fresh["id"]))["claimed_by"] == "worker_a"


async def test_requeue_frees_a_stale_claim_on_a_TERMINAL_row_too():
    """FINDING 2, second half. The requeue used to filter its inner SELECT to the pollable
    states, which made a dead pod's claim on a terminal row UNCLEARABLE — measured, a
    99999-second-old claim freed 0 rows. A claim on a terminal row is garbage to clear, not work
    to requeue."""
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    await ledger.claim_due_purchases("worker_a", limit=5)
    # It finishes while the worker holds the lease, by a path that does not clear the claim.
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'completed' WHERE id = :i",
        {"i": purchase["id"]},
    )
    await _age_claim(purchase["id"], 99999)

    assert await ledger.requeue_stale_claims(lease_seconds=300, limit=10) == 1
    assert (await ledger.get_purchase_internal(purchase["id"]))["claimed_by"] is None


async def test_clearing_a_terminal_rows_claim_does_not_make_it_claimable_again():
    """The other half of that fix, and the reason it is safe: the claim UPDATE carries its own
    state conjunct, so a freed terminal row is still not work."""
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    await ledger.claim_due_purchases("worker_a", limit=5)
    await database.execute(
        "UPDATE reap_agentic_purchases SET state = 'failed' WHERE id = :i",
        {"i": purchase["id"]},
    )
    await _age_claim(purchase["id"], 99999)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)

    assert await ledger.claim_due_purchases("worker_b", limit=5) == []


async def test_a_requeued_purchase_is_claimable_again():
    stale = await _make_due()
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(stale["id"], 900)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)

    reclaimed = await ledger.claim_due_purchases("worker_b", limit=5)
    assert [c["id"] for c in reclaimed] == [stale["id"]]
    assert reclaimed[0]["attempts"] == 2, "attempts counts claims, so a requeue+reclaim is two"


@pytest.mark.parametrize("bad", [0, 5, 29, -1])
async def test_requeue_refuses_a_lease_shorter_than_thirty_seconds(bad):
    """D4. This was `max(30, lease_seconds)` — an operator asking for a 5-second lease got 30 and
    no indication, which is the shape where someone tunes a number and watches nothing change."""
    with pytest.raises(ValueError):
        await ledger.requeue_stale_claims(lease_seconds=bad)


async def test_requeue_never_binds_a_python_datetime_for_its_cutoff():
    """The cutoff is `datetime('now', ...)` / `CURRENT_TIMESTAMP - interval`, computed by the
    SERVER. A Python-bound cutoff was measured seven hours wrong on this driver."""
    for sql in (ledger._REQUEUE_STALE_CLAIMS_SQL, ledger._REQUEUE_STALE_CLAIMS_SQL_SQLITE):
        assert ":cutoff" not in sql and ":stale_before" not in sql
    assert "CURRENT_TIMESTAMP - (:lease_seconds * INTERVAL '1 second')" in (
        ledger._REQUEUE_STALE_CLAIMS_SQL
    )
    assert "datetime('now', :lease_window)" in ledger._REQUEUE_STALE_CLAIMS_SQL_SQLITE


# ── D4/D5: the two bulk sweeps ───────────────────────────────────────────────────────────────


async def test_the_bulk_sweeps_only_use_legal_edges():
    """Both sweeps bypass `transition`, so NOTHING at runtime enforces ALLOWED_TRANSITIONS on
    them. This is what does."""
    for src in ledger._EXPIRE_SOURCE_STATES:
        assert "expired" in ledger.ALLOWED_TRANSITIONS[src], f"{src} -> expired is not legal"
    for src in ledger._FAIL_EXHAUSTED_SOURCE_STATES:
        assert "failed" in ledger.ALLOWED_TRANSITIONS[src], f"{src} -> failed is not legal"


@pytest.mark.parametrize("state", ["needs_enrollment", "awaiting_approval"])
async def test_expire_moves_an_overdue_hosted_page_and_scrubs_it(state):
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
    assert row["buyer_email"] is None
    assert row["shipping_address"] is None
    assert row["terminal_at"] is not None
    assert row["claimed_by"] is None and row["claimed_at"] is None
    assert row["last_error_code"] == "hosted_url_expired"


async def test_expire_leaves_a_live_page_and_a_wrong_state_alone():
    """The controls. Without them a sweep that expired everything would pass the test above."""
    live = await _mk(state="awaiting_approval")
    await _set_clock_column(live["id"], "hosted_url_expires_at", _FUTURE)
    wrong_state = await _mk(state="quoting", buyer_ref="bref_q")
    await _set_clock_column(wrong_state["id"], "hosted_url_expires_at", _PAST)
    no_deadline = await _mk(state="awaiting_approval", buyer_ref="bref_n")

    assert await ledger.expire_overdue_purchases() == []
    for row_id in (live["id"], wrong_state["id"], no_deadline["id"]):
        assert (await ledger.get_purchase_internal(row_id))["state"] != "expired"


async def test_expire_is_idempotent_and_races_cleanly_with_a_transition():
    """A purchase that reached a terminal state by the ordinary path must not be re-expired, and
    two sweeps must not both claim the same row."""
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)

    first, second = await asyncio.gather(
        ledger.expire_overdue_purchases(), ledger.expire_overdue_purchases()
    )
    assert len(first) + len(second) == 1, "exactly one sweep may move a row"
    assert await ledger.expire_overdue_purchases() == []


async def test_expire_loses_to_a_completion_that_got_there_first():
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)
    done = await ledger.transition(
        purchase["id"], from_states=["awaiting_approval"], to_state="completed"
    )
    assert done is not None
    assert await ledger.expire_overdue_purchases() == []
    assert (await ledger.get_purchase_internal(purchase["id"]))["state"] == "completed"


async def test_fail_exhausted_moves_a_stuck_row_and_scrubs_it():
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
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["terminal_at"] is not None
    assert row["claimed_by"] is None


async def test_fail_exhausted_leaves_rows_under_the_bound_and_terminal_rows_alone():
    under = await _mk(state="processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 4 WHERE id = :i", {"i": under["id"]}
    )
    already = await _mk(state="completed", buyer_ref="bref_c")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 99 WHERE id = :i", {"i": already["id"]}
    )

    assert await ledger.fail_exhausted_purchases(5) == []
    assert (await ledger.get_purchase_internal(already["id"]))["state"] == "completed"


async def test_fail_exhausted_races_cleanly():
    purchase = await _mk(state="processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 9 WHERE id = :i", {"i": purchase["id"]}
    )
    first, second = await asyncio.gather(
        ledger.fail_exhausted_purchases(5), ledger.fail_exhausted_purchases(5)
    )
    assert len(first) + len(second) == 1


@pytest.mark.parametrize("bad", [0, -1])
async def test_fail_exhausted_refuses_a_nonsense_bound(bad):
    with pytest.raises(ValueError):
        await ledger.fail_exhausted_purchases(bad)


# ── E5: the minor-unit helper ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,currency,expected",
    [
        ("42.50", "USD", 4250),
        ("0.01", "USD", 1),
        (500, "JPY", 500),  # zero-decimal currency
        ("500", "KRW", 500),
        ("1.005", "USD", None),  # more decimals than the currency has
        ("500.5", "JPY", None),
        ("-1.00", "USD", None),
        ("0", "USD", None),
        (42.50, "USD", None),  # a binary float is refused outright
        ("nan", "USD", None),
        ("inf", "USD", None),
        (None, "USD", None),
        ("abc", "USD", None),
    ],
)
async def test_amount_minor_or_none_refuses_rather_than_rounds(value, currency, expected):
    """E5. The first cut of this module re-exported `major_to_minor` and used it nowhere — a
    decorative import that reads as an endorsed entry point. This is the wrapper the ledger's own
    BIGINT minor-unit columns need, and these are its tests. It delegates to
    services/reap_webhooks, which is the ONE converter; a second implementation would be a second
    rounding policy."""
    assert ledger.amount_minor_or_none(value, currency) == expected


async def test_amount_minor_or_none_is_the_shared_converter_not_a_copy():
    from services.reap_webhooks import major_to_minor

    assert ledger.major_to_minor is major_to_minor


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


async def test_activating_a_DEAD_row_leaves_the_live_enrollment_alone():
    """FINDING 3. `mark_enrollment_active(<dead id>)` used to demote the buyer's live enrollment
    and THEN fail to promote the dead one — leaving the buyer with NO active card, reached by a
    call that answered None as though nothing had happened. Worse than the state it started in.
    """
    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    dead = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_dead(dead["id"])

    assert await ledger.mark_enrollment_active(dead["id"]) is None

    still = await ledger.get_active_enrollment("bref_alice")
    assert still is not None, "the buyer must still have an active enrollment"
    assert still["id"] == live["id"]


async def test_activating_an_id_that_does_not_exist_changes_nothing():
    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")

    assert await ledger.mark_enrollment_active("re_nonexistent") is None
    assert (await ledger.get_active_enrollment("bref_alice"))["id"] == live["id"]


async def test_reactivating_the_already_active_row_is_idempotent():
    """A redelivered webhook must not be punished — and must not demote its own target."""
    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")

    again = await ledger.mark_enrollment_active(live["id"], card_network="visa")
    assert again is not None and again["status"] == "active"
    assert again["card_network"] == "visa"
    assert (await ledger.get_active_enrollment("bref_alice"))["id"] == live["id"]


async def test_two_active_rows_for_one_buyer_are_refused_by_the_database():
    """The SELF-HEAL-BUILT schema's partial unique index, tested BEHAVIOURALLY. A token check on
    the DDL text cannot tell UNIQUE from non-unique — the reviewer changed CREATE UNIQUE INDEX to
    CREATE INDEX in both branches and every test stayed green."""
    a = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(a["id"], reap_enrollment_id="enr_1")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_second', 'bref_alice', 'pending')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await database.execute(
            "UPDATE reap_agentic_enrollments SET status = 'active' WHERE id = 're_second'"
        )


async def test_two_pending_rows_with_null_reap_ids_coexist():
    """The control for the PARTIAL half of that index: NULLs must not collide, or no buyer could
    ever have a second pending enrollment."""
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_p1', 'bref_a', 'pending')"
    )
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_p2', 'bref_b', 'pending')"
    )
    count = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_enrollments")
    assert count == 2


async def test_a_duplicate_reap_enrollment_id_is_refused():
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status, reap_enrollment_id) "
        "VALUES ('re_d1', 'bref_a', 'pending', 'enr_dup')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments "
            "(id, buyer_ref, status, reap_enrollment_id) "
            "VALUES ('re_d2', 'bref_b', 'pending', 'enr_dup')"
        )


async def test_a_duplicate_reap_checkout_id_is_refused():
    a = await _mk()
    b = await _mk(buyer_ref="bref_b")
    await database.execute(
        "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_dup' WHERE id = :i",
        {"i": a["id"]},
    )
    with pytest.raises(sqlite3.IntegrityError):
        await database.execute(
            "UPDATE reap_agentic_purchases SET reap_checkout_id = 'chk_dup' WHERE id = :i",
            {"i": b["id"]},
        )


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
    assert await ledger.mark_enrollment_dead(enrollment["id"]) is None


async def test_activation_stores_only_the_two_permitted_card_fields():
    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    active = await ledger.mark_enrollment_active(
        enrollment["id"], card_network="visa", card_last4="4242"
    )
    assert active["card_network"] == "visa"
    assert active["card_last4"] == "4242"
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


async def test_a_unique_violation_becomes_none_not_a_driver_exception():
    """E4, SQLite arm. Every other lost race in this module answers None; a caller that got a raw
    sqlite3/asyncpg exception could not tell "you lost" from "the database is broken". The
    two-connection proof is the Postgres one."""
    assert ledger._is_unique_violation(
        sqlite3.IntegrityError("UNIQUE constraint failed: reap_agentic_enrollments.buyer_ref")
    )
    assert not ledger._is_unique_violation(ValueError("something else"))
    # And a wrapped one is still recognised.
    outer = RuntimeError("wrapped")
    outer.__cause__ = sqlite3.IntegrityError("UNIQUE constraint failed: x")
    assert ledger._is_unique_violation(outer)


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

# The three that must be UNIQUE, and the predicate each one is partial on.
_EXPECTED_UNIQUE_INDEXES = {
    "uq_reap_agentic_enrollments_reap_id": "reap_enrollment_id IS NOT NULL",
    "uq_reap_agentic_enrollments_one_active": "status = 'active'",
    "uq_reap_agentic_purchases_checkout": "reap_checkout_id IS NOT NULL",
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


async def test_the_self_heal_built_indexes_are_actually_unique():
    """FINDING 4, SQLite arm. Reading the DDL the self-heal STORED, not the DDL the source file
    contains: `sqlite_master.sql` is what the database actually built, so a
    CREATE UNIQUE INDEX silently downgraded to CREATE INDEX shows up here.

    Paired with the behavioural tests above (two active rows, duplicate reap id, duplicate
    checkout id), which is the half a text comparison cannot do.
    """
    rows = await database.fetch_all(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name LIKE '%reap_agentic%'"
    )
    stored = {r["name"]: (r["sql"] or "") for r in rows}

    for name, predicate in _EXPECTED_UNIQUE_INDEXES.items():
        assert name in stored, f"{name} was not built by the self-heal"
        ddl = " ".join(stored[name].split())
        assert "CREATE UNIQUE INDEX" in ddl.upper(), (
            f"{name} was built as a NON-unique index: {ddl!r}. The invariant it carries is the "
            "whole reason it exists."
        )
        assert predicate in ddl, (
            f"{name} lost its partial predicate {predicate!r}: {ddl!r}"
        )

    for name in _EXPECTED_INDEXES - set(_EXPECTED_UNIQUE_INDEXES):
        ddl = " ".join(stored.get(name, "").split())
        assert "CREATE INDEX" in ddl.upper() and "UNIQUE" not in ddl.upper(), (
            f"{name} is not meant to be unique: {ddl!r}"
        )


async def test_self_heal_is_idempotent_on_a_second_run():
    """It runs on EVERY startup. A second run that errors takes the whole best-effort block with
    it — including the self-heals that come after this one in the same branch."""
    before = await _sqlite_objects()
    await ensure_required_schema_light()
    await ensure_required_schema_light()
    assert await _sqlite_objects() == before
    purchase = await _mk()
    assert await ledger.get_purchase_internal(purchase["id"]) is not None


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
    migration ran. Scope here is the parts that carry MEANING and that the Postgres schema
    comparison cannot reach on this dialect — the CHECK vocabularies and the partial predicates.
    The strength of this test is limited (a token is not a schema), which is exactly why the
    Postgres arm compares `pg_indexes.indexdef` and `pg_get_constraintdef` instead."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (root / "db/migrations/224_reap_agentic_ledger.sql").read_text(encoding="utf-8")
    guard = (root / "db/schema_guard.py").read_text(encoding="utf-8")

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
        purchase = await _mk(state=state, buyer_ref=f"bref_{state}")
        assert await _state_of(purchase["id"]) == state
