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

   THE SEAM, stated because it is where this pair stops: these two tests drive the critical
   section against two named connections, because databases==0.7.0 shares one Connection across
   child tasks of one Database and `decide()` can only be reached through the module-level
   handle — gathering two `decide()` calls would serialize on that shared connection and prove
   nothing about Postgres. So they prove the LOCK closes the race; that `decide()` actually
   TAKES it, with the right key, is pinned separately at the seam by
   test_reap_external_auth.test_the_decision_runs_in_one_transaction_under_a_per_card_lock.
   Both halves are needed — neither alone survives deleting the lock.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_reap_external_auth_postgres.py -q
"""

from __future__ import annotations

import asyncio
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


# --- the single-use race, with and without the lock ------------------------------------------


async def _authorize_on(db, card: Dict[str, Any], event_id: str, *, with_lock: bool,
                        hold_seconds: float) -> str:
    """The critical section of services.reap_external_auth.decide, run against ONE named
    connection so two of them can genuinely interleave.

    databases==0.7.0 shares a single Connection across child tasks of one Database, so
    asyncio.gather over the module-level `database` would not race — it would serialize on the
    shared connection and prove nothing. Each caller therefore brings its own Database.

    `hold_seconds` between the read and the insert makes the interleaving deterministic rather
    than timing-dependent: with the lock, the second caller is still blocked at the lock and
    the sleep only extends the hold; without it, both callers read before either writes.
    """
    async with db.transaction():
        if with_lock:
            await db.execute(
                "SELECT pg_advisory_xact_lock(CAST(hashtext(CAST(:lock_key AS text)) AS bigint))",
                {"lock_key": f"reap_auth:{card['issuer_card_ref']}"},
            )
        prior = await db.fetch_one(
            "SELECT event_id FROM agent_card_auth_decisions "
            "WHERE card_id = :c AND decision = 'APPROVE' LIMIT 1",
            {"c": card["card_id"]},
        )
        await asyncio.sleep(hold_seconds)
        decision = "DECLINE" if prior is not None else "APPROVE"
        await db.execute(
            """
            INSERT INTO agent_card_auth_decisions (
                event_id, card_id, issuer_card_ref, decision, reason, reason_code,
                amount_minor, currency, channel, merchant_verified, latency_ms
            ) VALUES (
                :event_id, :card_id, :issuer_card_ref, :decision, :reason, :reason_code,
                4250, 'USD', 'ECOMMERCE', CAST(:merchant_verified AS boolean),
                CAST(:latency_ms AS integer)
            )
            ON CONFLICT (event_id) DO NOTHING
            """,
            {
                "event_id": event_id,
                "card_id": card["card_id"],
                "issuer_card_ref": card["issuer_card_ref"],
                "decision": decision,
                "reason": None if decision == "APPROVE" else "TRANSACTION_NOT_ALLOWED",
                "reason_code": "approved" if decision == "APPROVE" else "already_authorized",
                "merchant_verified": True,
                "latency_ms": 1,
            },
        )
    return decision


async def _race(card: Dict[str, Any], *, with_lock: bool) -> int:
    """Two concurrent authorizations on one card. Returns how many APPROVE rows survived."""
    from databases import Database

    from db.database import database

    left, right = Database(DATABASE_URL), Database(DATABASE_URL)
    await left.connect()
    await right.connect()
    try:
        await asyncio.gather(
            _authorize_on(left, card, "evt_race_a", with_lock=with_lock, hold_seconds=0.25),
            _authorize_on(right, card, "evt_race_b", with_lock=with_lock, hold_seconds=0.25),
        )
    finally:
        await left.disconnect()
        await right.disconnect()

    return await database.fetch_val(
        "SELECT COUNT(*) FROM agent_card_auth_decisions "
        "WHERE card_id = :c AND decision = 'APPROVE'",
        {"c": card["card_id"]},
    )


async def test_without_the_lock_both_authorizations_approve():
    """The bug the lock exists to prevent, reproduced. A single-use card authorized TWICE is a
    cap breached by 100%: two full-value charges on an instrument minted for one."""
    card = await _insert_card()
    assert await _race(card, with_lock=False) == 2


async def test_the_advisory_lock_closes_the_race():
    """Same interleaving, same two connections, lock taken: the second authorization blocks
    until the first commits, then sees the reservation and declines."""
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


async def test_has_approval_reads_only_approvals():
    """A negative assertion with its positive counterpart: declines must not reserve the card,
    or the first failed authorization would kill a card the buyer can still legitimately use."""
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

    await record_decision(dict(base, event_id="evt_a1", decision="APPROVE",
                               reason=None, reason_code="approved"))
    assert await has_approval(card["card_id"]) is True
