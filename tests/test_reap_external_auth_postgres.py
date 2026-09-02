"""The external-authorization decision against REAL Postgres.

Two things only real Postgres can prove, and both are load-bearing:

1. THE SQL RUNS. Faked-DB tests captured bind dicts; this executes them against migration 207's
   actual DDL. That gap is not hypothetical on this rail — PR #1883 shipped 22 green faked-DB
   tests over an outcome write that died on a NOT NULL constraint in every real environment.

2. THE SINGLE-USE RACE IS CLOSED. Rule (d) is a read-then-write reservation, and at READ
   COMMITTED two concurrent authorizations for one card both see no prior APPROVE and both
   approve. `test_without_the_lock_both_authorizations_approve` REPRODUCES that with two
   independent connections, and `test_the_advisory_lock_closes_the_race` shows the same
   interleaving yielding exactly one APPROVE once the lock is taken. A concurrency test that
   only ever ran the safe version would be proving nothing.

   BOTH RACERS RUN THE SHIPPED decide(). An earlier version of this file hand-copied decide()'s
   critical section into a local helper and raced the copy, which proved the advisory lock works
   — never in doubt — while executing none of the production path. Mutants that dead-coded rule
   (d), bypassed the currency rule, or moved the lock to AFTER the reads it protects all passed
   the entire suite. The obstacle was real (databases==0.7.0 shares one Connection across child
   tasks, and decide() reaches the DB through three module globals) and _TaskLocalDatabase below
   removes it with a per-task ContextVar rather than by re-implementing the subject.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_reap_external_auth_postgres.py -q
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

_MIGRATIONS = [
    Path(__file__).resolve().parent.parent / "db/migrations" / name
    for name in (
        "199_card_rail_outcomes.sql",
        "201_agent_issued_cards.sql",
        "202_reap_webhook_events.sql",
        "207_agent_card_auth_decisions.sql",
    )
]

# Same convention as test_reap_webhooks_postgres.py: this gate DROPS tables, so it must be
# incapable of running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop card-rail tables in database {dbname!r} — throwaway only "
            f"(e.g. pivota_dialect_check)"
        )


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database
    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Drop first so a constraint deleted from a migration file cannot survive via
    # IF NOT EXISTS and test a schema the repo no longer declares.
    for table in (
        "agent_card_auth_decisions",
        "agent_card_merchant_descriptors",
        "reap_webhook_events",
        "agent_issued_cards",
        "card_rail_outcomes",
    ):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    for path in _MIGRATIONS:
        for statement in split_statements(path.read_text()):
            await database.execute(statement)
    yield
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _insert_card(**over) -> Dict[str, Any]:
    from db.database import database

    row = {
        "card_id": f"crd_pg_{uuid.uuid4().hex[:12]}",
        "agent_id": "agent_pgtest",
        "recommendation_id": f"rec_pg_{uuid.uuid4().hex[:12]}",
        "merchant_domain": "brand.example",
        "checkout_id": "chk_pg",
        "quote_total_minor": 4250,
        "amount_cap_minor": 4250,
        "currency": "USD",
        "issuer": "mock",
        "issuer_card_ref": f"ref_{uuid.uuid4().hex[:10]}",
        "status": "issued",
        "single_use": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    row.update(over)
    await database.execute(
        """
        INSERT INTO agent_issued_cards (
            card_id, agent_id, recommendation_id, merchant_domain, checkout_id,
            quote_total_minor, amount_cap_minor, currency, issuer, issuer_card_ref,
            status, single_use, expires_at
        ) VALUES (
            :card_id, :agent_id, :recommendation_id, :merchant_domain, :checkout_id,
            :quote_total_minor, :amount_cap_minor, :currency, :issuer, :issuer_card_ref,
            :status, :single_use, :expires_at
        )
        """,
        row,
    )
    return row


def _request(card_ref: str, **over) -> Any:
    from decimal import Decimal

    from services.reap_external_auth import AuthorizationRequest

    fields: Dict[str, Any] = {
        "event_id": f"evt_pg_{uuid.uuid4().hex[:12]}",
        "card_ref": card_ref,
        "channel": "ECOMMERCE",
        "currency": "USD",
        "amount": Decimal("42.50"),
        "original_currency": "USD",
        "original_amount": Decimal("42.50"),
        "merchant_name": "ACME Store",
        "merchant_city": "Berlin",
        "merchant_country": "DE",
        "mcc": "5732",
    }
    fields.update(over)
    return AuthorizationRequest(**fields)


async def _stored(event_id: str) -> Optional[Dict[str, Any]]:
    from db.database import database

    row = await database.fetch_one(
        "SELECT * FROM agent_card_auth_decisions WHERE event_id = :e", {"e": event_id}
    )
    return dict(row) if row else None


# --- every rule's write, through the REAL insert -------------------------------------------


async def _decide(card_ref: str, **over):
    """decide() plus the row it wrote, so every rule can assert BOTH halves of the contract:
    the wire answer Reap acts on, and the ledger row rule (d) later reserves against."""
    from services.reap_external_auth import decide

    request = _request(card_ref, **over)
    outcome = await decide(request, time.monotonic())
    return outcome, await _stored(request.event_id)


async def test_the_approve_path_writes_a_row_migration_207_accepts():
    """The F1-shaped regression: every NOT NULL and CHECK in the real DDL, cleared by the real
    bind set — including merchant_verified and latency_ms, where an explicit NULL bind would
    defeat the column default rather than fall back to it."""
    card = await _insert_card()
    outcome, row = await _decide(card["issuer_card_ref"])
    assert outcome.decision == "APPROVE"
    assert outcome.body() == {"decision": "APPROVE"}

    assert row is not None, "the decision was answered but never recorded"
    assert row["card_id"] == card["card_id"]
    assert row["issuer_card_ref"] == card["issuer_card_ref"]
    assert row["decision"] == "APPROVE" and row["reason"] is None
    assert row["reason_code"] == "approved"
    assert row["amount_minor"] == 4250 and row["currency"] == "USD"
    assert row["channel"] == "ECOMMERCE"
    assert row["merchant_name"] == "ACME Store" and row["merchant_country"] == "DE"
    assert row["merchant_verified"] is False       # first authorization for this domain
    assert row["latency_ms"] >= 0
    assert row["created_at"] is not None


@pytest.mark.parametrize("status", ["requested", "revoked", "exhausted", "expired", "failed"])
async def test_card_not_live_writes_a_decline_row(status):
    card = await _insert_card(status=status)
    outcome, row = await _decide(card["issuer_card_ref"])
    assert outcome.body() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert row["reason_code"] == "card_not_live"


async def test_unknown_card_writes_a_row_with_a_null_card_id():
    """The nullable card_id earns its keep here: 'we declined something we cannot explain' is
    exactly the event that must not be invisible."""
    outcome, row = await _decide(f"ref_never_minted_{uuid.uuid4().hex[:8]}")
    assert outcome.reason_code == "unknown_card"
    assert row["card_id"] is None
    assert row["issuer_card_ref"].startswith("ref_never_minted_")


async def test_over_cap_writes_insufficient_balance_with_the_amount_asked():
    from decimal import Decimal

    card = await _insert_card(amount_cap_minor=1000)
    outcome, row = await _decide(
        card["issuer_card_ref"], amount=Decimal("42.50"), original_amount=Decimal("42.50")
    )
    assert outcome.body() == {"decision": "DECLINE", "reason": "INSUFFICIENT_BALANCE"}
    assert row["reason_code"] == "over_cap"
    assert row["amount_minor"] == 4250     # what was asked, not the cap


async def test_the_expiry_comparison_uses_the_stored_timestamptz():
    card = await _insert_card(expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    outcome, row = await _decide(card["issuer_card_ref"])
    assert row["reason_code"] == "card_expired"
    assert outcome.decision == "DECLINE"


# --- rule (a): idempotency through the real primary key --------------------------------------


async def test_a_replayed_event_id_returns_the_stored_decision_and_adds_no_row():
    from db.database import database
    from services.reap_external_auth import decide

    card = await _insert_card()
    request = _request(card["issuer_card_ref"])
    first = await decide(request, time.monotonic())
    second = await decide(request, time.monotonic())
    assert first.decision == second.decision == "APPROVE"
    assert second.replayed is True and first.replayed is False

    count = await database.fetch_val(
        "SELECT COUNT(*) FROM agent_card_auth_decisions WHERE event_id = :e",
        {"e": request.event_id},
    )
    assert count == 1


# --- rule (g): the registry, on real rows -----------------------------------------------------


async def test_the_first_authorization_pins_and_the_second_matches():
    from db.database import database

    card_one = await _insert_card(merchant_domain="pinme.example")
    outcome, row = await _decide(card_one["issuer_card_ref"])
    assert outcome.decision == "APPROVE" and row["merchant_verified"] is False

    pins = await database.fetch_all(
        "SELECT name_norm, country, source, seen_count FROM agent_card_merchant_descriptors "
        "WHERE merchant_domain = :d",
        {"d": "pinme.example"},
    )
    assert [dict(p) for p in pins] == [
        {"name_norm": "acme store", "country": "DE", "source": "authorization", "seen_count": 1}
    ]

    card_two = await _insert_card(merchant_domain="pinme.example")
    outcome, row = await _decide(card_two["issuer_card_ref"], merchant_name="ACME  Store*991")
    assert outcome.decision == "APPROVE" and row["merchant_verified"] is True

    seen = await database.fetch_val(
        "SELECT seen_count FROM agent_card_merchant_descriptors WHERE merchant_domain = :d",
        {"d": "pinme.example"},
    )
    assert seen == 2


async def test_a_pinned_domain_declines_a_foreign_descriptor():
    card_one = await _insert_card(merchant_domain="locked.example")
    await _decide(card_one["issuer_card_ref"])

    card_two = await _insert_card(merchant_domain="locked.example")
    outcome, row = await _decide(card_two["issuer_card_ref"], merchant_name="SOMEWHERE ELSE")
    assert outcome.body() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert row["reason_code"] == "merchant_mismatch"


async def test_a_missing_country_pins_as_empty_string_not_null():
    """Postgres treats NULLs as DISTINCT in a UNIQUE constraint. A nullable country would make
    the registry's ON CONFLICT never fire and let one descriptor be pinned without bound."""
    from db.database import database

    card = await _insert_card(merchant_domain="nocountry.example")
    await _decide(card["issuer_card_ref"], merchant_country=None)
    for _ in range(3):
        other = await _insert_card(merchant_domain="nocountry.example")
        await _decide(other["issuer_card_ref"], merchant_country=None)

    rows = await database.fetch_all(
        "SELECT country FROM agent_card_merchant_descriptors WHERE merchant_domain = :d",
        {"d": "nocountry.example"},
    )
    assert len(rows) == 1, "the same descriptor was pinned more than once"
    assert rows[0]["country"] == ""


