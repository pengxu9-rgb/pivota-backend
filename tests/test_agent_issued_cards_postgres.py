"""`agent_issued_cards` transitions against REAL Postgres — because a returned-row idiom cannot
be tested anywhere else.

THE DEFECT THIS GATE EXISTS FOR. `mark_revoked` used `database.execute()` and reported
`result is not None`. On databases==0.7.0/asyncpg a non-RETURNING UPDATE answers None ALWAYS —
success and no-match alike — so the function was a constant False. Every confirmed revocation
was reported to jobs/agent_card_revocation_sweep.py as "the row did not advance", logged at
ERROR, counted unconfirmed, and retried forever; the row DID advance, so the retry then found
nothing and the sweep's own summary said the opposite of what happened. No faked-DB test could
see it: a double returns whatever it was told to.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_agent_issued_cards_postgres.py
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
    for name in ("201_agent_issued_cards.sql", "202_reap_webhook_events.sql")
]

# Same convention as tests/test_reap_webhooks_postgres.py: this gate DROPS tables, so it must be
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
        "recommendation_id": None,
        "merchant_domain": "brand.example",
        "checkout_id": "chk_pg",
        "quote_total_minor": 2317,
        "amount_cap_minor": 2317,
        "currency": "USD",
        "issuer": "reap",
        "issuer_card_ref": f"ref_{uuid.uuid4().hex[:10]}",
        "status": "failed",
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


async def _status_of(card_id: str) -> str:
    from db.database import database

    r = await database.fetch_one(
        "SELECT status FROM agent_issued_cards WHERE card_id = :c", {"c": card_id}
    )
    return r["status"]


# --- the orphan transition, through the real UPDATE --------------------------------------------


async def test_mark_revoked_reports_TRUE_when_it_really_advanced_an_orphan():
    """The regression. Against a real orphan row this must answer True AND move the status.

    Both halves matter: the old form moved the status correctly and still answered False, so a
    test asserting only the status change would have passed against the defect.
    """
    from db.agent_issued_cards import mark_revoked

    card = await _insert_card(status="failed")

    assert await mark_revoked(card["card_id"]) is True
    assert await _status_of(card["card_id"]) == "revoked"


async def test_mark_revoked_reports_FALSE_on_a_live_issued_card_and_changes_nothing():
    """The guard that keeps a sweep bug from cancelling a card someone is using. The return
    value has to distinguish this from the success above — which is exactly what a constant
    False could not do, in the direction that made every success look like this."""
    from db.agent_issued_cards import mark_revoked

    card = await _insert_card(status="issued")

    assert await mark_revoked(card["card_id"]) is False
    assert await _status_of(card["card_id"]) == "issued"


@pytest.mark.parametrize("status", ["requested", "exhausted", "expired", "revoked"])
async def test_mark_revoked_advances_ONLY_from_failed(status):
    from db.agent_issued_cards import mark_revoked

    card = await _insert_card(status=status)

    assert await mark_revoked(card["card_id"]) is False
    assert await _status_of(card["card_id"]) == status


async def test_a_failed_row_with_NO_issuer_ref_is_not_an_orphan():
    """The second conjunct: `failed` alone is a mint that produced nothing. Advancing it to
    `revoked` would claim we killed a card that never existed."""
    from db.agent_issued_cards import mark_revoked

    card = await _insert_card(status="failed", issuer_card_ref=None)

    assert await mark_revoked(card["card_id"]) is False
    assert await _status_of(card["card_id"]) == "failed"


async def test_an_unknown_card_id_is_False_not_an_error():
    from db.agent_issued_cards import mark_revoked

    assert await mark_revoked("crd_does_not_exist") is False


# --- the sweep's queue, through the real SELECT -------------------------------------------------


async def test_list_orphaned_cards_finds_exactly_the_structural_orphans():
    """`failed` + a ref. The predicate is structural rather than a failure_reason code list, so
    this asserts a row with an UNKNOWN reason is still swept."""
    from db.agent_issued_cards import list_orphaned_cards

    orphan = await _insert_card(status="failed")
    await _insert_card(status="failed", issuer_card_ref=None)
    await _insert_card(status="issued")

    found = {r["card_id"] for r in await list_orphaned_cards(100)}
    assert found == {orphan["card_id"]}


async def test_a_swept_orphan_leaves_the_queue():
    """The end-to-end shape the sweep depends on: revoke, advance, and the next run must not see
    it again. With the old constant-False return the sweep never believed this happened."""
    from db.agent_issued_cards import list_orphaned_cards, mark_revoked

    card = await _insert_card(status="failed")
    assert len(await list_orphaned_cards(100)) == 1

    assert await mark_revoked(card["card_id"]) is True
    assert await list_orphaned_cards(100) == []
