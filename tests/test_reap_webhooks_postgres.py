"""Reap webhook receiver against REAL Postgres — the gate whose absence let PR #1883's blocker
through: 22 faked-DB tests were green while every real outcome write died on a NOT NULL
constraint. Everything here executes the actual SQL.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_reap_webhooks_postgres.py
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    )
]

# Same convention as test_card_rail_outcomes_postgres.py: this gate DROPS tables, so it must be
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
    # Drop first so a constraint deleted from a migration file cannot survive via IF NOT EXISTS
    # and test a schema the repo no longer declares.
    await database.execute("DROP TABLE IF EXISTS reap_webhook_events")
    await database.execute("DROP TABLE IF EXISTS agent_issued_cards")
    await database.execute("DROP TABLE IF EXISTS card_rail_outcomes")
    for path in _MIGRATIONS:
        for statement in split_statements(path.read_text()):
            await database.execute(statement)
    yield
    if not was_connected and database.is_connected:
        await database.disconnect()


async def _insert_card(**over):
    from db.database import database

    row = {
        "card_id": f"crd_pg_{uuid.uuid4().hex[:12]}",
        "agent_id": "agent_pgtest",
        "recommendation_id": f"rec_pg_{uuid.uuid4().hex[:12]}",
        "merchant_domain": "brand.example",
        "checkout_id": "chk_pg",
        "quote_total_minor": 2317,
        "amount_cap_minor": 2317,
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


async def _status_of(card_id: str):
    from db.database import database

    r = await database.fetch_one(
        "SELECT status, auth_count, settled_amount_minor FROM agent_issued_cards WHERE card_id = :c",
        {"c": card_id},
    )
    return dict(r)


# --- the F1 regression: _outcome_values through the REAL upsert -------------------------------


async def test_outcome_values_satisfies_migration_199_for_all_three_paths():
    from db.card_rail_outcomes import record_outcome
    from routes.reap_webhooks import _outcome_values
    from services.reap_webhooks import ReapEvent

    card = await _insert_card()
    ev = ReapEvent(
        event_id="evt_pg1", event_type="auth_approved", issuer_card_ref=card["issuer_card_ref"],
        pivota_card_id=card["card_id"], amount_minor=2317, currency="USD", decline_reason=None,
    )
    # approved / declined / settlement — each bind set must clear every NOT NULL and CHECK.
    ok = await record_outcome(_outcome_values(card, ev, "completed", None, "approved"))
    assert ok and ok["recommendation_id"] == card["recommendation_id"]
    ok = await record_outcome(
        _outcome_values(card, ev, "failed", "payment_declined", "insufficient_funds")
    )
    assert ok
    ok = await record_outcome(_outcome_values(card, ev, "completed", None, None))
    assert ok

    from db.database import database

    row = await database.fetch_one(
        "SELECT outcome, reported_by, rail, actual_grand_total FROM card_rail_outcomes "
        "WHERE recommendation_id = :r",
        {"r": card["recommendation_id"]},
    )
    assert row["reported_by"] == "reap" and row["rail"] == "reap_card"
    assert str(row["actual_grand_total"]) == "23.1700"


# --- transition matrix ------------------------------------------------------------------------


async def test_approval_is_refused_on_every_non_issued_status():
    from db.agent_issued_cards import apply_auth_approved

    for status in ("requested", "revoked", "expired", "failed", "exhausted"):
        card = await _insert_card(status=status)
        assert await apply_auth_approved(card["card_id"], True) is False, status
        after = await _status_of(card["card_id"])
        assert after["status"] == status and after["auth_count"] == 0


async def test_single_use_exhausts_and_multi_use_does_not():
    from db.agent_issued_cards import apply_auth_approved

    single = await _insert_card(single_use=True)
    assert await apply_auth_approved(single["card_id"], True) is True
    after = await _status_of(single["card_id"])
    assert after["status"] == "exhausted" and after["auth_count"] == 1
    # second approval on the exhausted card is refused by the status guard
    assert await apply_auth_approved(single["card_id"], True) is False

    multi = await _insert_card(single_use=False)
    assert await apply_auth_approved(multi["card_id"], False) is True
    after = await _status_of(multi["card_id"])
    assert after["status"] == "issued" and after["auth_count"] == 1


async def test_settlement_is_deliberately_ungated():
    from db.agent_issued_cards import apply_settlement

    for status in ("exhausted", "expired"):
        card = await _insert_card(status=status)
        assert await apply_settlement(card["card_id"], 2200) is True
        assert (await _status_of(card["card_id"]))["settled_amount_minor"] == 2200


# --- dedup + the delivery contract ------------------------------------------------------------


async def test_dedup_claims_once():
    from db.agent_issued_cards import record_event_once

    assert await record_event_once("evt_dup_pg", "auth_approved", "crd_x") is True
    assert await record_event_once("evt_dup_pg", "auth_approved", "crd_x") is False


async def test_failed_transaction_rolls_back_the_dedup_claim():
    """The at-least-once contract (review finding 2): a failure AFTER the dedup insert must
    roll the claim back, so the provider's retry reprocesses instead of being told 'duplicate'
    while the event's effects were lost."""
    from db.agent_issued_cards import record_event_once
    from db.database import database

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        async with database.transaction():
            assert await record_event_once("evt_crash_pg", "settlement", "crd_y") is True
            raise _Boom()  # any handler failure after the claim

    row = await database.fetch_one(
        "SELECT event_id FROM reap_webhook_events WHERE event_id = :e", {"e": "evt_crash_pg"}
    )
    assert row is None  # claim rolled back...
    assert await record_event_once("evt_crash_pg", "settlement", "crd_y") is True  # ...retry lands