# --- the decision must not disturb the record path --------------------------------------------


async def test_after_an_approve_the_webhook_transition_still_applies():
    """The decision is not the record. If it had exhausted the card, the CARD_TRANSACTION_CREATED
    webhook that follows would find a non-'issued' card, refuse the transition, and fire
    AUTH_ON_NON_ISSUED_CARD — on every single approval we ever granted."""
    from db.agent_issued_cards import apply_auth_approved
    from db.database import database

    card = await _insert_card()
    outcome, _ = await _decide(card["issuer_card_ref"])
    assert outcome.decision == "APPROVE"

    after_decision = await database.fetch_one(
        "SELECT status, auth_count FROM agent_issued_cards WHERE card_id = :c",
        {"c": card["card_id"]},
    )
    assert after_decision["status"] == "issued", "the decision moved the card's status"
    assert after_decision["auth_count"] == 0

    # ...and the record, arriving afterwards, still applies exactly as it did before.
    assert await apply_auth_approved(card["card_id"], True) is True
    after_record = await database.fetch_one(
        "SELECT status, auth_count FROM agent_issued_cards WHERE card_id = :c",
        {"c": card["card_id"]},
    )
    assert after_record["status"] == "exhausted" and after_record["auth_count"] == 1

# --- more rules, through the REAL decide() on real rows ---------------------------------------


