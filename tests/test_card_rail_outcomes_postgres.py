"""Production-dialect gate for `card_rail_outcomes` (migration 199) and its upsert.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_card_rail_outcomes_postgres.py

WHY THIS MUST RUN ON REAL POSTGRES. Everything worth guarding here is server-side: four CHECK
constraints, `ON CONFLICT ... DO UPDATE`, `xmax = 0` as the inserted/updated discriminator, and
NUMERIC(18,4) rounding. A SQLite stand-in would accept all of it and prove nothing — the table is
the contract, and the route's validation only mirrors it.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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

_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/199_card_rail_outcomes.sql"


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    # Connect/disconnect PER TEST: each test runs on a fresh event loop and an asyncpg pool that
    # outlives its loop fails with "attached to a different loop".
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    # Apply the migration itself — the DDL under test, not a hand-written copy of it. A fixture
    # that re-declared the table would test the fixture.
    #
    # Split with the REPO's quote-aware splitter, not `text.split(";")`. A naive split cuts
    # through comment prose and string literals; the first version of this fixture did exactly
    # that and produced `syntax error at or near "\`"` from a fragment of a comment.
    from db.sql_migrations import split_statements

    # DROP FIRST. The migration is `CREATE TABLE IF NOT EXISTS`, so against a database where the
    # table already exists it is a no-op — and a CHECK constraint deleted from the file would
    # still be enforced by the surviving table. The gate would then pass while testing a schema
    # the repo no longer declares. (Found by a mutation run: removing
    # ck_card_rail_failure_has_reason from the migration left every test green.)
    await database.execute("DROP TABLE IF EXISTS card_rail_outcomes")
    for statement in split_statements(_MIGRATION.read_text()):
        await database.execute(statement)
    yield
    if not was_connected and database.is_connected:
        await database.disconnect()


def _vals(**over):
    base = {
        "recommendation_id": f"rec_pgtest_{uuid.uuid4().hex[:12]}",
        "recommendation_set_id": None,
        "trace_id": None,
        "click_id": None,
        "agent_id": "agent_pgtest",
        "merchant_domain": "brand.example",
        "product_key": None,
        "variant_id": None,
        "rail": "shopify_cart",
        "quoted_item_total": None,
        "quoted_grand_total": None,
        "quoted_currency": None,
        "quoted_at": None,
        "spec_expires_at": None,
        "actual_item_total": None,
        "actual_grand_total": None,
        "actual_currency": None,
        "outcome": "completed",
        "failure_reason": None,
        "failure_reason_raw": None,
        "latency_ms": json.dumps({}),
        "auth_outcome": None,
        "reported_by": "agent",
        "occurred_at": datetime.now(tz=timezone.utc),
    }
    base.update(over)
    return base


async def _row(rec_id):
    from db.database import database

    r = await database.fetch_one(
        "SELECT * FROM card_rail_outcomes WHERE recommendation_id = :i", {"i": rec_id}
    )
    return dict(r) if r else None


# --- the constraints are the contract ---------------------------------------------------------

async def test_an_unknown_outcome_is_refused_by_the_database_not_just_the_route():
    """The route validates too, but the table is what makes the metric countable. If only the
    route checked, any other writer — a backfill, a poller, a psql session — could put an
    uncountable value in the column the dashboards group by."""
    import asyncpg
    from db.card_rail_outcomes import record_outcome

    with pytest.raises((asyncpg.IntegrityConstraintViolationError, Exception)) as exc:
        await record_outcome(_vals(outcome="kinda_worked"))
    assert "ck_card_rail_outcome" in str(exc.value)


async def test_a_failure_must_say_why():
    """A `failed` row with no reason is the one row that teaches nothing — it consumes a
    recommendation_id and yields no signal. Both the typed and the raw column satisfy it, so an
    unrecognised reason is still an acceptable answer."""
    import asyncpg
    from db.card_rail_outcomes import record_outcome

    with pytest.raises(Exception) as exc:
        await record_outcome(_vals(outcome="failed"))
    assert "ck_card_rail_failure_has_reason" in str(exc.value)

    # Either column satisfies it.
    ok_typed = _vals(outcome="failed", failure_reason="out_of_stock")
    assert await record_outcome(ok_typed)
    ok_raw = _vals(outcome="failed", failure_reason_raw="captcha_wall")
    assert await record_outcome(ok_raw)


async def test_abandoned_is_exempt_from_needing_a_reason():
    """A buyer who walked away has no failure to name. Demanding one would push callers into
    inventing a reason, which is worse than an honest null."""
    from db.card_rail_outcomes import record_outcome

    v = _vals(outcome="abandoned")
    assert await record_outcome(v)
    row = await _row(v["recommendation_id"])
    assert row["outcome"] == "abandoned" and row["failure_reason"] is None


async def test_an_unknown_failure_reason_cannot_enter_the_TYPED_column():
    """The typed column's domain must stay closed or the metric cannot be trended. The raw column
    is where an unrecognised value belongs — see the route, which routes it there."""
    from db.card_rail_outcomes import record_outcome

    with pytest.raises(Exception) as exc:
        await record_outcome(_vals(outcome="failed", failure_reason="captcha_wall"))
    assert "ck_card_rail_failure_reason" in str(exc.value)


async def test_reported_by_is_constrained_so_evidence_kinds_stay_separable():
    """An agent's self-report and a poller's observation are different kinds of evidence. If the
    column accepted anything, they could not be told apart when averaging."""
    import asyncpg
    from db.card_rail_outcomes import record_outcome

    with pytest.raises(Exception) as exc:
        await record_outcome(_vals(reported_by="somebody"))
    assert "ck_card_rail_reported_by" in str(exc.value)


# --- upsert semantics -------------------------------------------------------------------------

async def test_a_re_report_CORRECTS_the_row_rather_than_appending():
    """One row per handoff is the grain. Appending would turn "what completed" into a windowing
    query and let one chatty agent outvote a quiet one."""
    from db.card_rail_outcomes import record_outcome
    from db.database import database

    rec = f"rec_pgtest_{uuid.uuid4().hex[:12]}"
    first = await record_outcome(_vals(
        recommendation_id=rec, outcome="failed", failure_reason="checkout_error"
    ))
    assert first["inserted"] is True, "the first report must be an INSERT"

    second = await record_outcome(_vals(recommendation_id=rec, outcome="completed"))
    assert second["inserted"] is False, "a re-report must be an UPDATE"

    n = await database.fetch_val(
        "SELECT count(*) FROM card_rail_outcomes WHERE recommendation_id = :i", {"i": rec}
    )
    assert n == 1, "a correction must not append a second row"

    row = await _row(rec)
    assert row["outcome"] == "completed"
    assert row["failure_reason"] is None, (
        "a row that recovers to completed must stop asserting the failure it recovered from"
    )


async def test_a_correction_does_not_wipe_the_fields_it_omits():
    """An agent reporting only the final outcome must not erase the quoted totals it reported
    earlier — those are half of the single most valuable number in this table."""
    from db.card_rail_outcomes import record_outcome

    rec = f"rec_pgtest_{uuid.uuid4().hex[:12]}"
    await record_outcome(_vals(
        recommendation_id=rec,
        outcome="abandoned",
        quoted_grand_total=Decimal("41.9900"),
        quoted_currency="USD",
        trace_id="trace-1",
        latency_ms=json.dumps({"recommend": 120.0}),
    ))
    await record_outcome(_vals(recommendation_id=rec, outcome="completed"))

    row = await _row(rec)
    assert row["quoted_grand_total"] == Decimal("41.9900"), "quoted total was wiped by a correction"
    assert row["quoted_currency"] == "USD"
    assert row["trace_id"] == "trace-1"
    # JSONB comes back as a STRING through this driver (databases 0.7 + asyncpg register no
    # json codec), so a reader that assumed a dict would silently compare against text. Parsed
    # here rather than papered over, because any consumer of this column has the same problem.
    assert json.loads(row["latency_ms"]) == {"recommend": 120.0}, (
        "an empty latency must not erase a real one"
    )


async def test_quoted_and_actual_are_both_kept_so_our_own_error_is_derivable():
    """The audit's finding is that 31.1% of index records would produce a wrong spec. Storing only
    one side of the comparison makes the size and DIRECTION of that error underivable forever."""
    from db.card_rail_outcomes import record_outcome

    rec = f"rec_pgtest_{uuid.uuid4().hex[:12]}"
    await record_outcome(_vals(
        recommendation_id=rec,
        outcome="aborted_on_mismatch",
        quoted_grand_total=Decimal("41.9900"),
        quoted_currency="USD",
        actual_grand_total=Decimal("48.5000"),
        actual_currency="USD",
    ))
    row = await _row(rec)
    assert row["quoted_grand_total"] == Decimal("41.9900")
    assert row["actual_grand_total"] == Decimal("48.5000")
    assert row["actual_grand_total"] - row["quoted_grand_total"] == Decimal("6.5100")


async def test_updated_at_moves_on_a_correction_so_a_late_report_is_visible():
    from db.card_rail_outcomes import record_outcome

    rec = f"rec_pgtest_{uuid.uuid4().hex[:12]}"
    await record_outcome(_vals(recommendation_id=rec, outcome="abandoned"))
    before = (await _row(rec))["updated_at"]
    await record_outcome(_vals(recommendation_id=rec, outcome="completed"))
    after = (await _row(rec))["updated_at"]
    assert after >= before
    assert (await _row(rec))["created_at"] <= after
