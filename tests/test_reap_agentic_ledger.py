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


# ── FINDING 1 (P1): the claim fence on transition ────────────────────────────────────────────


async def test_a_stalled_worker_cannot_advance_a_row_whose_lease_it_lost():
    """THE INTERLEAVING, END TO END. Reproduced on both dialects before the fence existed.

    Worker A claims a 'quoting' row and stalls. Its lease ages out; `requeue_stale_claims` frees
    it. Worker B claims the row and advances it to 'awaiting_approval'. A then wakes up holding
    nothing and advances it to 'processing' with ITS OWN checkout id — and used to succeed. The
    row would land in 'processing' carrying a checkout minted by a worker that does not own it,
    which is a buyer charged for the wrong checkout.

    `from_states` cannot catch this: A's view of the STATE is correct. It is A's view of
    OWNERSHIP that is stale, and only the fence sees that."""
    purchase = await _mk(state="quoting")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)

    await ledger.claim_due_purchases("worker_A", limit=5)
    await _age_claim(purchase["id"], 900)
    assert await ledger.requeue_stale_claims(lease_seconds=300, limit=10) == 1

    claimed_b = await ledger.claim_due_purchases("worker_B", limit=5)
    assert claimed_b[0]["claimed_by"] == "worker_B"
    await ledger.transition_as_holder(
        purchase["id"], "worker_B", from_states=["quoting"],
        to_state="awaiting_approval", reap_quote_id="QUOTE_FROM_B",
    )

    lost = await ledger.transition_as_holder(
        purchase["id"], "worker_A", from_states=["awaiting_approval"],
        to_state="processing", reap_checkout_id="CHECKOUT_FROM_A",
    )
    assert lost is None, "a worker that no longer holds the lease must not advance the row"

    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "awaiting_approval"
    assert row["reap_checkout_id"] is None, "worker A's checkout id must never have landed"
    assert row["claimed_by"] == "worker_B"


async def test_a_fenced_transition_by_the_true_holder_succeeds():
    """The control. A fence that refused everybody would pass the test above."""
    purchase = await _mk(state="quoting")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    await ledger.claim_due_purchases("worker_A", limit=5)

    moved = await ledger.transition_as_holder(
        purchase["id"], "worker_A", from_states=["quoting"],
        to_state="awaiting_approval", reap_quote_id="q_1",
    )
    assert moved is not None
    assert moved["state"] == "awaiting_approval"
    assert moved["reap_quote_id"] == "q_1"
    assert moved["claimed_by"] == "worker_A", "a non-terminal advance keeps the lease"


async def test_a_fenced_transition_on_an_unclaimed_row_returns_none():
    """Nobody holds the lease, so nobody may advance it AS a holder. The unfenced form is the
    one for callers that legitimately hold nothing."""
    purchase = await _mk(state="quoting")
    assert await ledger.transition_as_holder(
        purchase["id"], "worker_A", from_states=["quoting"], to_state="awaiting_approval"
    ) is None
    assert await _state_of(purchase["id"]) == "quoting"

    unfenced = await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    )
    assert unfenced is not None, (
        "holder=None must leave the statement unfenced — the route that starts a purchase holds "
        "no lease"
    )


async def test_a_terminal_transition_by_the_holder_still_clears_the_claim():
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    await ledger.claim_due_purchases("worker_A", limit=5)

    done = await ledger.transition_as_holder(
        purchase["id"], "worker_A", from_states=["awaiting_approval"], to_state="completed"
    )
    assert done is not None
    assert done["claimed_by"] is None and done["claimed_at"] is None
    assert done["buyer_email"] is None and done["terminal_at"] is not None


async def test_claimed_by_is_not_a_writable_transition_field():
    """The poller must not be able to build the fence itself out of a field write — that is what
    makes `holder=` the only way to express it."""
    purchase = await _mk()
    assert "claimed_by" not in ledger._TRANSITION_FIELDS
    with pytest.raises(TypeError):
        await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="quoting", claimed_by="w"
        )


async def test_transition_as_holder_requires_a_worker_id():
    purchase = await _mk()
    for bad in (None, "", "  "):
        with pytest.raises(ValueError):
            await ledger.transition_as_holder(
                purchase["id"], bad, from_states=["resolving"], to_state="quoting"
            )


async def test_transition_rejects_a_blank_holder():
    purchase = await _mk()
    with pytest.raises(ValueError):
        await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="quoting", holder="   "
        )


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


# WRITTEN OUT HERE, NOT IMPORTED FROM THE MODULE. This is the whole point of the allowlist fix:
# the previous test intersected the view with `_PRIVATE_PURCHASE_COLUMNS`, i.e. it asked "is the
# view free of the things the module currently calls private?" — which cannot notice the
# definition of private SHRINKING. Deleting "shipping_address" or "agent_user_ref_hash" from that
# set left 203/203 and 86/86 green. An independent literal cannot be satisfied that way: to
# change what an owner sees you have to change this list too, deliberately.
_EXPECTED_PUBLIC_COLUMNS = {
    "id", "state", "merchant_domain", "product_key", "variant_key", "product_name",
    "variant_title", "brand", "category", "quantity", "currency", "our_price_minor",
    "quoted_total_minor", "final_total_minor", "shipping_minor", "tax_minor", "hosted_url",
    "hosted_url_expires_at", "reap_quote_expires_at", "reap_order_id", "refusal_reason",
    "last_error_code", "created_at", "updated_at", "terminal_at",
    # mig 233. THE BUYER'S OWN CONSENT TAG IS THEIRS TO READ — the one column on this row that
    # records something the OWNER did, rather than something we or a partner did. Not PII (it
    # names a document, not a person), which is also why the terminal write leaves it while it
    # NULLs the email and the address. Added here DELIBERATELY, as an edit to this literal: the
    # partition test below forces the choice, and the allowlist's whole design is that a new
    # column is invisible until somebody writes it into both lists.
    "consent_version", "consented_at",
}
_EXPECTED_NEVER_PUBLIC = {
    "buyer_ref", "agent_id", "agent_user_ref_hash", "buyer_email", "shipping_address",
    "enrollment_id", "click_id", "return_url", "reap_product_id", "reap_variant_id",
    "reap_quote_id", "reap_checkout_id", "queries_tried", "attempts", "next_poll_at",
    "claimed_by", "claimed_at", "state_entered_at",
    # mig 225. Not PII and not identity — catalog assertions the caller made — so these are in
    # the never-public list on the "keep the allowlist minimal" argument rather than on a
    # confidentiality one. Written here deliberately, because
    # test_the_two_expected_column_sets_partition_the_table forces the choice to be made.
    "accept_variant_labels", "also_accept_domains", "market_country",
    # mig 229. cart_url carries our click id and the merchant's variant id; item_source is how
    # the quote is asked for. Neither is the owner's business — the brief says so for cart_url,
    # and the minimal-allowlist rule decides item_source.
    "item_source", "cart_url",
}


async def test_owner_reads_contain_exactly_the_public_columns():
    """B2, as an EQUALITY against a literal written in this file. `>=` or "no private keys" would
    both pass a view that quietly grew a column."""
    purchase = await _mk()
    got = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    listed = await ledger.list_purchases_for_owner("agent_one", "hash_alice")

    for view in (got, listed[0]):
        assert set(view) == _EXPECTED_PUBLIC_COLUMNS, (
            f"unexpected: {sorted(set(view) - _EXPECTED_PUBLIC_COLUMNS)}; "
            f"missing: {sorted(_EXPECTED_PUBLIC_COLUMNS - set(view))}"
        )
        assert not (set(view) & _EXPECTED_NEVER_PUBLIC)


async def test_the_two_expected_column_sets_partition_the_table():
    """Guard the guard: if a column exists that is in neither list, this test — not a leak — is
    what notices."""
    columns = {
        r["name"] for r in await database.fetch_all("PRAGMA table_info(reap_agentic_purchases);")
    }
    unclassified = columns - _EXPECTED_PUBLIC_COLUMNS - _EXPECTED_NEVER_PUBLIC
    assert not unclassified, (
        f"these columns are in neither the public nor the never-public list: "
        f"{sorted(unclassified)} — decide which, in this test AND in "
        "db/reap_agentic_ledger.PUBLIC_PURCHASE_COLUMNS"
    )


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


async def test_a_column_added_to_the_table_later_does_not_appear_in_the_view():
    """The allowlist's defining property, tested THROUGH THE DATABASE rather than on a synthetic
    dict: a column added to the real table shows up in `SELECT *`, reaches `get_purchase_for_owner`
    — and must still not reach the caller until somebody adds it to PUBLIC_PURCHASE_COLUMNS.

    Under a denylist this is exactly backwards: the new column is public by default, and whoever
    added it has to remember a file they were not editing."""
    purchase = await _mk()
    await database.execute(
        "ALTER TABLE reap_agentic_purchases ADD COLUMN internal_secret_note TEXT"
    )
    await database.execute(
        "UPDATE reap_agentic_purchases SET internal_secret_note = 'do not leak' WHERE id = :i",
        {"i": purchase["id"]},
    )

    internal = await ledger.get_purchase_internal(purchase["id"])
    assert internal["internal_secret_note"] == "do not leak", (
        "precondition: the new column really is on the row the view is built from"
    )

    got = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    assert "internal_secret_note" not in got
    listed = await ledger.list_purchases_for_owner("agent_one", "hash_alice")
    assert "internal_secret_note" not in listed[0]
    assert set(got) == _EXPECTED_PUBLIC_COLUMNS


async def test_public_view_handles_none_and_partial_rows():
    assert ledger.public_purchase_view(None) is None
    # A partial row projects to a partial view, not to one padded with None.
    assert ledger.public_purchase_view({"id": "rp_1", "buyer_email": "x@y.z"}) == {"id": "rp_1"}


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


@pytest.mark.parametrize("state", ["resolving", "quoting", "processing"])
async def test_a_claim_in_a_WORKING_state_increments_attempts(state):
    """P2-3. `attempts` is the input to `fail_exhausted_purchases`, so it has to mean "we tried
    and it did not finish"."""
    purchase = await _mk(state=state)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert claimed[0]["attempts"] == 1


@pytest.mark.parametrize("state", ["awaiting_approval", "needs_enrollment"])
async def test_a_claim_in_a_WAITING_state_does_not_increment_attempts(state):
    """P2-3, the half that matters. A buyer with the hosted page open for an hour, polled every
    30 seconds, reaches ~120 claims on a purchase where NOTHING has gone wrong — and
    `fail_exhausted_purchases` would kill it for being patient. Waiting on a human is not a
    failed attempt; those states are bounded by `expire_overdue_purchases`, on a clock.

    Without this, the poller would have to choose `max_attempts` against its own poll cadence —
    a coupling between two packages that nothing would check."""
    purchase = await _mk(state=state)
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)

    for _ in range(3):
        await ledger.claim_due_purchases("worker_a", limit=5)
        await ledger.release_claim(purchase["id"], "worker_a")

    assert (await ledger.get_purchase_internal(purchase["id"]))["attempts"] == 0


async def test_the_attempt_exempt_states_are_the_waiting_ones():
    """Parsed out of the claim statement itself, so this cannot drift from the SQL."""
    assert set(ledger._CLAIM_ATTEMPT_EXEMPT_STATES) == {"awaiting_approval", "needs_enrollment"}
    assert set(ledger._CLAIM_ATTEMPT_EXEMPT_STATES) <= set(ledger._POLLABLE_STATES)


async def test_the_select_due_and_claim_statements_agree_on_the_pollable_states():
    """Two statements, one vocabulary. Both lists are parsed out of the SQL that enforces them,
    so this compares what the database will really do."""
    assert ledger._POLLABLE_STATES == ledger._SELECT_DUE_STATES
    assert set(ledger._POLLABLE_STATES) == ledger.PURCHASE_STATES - ledger.TERMINAL_STATES


async def test_the_candidate_select_does_not_offer_already_claimed_rows():
    """M38. The claim UPDATE re-checks `claimed_by IS NULL`, so dropping it from the SELECT keeps
    the result CORRECT and makes the poller wasteful: N workers each fill their whole `limit`
    with candidates somebody else holds, and take none of them. Cheap to assert, so asserted."""
    held = await _make_due()
    free = await _make_due(buyer_ref="bref_free")
    # `held` is strictly the older candidate, so `ORDER BY next_poll_at ASC` puts it first and
    # `limit=1` takes it deterministically — otherwise the two share a second and the winner is
    # decided by a random uuid, which is how this test first went intermittent.
    await _set_clock_column(held["id"], "next_poll_at", "datetime('now', '-600 seconds')")
    claimed = await ledger.claim_due_purchases("worker_a", limit=1)
    assert [c["id"] for c in claimed] == [held["id"]]

    sql = (
        ledger._SELECT_DUE_PURCHASES_SQL
        if IS_POSTGRES
        else ledger._SELECT_DUE_PURCHASES_SQL_SQLITE
    )
    offered = {r["id"] for r in await database.fetch_all(sql, {"limit": 50})}
    assert free["id"] in offered
    assert held["id"] not in offered, (
        "a row somebody already holds must not be offered as a candidate — the claim would "
        "refuse it and the worker would have burned a slot of its limit"
    )


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

    sql = ledger._CLAIM_PURCHASE_SQL if IS_POSTGRES else ledger._CLAIM_PURCHASE_SQL_SQLITE
    row = await database.fetch_one(sql, {"id": purchase["id"], "worker_id": "worker_a"})
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
    # 'awaiting_approval' is attempt-exempt (see P2-3), so the counter stays at 0 here; what
    # this test is about is that the row became claimable again at all.
    assert reclaimed[0]["attempts"] == 0
    assert reclaimed[0]["claimed_by"] == "worker_b"


@pytest.mark.parametrize("bad", [0, 5, 29, -1])
async def test_requeue_refuses_a_lease_shorter_than_thirty_seconds(bad):
    """D4. This was `max(30, lease_seconds)` — an operator asking for a 5-second lease got 30 and
    no indication, which is the shape where someone tunes a number and watches nothing change."""
    with pytest.raises(ValueError):
        await ledger.requeue_stale_claims(lease_seconds=bad)