async def test_currency_mismatch_writes_a_decline_row():
    """On Postgres and through decide(), not a hand-copy of it. The currency rule had no
    real-engine test at all, which is how a mutant that bypassed it passed the whole suite."""
    from decimal import Decimal

    card = await _insert_card(currency="USD")
    outcome, row = await _decide(
        card["issuer_card_ref"], currency="GBP", amount=Decimal("33.10"),
        original_currency="EUR", original_amount=Decimal("39.10"),
    )
    assert outcome.body() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert row["reason_code"] == "currency_mismatch"
    assert row["amount_minor"] is None


async def test_both_legs_in_the_cards_currency_take_the_larger():
    """F6, on real rows: presentment-first would pass a cap check on a 1-cent decoy while the
    billing leg debits the account for the real amount."""
    from decimal import Decimal

    card = await _insert_card(currency="USD", amount_cap_minor=4250)
    outcome, row = await _decide(
        card["issuer_card_ref"], currency="USD", amount=Decimal("999999.00"),
        original_currency="USD", original_amount=Decimal("0.01"),
    )
    assert outcome.body() == {"decision": "DECLINE", "reason": "INSUFFICIENT_BALANCE"}
    assert row["reason_code"] == "over_cap"
    assert row["amount_minor"] == 99999900


