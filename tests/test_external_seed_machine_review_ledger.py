"""The ledger uses the caller's transaction and never changes a recorded decision."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import os
import pytest

from services.external_seed_machine_review_ledger import (
    MachineReviewReplayConflict,
    append_machine_review,
    load_machine_review,
)


def _review(**changes):
    return {
        "request_id": "request-one",
        "seed_id": "eps_test",
        "review_version": "a" * 64,
        "policy_version": "pilot-v1",
        "evidence_digest": "b" * 64,
        "reviewer_identity": "pilot-test@example.com",
        "outcome": "hold",
        "decision": {
            "inputs": {"date": date(2026, 11, 13), "checked_at": datetime(2026, 11, 13, 8, tzinfo=timezone(timedelta(hours=8)))},
            "votes": [{"model": "first", "vote": "hold"}, {"model": "second", "vote": "hold"}],
        },
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": ""},
        {"seed_id": "a" * 129},
        {"reviewer_identity": " reviewer"},
        {"review_version": "A" * 64},
        {"evidence_digest": "not-a-digest"},
        {"outcome": "rejected"},
        {"outcome": "activated"},
        {"active_review_version": "c" * 64},
        {"decision": []},
        {"decision": {"value": object()}},
        {"decision": {"value": float("nan")}},
        {"decision": {"value": datetime(2026, 11, 13)}},
        {"decision": {"value": {1: "a"}}},
    ],
)
async def test_invalid_reviews_never_reach_the_database(changes):
    db = AsyncMock()
    with pytest.raises(ValueError):
        await append_machine_review(db, **_review(**changes))
    db.fetch_one.assert_not_awaited()


@pytest.fixture
async def ledger_db():
    url = os.getenv("PIVOTA_PG_TEST_URL") or os.getenv("DATABASE_URL", "")
    endpoint = urlsplit(url)
    if endpoint.scheme not in ("postgres", "postgresql", "postgresql+asyncpg") or endpoint.hostname not in (
        "localhost", "127.0.0.1", "::1",
    ):
        pytest.skip("requires a local Postgres test database")

    from databases import Database
    from db.sql_migrations import split_statements

    db = Database(url)
    migration = Path(__file__).resolve().parents[1] / "db/migrations/285_external_seed_machine_reviews.sql"
    async with db:
        # An isolated temporary schema on this connection keeps the migration away from existing tables.
        async with db.transaction(force_rollback=True):
            schema = "test_machine_review_" + uuid4().hex
            await db.execute(f"CREATE SCHEMA {schema}")
            await db.execute(f"SET LOCAL search_path = {schema}")
            for statement in split_statements(migration.read_text(encoding="utf-8")):
                await db.execute(statement)
            yield db


async def test_exact_replay_returns_the_original_normalized_receipt(ledger_db):
    review = _review()
    assert await load_machine_review(ledger_db, review["request_id"]) is None
    original = await append_machine_review(ledger_db, **review)
    assert original["decision"]["inputs"] == {"checked_at": "2026-11-13T00:00:00+00:00", "date": "2026-11-13"}
    assert datetime.fromisoformat(original["created_at"]).utcoffset() == timedelta(0)
    assert original["decision"]["votes"] == review["decision"]["votes"]
    assert await append_machine_review(ledger_db, **review) == original
    assert await load_machine_review(ledger_db, review["request_id"]) == original

    with pytest.raises(MachineReviewReplayConflict):
        await append_machine_review(ledger_db, **_review(decision={"votes": []}))
    assert await load_machine_review(ledger_db, review["request_id"]) == original


@pytest.mark.parametrize("statement", ["UPDATE external_seed_machine_reviews SET outcome='hold'",
                                       "DELETE FROM external_seed_machine_reviews",
                                       "TRUNCATE external_seed_machine_reviews"])
async def test_migration_blocks_changes_even_outside_the_service(ledger_db, statement):
    from asyncpg import RaiseError

    original = await append_machine_review(ledger_db, **_review())
    with pytest.raises(RaiseError, match="append-only"):
        async with ledger_db.transaction():
            await ledger_db.execute(statement)
    assert await load_machine_review(ledger_db, original["request_id"]) == original


async def test_conflicting_replay_rolls_back_the_callers_activation(ledger_db):
    await ledger_db.execute("CREATE TABLE test_seed_state (status TEXT NOT NULL)")
    await ledger_db.execute("INSERT INTO test_seed_state VALUES ('draft')")
    await append_machine_review(ledger_db, **_review())

    with pytest.raises(MachineReviewReplayConflict):
        async with ledger_db.transaction():
            await ledger_db.execute("UPDATE test_seed_state SET status = 'active'")
            await append_machine_review(ledger_db, **_review(outcome="activated", active_review_version="c" * 64))
    assert (await ledger_db.fetch_one("SELECT status FROM test_seed_state"))["status"] == "draft"

    activated = _review(request_id="request-two", outcome="activated", active_review_version="c" * 64)
    async with ledger_db.transaction():
        await ledger_db.execute("UPDATE test_seed_state SET status = 'active'")
        receipt = await append_machine_review(ledger_db, **activated)
    assert (await ledger_db.fetch_one("SELECT status FROM test_seed_state"))["status"] == "active"
    assert receipt["active_review_version"] == "c" * 64
    assert await load_machine_review(ledger_db, "request-two") == receipt