async def test_requeue_never_binds_a_python_datetime_for_its_cutoff():
    """The cutoff is `datetime('now', ...)` / `clock_timestamp() - interval`, computed by the
    SERVER. A Python-bound cutoff was measured seven hours wrong on this driver."""
    for sql in (ledger._REQUEUE_STALE_CLAIMS_SQL, ledger._REQUEUE_STALE_CLAIMS_SQL_SQLITE):
        assert ":cutoff" not in sql and ":stale_before" not in sql
    assert "clock_timestamp() - (:lease_seconds * INTERVAL '1 second')" in (
        ledger._REQUEUE_STALE_CLAIMS_SQL
    )
    assert "datetime('now', :lease_window)" in ledger._REQUEUE_STALE_CLAIMS_SQL_SQLITE


async def test_the_postgres_cutoffs_use_clock_timestamp_not_current_timestamp():
    """P2-4. On Postgres `CURRENT_TIMESTAMP` is TRANSACTION-start time. This module opens no
    transactions today (see the next test), so the two are equivalent right now — but a future
    caller that wraps a poll loop in one would FREEZE every cutoff at the moment the transaction
    began, and the sweep would then take everything or nothing. `clock_timestamp()` reads the
    wall clock at STATEMENT time and cannot be frozen that way.

    EVERY Postgres statement is checked, not a hand-picked three — the cutoffs AND the
    `terminal_at`/`updated_at`/`state_entered_at` stamps, so a freezable clock cannot creep back
    in anywhere."""
    checked = 0
    for name, sql in vars(ledger).items():
        if not (name.endswith("_SQL") and isinstance(sql, str)):
            continue
        assert "CURRENT_TIMESTAMP" not in sql, (
            f"{name} uses CURRENT_TIMESTAMP, which is TRANSACTION-start time on Postgres and "
            "freezes inside a caller-supplied transaction"
        )
        checked += 1
    assert checked >= 12, f"only inspected {checked} Postgres statements — did the suffix change?"

    # SQLite has no clock_timestamp(); CURRENT_TIMESTAMP is statement-time there anyway.
    for name, sql in vars(ledger).items():
        if name.endswith("_SQLITE") and isinstance(sql, str):
            assert "clock_timestamp()" not in sql, f"{name} cannot use clock_timestamp()"


async def test_the_module_opens_no_database_transactions():
    """P1-2's structural half, checked on the SOURCE so a later edit cannot reintroduce it
    quietly.

    `mark_enrollment_active` held the module's only one, and it was actively harmful:
    databases==0.7.0 scopes its connection by ContextVar, so two `asyncio.gather`-ed calls on the
    app's shared `database` land inside each other's transaction — measured on real Postgres,
    one task's committed write reported as an error and another task's write rolled back.

    An AST walk, not a grep: prose in the docstrings names `database.transaction()` precisely
    because the story is worth keeping, and a text search would trip over the explanation of why
    the thing is gone."""
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "db/reap_agentic_ledger.py").read_text("utf-8")
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
    assert not calls, (
        f"db/reap_agentic_ledger.py opens {len(calls)} database.transaction() block(s) at "
        f"line(s) {[c.lineno for c in calls]}. On databases==0.7.0 a transaction on the shared "
        "`database` object is inherited by gathered tasks; see mark_enrollment_active."
    )


# ── D4/D5: the two bulk sweeps ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql_name,sqlite_name,target",
    [
        ("_EXPIRE_OVERDUE_SQL", "_EXPIRE_OVERDUE_SQL_SQLITE", "expired"),
        ("_FAIL_EXHAUSTED_SQL", "_FAIL_EXHAUSTED_SQL_SQLITE", "failed"),
    ],
)
async def test_the_bulk_sweeps_only_use_legal_edges(sql_name, sqlite_name, target):
    """FINDING 4. Both sweeps bypass `transition`, so NOTHING at runtime enforces
    ALLOWED_TRANSITIONS on them — this is what does.

    THE STATE LIST IS PARSED OUT OF THE FINAL SQL STRING, not read from a tuple sitting next to
    it. While they were two separate things this test watched a constant the SQL never read, and
    adding 'processing' to the expire sweep's IN list — expiring a purchase the buyer had ALREADY
    APPROVED, after they approved it — survived both dialects.

    Both dialect twins are checked, because a sweep is only as safe as the statement that runs."""
    for name in (sql_name, sqlite_name):
        sources = ledger._states_in(getattr(ledger, name), "WHERE state IN (")
        assert sources, f"{name}: parsed no state list — has the statement's shape changed?"
        for src in sources:
            assert src in ledger.PURCHASE_STATES, f"{name}: {src!r} is not a state"
            assert target in ledger.ALLOWED_TRANSITIONS[src], (
                f"{name} would move {src!r} -> {target!r}, which ALLOWED_TRANSITIONS does not "
                f"permit ({src!r} permits {sorted(ledger.ALLOWED_TRANSITIONS[src])})"
            )


@pytest.mark.parametrize(
    "name",
    ["_EXPIRE_OVERDUE_SQL", "_EXPIRE_OVERDUE_SQL_SQLITE",
     "_FAIL_EXHAUSTED_SQL", "_FAIL_EXHAUSTED_SQL_SQLITE"],
)
async def test_each_sweep_repeats_its_state_predicate_at_the_top_level(name):
    """THE RACE GUARD, found by this package's own two-connection test rather than reasoned out
    in advance.

    With the state predicate only inside the LIMIT subquery, two concurrent sweeps both moved the
    same row and both reported it. Under READ COMMITTED the second UPDATE blocks on the row lock
    and re-checks its qual, but the subplan it re-checks has already been materialised, so
    `id IN (…)` is still true: the row is written twice, terminal_at is overwritten, and the id is
    counted by two callers. The outer copy is what the re-check can find false."""
    sql = getattr(ledger, name)
    assert sql.count("state IN (") == 2, (
        f"{name} must state its source states BOTH at the top level (the post-lock re-check) and "
        f"inside the LIMIT subquery (candidate selection); found {sql.count('state IN (')}"
    )
    outer = ledger._states_in(sql, "WHERE state IN (")
    inner_at = sql.index("SELECT id FROM reap_agentic_purchases")
    inner = ledger._states_in(sql[inner_at:], "WHERE state IN (")
    assert outer == inner, f"{name}: outer {outer} != inner {inner}"


async def test_the_sweep_state_lists_agree_across_dialects():
    """A guard that holds on one engine and not the other is not a guard."""
    assert ledger._states_in(ledger._EXPIRE_OVERDUE_SQL, "WHERE state IN (") == ledger._states_in(
        ledger._EXPIRE_OVERDUE_SQL_SQLITE, "WHERE state IN ("
    )
    assert ledger._states_in(
        ledger._FAIL_EXHAUSTED_SQL, "WHERE state IN ("
    ) == ledger._states_in(ledger._FAIL_EXHAUSTED_SQL_SQLITE, "WHERE state IN (")
    assert ledger._EXPIRE_SOURCE_STATES == ("needs_enrollment", "awaiting_approval")


async def test_the_expire_sweep_never_touches_an_approved_purchase():
    """The behavioural twin of the edge check, aimed squarely at the mutant that survived: a
    purchase in 'processing' has been APPROVED BY THE BUYER and is being settled. Expiring it
    would abandon a payment in flight."""
    purchase = await _mk(state="processing")
    await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)
    await _set_clock_column(
        purchase["id"], "state_entered_at", "datetime('now', '-99999 seconds')"
    )

    assert await ledger.expire_overdue_purchases(max_age_seconds=60) == []
    assert await _state_of(purchase["id"]) == "processing"


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
    assert await ledger.fail_exhausted_purchases(5, include_processing=True) == [purchase["id"]]

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

    assert await ledger.fail_exhausted_purchases(5, include_processing=True) == []
    assert (await ledger.get_purchase_internal(already["id"]))["state"] == "completed"


async def test_fail_exhausted_races_cleanly():
    purchase = await _mk(state="processing")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 9 WHERE id = :i", {"i": purchase["id"]}
    )
    first, second = await asyncio.gather(
        ledger.fail_exhausted_purchases(5, include_processing=True),
        ledger.fail_exhausted_purchases(5, include_processing=True),
    )
    assert len(first) + len(second) == 1


@pytest.mark.parametrize("bad", [0, -1])
async def test_fail_exhausted_refuses_a_nonsense_bound(bad):
    with pytest.raises(ValueError):
        await ledger.fail_exhausted_purchases(bad)


async def test_fail_exhausted_takes_a_row_at_EXACTLY_max_attempts():
    """M22. `attempts >= :max_attempts` vs `>` is an off-by-one that grants every stuck purchase
    one extra claim — and on a rail whose whole job is not charging people twice, "one more
    attempt than configured" is not a rounding detail."""
    at_bound = await _mk(state="processing")
    below = await _mk(state="processing", buyer_ref="bref_below")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 5 WHERE id = :i", {"i": at_bound["id"]}
    )
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 4 WHERE id = :i", {"i": below["id"]}
    )
    assert await ledger.fail_exhausted_purchases(5, include_processing=True) == [at_bound["id"]]


# ── P2-1: the sweeps are bounded ─────────────────────────────────────────────────────────────


async def test_expire_is_bounded_by_limit_and_the_caller_loops():
    """Prod and staging share one Postgres. An unbounded UPDATE on first arming — when the whole
    backlog qualifies at once — locks every matching row simultaneously."""
    ids = []
    for index in range(7):
        purchase = await _mk(state="awaiting_approval", buyer_ref=f"bref_{index}")
        await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)
        ids.append(purchase["id"])

    first = await ledger.expire_overdue_purchases(limit=3)
    assert len(first) == 3
    second = await ledger.expire_overdue_purchases(limit=3)
    assert len(second) == 3
    third = await ledger.expire_overdue_purchases(limit=3)
    assert len(third) == 1, "fewer than `limit` is how the caller knows to stop"
    assert sorted(first + second + third) == sorted(ids)
    assert await ledger.expire_overdue_purchases(limit=3) == []


async def test_fail_exhausted_is_bounded_by_limit():
    ids = []
    for index in range(5):
        purchase = await _mk(state="processing", buyer_ref=f"bref_{index}")
        await database.execute(
            "UPDATE reap_agentic_purchases SET attempts = 9 WHERE id = :i", {"i": purchase["id"]}
        )
        ids.append(purchase["id"])

    first = await ledger.fail_exhausted_purchases(5, limit=2, include_processing=True)
    assert len(first) == 2
    rest = await ledger.fail_exhausted_purchases(5, limit=10, include_processing=True)
    assert len(rest) == 3
    assert sorted(first + rest) == sorted(ids)


@pytest.mark.parametrize("bad", [0, -1, 501, 10_000])
async def test_both_sweeps_refuse_a_limit_outside_one_to_five_hundred(bad):
    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(limit=bad)
    with pytest.raises(ValueError):
        await ledger.fail_exhausted_purchases(5, limit=bad)


# ── P2-2: the absolute age fallback ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["needs_enrollment", "awaiting_approval"])
async def test_a_row_with_no_hosted_deadline_is_expired_on_absolute_age(state):
    """P2-2. Reproduced before the fix: a row in one of these two states whose
    `hosted_url_expires_at` is NULL was expired by NOTHING, and kept the buyer's address and
    email indefinitely — while the sweep's docstring called itself a PII deadline.

    Reap does not always give us an expiry, and we do not always get a hosted URL at all, so the
    NULL case is the ordinary one for an abandoned purchase rather than an edge."""
    purchase = await _mk(state=state)
    assert purchase["hosted_url_expires_at"] is None
    await _set_clock_column(
        purchase["id"], "state_entered_at", "datetime('now', '-7200 seconds')"
    )

    assert await ledger.expire_overdue_purchases(max_age_seconds=3600) == [purchase["id"]]
    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["terminal_at"] is not None


# ── ROUND 3 (P1): the expiry clock the poll loop cannot reset ────────────────────────────────


@pytest.mark.parametrize("state", ["needs_enrollment", "awaiting_approval"])
async def test_a_poll_loop_cannot_postpone_the_absolute_expiry(state):
    """THE P1. The fallback used to measure `updated_at` — which `_CLAIM_PURCHASE_SQL`,
    `_RELEASE_CLAIM_SQL` and `_REQUEUE_STALE_CLAIMS_SQL` all write. So the poll loop reset the
    deadline every cycle and at the documented 30-second cadence it NEVER FIRED.

    Reproduced on both dialects: a row aged 99999 s, then ONE claim+release, was not expired and
    kept the buyer's address and email. These are exactly the rows with no other bound —
    `attempts` is exempt in both waiting states and 'needs_enrollment' has no hosted URL at all.

    This drives the REAL path: a genuine transition into the state, then real claim / release /
    requeue calls, then the sweep."""
    purchase = await _mk()
    moved = await ledger.transition(
        purchase["id"],
        from_states=["resolving"],
        to_state=("needs_enrollment" if state == "needs_enrollment" else "quoting"),
    )
    if state == "awaiting_approval":
        moved = await ledger.transition(
            purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
        )
    assert moved["state"] == state
    assert moved["hosted_url_expires_at"] is None

    # The row has now been sitting in this state for a long time.
    await _set_clock_column(
        purchase["id"], "state_entered_at", "datetime('now', '-99999 seconds')"
    )
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)

    # Several ordinary poll cycles, plus a dead worker's requeue. Every one of these writes
    # updated_at; none of them may touch state_entered_at.
    for _ in range(3):
        await ledger.claim_due_purchases("worker_a", limit=5)
        await ledger.release_claim(purchase["id"], "worker_a")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await _age_claim(purchase["id"], 99999)
    await ledger.requeue_stale_claims(lease_seconds=300, limit=10)

    assert await ledger.expire_overdue_purchases(max_age_seconds=60) == [purchase["id"]], (
        "the absolute deadline must survive a poll loop — it measures state_entered_at, which "
        "only a state change moves"
    )
    row = await ledger.get_purchase_internal(purchase["id"])
    assert row["state"] == "expired"
    assert row["buyer_email"] is None and row["shipping_address"] is None
    assert row["terminal_at"] is not None
    assert row["claimed_by"] is None and row["claimed_at"] is None