async def test_an_absurd_amount_declines_instead_of_killing_the_bigint_bind():
    """F2 on the real column. Unbounded, this scaled to 10^22, cleared the cap comparison as a
    Python int, and died on the BIGINT bind — a 500 with NO row, and the amount in the logged
    traceback. The bound turns it into an ordinary recorded decline."""
    from decimal import Decimal

    card = await _insert_card()
    for amount in (Decimal("1e20"), Decimal(10 ** 16)):
        outcome, row = await _decide(
            card["issuer_card_ref"], currency="USD", amount=amount,
            original_currency="USD", original_amount=amount,
        )
        assert outcome.body() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
        assert row["reason_code"] == "amount_unparseable"
        assert row["amount_minor"] is None


async def test_a_zero_amount_verification_approves_and_reserves_nothing():
    """F7 end to end: the $0.00 live-card check approves, records amount_minor 0, pins nothing,
    and — the point — does not burn the single-use card, so the real charge behind it still
    goes through."""
    from decimal import Decimal

    from db.agent_card_auth_decisions import has_approval
    from db.database import database

    card = await _insert_card(merchant_domain="zeroauth.example")
    outcome, row = await _decide(
        card["issuer_card_ref"], currency="USD", amount=Decimal("0"),
        original_currency="USD", original_amount=Decimal("0"),
    )
    assert outcome.body() == {"decision": "APPROVE"}
    assert row["reason_code"] == "zero_amount_verification"
    assert row["amount_minor"] == 0

    # It is an APPROVE row, and it must still not act as a rule (d) reservation.
    assert await has_approval(card["card_id"]) is False
    pins = await database.fetch_all(
        "SELECT id FROM agent_card_merchant_descriptors WHERE merchant_domain = :d",
        {"d": "zeroauth.example"},
    )
    assert pins == []

    # ...and the real charge that follows still approves.
    outcome, row = await _decide(card["issuer_card_ref"])
    assert outcome.body() == {"decision": "APPROVE"}
    assert row["reason_code"] == "approved"
    assert await has_approval(card["card_id"]) is True


async def test_a_multi_use_card_is_bounded_by_the_sum_of_its_approvals():
    """F10 on real rows. Without the SUM, amount_cap_minor bounds each authorization and not the
    card: these three at 42.50 against a 100.00 cap would all approve."""
    from decimal import Decimal

    card = await _insert_card(single_use=False, amount_cap_minor=10000)
    first, row = await _decide(card["issuer_card_ref"])
    assert first.decision == "APPROVE" and row["amount_minor"] == 4250

    second, row = await _decide(card["issuer_card_ref"])
    assert second.decision == "APPROVE"          # 4250 + 4250 = 8500 <= 10000

    third, row = await _decide(card["issuer_card_ref"])
    assert third.body() == {"decision": "DECLINE", "reason": "INSUFFICIENT_BALANCE"}
    assert row["reason_code"] == "over_cap"      # 8500 + 4250 = 12750 > 10000


async def test_zero_amount_approvals_do_not_consume_a_multi_use_cards_headroom():
    """The two F7/F10 rules meeting: a verification adds 0 to the sum, so it neither reserves a
    single-use card nor eats a multi-use card's headroom."""
    from decimal import Decimal

    from db.agent_card_auth_decisions import approved_total_minor

    card = await _insert_card(single_use=False, amount_cap_minor=5000)
    await _decide(
        card["issuer_card_ref"], currency="USD", amount=Decimal("0"),
        original_currency="USD", original_amount=Decimal("0"),
    )
    assert await approved_total_minor(card["card_id"]) == 0
    outcome, _ = await _decide(card["issuer_card_ref"])
    assert outcome.decision == "APPROVE"


async def test_two_live_cards_for_one_issuer_ref_decline_ambiguous():
    """issuer_card_ref has no unique constraint, so this is representable on the real table. The
    two rows carry different caps at different merchants and find_by_issuer_ref's ORDER BY picks
    one deterministically but not correctly."""
    shared = f"ref_dupe_{uuid.uuid4().hex[:8]}"
    await _insert_card(issuer_card_ref=shared, amount_cap_minor=1000,
                       merchant_domain="one.example")
    await _insert_card(issuer_card_ref=shared, amount_cap_minor=999999,
                       merchant_domain="two.example")

    outcome, row = await _decide(shared)
    assert outcome.body() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert row["reason_code"] == "ambiguous_card"


async def test_a_revoked_sibling_does_not_make_a_card_ambiguous():
    """The positive counterpart: the count is scoped to 'issued'. Counting terminal siblings
    would decline every perfectly unambiguous re-issue."""
    shared = f"ref_reissue_{uuid.uuid4().hex[:8]}"
    await _insert_card(issuer_card_ref=shared, status="revoked")
    await _insert_card(issuer_card_ref=shared, status="issued")

    outcome, row = await _decide(shared)
    assert outcome.decision == "APPROVE", row["reason_code"]


async def test_find_by_issuer_ref_returns_the_newest_row():
    """ORDER BY created_at DESC LIMIT 1 — deterministic, not arbitrary. Not a safety property on
    its own (that is ambiguous_card's job), but the tie has to break the same way every time."""
    from db.agent_issued_cards import find_by_issuer_ref
    from db.database import database

    shared = f"ref_order_{uuid.uuid4().hex[:8]}"
    older = await _insert_card(issuer_card_ref=shared, status="revoked", amount_cap_minor=111)
    newer = await _insert_card(issuer_card_ref=shared, status="revoked", amount_cap_minor=222)
    await database.execute(
        "UPDATE agent_issued_cards SET created_at = now() - interval '1 day' WHERE card_id = :c",
        {"c": older["card_id"]},
    )
    found = await find_by_issuer_ref(shared)
    assert found["card_id"] == newer["card_id"] and found["amount_cap_minor"] == 222


# --- F5: an operator can correct a wrong pin ---------------------------------------------------


async def test_a_manual_pin_is_matched():
    """The learned pin is a guess made under a 1.6s budget. When it is wrong, every later
    authorization declines merchant_mismatch and nothing recovers on its own — this is the
    recovery, and it has to actually match the decision path's normalization."""
    from db.agent_card_auth_decisions import pin_descriptor_manual
    from db.database import database

    await pin_descriptor_manual("manualpin.example", "SQ *THE REAL SHOP", "DE")
    row = await database.fetch_one(
        "SELECT name_norm, country, source FROM agent_card_merchant_descriptors "
        "WHERE merchant_domain = :d",
        {"d": "manualpin.example"},
    )
    assert (row["name_norm"], row["country"], row["source"]) == ("the real shop", "DE", "manual")

    card = await _insert_card(merchant_domain="manualpin.example")
    outcome, decision = await _decide(card["issuer_card_ref"], merchant_name="SQ *THE REAL SHOP")
    assert outcome.decision == "APPROVE"
    assert decision["merchant_verified"] is True


async def test_a_manual_pin_promotes_an_existing_learned_pin():
    """An operator asserting a pin must end with that pin asserted, whatever was there before —
    not with an integrity error because the domain had already learned it."""
    from db.agent_card_auth_decisions import pin_descriptor_manual
    from db.database import database

    card = await _insert_card(merchant_domain="promote.example")
    await _decide(card["issuer_card_ref"])   # learns 'acme store' / DE, source 'authorization'

    await pin_descriptor_manual("promote.example", "ACME Store", "DE")
    rows = await database.fetch_all(
        "SELECT source FROM agent_card_merchant_descriptors WHERE merchant_domain = :d",
        {"d": "promote.example"},
    )
    assert [r["source"] for r in rows] == ["manual"]