@pytest.mark.parametrize(
    "call",
    [
        "claim",
        "release",
        "requeue",
    ],
)
async def test_the_poll_loop_does_not_touch_state_entered_at(call):
    """The guard stated directly: claim, release and requeue move `updated_at` and must leave
    `state_entered_at` alone. A mutant that makes any of them stamp it dies here."""
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "next_poll_at", _PAST)
    before = (await ledger.get_purchase_internal(purchase["id"]))["state_entered_at"]

    if call == "claim":
        await ledger.claim_due_purchases("worker_a", limit=5)
    elif call == "release":
        await ledger.claim_due_purchases("worker_a", limit=5)
        await ledger.release_claim(purchase["id"], "worker_a")
    else:
        await ledger.claim_due_purchases("worker_a", limit=5)
        await _age_claim(purchase["id"], 99999)
        await ledger.requeue_stale_claims(lease_seconds=300, limit=10)

    after = await ledger.get_purchase_internal(purchase["id"])
    assert after["state_entered_at"] == before, f"{call} moved state_entered_at"
    assert after["updated_at"] >= before, f"{call} should still move updated_at"


async def test_a_transition_into_a_new_state_resets_state_entered_at():
    """The other half: the clock is per-STATE, not per-row. A purchase that legitimately spent an
    hour resolving and re-quoting must get a fresh deadline the moment it reaches
    'awaiting_approval' — that is the moment the buyer starts looking at the page.

    This is why the column is not `created_at`."""
    purchase = await _mk()
    await _set_clock_column(
        purchase["id"], "state_entered_at", "datetime('now', '-99999 seconds')"
    )
    old = (await ledger.get_purchase_internal(purchase["id"]))["state_entered_at"]

    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting"
    )
    assert moved["state_entered_at"] > old, "entering a state must restart its clock"

    # And an old creation date alone does not expire a freshly-arrived row.
    await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    )
    await _set_clock_column(purchase["id"], "created_at", "datetime('now', '-99999 seconds')")
    assert await ledger.expire_overdue_purchases(max_age_seconds=60) == []
    assert await _state_of(purchase["id"]) == "awaiting_approval"


async def test_the_expire_fallback_reads_state_entered_at_not_updated_at():
    """A shape assertion naming the specific defect, so an edit that switches the column back is
    refused here rather than in production three weeks later."""
    for name in ("_EXPIRE_OVERDUE_SQL", "_EXPIRE_OVERDUE_SQL_SQLITE"):
        sql = getattr(ledger, name)
        assert "OR state_entered_at <" in sql, f"{name}: the fallback must read state_entered_at"
        assert "OR updated_at <" not in sql, (
            f"{name}: measuring updated_at makes the deadline resettable by the poll loop"
        )


async def test_only_state_changing_statements_stamp_state_entered_at():
    """`state_entered_at` may be written by exactly three kinds of statement — the transition and
    the two sweeps — and by no other."""
    stamps = {
        name: value.count("state_entered_at =")
        for name, value in vars(ledger).items()
        if name.endswith(("_SQL", "_SQLITE")) and isinstance(value, str)
    }
    writers = {name for name, count in stamps.items() if count}
    assert writers == {
        "_TRANSITION_SQL", "_TRANSITION_SQL_SQLITE",
        "_EXPIRE_OVERDUE_SQL", "_EXPIRE_OVERDUE_SQL_SQLITE",
        "_FAIL_EXHAUSTED_SQL", "_FAIL_EXHAUSTED_SQL_SQLITE",
    }, f"unexpected writers of state_entered_at: {sorted(writers)}"


async def test_state_entered_at_is_never_public():
    purchase = await _mk()
    view = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    assert "state_entered_at" not in view
    assert "state_entered_at" not in ledger.PUBLIC_PURCHASE_COLUMNS


# ── ROUND 3: 'processing' is not auto-failable ───────────────────────────────────────────────


async def test_fail_exhausted_skips_processing_unless_asked():
    """A purchase in 'processing' has been APPROVED BY THE BUYER and its payment is in flight.
    Auto-failing it on an attempt counter writes a terminal state over a charge whose outcome we
    do not know — our ledger saying 'failed' while the buyer's card says otherwise."""
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
    assert await _state_of(processing["id"]) == "failed"


# ── ROUND 3 (P3): bounds are bounds, not things to coerce ────────────────────────────────────


@pytest.mark.parametrize("bad", [True, False, 2.9, "5", None, b"5"])
async def test_sweep_bounds_refuse_non_integers(bad):
    """`int()` reads True as 1, 2.9 as 2 and "5" as 5. Each of these parameters is a bound an
    operator chose, and silently reinterpreting one turns a typo into a setting nobody can see.
    Measured before this: `expire_overdue_purchases(limit=True)` ran."""
    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(limit=bad)
    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(max_age_seconds=bad)
    with pytest.raises(ValueError):
        await ledger.fail_exhausted_purchases(bad)
    with pytest.raises(ValueError):
        await ledger.fail_exhausted_purchases(5, limit=bad)
    with pytest.raises(ValueError):
        await ledger.requeue_stale_claims(lease_seconds=bad)
    with pytest.raises(ValueError):
        await ledger.requeue_stale_claims(limit=bad)


@pytest.mark.parametrize("bad", [b"worker", 7, "", "   ", object()])
async def test_worker_ids_and_holders_must_be_non_empty_strings(bad):
    """`b"w"` sailed through `(value or "").strip()` and was STORED as the claim holder on
    SQLite, while asyncpg raised a DataError naming a parameter index. Two wrong answers for one
    typo, neither of them saying "worker id".

    `None` is deliberately absent: it is the LEGAL unfenced value for `holder`, and its own test
    is below."""
    purchase = await _mk()
    with pytest.raises(ValueError):
        await ledger.transition(
            purchase["id"], from_states=["resolving"], to_state="quoting", holder=bad
        )
    with pytest.raises(ValueError):
        await ledger.transition_as_holder(
            purchase["id"], bad, from_states=["resolving"], to_state="quoting"
        )
    with pytest.raises(ValueError):
        await ledger.claim_due_purchases(bad, limit=5)
    with pytest.raises(ValueError):
        await ledger.release_claim(purchase["id"], bad)


async def test_none_is_a_worker_id_nowhere_but_a_valid_unfenced_holder():
    """The control for the strictness above — `holder=None` must stay the unfenced path, and a
    worker id of None must still be refused."""
    purchase = await _mk()
    assert await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting", holder=None
    ) is not None
    for call in (
        lambda: ledger.transition_as_holder(
            purchase["id"], None, from_states=["quoting"], to_state="awaiting_approval"
        ),
        lambda: ledger.claim_due_purchases(None, limit=5),
        lambda: ledger.release_claim(purchase["id"], None),
    ):
        with pytest.raises(ValueError):
            await call()


async def test_a_young_row_with_no_hosted_deadline_is_left_alone():
    """The control: the fallback is an age limit, not a blanket."""
    purchase = await _mk(state="awaiting_approval")
    assert await ledger.expire_overdue_purchases(max_age_seconds=3600) == []
    assert await _state_of(purchase["id"]) == "awaiting_approval"


async def test_the_hosted_deadline_still_works_independently_of_age():
    """A young row whose hosted page has ALREADY expired goes now, not in an hour."""
    purchase = await _mk(state="awaiting_approval")
    await _set_clock_column(purchase["id"], "hosted_url_expires_at", _PAST)
    assert await ledger.expire_overdue_purchases(max_age_seconds=86400) == [purchase["id"]]


@pytest.mark.parametrize("bad", [0, 59, -1])
async def test_expire_refuses_a_max_age_under_a_minute(bad):
    with pytest.raises(ValueError):
        await ledger.expire_overdue_purchases(max_age_seconds=bad)


# ── remaining boundary guards ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, -99])
async def test_create_refuses_a_non_positive_quantity(bad):
    """M39. `quantity` multiplies a price. A zero-quantity purchase is a checkout for nothing;
    a negative one is a refund shaped like a purchase."""
    with pytest.raises(ValueError):
        await ledger.create_purchase(
            buyer_ref="b", agent_id="a", agent_user_ref_hash="h", quantity=bad
        )
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == 0


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


async def test_activating_a_dead_row_with_no_competitor_stays_dead():
    """FINDING 5. `_ACTIVATE_ENROLLMENT_SQL`'s `status IN ('pending','active')` conjunct had no
    killing test: the existing dead-row test had a COMPETING ACTIVE ROW, so removing the conjunct
    still produced None — via the unique index, through the except clause, for entirely the wrong
    reason. A guard whose test passes for the wrong reason is not a tested guard.

    With no competitor there is no index to save us, and the conjunct is the only thing standing
    between `mark_enrollment_dead` and a revoked card being silently resurrected."""
    enrollment = await ledger.upsert_pending_enrollment(buyer_ref="bref_solo")
    await ledger.mark_enrollment_dead(enrollment["id"], reap_status="REVOKED")
    assert await ledger.get_active_enrollment("bref_solo") is None

    assert await ledger.mark_enrollment_active(
        enrollment["id"], reap_enrollment_id="enr_zombie"
    ) is None

    row = await database.fetch_one(
        "SELECT status, reap_enrollment_id FROM reap_agentic_enrollments WHERE id = :i",
        {"i": enrollment["id"]},
    )
    assert row["status"] == "dead", "a revoked enrollment must never be resurrected"
    assert row["reap_enrollment_id"] is None, "and nothing on it may be written either"
    assert await ledger.get_active_enrollment("bref_solo") is None


async def test_a_real_unique_violation_through_mark_enrollment_active_returns_none():
    """P2-5. The catch used to be asserted in PROSE only, and the unit-level check fed
    `_is_unique_violation` a synthetic exception — which proves the predicate, not the path.

    This drives a REAL sqlite3.IntegrityError out of statement (b), twice.

    With (b) now running FIRST, the violation happens on its own — the buyer already has a live
    active row. The module then demotes and retries, and the retry SUCCEEDS, which is the ordinary
    re-enrolment path. To hold the failure open long enough to observe the caller's answer, the
    demotion is neutered for the duration, so the retry hits the index too. That is exactly the
    seam under test: "(b) raised, and raised again — what does the caller see?" The answer must be
    None, never a driver exception."""
    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_clash")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_clash', 'bref_clash', 'pending')"
    )

    inert = (
        "UPDATE reap_agentic_enrollments SET status = status "
        "WHERE id = :id AND 1 = 0 RETURNING id"
    )
    original = (ledger._DEMOTE_OTHER_ACTIVE_SQL, ledger._DEMOTE_OTHER_ACTIVE_SQL_SQLITE)
    ledger._DEMOTE_OTHER_ACTIVE_SQL = inert
    ledger._DEMOTE_OTHER_ACTIVE_SQL_SQLITE = inert
    try:
        result = await ledger.mark_enrollment_active("re_clash", reap_enrollment_id="enr_clash")
    finally:
        ledger._DEMOTE_OTHER_ACTIVE_SQL, ledger._DEMOTE_OTHER_ACTIVE_SQL_SQLITE = original

    assert result is None, (
        "a unique violation out of statement (b) must surface as None, like every other lost "
        "race in this module — not as a raw driver exception the caller cannot interpret"
    )
    assert (await ledger.get_active_enrollment("bref_clash"))["id"] == live["id"]
    still = await database.fetch_one(
        "SELECT status FROM reap_agentic_enrollments WHERE id = 're_clash'"
    )
    assert still["status"] == "pending", "the loser stays retryable, never half-written"


class _KillTargetAfterFirstStatement:
    """A `database` stand-in that marks `target` dead immediately after the FIRST statement
    `mark_enrollment_active` issues, whichever statement that is, then delegates everything else.

    This is the only way to drive the reviewer's interleaving deterministically: the two
    statements are issued back-to-back inside the function, so the concurrent
    `mark_enrollment_dead(target)` has to be injected between them rather than raced.
    """

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


async def test_a_concurrent_dead_between_the_two_statements_cannot_strand_the_buyer():
    """ROUND 3, P2 — THE ORDERING, PROVEN BY INTERLEAVING.

    With (a) DEMOTE first: the demotion sees a still-pending target, passes its gate and kills
    the buyer's live enrollment; the concurrent `mark_enrollment_dead(target)` then lands; (b)
    finds nothing to activate. The buyer ends with ZERO active enrollments and no retry can heal
    it, because the target is now permanently unactivatable. Reviewer-verified, and reproduced
    here before the reorder.

    With (b) ACTIVATE first: (b) raises the unique violation (the live row holds the index), the
    kill lands, and the demotion's gate now sees a DEAD target and demotes nothing. The retry of
    (b) returns None, and the live enrollment is untouched.

    Both orders pass every single-call test, because the demotion's gate already covers a target
    that was dead BEFORE the call. Only this interleaving separates them — which is exactly why
    the pure-reorder mutant survived the suite until this test existed.
    """
    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_interleave")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")
    await database.execute(
        "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
        "VALUES ('re_target', 'bref_interleave', 'pending')"
    )

    original = ledger.database
    ledger.database = _KillTargetAfterFirstStatement(original, "re_target")
    try:
        result = await ledger.mark_enrollment_active(
            "re_target", reap_enrollment_id="enr_target"
        )
    finally:
        ledger.database = original

    assert result is None, "the target died mid-flight, so nothing could be activated"

    still = await ledger.get_active_enrollment("bref_interleave")
    assert still is not None and still["id"] == live["id"], (
        "the buyer's live enrollment must survive: the demotion may not run on behalf of a "
        "promotion that cannot happen. Demoting FIRST is what strands the buyer here."
    )
    statuses = {
        r["id"]: r["status"]
        for r in await database.fetch_all(
            "SELECT id, status FROM reap_agentic_enrollments WHERE buyer_ref = :b",
            {"b": "bref_interleave"},
        )
    }
    assert statuses == {live["id"]: "active", "re_target": "dead"}, statuses


async def test_the_demotion_statement_never_demotes_its_own_target():
    """`AND id <> :id` in statement (a), exercised DIRECTLY.

    Since the reorder it is no longer reachable through `mark_enrollment_active`: (a) runs only
    after (b) raised a unique violation, which can only happen when the target is 'pending' and
    some OTHER row holds the active slot — so the target is never a demotion candidate anyway.
    That made the conjunct un-killable through the public function, which is precisely the state
    the repo calls out: an unreachable guard reads as protection that does not exist.

    It is kept rather than deleted because it is the only thing that would stop a future
    reordering from having the demotion eat the row it is about to promote, and it is tested at
    the statement level so that "kept" means "checked". Without it this demotes the target and
    the buyer ends up with nothing."""
    live = await ledger.upsert_pending_enrollment(buyer_ref="bref_self")
    await ledger.mark_enrollment_active(live["id"], reap_enrollment_id="enr_live")

    sql = (
        ledger._DEMOTE_OTHER_ACTIVE_SQL
        if IS_POSTGRES
        else ledger._DEMOTE_OTHER_ACTIVE_SQL_SQLITE
    )
    demoted = await database.fetch_all(sql, {"id": live["id"]})

    assert demoted == [], (
        "the demotion must never name its own target: with `id <> :id` gone it demotes the row "
        "it was asked to promote"
    )
    still = await ledger.get_active_enrollment("bref_self")
    assert still is not None and still["id"] == live["id"]


async def test_activation_is_idempotent_across_a_retry_of_the_same_target():
    """The transient window closes on retry: calling again with the same target completes the
    activation, because (a) is a no-op once the others are dead and (b) accepts 'pending'."""
    first = await ledger.upsert_pending_enrollment(buyer_ref="bref_retry")
    await ledger.mark_enrollment_active(first["id"], reap_enrollment_id="enr_1")
    second = await ledger.upsert_pending_enrollment(buyer_ref="bref_retry")

    once = await ledger.mark_enrollment_active(second["id"], reap_enrollment_id="enr_2")
    twice = await ledger.mark_enrollment_active(second["id"], reap_enrollment_id="enr_2")
    assert once is not None and twice is not None
    assert once["id"] == twice["id"] == second["id"]
    assert twice["status"] == "active"
    actives = await database.fetch_all(
        "SELECT id FROM reap_agentic_enrollments WHERE buyer_ref = :b AND status = 'active'",
        {"b": "bref_retry"},
    )
    assert [r["id"] for r in actives] == [second["id"]]


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


# ═════════════════════════════════════════════════════════════════════════════════════════════
# WP2c — migration 225: resolution hints, release reasons, enrollment reads by id
# ═════════════════════════════════════════════════════════════════════════════════════════════
#
# WHAT THIS BLOCK IS DEFENDING. Three gaps that PR #2204 (services/reap_agentic_purchase.py) hit
# from the outside and named in its own docstrings:
#
#   1. the resolver's hints had no column, so that service REFUSES any purchase carrying them
#      (`resolution_hints_not_persistable`) — `advance` runs in another process and anything not
#      in a column is gone by then;
#   2. `release_claim` could not say WHY a step was released, so a transport failure left no
#      trace on the row;
#   3. nothing read an enrollment BY ID, so that service calls `upsert_pending_enrollment` — a
#      WRITE — to get a row back, and mints a stray pending row when the target stopped being
#      pending in between.


_HINT_COLUMNS = ("accept_variant_labels", "also_accept_domains", "market_country")


async def _purchase_columns() -> set:
    return {
        r["name"] for r in await database.fetch_all("PRAGMA table_info(reap_agentic_purchases);")
    }


async def _raw_column(purchase_id: str, column: str):
    """Read a column WITHOUT going through the module's decode. On this dialect the JSON columns
    are TEXT, so what comes back is the stored string — which is what makes
    `test_the_hint_columns_are_decoded_not_handed_back_as_text` able to see the decode at all."""
    row = await database.fetch_one(
        f"SELECT {column} AS v FROM reap_agentic_purchases WHERE id = :i", {"i": purchase_id}
    )
    return None if row is None else row["v"]


# ── 225.1 the three hint columns exist and round-trip ────────────────────────────────────────


async def test_the_self_heal_built_the_three_hint_columns():
    """The columns exist on a database built the way PRODUCTION builds it — through the
    self-heal, never through db/migrations. On SQLite this is also the only check that the
    per-column ALTER loop ran at all: `ADD COLUMN IF NOT EXISTS` does not exist on this engine,
    so that branch cannot be a copy of the Postgres statement."""
    columns = await _purchase_columns()
    for name in _HINT_COLUMNS:
        assert name in columns, f"the mig-225 self-heal did not add {name}"


async def test_hints_round_trip_as_lists_and_a_country():
    purchase = await _mk(
        accept_variant_labels=["Nude Glow", "nude-glow"],
        also_accept_domains=["shop.brand.example", "brand.example"],
        market_country="US",
    )
    for row in (purchase, await ledger.get_purchase_internal(purchase["id"])):
        assert row["accept_variant_labels"] == ["Nude Glow", "nude-glow"]
        assert row["also_accept_domains"] == ["shop.brand.example", "brand.example"]
        assert row["market_country"] == "US"


async def test_hints_default_to_none_when_not_supplied():
    """The column is NULL, not `[]` — and the read hands back None, not an empty list. One
    spelling of "no hints", so no reader needs a `or []`."""
    purchase = await _mk()
    read = await ledger.get_purchase_internal(purchase["id"])
    for name in _HINT_COLUMNS:
        assert purchase[name] is None
        assert read[name] is None
        assert await _raw_column(purchase["id"], name) is None


@pytest.mark.parametrize("empty", [[], ()])
async def test_an_empty_hint_list_is_stored_as_null_not_as_an_empty_list(empty):
    """`[]` and None are the SAME assertion — the caller offered no aliases. Storing two
    spellings of it would make every reader carry a `or []`, and one of them would forget."""
    purchase = await _mk(accept_variant_labels=empty, also_accept_domains=empty)
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] is None
    assert read["also_accept_domains"] is None
    assert await _raw_column(purchase["id"], "accept_variant_labels") is None


async def test_a_tuple_of_hints_is_accepted_and_read_back_as_a_list():
    """PR #2204's `PurchaseRow` holds these as TUPLES (the dataclass is frozen), so a ledger that
    took only `list` would refuse its actual caller."""
    purchase = await _mk(accept_variant_labels=("Nude Glow",), also_accept_domains=("a.example",))
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] == ["Nude Glow"]
    assert read["also_accept_domains"] == ["a.example"]


async def test_domains_are_lowercased_on_the_way_in():
    """Hostnames are case-insensitive, so folding is a normalisation rather than a judgement —
    and it is what makes the stored value comparable to a domain the resolver read off a URL
    without either side remembering to fold. Labels are NOT folded: a shade name's case is part
    of the name."""
    purchase = await _mk(
        accept_variant_labels=["Nude Glow"], also_accept_domains=["Shop.BRAND.example"]
    )
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["also_accept_domains"] == ["shop.brand.example"]
    assert read["accept_variant_labels"] == ["Nude Glow"], "labels keep their case"


async def test_hint_entries_are_stripped():
    purchase = await _mk(
        accept_variant_labels=["  Nude Glow  "], also_accept_domains=[" brand.example "]
    )
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] == ["Nude Glow"]
    assert read["also_accept_domains"] == ["brand.example"]


async def test_the_hint_columns_are_decoded_not_handed_back_as_text():
    """THE DECODE, WITH A CONTROL. Asserting only that the read is a list would pass on an engine
    that never had the problem; asserting only that the raw value is text would pass if the
    decode were removed. Both halves, so the mutant "drop them from _JSON_COLUMNS" dies here on
    BOTH dialects — on Postgres because asyncpg hands back the jsonb verbatim (property 2), and
    on SQLite because the column really is TEXT."""
    purchase = await _mk(accept_variant_labels=["Nude Glow"])
    raw = await _raw_column(purchase["id"], "accept_variant_labels")
    assert isinstance(raw, str), "precondition: the column stores JSON TEXT on this dialect"
    assert json.loads(raw) == ["Nude Glow"]

    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] == ["Nude Glow"]
    assert isinstance(read["accept_variant_labels"], list), (
        "get_purchase_internal handed back the raw JSON string; a resolver iterating it would "
        "iterate the CHARACTERS of that string and accept nothing"
    )


async def test_the_two_hint_columns_are_in_the_module_json_column_list():
    """The shape assertion behind the test above, naming the mechanism so an edit that removes
    them is refused here rather than by a confusing round-trip failure."""
    assert "accept_variant_labels" in ledger._JSON_COLUMNS
    assert "also_accept_domains" in ledger._JSON_COLUMNS


# ── 225.2 hint validation, and that it refuses BEFORE any write ──────────────────────────────


_BAD_LABEL_LISTS = [
    "not-a-list",                         # a bare string would store as its characters
    b"bytes",
    123,
    {"a": 1},
    [""],                                 # blank entry
    ["   "],                              # blank after strip
    [None],                               # non-string entry
    [123],
    ["x"] * 33,                           # over the entry cap
    ["x" * 129],                          # over the char cap
    ["a\x00b"],                           # NUL: unstorable on Postgres, stored on SQLite
    ["a\nb"],                             # a newline forges a second log line
    ["a\rb"],
    ["a\tb"],
    ["\x1b[0m"],                          # an ANSI escape, and these are printed to terminals
    ["a\x7fb"],                           # DEL
]

_BAD_DOMAIN_LISTS = [
    "brand.example",                      # a bare string, not a list
    ["https://brand.example"],            # a scheme
    ["brand.example/path"],               # a path
    ["brand.example:443"],                # a port
    ["localhost"],                        # no dot
    ["brand..example"],                   # an empty label
    ["-brand.example"],                   # a leading hyphen
    ["brand-.example"],                   # a trailing hyphen
    ["bra nd.example"],                   # a space inside
    ["brand.123"],                        # a numeric TLD
    [""],
    ["x" * 250 + ".example"],             # over 253 characters
    ["a.example"] * 33,                   # over the entry cap
    ["a\x00b.example"],                   # control characters, same rule as labels
    ["a\nb.example"],
    ["\x1b[0m.example"],
]

_BAD_COUNTRIES = ["us", "USA", "U", "", "  ", "U5", 12, b"US", "us ", "Us", "U S"]


@pytest.mark.parametrize("bad", _BAD_LABEL_LISTS)
async def test_create_refuses_a_bad_accept_variant_labels(bad):
    with pytest.raises(ValueError):
        await _mk(accept_variant_labels=bad)


@pytest.mark.parametrize("bad", _BAD_DOMAIN_LISTS)
async def test_create_refuses_a_bad_also_accept_domains(bad):
    with pytest.raises(ValueError):
        await _mk(also_accept_domains=bad)


@pytest.mark.parametrize("bad", _BAD_COUNTRIES)
async def test_create_refuses_a_bad_market_country(bad):
    """UPPERCASE, exactly two letters. Not case-folded: this column feeds a market lookup and
    'us' and 'US' must not become two markets. A caller holding a lowercase code gets a
    ValueError naming the rule rather than a row that is subtly the wrong market."""
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
async def test_a_refused_hint_leaves_no_row_behind(kwargs):
    """REFUSED BEFORE THE INSERT, not after it. A purchase created and then rejected would be a
    'resolving' row holding the buyer's address and email with nothing scheduled to terminate it
    — the PII deadline only starts once the row exists, and the caller that got the exception
    would never come back for it."""
    before = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases")
    with pytest.raises(ValueError):
        await _mk(**kwargs)
    after = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases")
    assert after == before, "a refused hint still wrote a purchase row"


@pytest.mark.parametrize(
    "good",
    ["brand.example", "shop.brand.example", "a-b.co.uk", "xn--80ak6aa92e.com", "a1.example"],
)
async def test_the_domain_shape_accepts_real_hostnames(good):
    """CONTROL for the refusal tests above: a validator that refused everything would pass every
    one of them and be useless."""
    purchase = await _mk(also_accept_domains=[good])
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["also_accept_domains"] == [good]


async def test_the_hint_bounds_accept_their_own_limit():
    """The bounds are `<=`, not `<`. A test that only proved 33 refused would leave the
    off-by-one free to move."""
    labels = [f"label-{i}" for i in range(32)]
    # 63 is the longest a single DNS label may be, so this is the domain validator's own limit
    # rather than an arbitrary long string. It is also inside the shared 128-character bound,
    # which applies to both lists and on the domains path is belt to the hostname shape's brace.
    purchase = await _mk(
        accept_variant_labels=labels, also_accept_domains=["x" * 63 + ".example"]
    )
    read = await ledger.get_purchase_internal(purchase["id"])
    assert len(read["accept_variant_labels"]) == 32

    at_cap = await _mk(buyer_ref="bref_cap", accept_variant_labels=["x" * 128])
    stored = (await ledger.get_purchase_internal(at_cap["id"]))["accept_variant_labels"]
    assert len(stored[0]) == 128


async def test_hints_cannot_be_revised_by_a_transition():
    """A hint is an assertion the CALLER made when the purchase was opened. A poller step that
    could widen it could widen it past what the buyer approved — so the columns are deliberately
    absent from `_TRANSITION_FIELDS`, and naming one is a TypeError rather than a dropped
    write."""
    purchase = await _mk(accept_variant_labels=["Nude Glow"])
    for field in _HINT_COLUMNS:
        assert field not in ledger._TRANSITION_FIELDS
        with pytest.raises(TypeError):
            await ledger.transition(
                purchase["id"], from_states=["resolving"], to_state="quoting", **{field: ["x"]}
            )
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] == ["Nude Glow"]