async def test_unpin_lets_a_domain_relearn():
    """The other half of the recovery: remove the bad pin, and the next authorization teaches
    the domain again."""
    from db.agent_card_auth_decisions import unpin_descriptor
    from db.database import database

    first = await _insert_card(merchant_domain="relearn.example")
    await _decide(first["issuer_card_ref"], merchant_name="WRONG MERCHANT")

    second = await _insert_card(merchant_domain="relearn.example")
    _, row = await _decide(second["issuer_card_ref"])
    assert row["reason_code"] == "merchant_mismatch"   # locked to the wrong descriptor

    assert await unpin_descriptor("relearn.example", "wrong merchant") == 1

    third = await _insert_card(merchant_domain="relearn.example")
    outcome, row = await _decide(third["issuer_card_ref"])
    assert outcome.decision == "APPROVE"
    pins = await database.fetch_all(
        "SELECT name_norm FROM agent_card_merchant_descriptors WHERE merchant_domain = :d",
        {"d": "relearn.example"},
    )
    assert [p["name_norm"] for p in pins] == ["acme store"]


async def test_unpin_of_an_absent_descriptor_is_a_no_op():
    from db.agent_card_auth_decisions import unpin_descriptor

    assert await unpin_descriptor("nothing.example", "not there") == 0


async def test_an_identityless_descriptor_is_never_learned():
    """F4 on real rows: "SQ *" normalizes to something with no merchant identity, and pinning it
    would let the next Square merchant match it verified."""
    from db.database import database

    card = await _insert_card(merchant_domain="weak.example")
    outcome, row = await _decide(card["issuer_card_ref"], merchant_name="SQ *")
    assert outcome.decision == "APPROVE"
    assert row["merchant_verified"] is False
    pins = await database.fetch_all(
        "SELECT id FROM agent_card_merchant_descriptors WHERE merchant_domain = :d",
        {"d": "weak.example"},
    )
    assert pins == []


# --- F3: the deadline, and the lock ceiling ----------------------------------------------------


async def test_a_late_approval_is_downgraded_and_reserves_nothing():
    """Reap declines at 1.6s. An APPROVE committed after that is a decision nobody acted on, and
    on a single-use card it reserves the instrument against a purchase that was already refused
    — the buyer's real retry then dies on already_authorized."""
    import services.reap_external_auth as svc
    from db.agent_card_auth_decisions import has_approval

    card = await _insert_card()
    # Move OUR clock, never the stdlib module object.
    original = svc._now_monotonic
    svc._now_monotonic = lambda: original() + 10.0
    try:
        outcome, row = await _decide(card["issuer_card_ref"])
    finally:
        svc._now_monotonic = original

    assert outcome.body() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert row["decision"] == "DECLINE" and row["reason_code"] == "deadline_exceeded"
    assert row["amount_minor"] == 4250          # the evidence survives the downgrade
    assert await has_approval(card["card_id"]) is False   # ...but it reserves nothing


async def test_a_contended_lock_aborts_instead_of_committing_a_phantom_approval():
    """pg_advisory_xact_lock blocks INDEFINITELY and DB_STATEMENT_TIMEOUT_SECONDS defaults to 0.
    With SET LOCAL lock_timeout armed, a decision that cannot get the lock inside its budget
    raises — which rolls back, returns 500, and leaves NO row. Reap declines either way; the
    difference is whether a phantom APPROVE is left behind reserving the card."""
    import asyncpg
    from databases import Database

    from db.database import database

    card = await _insert_card()

    holder = Database(DATABASE_URL)
    await holder.connect()
    try:
        async with holder.transaction():
            # Take the SAME advisory lock the decision will want, and hold it.
            await holder.execute(
                "SELECT pg_advisory_xact_lock(CAST(hashtext(CAST(:k AS text)) AS bigint))",
                {"k": f"reap_auth:{card['issuer_card_ref']}"},
            )
            with pytest.raises((asyncpg.exceptions.LockNotAvailableError,
                                asyncpg.exceptions.QueryCanceledError)):
                await _decide(card["issuer_card_ref"])
    finally:
        await holder.disconnect()

    left_behind = await database.fetch_val(
        "SELECT COUNT(*) FROM agent_card_auth_decisions WHERE card_id = :c",
        {"c": card["card_id"]},
    )
    assert left_behind == 0, "an aborted decision left a row behind"


async def test_the_lock_timeout_is_actually_armed_on_the_transaction():
    """The positive counterpart to the abort above: SET LOCAL really is in effect inside the
    decision's transaction, rather than the abort coming from somewhere else."""
    from databases import Database

    import services.reap_external_auth as svc

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        async with db.transaction():
            original = svc.database
            svc.database = db
            try:
                await svc._arm_deadlines()
            finally:
                svc.database = original
            assert await db.fetch_val("SHOW lock_timeout") == f"{svc.LOCK_TIMEOUT_MS}ms"
            assert await db.fetch_val("SHOW statement_timeout") == (
                f"{svc.STATEMENT_TIMEOUT_MS}ms"
            )
    finally:
        await db.disconnect()