async def test_the_hints_survive_a_terminal_transition():
    """NOT PII. shipping_address and buyer_email are nulled on terminal because they name the
    BUYER; these name products and hostnames, and a completed purchase that kept the alias it
    resolved through is exactly the record you want when that alias is questioned later."""
    purchase = await _mk(
        accept_variant_labels=["Nude Glow"],
        also_accept_domains=["brand.example"],
        market_country="US",
    )
    await ledger.transition(purchase["id"], from_states=["resolving"], to_state="refused")
    done = await ledger.get_purchase_internal(purchase["id"])
    assert done["state"] == "refused"
    assert done["buyer_email"] is None, "precondition: the PII rule still fires"
    assert done["accept_variant_labels"] == ["Nude Glow"]
    assert done["also_accept_domains"] == ["brand.example"]
    assert done["market_country"] == "US"


async def test_the_hints_are_not_in_the_public_view():
    purchase = await _mk(
        accept_variant_labels=["Nude Glow"],
        also_accept_domains=["brand.example"],
        market_country="US",
    )
    view = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    for name in _HINT_COLUMNS:
        assert name not in view
        assert name not in ledger.PUBLIC_PURCHASE_COLUMNS
    # And they ARE reachable when a caller says so, which is the whole point of the column.
    private = await ledger.get_purchase_for_owner(
        purchase["id"], "agent_one", "hash_alice", include_private=True
    )
    assert private["accept_variant_labels"] == ["Nude Glow"]


# ── 225.3 release_claim records WHY ──────────────────────────────────────────────────────────


async def _claimed(**over):
    purchase = await _mk(**over)
    claimed = await ledger.claim_due_purchases("worker_a", limit=5)
    assert [r["id"] for r in claimed] == [purchase["id"]], "precondition: the row was claimable"
    return purchase


async def test_release_records_the_error_code_and_it_is_readable_internally():
    purchase = await _claimed()
    released = await ledger.release_claim(
        purchase["id"], "worker_a", last_error_code="transport_error:readtimeout"
    )
    assert released is not None
    assert released["last_error_code"] == "transport_error:readtimeout"
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "transport_error:readtimeout"
    assert read["claimed_by"] is None, "the release still released"


async def test_the_release_error_code_projects_through_the_allowlist_only():
    """`last_error_code` IS in PUBLIC_PURCHASE_COLUMNS — it was there before this change, and it
    is OUR vocabulary rather than a partner body — so this asserts the narrower true thing: a row
    carrying a release-written code still projects to EXACTLY the public set, and none of the
    never-public columns rides out with it."""
    purchase = await _claimed()
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="quote_expired")
    row = await ledger.get_purchase_internal(purchase["id"])
    view = ledger.public_purchase_view(row)
    assert set(view) == _EXPECTED_PUBLIC_COLUMNS
    assert not (set(view) & _EXPECTED_NEVER_PUBLIC)


async def test_release_without_an_error_code_leaves_the_previous_one_alone():
    """COALESCE, not a bare assignment. The poller releases on EVERY ordinary poll of a healthy
    row, so a bare assignment would erase the reason the row stalled on the next uneventful tick
    — the value would survive exactly one poll interval, which is the same as not storing it."""
    purchase = await _claimed()
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="quote_expired")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await ledger.release_claim(purchase["id"], "worker_a")
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "quote_expired"


async def test_a_later_release_overwrites_the_earlier_code():
    """The other direction: COALESCE means "None leaves it alone", not "the first code wins"."""
    purchase = await _claimed()
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="quote_expired")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="checkout_failed")
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "checkout_failed"


async def test_release_with_an_error_code_does_not_touch_state_entered_at():
    """THE MUTANT THIS TEST EXISTS FOR: adding `state_entered_at = CURRENT_TIMESTAMP` to the two
    release statements, which looks like a harmless consistency tidy-up and re-opens the PII
    deadline hole from the other end.

    `state_entered_at` is the clock `expire_overdue_purchases` measures its absolute fallback
    against, and it is deliberately one the poll loop CANNOT RESET. Measured before that was
    fixed: a row aged 99999 seconds, then ONE ordinary claim+release, was not expired and kept
    the buyer's address and email.
    """
    purchase = await _claimed(state="resolving")
    await _set_clock_column(
        purchase["id"], "state_entered_at", "datetime('now', '-99999 seconds')"
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


async def test_a_release_that_records_a_code_still_lets_the_absolute_expiry_fire():
    """The behavioural half of the mutant above, through the sweep that actually matters. If the
    release stamped `state_entered_at`, this row would be young again and survive."""
    purchase = await _claimed(state="needs_enrollment")
    await _set_clock_column(
        purchase["id"], "state_entered_at", "datetime('now', '-99999 seconds')"
    )
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="enrollment_pending")

    moved = await ledger.expire_overdue_purchases(max_age_seconds=60)
    assert purchase["id"] in moved, (
        "the abandoned row survived its absolute deadline — release reset the clock"
    )
    expired = await ledger.get_purchase_internal(purchase["id"])
    assert expired["buyer_email"] is None and expired["shipping_address"] is None


async def test_release_is_still_not_a_writer_of_state_entered_at():
    """The shape assertion behind the two tests above, so the mutant is refused by a test that
    NAMES it rather than by a timestamp comparison a reader has to decode. The existing
    `test_only_state_changing_statements_stamp_state_entered_at` already pins the full writer
    set; this says the release statements specifically, which is what this PR touched."""
    for name in ("_RELEASE_CLAIM_SQL", "_RELEASE_CLAIM_SQL_SQLITE"):
        sql = getattr(ledger, name)
        assert "state_entered_at" not in sql, (
            f"{name} writes state_entered_at; the PII deadline must not be resettable by a "
            "release"
        )
        assert "last_error_code = COALESCE(:last_error_code, last_error_code)" in sql, (
            f"{name} must write the code through COALESCE, or an uneventful release erases it"
        )
        assert "updated_at =" in sql, f"{name} must still bump updated_at"


@pytest.mark.parametrize(
    "bad",
    [
        "TRANSPORT_ERROR",                 # uppercase
        "transport error",                 # a space
        "transport/error",                 # a separator outside the set
        "x" * 65,                          # over the column width
        "",                                # blank
        "   ",
        b"transport_error",                # bytes
        123,
        ["transport_error"],
        "café_error",                 # non-ASCII
        "transport_error\n",               # a newline, which a log line would swallow
    ],
)
async def test_release_refuses_a_malformed_error_code(bad):
    """REFUSED, NOT TRUNCATED OR FOLDED. The two failure modes to avoid are a code that is
    silently a DIFFERENT code (case-folded, or cut at 64) and a code that is a sentence."""
    purchase = await _claimed()
    with pytest.raises(ValueError):
        await ledger.release_claim(purchase["id"], "worker_a", last_error_code=bad)


async def test_a_refused_error_code_releases_nothing():
    """The refusal happens BEFORE the statement, so a bad code does not half-release the claim."""
    purchase = await _claimed()
    with pytest.raises(ValueError):
        await ledger.release_claim(purchase["id"], "worker_a", last_error_code="NOT VALID")
    still = await ledger.get_purchase_internal(purchase["id"])
    assert still["claimed_by"] == "worker_a", "the lease was given back on a refused call"
    assert still["last_error_code"] is None


@pytest.mark.parametrize(
    "good",
    ["transport_error:readtimeout", "quote_expired", "a", "x" * 64, "err.4xx", "err-code_1"],
)
async def test_the_error_code_shape_accepts_the_rails_own_vocabulary(good):
    """CONTROL. A validator that refused everything would pass every refusal test above."""
    purchase = await _claimed()
    released = await ledger.release_claim(purchase["id"], "worker_a", last_error_code=good)
    assert released["last_error_code"] == good


async def test_release_by_the_wrong_worker_records_nothing():
    """The error-code write inherits the release's fence. A worker that has already lost the
    lease must not be able to stamp a reason onto somebody else's step."""
    purchase = await _claimed()
    lost = await ledger.release_claim(purchase["id"], "worker_b", last_error_code="not_my_step")
    assert lost is None
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] is None
    assert read["claimed_by"] == "worker_a"


async def test_release_claim_has_no_attempts_delta():
    """`attempts` is counted at CLAIM and in one statement only. A second place that could move
    it would make the counter mean two things at once, and `fail_exhausted_purchases` reads it as
    one."""
    import inspect

    params = inspect.signature(ledger.release_claim).parameters
    assert "attempts_delta" not in params
    assert set(params) == {"purchase_id", "worker_id", "next_poll_at", "last_error_code"}

    purchase = await _claimed()
    before = (await ledger.get_purchase_internal(purchase["id"]))["attempts"]
    await ledger.release_claim(purchase["id"], "worker_a", last_error_code="quote_expired")
    assert (await ledger.get_purchase_internal(purchase["id"]))["attempts"] == before


async def test_release_still_schedules_the_next_poll_alongside_the_code():
    """The two writes share one statement, so neither can happen without the other."""
    purchase = await _claimed()
    when = datetime.now(timezone.utc) + timedelta(seconds=900)
    released = await ledger.release_claim(
        purchase["id"], "worker_a", next_poll_at=when, last_error_code="quote_expired"
    )
    assert released["last_error_code"] == "quote_expired"
    assert released["next_poll_at"] is not None
    assert released["next_poll_at"] > datetime.now(timezone.utc) + timedelta(seconds=600)


# ── 225.4 the two enrollment reads ───────────────────────────────────────────────────────────


# WRITTEN OUT, NOT IMPORTED, for the same reason as _EXPECTED_PUBLIC_COLUMNS: a projection
# compared against the module's own idea of itself cannot notice that idea changing.
_ENROLLMENT_PROJECTION = {
    "id", "buyer_ref", "reap_enrollment_id", "status", "hosted_url", "hosted_url_expires_at",
    "card_network", "card_last4",
}
_ENROLLMENT_NEVER_PROJECTED = {"agent_id", "reap_status", "created_at", "updated_at"}


async def test_get_enrollment_internal_reads_a_pending_row_by_id():
    """THE READ THE POLLER NEEDED AND DID NOT HAVE. A purchase in 'needs_enrollment' points at a
    PENDING enrollment, which `get_active_enrollment` cannot see by definition — so PR #2204
    recovers it today by calling `upsert_pending_enrollment`, a WRITE, purely to get the row."""
    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", reap_enrollment_id="enr_1", hosted_url="https://hosted.example/a"
    )
    read = await ledger.get_enrollment_internal(created["id"])
    assert read is not None
    assert read["id"] == created["id"]
    assert read["buyer_ref"] == "bref_alice"
    assert read["reap_enrollment_id"] == "enr_1"
    assert read["status"] == "pending"
    assert read["hosted_url"] == "https://hosted.example/a"


async def test_get_enrollment_internal_writes_nothing():
    """The whole reason it exists. The workaround it replaces bumps `updated_at` on every call,
    and mints a stray pending row whenever the target stopped being pending in between."""
    created = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    before = await database.fetch_all(
        "SELECT id, updated_at FROM reap_agentic_enrollments ORDER BY id"
    )
    await ledger.get_enrollment_internal(created["id"])
    await ledger.get_enrollment_internal("re_does_not_exist")
    after = await database.fetch_all(
        "SELECT id, updated_at FROM reap_agentic_enrollments ORDER BY id"
    )
    assert [dict(r) for r in after] == [dict(r) for r in before], (
        "the read changed the table — it is supposed to be a read"
    )


@pytest.mark.parametrize("status", ["pending", "active", "dead"])
async def test_get_enrollment_internal_reads_every_status(status):
    """Unlike `get_active_enrollment`, which is the point: the poller's row is pending, and the
    webhook's row may already be dead."""
    created = await ledger.upsert_pending_enrollment(buyer_ref=f"bref_{status}")
    if status == "active":
        await ledger.mark_enrollment_active(created["id"], card_network="visa", card_last4="4242")
    elif status == "dead":
        await ledger.mark_enrollment_dead(created["id"])
    read = await ledger.get_enrollment_internal(created["id"])
    assert read["status"] == status


async def test_get_enrollment_internal_is_none_for_an_unknown_id():
    assert await ledger.get_enrollment_internal("re_nope") is None


@pytest.mark.parametrize("bad", [None, "", "   ", 123, b"re_1", 1.5, ["re_1"]])
@pytest.mark.parametrize(
    "reader", ["get_enrollment_internal", "get_enrollment_by_reap_id"]
)
async def test_the_enrollment_reads_refuse_a_non_string_or_blank_id(reader, bad):
    """A non-string id produced a DIFFERENT wrong answer per engine: a quiet None on SQLite —
    which a caller reads as "no such enrollment" and acts on by failing the purchase for the
    wrong reason — and a raw asyncpg DataError naming a parameter index on Postgres. Blank is
    refused for the same reason: `""` is a caller that lost its id upstream, and it matches
    nothing in both engines, which is exactly what makes the bug survivable."""
    with pytest.raises(ValueError):
        await getattr(ledger, reader)(bad)


async def test_the_enrollment_reads_carry_exactly_the_projection():
    """An EQUALITY against a literal written in this file. `>=` would pass a projection that
    quietly grew `agent_id` or the partner's raw `reap_status`."""
    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice",
        agent_id="agent_one",
        reap_enrollment_id="enr_p",
        reap_status="upstream_words",
    )
    by_id = await ledger.get_enrollment_internal(created["id"])
    by_reap = await ledger.get_enrollment_by_reap_id("enr_p")
    for row in (by_id, by_reap):
        assert set(row) == _ENROLLMENT_PROJECTION, (
            f"unexpected: {sorted(set(row) - _ENROLLMENT_PROJECTION)}; "
            f"missing: {sorted(_ENROLLMENT_PROJECTION - set(row))}"
        )
        assert not (set(row) & _ENROLLMENT_NEVER_PROJECTED)


async def test_the_two_enrollment_statements_project_identically():
    """The column list is written out TWICE — a shared constant interpolated into both would make
    them invisible to the repo's PREPARE sweep (property 5). So a test holds them in step."""

    def _projection(sql: str) -> str:
        body = sql[sql.index("SELECT") + len("SELECT"): sql.index("FROM")]
        return " ".join(body.split())

    assert _projection(ledger._SELECT_ENROLLMENT_BY_ID_SQL) == _projection(
        ledger._SELECT_ENROLLMENT_BY_REAP_ID_SQL
    )