# --- the single-use race, through the REAL decide() ---------------------------------------------
#
# THE POINT OF THIS SECTION, and what an earlier version of it got wrong. It used to hand-copy
# decide()'s critical section into a local helper and race THAT. It proved the advisory lock
# works — which was never in doubt — while running none of the shipped code, so mutants that
# dead-coded rule (d), bypassed the currency rule, or moved the lock to AFTER the reads it
# protects all passed the whole Postgres suite. A concurrency test that races a copy of the
# subject is testing the copy.
#
# The obstacle was real: databases==0.7.0 shares ONE Connection across child tasks of a Database,
# and decide() reaches the database through THREE module globals
# (services.reap_external_auth, db.agent_card_auth_decisions, db.agent_issued_cards). Gathering
# two decide() calls on the shared handle serializes them on that connection and proves nothing.
#
# _TaskLocalDatabase solves it without touching production code: all three globals are pointed at
# one proxy that resolves, per asyncio task, to whichever Database that task bound in a
# ContextVar. asyncio.gather copies the context into each task, so the two racers genuinely run
# on two connections against one Postgres.

_ACTIVE_DB: contextvars.ContextVar = contextvars.ContextVar("reap_auth_active_db")


class _TaskLocalDatabase:
    """Forwards every call to the Database bound to the CURRENT TASK."""

    def _target(self):
        return _ACTIVE_DB.get()

    def transaction(self, *args, **kwargs):
        return self._target().transaction(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        return await self._target().execute(*args, **kwargs)

    async def fetch_one(self, *args, **kwargs):
        return await self._target().fetch_one(*args, **kwargs)

    async def fetch_all(self, *args, **kwargs):
        return await self._target().fetch_all(*args, **kwargs)

    async def fetch_val(self, *args, **kwargs):
        return await self._target().fetch_val(*args, **kwargs)


async def _race(card: Dict[str, Any], *, with_lock: bool) -> int:
    """Two concurrent REAL decide() calls on one card. Returns how many APPROVE rows survived.

    `with_lock=False` reproduces the bug by turning IS_POSTGRES off inside the service module,
    which is the one flag both _take_card_lock and _arm_deadlines are gated on — the same code
    path production takes on sqlite, and exactly the state the lock exists to rule out.

    The interleaving is forced deterministically by delaying INSIDE has_approval, i.e. between
    the reservation read and the write it is supposed to guard. Unlocked, both racers read
    before either writes. Locked, the second is still blocked at the lock and the delay only
    extends the first one's hold.
    """
    import db.agent_card_auth_decisions as decisions_db
    import db.agent_issued_cards as cards_db
    import services.reap_external_auth as svc
    from databases import Database
    from db.database import database

    proxy = _TaskLocalDatabase()
    real_has_approval = decisions_db.has_approval

    async def slow_has_approval(card_id):
        result = await real_has_approval(card_id)
        await asyncio.sleep(0.25)
        return result

    saved = (svc.database, decisions_db.database, cards_db.database,
             svc.has_approval, svc.IS_POSTGRES)
    _ACTIVE_DB.set(database)
    svc.database = decisions_db.database = cards_db.database = proxy
    svc.has_approval = slow_has_approval
    svc.IS_POSTGRES = with_lock

    async def one(handle, event_id: str):
        _ACTIVE_DB.set(handle)
        await svc.decide(_request(card["issuer_card_ref"], event_id=event_id), time.monotonic())

    left, right = Database(DATABASE_URL), Database(DATABASE_URL)
    await left.connect()
    await right.connect()
    try:
        await asyncio.gather(one(left, "evt_race_a"), one(right, "evt_race_b"))
    finally:
        await left.disconnect()
        await right.disconnect()
        (svc.database, decisions_db.database, cards_db.database,
         svc.has_approval, svc.IS_POSTGRES) = saved

    return await database.fetch_val(
        "SELECT COUNT(*) FROM agent_card_auth_decisions "
        "WHERE card_id = :c AND decision = 'APPROVE'",
        {"c": card["card_id"]},
    )


async def test_the_race_helper_actually_runs_the_shipped_decide():
    """Guards the guard. If _race ever stops calling services.reap_external_auth.decide, both
    race tests keep passing while testing nothing — which is precisely the failure this section
    was rewritten to fix, so it gets its own assertion rather than a comment."""
    import inspect

    import services.reap_external_auth as svc

    source = inspect.getsource(_race)
    assert "svc.decide(" in source
    assert svc.decide.__module__ == "services.reap_external_auth"


async def test_without_the_lock_both_authorizations_approve():
    """The bug the lock exists to prevent, reproduced against the real decide(). A single-use
    card authorized TWICE is a cap breached by 100%: two full-value charges on an instrument
    minted for one."""
    card = await _insert_card()
    assert await _race(card, with_lock=False) == 2


async def test_the_advisory_lock_closes_the_race():
    """Same interleaving, same two connections, same code — lock taken. The second authorization
    blocks until the first commits, then sees the reservation and declines."""
    from db.database import database

    card = await _insert_card()
    assert await _race(card, with_lock=True) == 1

    rows = await database.fetch_all(
        "SELECT decision, reason_code FROM agent_card_auth_decisions "
        "WHERE card_id = :c ORDER BY decision",
        {"c": card["card_id"]},
    )
    assert [(r["decision"], r["reason_code"]) for r in rows] == [
        ("APPROVE", "approved"),
        ("DECLINE", "already_authorized"),
    ]


async def test_the_event_id_primary_key_refuses_a_second_decision():
    """The belt to the lock's braces: even with no serialization at all, one authorization
    cannot produce two verdicts."""
    from db.agent_card_auth_decisions import record_decision

    card = await _insert_card()
    values = {
        "event_id": "evt_pk_guard", "card_id": card["card_id"],
        "issuer_card_ref": card["issuer_card_ref"], "decision": "APPROVE", "reason": None,
        "reason_code": "approved", "amount_minor": 4250, "currency": "USD",
        "channel": "ECOMMERCE", "merchant_name": "ACME Store", "merchant_city": "Berlin",
        "merchant_country": "DE", "mcc": "5732", "merchant_verified": True, "latency_ms": 12,
    }
    assert await record_decision(values) is True
    assert await record_decision(dict(values, decision="DECLINE",
                                      reason="TRANSACTION_NOT_ALLOWED",
                                      reason_code="already_authorized")) is False

    stored = await _stored("evt_pk_guard")
    assert stored["decision"] == "APPROVE"   # the first verdict stands


async def test_has_approval_reserves_only_on_an_approved_spend():
    """Three things must NOT reserve the card, each for its own reason, and one must.

      a DECLINE                  — the first failed authorization would otherwise kill a card
                                   the buyer can still legitimately use;
      an APPROVE with NULL amount — a decision that never resolved an amount moved no money;
      a ZERO-amount APPROVE      — the routine $0.00 verification, which exists precisely to
                                   precede the real charge and must not consume it;
      an APPROVE with amount > 0 — the actual spend. This one reserves.

    The NULL case is not incidental: `NULL > 0` is NULL, not false, so a predicate written any
    other way would quietly include it.
    """
    from db.agent_card_auth_decisions import has_approval, record_decision

    card = await _insert_card()
    base = {
        "card_id": card["card_id"], "issuer_card_ref": card["issuer_card_ref"],
        "amount_minor": None, "currency": None, "channel": "ECOMMERCE",
        "merchant_name": None, "merchant_city": None, "merchant_country": None, "mcc": None,
        "merchant_verified": False, "latency_ms": 3,
    }
    await record_decision(dict(base, event_id="evt_d1", decision="DECLINE",
                               reason="TRANSACTION_NOT_ALLOWED", reason_code="channel_not_allowed"))
    assert await has_approval(card["card_id"]) is False

    await record_decision(dict(base, event_id="evt_a_null", decision="APPROVE",
                               reason=None, reason_code="approved"))
    assert await has_approval(card["card_id"]) is False, "a NULL-amount APPROVE reserved the card"

    await record_decision(dict(base, event_id="evt_a_zero", decision="APPROVE", amount_minor=0,
                               currency="USD", reason=None,
                               reason_code="zero_amount_verification"))
    assert await has_approval(card["card_id"]) is False, "a verification burned the card"

    await record_decision(dict(base, event_id="evt_a_spend", decision="APPROVE",
                               amount_minor=4250, currency="USD", reason=None,
                               reason_code="approved"))
    assert await has_approval(card["card_id"]) is True