async def test_the_card_fields_are_readable_and_are_only_network_and_last4():
    """Four digits and a network name are exactly what migration 224 permits this table to hold,
    and they exist to be displayed. The test that matters is the absence of anything else, which
    the projection equality above carries."""
    created = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    await ledger.mark_enrollment_active(created["id"], card_network="visa", card_last4="4242")
    read = await ledger.get_enrollment_internal(created["id"])
    assert (read["card_network"], read["card_last4"]) == ("visa", "4242")


async def test_get_enrollment_by_reap_id_finds_the_row():
    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", reap_enrollment_id="enr_webhook"
    )
    read = await ledger.get_enrollment_by_reap_id("enr_webhook")
    assert read is not None and read["id"] == created["id"]


async def test_the_by_reap_id_STATEMENT_does_not_wildcard_on_a_null_bind():
    """EVERY pending row has a NULL `reap_enrollment_id`. A statement that wildcarded on NULL
    would hand a webhook receiver an arbitrary unrelated buyer's enrollment — `NULL = NULL is
    NULL` is what stops it, the same property the ownership conjuncts rest on.

    RUN AGAINST THE STATEMENT, NOT THROUGH THE FUNCTION, and deliberately so. `_require_lookup_id`
    now refuses None before the query, which means a test that called the function would pass
    with the SQL rewritten to `(:reap_enrollment_id IS NULL OR …)` — the Python guard would hide
    the SQL one. Binding NULL straight into the statement is the only way to see it, and it keeps
    the wildcard mutant dead on this dialect (on Postgres the PREPARE sweep kills it too, as
    AmbiguousParameter)."""
    await ledger.upsert_pending_enrollment(buyer_ref="bref_one")
    await ledger.upsert_pending_enrollment(buyer_ref="bref_two")
    rows = await database.fetch_all(
        ledger._SELECT_ENROLLMENT_BY_REAP_ID_SQL, {"reap_enrollment_id": None}
    )
    assert rows == [], (
        "the by-reap-id statement matched rows on a NULL bind — every pending enrollment in the "
        "table is reachable by anyone who can reach this read"
    )


async def test_get_enrollment_by_reap_id_is_none_for_an_unknown_id():
    await ledger.upsert_pending_enrollment(buyer_ref="bref_one")
    assert await ledger.get_enrollment_by_reap_id("enr_unknown") is None


async def test_get_enrollment_by_reap_id_survives_the_row_going_dead():
    """The webhook that retires a card names it by the partner's id, and the row it names may
    already be dead. A read filtered on status would answer None and the receiver would have
    nothing to act on."""
    created = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", reap_enrollment_id="enr_dead"
    )
    await ledger.mark_enrollment_dead(created["id"])
    read = await ledger.get_enrollment_by_reap_id("enr_dead")
    assert read is not None and read["status"] == "dead"


async def test_the_enrollment_reads_are_named_and_exported_deliberately():
    """`get_enrollment_internal` is UNSCOPED, so it carries the suffix and stays out of `__all__`
    — the same argument `get_purchase_internal` makes. `get_enrollment_by_reap_id` is exported
    because the trap the suffix guards against is a NAME a route author reaches for by mistake,
    and nobody reaches for "by reap id" when they meant "the buyer's enrollment"."""
    assert "get_enrollment_internal" not in ledger.__all__
    assert "get_enrollment_by_reap_id" not in ledger.__all__, (
        "both new enrollment reads are UNSCOPED and both hand back `hosted_url` — a live page on "
        "which a card can be enrolled. get_enrollment_by_reap_id is keyed on an identifier that "
        "comes from OUTSIDE this system, so its caller must have authenticated that id first; "
        "keeping it off the advertised surface is what makes importing it a deliberate act"
    )
    assert "get_active_enrollment" in ledger.__all__, (
        "the scoped-by-construction read stays exported: it is keyed on buyer_ref, which we "
        "minted and the caller has already proved is theirs"
    )
    assert not hasattr(ledger, "get_enrollment"), (
        "the un-suffixed name is the trap; it must not appear"
    )
    assert "upsert_pending_enrollment" in ledger.__all__, "the upsert is kept as is"


async def test_upsert_pending_enrollment_still_behaves_as_before():
    """The brief says keep it as is, so this pins the behaviour PR #2204 relies on today: pinning
    `enrollment_id` addresses OUR row, and it is still an upsert that returns the row it
    touched."""
    created = await ledger.upsert_pending_enrollment(buyer_ref="bref_alice")
    again = await ledger.upsert_pending_enrollment(
        buyer_ref="bref_alice", enrollment_id=created["id"], hosted_url="https://hosted.example/b"
    )
    assert again["id"] == created["id"]
    assert again["hosted_url"] == "https://hosted.example/b"


# ── 225.5 the self-heal, on an empty DB and on a 224-shaped one ──────────────────────────────


# The purchases table EXACTLY as migration 224 left it — the shape every environment that
# deployed 224 is actually running. Written out here rather than derived from schema_guard, so
# that deleting the mig-225 ALTER from the self-heal cannot also delete this test's precondition.
_MIG_224_PURCHASES_DDL = """
    CREATE TABLE reap_agentic_purchases (
        id VARCHAR(64) PRIMARY KEY,
        buyer_ref VARCHAR(128) NOT NULL,
        agent_id VARCHAR(128),
        agent_user_ref_hash VARCHAR(64),
        enrollment_id VARCHAR(64),
        state VARCHAR(24) NOT NULL CHECK (state IN (
            'resolving', 'needs_enrollment', 'quoting', 'awaiting_approval',
            'processing', 'completed', 'failed', 'refused', 'expired'
        )),
        state_entered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        merchant_domain VARCHAR(255),
        product_key TEXT,
        variant_key TEXT,
        product_name TEXT,
        variant_title TEXT,
        brand TEXT,
        category TEXT,
        quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity > 0),
        currency VARCHAR(8),
        our_price_minor BIGINT,
        click_id VARCHAR(128),
        return_url TEXT,
        reap_product_id VARCHAR(128),
        reap_variant_id VARCHAR(128),
        reap_quote_id VARCHAR(128),
        reap_quote_expires_at TIMESTAMP,
        reap_checkout_id VARCHAR(128),
        reap_order_id VARCHAR(128),
        quoted_total_minor BIGINT,
        final_total_minor BIGINT,
        shipping_minor BIGINT,
        tax_minor BIGINT,
        hosted_url TEXT,
        hosted_url_expires_at TIMESTAMP,
        refusal_reason VARCHAR(64),
        queries_tried TEXT,
        last_error_code VARCHAR(64),
        attempts INTEGER NOT NULL DEFAULT 0,
        claimed_by VARCHAR(128),
        claimed_at TIMESTAMP,
        next_poll_at TIMESTAMP,
        shipping_address TEXT,
        buyer_email TEXT,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        terminal_at TIMESTAMP
    )
"""


async def test_the_self_heal_adds_the_hint_columns_to_an_empty_database():
    """Path one: nothing exists. The CREATE TABLE builds the 224 shape and the mig-225 ALTER adds
    the three columns on top — the same route the migrations take, which is what keeps the two
    builds identical down to ordinal position."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute("DROP TABLE IF EXISTS reap_agentic_enrollments")
    await ensure_required_schema_light()
    assert set(_HINT_COLUMNS) <= await _purchase_columns()


async def test_the_self_heal_adds_the_hint_columns_to_a_224_shaped_database():
    """PATH TWO, AND THE ONE THAT ACTUALLY MATTERS IN PRODUCTION. Every environment that deployed
    migration 224 already HAS this table, so `CREATE TABLE IF NOT EXISTS` is a no-op there and the
    ALTER is the whole of the heal. A test that only covered the empty database would pass with
    the ALTER deleted entirely."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    before = await _purchase_columns()
    assert "accept_variant_labels" not in before, "precondition: this is the 224 shape"

    await ensure_required_schema_light()

    after = await _purchase_columns()
    assert set(_HINT_COLUMNS) <= after
    # A 224-shaped database is ALSO pre-229 and pre-233, so the heal lands those migrations'
    # columns in the same run. Named explicitly rather than loosened to `<=`: "nothing else" is
    # still the assertion.
    assert after - before == set(_HINT_COLUMNS) | {
        "item_source",
        "cart_url",
        "consent_version",
        "consented_at",
    }, (
        "the heal added something other than the three mig-225, two mig-229 and two mig-233 "
        "columns"
    )

    # And the rail works on the healed table.
    purchase = await _mk(accept_variant_labels=["Nude Glow"], market_country="US")
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["accept_variant_labels"] == ["Nude Glow"] and read["market_country"] == "US"


async def test_the_hint_self_heal_is_idempotent_on_a_second_run():
    """It runs on EVERY startup, and on SQLite a duplicate ADD COLUMN RAISES — so "no-op" here
    means "the except swallowed it AND the columns after it still ran", which is why the branch
    is one try PER COLUMN rather than one try around all three."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    await ensure_required_schema_light()
    once = await _purchase_columns()
    rows_before = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases")

    await ensure_required_schema_light()
    await ensure_required_schema_light()

    assert await _purchase_columns() == once
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == rows_before
    purchase = await _mk(also_accept_domains=["brand.example"])
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["also_accept_domains"] == ["brand.example"]


async def test_a_partial_hint_heal_converges():
    """One column present, two missing — the shape a SHARED try/except would leave permanently
    short, because the first duplicate would abandon the two after it."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    await database.execute(
        "ALTER TABLE reap_agentic_purchases ADD COLUMN accept_variant_labels TEXT"
    )
    await ensure_required_schema_light()
    assert set(_HINT_COLUMNS) <= await _purchase_columns(), (
        "a partially-healed table did not converge — the ALTERs are sharing an except"
    )


async def test_the_migration_and_both_self_heal_branches_name_the_same_columns():
    """The coverage gate (tests/test_schema_guard_migration_coverage.py) checks the POSTGRES
    branch's ADD COLUMN pairs. SQLite has no `ADD COLUMN IF NOT EXISTS`, so its twin cannot be
    written in the shape that gate parses — this is what holds it in step instead."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "db/migrations/225_reap_agentic_purchase_hints.sql"
    ).read_text(encoding="utf-8")
    guard = (root / "db/schema_guard.py").read_text(encoding="utf-8")

    for column in _HINT_COLUMNS:
        assert column in migration, f"migration 225 no longer names {column}"
        assert guard.count(column) >= 2, (
            f"{column} appears in migration 225 but not in BOTH schema_guard branches"
        )

    # The SQLite twin must not declare JSONB: SQLite resolves that unknown type name to NUMERIC
    # affinity and silently stores 0 for a JSON payload — the same trap as CAST(:x AS JSONB).
    # `rsplit`, NOT `split`: the substring "if IS_SQLITE:" also occurs inside the `elif
    # IS_SQLITE:` far earlier in the file, so splitting from the left hands back a chunk that
    # still CONTAINS the Postgres branch — and this assertion then fails on the Postgres
    # statement while reporting it as the SQLite twin. It did, on the first cut of this test.
    sqlite_branch = guard.rsplit("if IS_SQLITE:", 1)[1]
    hint_lines = [
        line
        for line in sqlite_branch.splitlines()
        if any(c in line for c in ("accept_variant_labels", "also_accept_domains"))
    ]
    assert hint_lines, "the SQLite branch does not mention the hint columns at all"
    for line in hint_lines:
        assert "JSONB" not in line.upper(), (
            f"the SQLite twin declares JSONB: {line.strip()!r} — NUMERIC affinity stores 0"
        )


async def test_the_down_migration_exists_and_drops_all_three():
    from pathlib import Path

    down = (
        Path(__file__).resolve().parents[1]
        / "db/migrations/down/225_reap_agentic_purchase_hints_down.sql"
    ).read_text(encoding="utf-8")
    for column in _HINT_COLUMNS:
        assert f"DROP COLUMN IF EXISTS {column}" in down


# ── 225.6 a failing sibling statement must not starve the hint columns ───────────────────────
#
# THE REVIEWER'S P1, AS A TEST. The mig-225 ALTERs used to be the LAST statements inside the
# mig-224 try, and every statement in that try can raise — one of them raises on databases that
# exist. `CREATE UNIQUE INDEX uq_reap_agentic_enrollments_one_active` FAILS on a table that
# already holds two 'active' enrollments for one buyer_ref, and that raise abandoned everything
# after it. Production never runs db/migrations, so there was no other route: the three hint
# columns never landed and `create_purchase` raised on every call, forever, on exactly the
# databases that were already unwell.
#
# The fix is position PLUS its own try — the mig-207 / mig-212 shape. These tests are what keeps
# it, and the mutant that folds the ALTERs back into the mig-224 try must fail them.


async def _seed_two_active_enrollments_for_one_buyer():
    """Put the database in the state that makes the unique-index creation fail.

    Built with RAW INSERTs against a table created WITHOUT the index, because that is the only
    way this state is reachable — `mark_enrollment_active` cannot produce it, which is the whole
    point of the index. It is reachable in production the way any pre-index duplicate is: rows
    written before the index existed, or an index dropped by hand during an incident.
    """
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
            hosted_url_expires_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for row_id in ("re_dup_a", "re_dup_b"):
        await database.execute(
            "INSERT INTO reap_agentic_enrollments (id, buyer_ref, status) "
            "VALUES (:i, 'bref_dup', 'active')",
            {"i": row_id},
        )


async def test_a_failing_sibling_statement_does_not_starve_the_hint_columns():
    """THE P1 REGRESSION GUARD. Two active enrollments for one buyer_ref make
    `CREATE UNIQUE INDEX uq_reap_agentic_enrollments_one_active` raise. The hint columns must
    land anyway — they are in their own try, after the mig-224 one, so a failure there cannot
    reach them."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    await _seed_two_active_enrollments_for_one_buyer()

    await ensure_required_schema_light()

    columns = await _purchase_columns()
    assert set(_HINT_COLUMNS) <= columns, (
        f"a failing sibling statement starved the mig-225 columns: missing "
        f"{sorted(set(_HINT_COLUMNS) - columns)}. Production never runs db/migrations, so these "
        "columns would not exist there at all and create_purchase would raise on every call."
    )


async def test_the_rail_still_works_after_a_failing_sibling_statement():
    """The behavioural half: not just "the columns exist" but "a purchase can be opened". This is
    the call that raised in the reviewer's repro."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
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


async def test_the_precondition_really_does_break_the_unique_index():
    """CONTROL, and it is not optional. The two tests above assert that something SURVIVES a
    failure — and an assertion that a mechanism survives a failure passes just as happily when
    THERE WAS NO FAILURE. If the duplicate rows stopped making the index creation fail, those
    tests would keep passing while testing nothing at all.

    So: prove the failure is real, by attempting the same statement the self-heal attempts and
    requiring it to raise."""
    await _seed_two_active_enrollments_for_one_buyer()
    with pytest.raises(Exception) as caught:
        await database.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reap_agentic_enrollments_one_active "
            "ON reap_agentic_enrollments (buyer_ref) WHERE status = 'active';"
        )
    assert "unique" in str(caught.value).lower(), (
        f"the repro no longer breaks the index, so the starvation tests prove nothing: "
        f"{caught.value!r}"
    )


def _innermost_try_containing(source: str, marker: str):
    """The innermost `try:` block whose body contains `marker`, as (lineno, col_offset).

    Structural, via the AST, because the thing under test is NESTING and the only honest way to
    read nesting out of this file is to parse it. An indentation heuristic cannot: the SQL lives
    in triple-quoted strings whose own indentation has nothing to do with the Python block
    structure, and the first cut of this test compared exactly those and failed for that reason.
    """
    import ast

    tree = ast.parse(source)
    found = []

    def walk(node, try_stack):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if marker in node.value:
                found.append(try_stack[-1] if try_stack else None)
        for child in ast.iter_child_nodes(node):
            if isinstance(node, ast.Try) and child in node.body:
                walk(child, try_stack + [(node.lineno, node.col_offset)])
            else:
                walk(child, try_stack)

    walk(tree, [])
    assert found, f"marker not found in the source: {marker!r}"
    return found


async def test_the_hint_alters_are_not_inside_the_mig_224_try():
    """THE SHAPE ASSERTION BEHIND THE STARVATION TESTS, so the mutant that folds the mig-225
    ALTERs back into the mig-224 try is refused by a test that NAMES it rather than by a schema
    diff a reader has to decode.

    Read STRUCTURALLY out of the AST: the innermost `try:` enclosing the mig-225 ALTER must not
    be the innermost `try:` enclosing the mig-224 `CREATE TABLE`. That is exactly what "its own
    try/except" means, and it is what stops a failing CREATE INDEX reaching these columns.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "db/schema_guard.py").read_text(
        encoding="utf-8"
    )

    create_tries = set(
        _innermost_try_containing(source, "CREATE TABLE IF NOT EXISTS reap_agentic_purchases")
    )
    pg_alter_tries = set(
        _innermost_try_containing(source, "ADD COLUMN IF NOT EXISTS accept_variant_labels")
    )
    assert create_tries and pg_alter_tries, "precondition: both statements were located"
    assert not (create_tries & pg_alter_tries), (
        "the Postgres mig-225 ALTER shares its try with the mig-224 CREATE TABLE — a failing "
        "CREATE INDEX in that block would starve the hint columns, and production never runs "
        "db/migrations so there is no other route"
    )

    # The SQLite twin: its per-column ALTER is an f-string, so the marker is the literal fragment
    # that survives into the AST as a constant.
    sqlite_alter_tries = set(
        _innermost_try_containing(source, "ALTER TABLE reap_agentic_purchases ")
    )
    assert sqlite_alter_tries, "precondition: the SQLite mig-225 ALTER was located"
    assert not (sqlite_alter_tries & create_tries), (
        "the SQLite mig-225 ALTER loop shares its try with the mig-224 CREATE TABLE"
    )


async def test_the_coverage_gate_sees_the_mig_225_columns():
    """The repo-wide gate (tests/test_schema_guard_migration_coverage.py) already checks this —
    but when it fails it says "migration 225 is uncovered", which sends the reader looking for a
    missing self-heal that is in fact right there. This says the other thing.

    THE FAILURE MODE IS PROSE. That gate scans db/schema_guard.py with a regex matching the words
    ALTER + TABLE followed by a name, then captures everything up to the next `;`. A MENTION OF
    THOSE WORDS IN A COMMENT matches first, swallows the real statement below it into its body,
    and files these three columns under a table named "if" — after which the gate reports the
    migration as uncovered and `re.finditer` never looks at the real statement at all. That
    happened while this PR was being written, to a comment explaining the statement.
    """
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "_coverage_gate", root / "tests/test_schema_guard_migration_coverage.py"
    )
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    covered = gate._schema_guard_covered()
    for column in _HINT_COLUMNS:
        assert ("reap_agentic_purchases", column) in covered, (
            f"the coverage gate cannot see the self-heal for {column}. The ALTER is in "
            "db/schema_guard.py — check whether a COMMENT in that file mentions the words ALTER "
            "and TABLE together, which makes the gate's regex swallow the real statement."
        )


# ── 225.7 a terminal transition CLEARS a stale last_error_code ───────────────────────────────
#
# FROM THE #2204 RE-REVIEW. `last_error_code` is the one optional field in `transition` whose
# "None means leave it alone" is wrong once the purchase is FINISHED: the column then stops
# meaning "the last thing that went wrong" and starts meaning "why this purchase ended". With an
# unconditional COALESCE, a retry that eventually SUCCEEDS carried the failed attempt's code into
# a 'completed' row, and every reader of that row would have believed the order had a problem it
# did not have.


async def _with_stored_error_code(state: str, code: str = "completed_without_order_id", **over):
    """A purchase sitting in `state` with `code` already on the row.

    The code is put there with a RAW UPDATE rather than through `release_claim`, because
    `release_claim` is not what these tests are about and a claim/release cycle would also move
    `claimed_by`, `attempts` and `next_poll_at` — three things that could mask or explain a
    result here.
    """
    purchase = await _mk(state=state, **over)
    await database.execute(
        "UPDATE reap_agentic_purchases SET last_error_code = :c WHERE id = :i",
        {"c": code, "i": purchase["id"]},
    )
    assert (await ledger.get_purchase_internal(purchase["id"]))["last_error_code"] == code
    return purchase


async def test_a_clean_completion_clears_a_stale_error_code():
    """THE CASE THE RE-REVIEW FOUND. One `processing` poll failed and recorded why; the next one
    completed the order. The finished row must not still say the first poll's reason."""
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
async def test_every_terminal_target_clears_a_stale_code_when_none_is_passed(terminal):
    """All four terminals, not just 'completed' — the rule is about the column's MEANING once the
    row is finished, which does not depend on which terminal it reached."""
    purchase = await _with_stored_error_code(
        _REACHES_TERMINAL[terminal], buyer_ref=f"bref_{terminal}"
    )
    moved = await ledger.transition(
        purchase["id"], from_states=[_REACHES_TERMINAL[terminal]], to_state=terminal
    )
    assert moved["state"] == terminal
    assert moved["last_error_code"] is None


async def test_a_terminal_transition_stores_an_explicit_code():
    """The other half, and the one that makes 'failed' and 'refused' able to speak at all. The
    rule clears on None; it does not stop a caller SAYING why."""
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


async def test_a_terminal_transition_can_keep_the_old_code_by_restating_it():
    """A caller that WANTS the finished row to carry the code it already had passes it. Stated
    because "None clears" would otherwise read as "the ledger throws the reason away"."""
    purchase = await _with_stored_error_code("processing", code="transport_error:readtimeout")
    moved = await ledger.transition(
        purchase["id"],
        from_states=["processing"],
        to_state="failed",
        last_error_code="transport_error:readtimeout",
    )
    assert moved["last_error_code"] == "transport_error:readtimeout"


async def test_a_non_terminal_transition_still_keeps_the_previous_code():
    """THE COALESCE HALF, AND THE CONTROL FOR THE WHOLE RULE. A step in flight that reports
    nothing must not erase the last thing that did go wrong — otherwise the fix for the stale
    code would just be "never store one". Without this test, replacing the CASE with a bare
    assignment on BOTH arms would pass every test above."""
    purchase = await _with_stored_error_code("quoting", code="quote_expired")

    moved = await ledger.transition(
        purchase["id"], from_states=["quoting"], to_state="awaiting_approval"
    )

    assert moved["state"] == "awaiting_approval"
    assert moved["last_error_code"] == "quote_expired", (
        "a non-terminal advance erased the previous error code — a row in flight would forget "
        "why it last stalled on every uneventful step"
    )


async def test_a_non_terminal_transition_still_overwrites_with_a_new_code():
    purchase = await _with_stored_error_code("resolving", code="quote_expired")
    moved = await ledger.transition(
        purchase["id"],
        from_states=["resolving"],
        to_state="quoting",
        last_error_code="resolve_retry",
    )
    assert moved["last_error_code"] == "resolve_retry"


async def test_release_claim_still_coalesces_even_though_transition_does_not_always():
    """The rule is about TERMINAL writes, and a release is never terminal. Pinned here so that
    "make them consistent" is a change somebody has to argue for rather than tidy into."""
    purchase = await _with_stored_error_code("quoting", code="quote_expired")
    await ledger.claim_due_purchases("worker_a", limit=5)
    await ledger.release_claim(purchase["id"], "worker_a")
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["last_error_code"] == "quote_expired"


async def test_the_terminal_clear_is_conditional_in_both_transition_statements():
    """The shape assertion, on both dialect twins. The mutant this refuses is the obvious tidy-up
    — putting `last_error_code` back on the unconditional COALESCE line with its neighbours."""
    for name in ("_TRANSITION_SQL", "_TRANSITION_SQL_SQLITE"):
        sql = getattr(ledger, name)
        assert "last_error_code = COALESCE(:last_error_code, last_error_code),\n" not in sql, (
            f"{name} writes last_error_code through an unconditional COALESCE; a clean "
            "completion after a failed retry would keep the stale code"
        )
        assert (
            "last_error_code = CASE WHEN :to_state_probe IN "
            "('completed', 'failed', 'refused', 'expired')" in sql
        ), f"{name} lost the terminal-clears-the-code rule"
        # And the NON-terminal arm must still be a COALESCE, or a step in flight erases it.
        assert "ELSE COALESCE(:last_error_code, last_error_code) END" in sql, (
            f"{name} lost the COALESCE on the non-terminal arm"
        )

    # The release statements keep the unconditional form — a release is never terminal.
    for name in ("_RELEASE_CLAIM_SQL", "_RELEASE_CLAIM_SQL_SQLITE"):
        assert (
            "last_error_code = COALESCE(:last_error_code, last_error_code)"
            in getattr(ledger, name)
        )


async def test_the_bulk_sweeps_still_write_their_own_terminal_codes():
    """The two sweeps write terminal states WITHOUT going through `transition`, so the rule above
    does not reach them — and they must keep saying why, since a sweep is the one terminal write
    no caller is watching."""
    expired = await _mk(state="needs_enrollment")
    await _set_clock_column(
        expired["id"], "state_entered_at", "datetime('now', '-99999 seconds')"
    )
    assert expired["id"] in await ledger.expire_overdue_purchases(max_age_seconds=60)
    assert (await ledger.get_purchase_internal(expired["id"]))["last_error_code"] == (
        "hosted_url_expired"
    )

    stuck = await _mk(state="quoting", buyer_ref="bref_stuck")
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 99 WHERE id = :i", {"i": stuck["id"]}
    )
    assert stuck["id"] in await ledger.fail_exhausted_purchases(5)
    assert (await ledger.get_purchase_internal(stuck["id"]))["last_error_code"] == (
        "attempts_exhausted"
    )


# ── migration 233: the consent that was in force when the purchase was opened ────────────────
#
# WHAT THESE TESTS ARE FOR, in one sentence: the tag has to be UNREVISABLE and UNDELETABLE, and
# "unrevisable" has three separate mechanisms behind it (absent from `_TRANSITION_FIELDS`,
# absent from the transition statement, absent from every sweep), each of which a mutant can
# break on its own.

CONSENT_TAG = "terms-2026-09"


async def test_a_purchase_opened_with_a_consent_carries_both_columns():
    purchase = await _mk(consent_version=CONSENT_TAG)
    assert purchase["consent_version"] == CONSENT_TAG
    assert isinstance(purchase["consented_at"], datetime)
    assert purchase["consented_at"].tzinfo is not None, (
        "consented_at must come back aware — a naive one read as local time is the wrong moment"
    )

    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["consent_version"] == CONSENT_TAG
    assert read["consented_at"] == purchase["consented_at"]


async def test_a_purchase_opened_without_one_reads_as_none():
    """THE PRE-233 SHAPE. The column is nullable for rows this rail opened before the migration,
    and None must be distinguishable from every value rather than defaulted into one."""
    purchase = await _mk()
    assert purchase["consent_version"] is None
    assert purchase["consented_at"] is None


async def test_a_caller_supplied_consented_at_is_stored_as_given():
    """The route and the ledger write the pair in the same request; a caller that wants them to
    be the SAME instant rather than milliseconds apart can say so."""
    moment = datetime(2026, 9, 20, 11, 22, 33, tzinfo=timezone.utc)
    purchase = await _mk(consent_version=CONSENT_TAG, consented_at=moment)
    assert purchase["consented_at"] == moment


@pytest.mark.parametrize(
    "bad,why",
    [
        ("", "blank"),
        ("   ", "whitespace only"),
        ("v" * 33, "past the column's width"),
        ("v1\x00", "a NUL"),
        ("v1\nv2", "an interior newline"),
        ("v1\x1b[31m", "an ANSI escape"),
        (123, "not a string"),
        (True, "not a string"),
    ],
)
async def test_a_malformed_consent_is_refused_and_leaves_no_row(bad, why):
    """REFUSED BEFORE THE INSERT, like the hints: a row written and then rejected would be a
    'resolving' row holding the buyer's address with no consent and nothing to terminate it.

    A BLANK IS NOT None. `""` is a caller that meant to send a tag; storing it as NULL would make
    it indistinguishable from a pre-233 row, which is the one distinction the column exists for.
    """
    before = await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases")
    with pytest.raises(ValueError):
        await _mk(consent_version=bad)
    assert await database.fetch_val("SELECT COUNT(*) FROM reap_agentic_purchases") == before, (
        f"a consent that is {why} left a row behind"
    )


async def test_a_consented_at_without_a_version_is_refused():
    """A moment attached to no version records nothing."""
    with pytest.raises(ValueError, match="without a consent_version"):
        await _mk(consented_at=datetime(2026, 9, 20, tzinfo=timezone.utc))


async def test_a_naive_consented_at_is_refused_rather_than_assumed_utc():
    """`_bind_dt` ASSUMES UTC for the rail's own clocks, and that is right for them. This value
    is evidence about a human act: a timestamp read eight hours off is evidence of the wrong
    moment, so the caller is told rather than silently repaired."""
    with pytest.raises(ValueError, match="timezone-aware"):
        await _mk(consent_version=CONSENT_TAG, consented_at=datetime(2026, 9, 20, 11, 0, 0))


async def test_a_consent_tag_the_route_accepts_is_not_refused_here():
    """THE TWO VALIDATORS MUST ACCEPT THE SAME SET. The route validates and writes the tag onto
    the buyer-ref row, then calls this module; a NARROWER rule here would refuse, AFTER the
    consent had been recorded, a value the route had already accepted — two stores disagreeing,
    and a refusal that arrives too late to prevent it.

    These are all values `routes.agent_commerce_reap._consent_version` returns unchanged.
    """
    for n, tag in enumerate(("v1", "terms 2026-09", "Terms/v2 (EU)", "条款-v1", "x" * 32)):
        purchase = await _mk(consent_version=tag, buyer_ref=f"bref_tag_{n}")
        assert purchase["consent_version"] == tag


async def test_the_consent_columns_are_not_transition_fields():
    """THE MUTANT THIS KILLS: adding them to `_TRANSITION_FIELDS`. A poller step that could
    rewrite the tag could rewrite what the buyer is recorded as having agreed to."""
    assert "consent_version" not in ledger._TRANSITION_FIELDS
    assert "consented_at" not in ledger._TRANSITION_FIELDS

    # And the STATEMENT does not name them either — the tuple and the SQL are two places a
    # mutant can reach, and only one of them is what the database acts on.
    for name in ("_TRANSITION_SQL", "_TRANSITION_SQL_SQLITE"):
        sql = getattr(ledger, name)
        assert "consent_version" not in sql, f"{name} writes consent_version"
        assert "consented_at" not in sql, f"{name} writes consented_at"


async def test_a_transition_cannot_change_the_consent():
    """The behavioural reading of the above: passing the field is a TypeError at the call site,
    not a silently dropped write."""
    purchase = await _mk(consent_version=CONSENT_TAG)
    with pytest.raises(TypeError):
        await ledger.transition(
            purchase["id"],
            from_states=["resolving"],
            to_state="quoting",
            consent_version="terms-forged",
        )
    moved = await ledger.transition(
        purchase["id"], from_states=["resolving"], to_state="quoting"
    )
    assert moved["consent_version"] == CONSENT_TAG


@pytest.mark.parametrize("terminal,from_state", sorted(_REACHES_TERMINAL.items()))
async def test_the_terminal_write_keeps_the_consent_and_nulls_the_pii(terminal, from_state):
    """THE WHOLE POINT OF MIGRATION 233, as a property of the one statement that writes a
    terminal state: the buyer's email and address go, the consent stays.

    BOTH HALVES IN ONE TEST, deliberately. "The consent survives" on its own would pass on a
    build where the terminal write had stopped nulling ANYTHING, which is a far worse defect.
    """
    purchase = await _mk(state=from_state, consent_version=CONSENT_TAG)
    assert purchase["buyer_email"] and purchase["shipping_address"]

    done = await ledger.transition(purchase["id"], from_states=[from_state], to_state=terminal)
    assert done["state"] == terminal
    assert done["buyer_email"] is None, "the terminal write stopped nulling the email"
    assert done["shipping_address"] is None, "the terminal write stopped nulling the address"
    assert done["consent_version"] == CONSENT_TAG, (
        "the terminal write took the consent evidence with the PII"
    )
    assert done["consented_at"] is not None


async def test_the_bulk_sweeps_keep_the_consent_too():
    """The two sweeps write a terminal state WITHOUT going through `transition`, so the rule
    above does not reach them — they are their own statements and their own mutants."""
    expired = await _mk(state="needs_enrollment", consent_version=CONSENT_TAG)
    await _set_clock_column(
        expired["id"], "state_entered_at", "datetime('now', '-99999 seconds')"
    )
    assert expired["id"] in await ledger.expire_overdue_purchases(max_age_seconds=60)
    swept = await ledger.get_purchase_internal(expired["id"])
    assert swept["buyer_email"] is None and swept["consent_version"] == CONSENT_TAG

    stuck = await _mk(state="quoting", buyer_ref="bref_stuck2", consent_version=CONSENT_TAG)
    await database.execute(
        "UPDATE reap_agentic_purchases SET attempts = 99 WHERE id = :i", {"i": stuck["id"]}
    )
    assert stuck["id"] in await ledger.fail_exhausted_purchases(5)
    failed = await ledger.get_purchase_internal(stuck["id"])
    assert failed["buyer_email"] is None and failed["consent_version"] == CONSENT_TAG


async def test_the_owner_can_read_their_own_consent_tag():
    """IN the allowlist, unlike every other column added to this table since 224. A buyer's own
    consent tag is theirs to read — see the note on PUBLIC_PURCHASE_COLUMNS."""
    purchase = await _mk(consent_version=CONSENT_TAG)
    view = await ledger.get_purchase_for_owner(purchase["id"], "agent_one", "hash_alice")
    assert view["consent_version"] == CONSENT_TAG
    assert view["consented_at"] is not None

    assert "consent_version" in ledger.PUBLIC_PURCHASE_COLUMNS
    assert "consented_at" in ledger.PUBLIC_PURCHASE_COLUMNS

    listed = await ledger.list_purchases_for_owner("agent_one", "hash_alice")
    assert listed[0]["consent_version"] == CONSENT_TAG


async def test_retiring_the_buyer_refs_row_leaves_the_purchase_consent_intact():
    """WP4c's repoint DELETES the refs row — which was the only carrier of the tag before 233.
    This is the defect migration 233 exists to close, tested through the real sweep."""
    await database.execute(
        "INSERT INTO reap_agentic_buyer_refs "
        "(buyer_id, reap_buyer_ref, consent_version, consented_at) "
        "VALUES ('u_abc', 'bref_alice', :c, CURRENT_TIMESTAMP)",
        {"c": CONSENT_TAG},
    )
    purchase = await _mk(consent_version=CONSENT_TAG)
    assert (await ledger.get_buyer_ref_consent("bref_alice"))["consent_version"] == CONSENT_TAG

    report = await ledger.retire_buyer_refs_for_buyer("u_abc", reason="buyer_link_repointed")
    assert report.refs_retired == 1

    # The refs row is gone — the evidence it used to carry went with it.
    assert await ledger.get_buyer_ref_consent("bref_alice") is None
    # The purchase still says what it was opened under.
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["consent_version"] == CONSENT_TAG
    assert read["consented_at"] is not None


async def test_the_self_heal_lands_the_consent_columns_on_a_224_shaped_database():
    """PATH TWO for migration 233, the one production takes: a database that already has the
    table gets the columns from the self-heal's own statement, not from the CREATE TABLE.

    A test that only covered the empty database would pass with this heal deleted.
    """
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    before = await _purchase_columns()
    assert "consent_version" not in before, "precondition: this is a pre-233 shape"

    await ensure_required_schema_light()

    after = await _purchase_columns()
    assert {"consent_version", "consented_at"} <= after

    # And the rail works on the healed table.
    purchase = await _mk(consent_version=CONSENT_TAG)
    read = await ledger.get_purchase_internal(purchase["id"])
    assert read["consent_version"] == CONSENT_TAG


async def test_the_consent_self_heal_is_idempotent_on_a_second_run():
    """SQLite's ADD COLUMN has no IF NOT EXISTS and RAISES on a duplicate, so "no-op" here means
    "the except swallowed it AND the column after it still ran" — which is why that branch is one
    try PER COLUMN. A shared try would leave `consented_at` permanently missing."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    await ensure_required_schema_light()
    once = await _purchase_columns()

    await ensure_required_schema_light()
    await ensure_required_schema_light()

    assert await _purchase_columns() == once
    assert {"consent_version", "consented_at"} <= once


async def test_the_consent_heal_finishes_a_PARTIALLY_healed_table():
    """THE STATE THE PER-COLUMN try ACTUALLY EXISTS FOR, and the one an idempotence test cannot
    reach.

    A previous run that added `consent_version` and then died — a crash, a killed pod, an
    operator who ran half the ALTER by hand — leaves a table with one of the two columns. On the
    next startup SQLite raises "duplicate column name" on the FIRST statement, and a branch that
    shared one try (or broke out of the loop) would treat that as "nothing to do here" and never
    reach `consented_at`. The table would then be permanently short of it, on exactly the
    databases that were already unwell, and production has no other route to that column.

    A sweep mutant that turns the per-column `continue` into a `break` survives the idempotence
    test above — both columns are absent on its first run, so nothing raises — and is killed
    here. That is the whole reason this test is separate.
    """
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    await database.execute(
        "ALTER TABLE reap_agentic_purchases ADD COLUMN consent_version VARCHAR(32)"
    )
    partial = await _purchase_columns()
    assert "consent_version" in partial and "consented_at" not in partial, (
        "precondition: exactly one of the two columns is present"
    )

    await ensure_required_schema_light()

    healed = await _purchase_columns()
    assert "consented_at" in healed, (
        "the heal stopped at the already-present column and never reached consented_at"
    )
    # And the rail works on the finished table.
    purchase = await _mk(consent_version=CONSENT_TAG)
    assert (await ledger.get_purchase_internal(purchase["id"]))["consented_at"] is not None


async def test_the_hint_heal_also_finishes_a_partially_healed_table():
    """THE CONTROL, and a check on the claim the mig-225 twin's comment already makes. The same
    `break`-instead-of-`continue` defect is available in every per-column loop in that branch;
    this one proves the property is not special to the mig-233 loop."""
    await database.execute("DROP TABLE IF EXISTS reap_agentic_purchases")
    await database.execute(_MIG_224_PURCHASES_DDL)
    await database.execute(
        "ALTER TABLE reap_agentic_purchases ADD COLUMN accept_variant_labels TEXT"
    )
    await ensure_required_schema_light()

    healed = await _purchase_columns()
    assert {"also_accept_domains", "market_country"} <= healed, (
        "the mig-225 heal stopped at the already-present column"
    )


def test_the_233_self_heal_twins_declare_the_migrations_columns():
    """SOURCE-LEVEL, and it is the only thing that can see the SQLite twin at all: the coverage
    gate reads `ADD COLUMN <name>` out of the POSTGRES branch, and the SQLite twin builds its
    DDL with an f-string whose column name is a placeholder. So this test names both.

    The Postgres side is compared token-for-token against the migration; the SQLite side is
    checked for the two column names and their types, which is what the placeholder hides.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    migration = (
        root / "db/migrations/233_reap_agentic_purchase_consent.sql"
    ).read_text(encoding="utf-8")
    guard = (root / "db/schema_guard.py").read_text(encoding="utf-8")

    def _statement(text: str, start: int) -> str:
        return " ".join(text[start: text.index(";", start) + 1].split())

    # BY MEMBERSHIP, NOT BY POSITION. The guard's Postgres branch holds SEVERAL statements that
    # open this table (225, 229, 233) and the buyer-refs one (227) names the SAME TWO COLUMNS on
    # a different table — so "the first ALTER before the first mention of consent_version" reads
    # the wrong statement, which is how the first cut of this test failed. Collect every one of
    # them and require the migration's, verbatim, to be present.
    opener = "ALTER TABLE IF EXISTS reap_agentic_purchases"
    found, at = [], guard.find(opener)
    while at != -1:
        found.append(_statement(guard, at))
        at = guard.find(opener, at + 1)

    wanted = _statement(migration, migration.index(opener))
    assert wanted in found, (
        "the mig-233 statement in the migration is not one of db/schema_guard.py's:\n"
        f"  migration:    {wanted}\n"
        + "".join(f"  schema_guard: {f}\n" for f in found)
    )
    assert found.count(wanted) == 1, "the mig-233 heal is duplicated in db/schema_guard.py"

    assert '("consent_version", "VARCHAR(32)")' in guard, (
        "the SQLite twin of the mig-233 heal does not add consent_version"
    )
    assert '("consented_at", "TIMESTAMP")' in guard, (
        "the SQLite twin of the mig-233 heal does not add consented_at"
    )


def test_the_233_down_migration_drops_both_columns_in_the_safe_order():
    """cart_url/item_source taught this file that a down file's ORDER is load-bearing. These two
    have no dependency on each other, so what is asserted is only that both are dropped and both
    carry `IF EXISTS` — a partial apply must reverse cleanly."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    down = (
        root / "db/migrations/down/233_reap_agentic_purchase_consent_down.sql"
    ).read_text(encoding="utf-8")
    assert "DROP COLUMN IF EXISTS consented_at" in down
    assert "DROP COLUMN IF EXISTS consent_version" in down
    assert down.count("ALTER TABLE IF EXISTS reap_agentic_purchases") == 2, (
        "one statement per column, so a partial apply reverses"
    )
