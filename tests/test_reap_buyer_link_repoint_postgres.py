"""WP4c — revoke on repoint, against REAL Postgres.

Picked up automatically by .github/workflows/postgres-dialect-gate.yml via the
`tests/test_*_postgres.py` glob.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_reap_wp4c_test \\
        .venv/bin/python -m pytest tests/test_reap_buyer_link_repoint_postgres.py

THIS MODULE DOES NOT IMPORT `main`, AND THAT IS A GATE-WIDE RULE, NOT A PREFERENCE. `main` runs
`metadata.create_all` at import, which builds the MODEL's shape ahead of the migrations for every
test in the gate process, not only for this file — so one import here poisons unrelated suites.
The function under test, `routes/buyer_api._upsert_buyer_identity_link`, is module-level and is
called directly; nothing here needs an app.

WHAT POSTGRES PROVES THAT SQLITE CANNOT, on this path specifically:

  1. THE UPSERT TAKES ITS FIRST ARM. `uq_buyer_identity_links_agent_ref_hash` is a REAL unique
     index here, so `ON CONFLICT (agent_id, agent_user_ref_hash) DO UPDATE` is the statement that
     actually runs — the arm production takes. The SQLite arm exercises the same source, but a
     dialect that silently fell through to the fallback would look identical from the outside.

  2. THE STATEMENTS PLAN. Every SQL constant WP4c added is PREPAREd against the real schema
     below, which is where an ambiguous parameter type or a column that does not exist shows up.
     SQLite cannot see either.

  3. `DELETE … RETURNING`-FREE COUNTING IS ENGINE-INDEPENDENT. `databases` 0.7.0 gives no usable
     rowcount for `execute()` here, which is exactly why `retire_buyer_refs_for_buyer` counts
     what it read rather than what the driver reported. The shared idempotency case is the
     assertion; running it here is what makes it a statement about both engines.

  4. A SECOND BACKEND CONNECTION sees the retirement. The hook is not transactional by design —
     the shared-connection rule forbids `database.transaction()` — so "the rows are really gone"
     is asserted from a raw asyncpg connection that never saw this session's work.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

# Same convention as the rest of this rail's Postgres gates: this file DROPS tables, so it must
# be INCAPABLE of running anywhere but a throwaway — made true, not merely stated.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(marker in dbname or marker in DATABASE_URL for marker in _SAFE_DB_MARKERS):
        pytest.skip(
            f"refusing to drop the agentic rail's tables in database {dbname!r} — "
            f"throwaway only (e.g. pivota_reap_wp4c_test)"
        )


_assert_throwaway_database()

from reap_repoint_cases import *  # noqa: E402,F401,F403
from reap_repoint_cases import (  # noqa: E402
    AGENT,
    MINTED_BUYER,
    REAL_BUYER,
    REAP_REF,
    ref_hash,
    enrollment_row,
    seed_buyer_ref,
    seed_enrollment,
    seed_link,
    upsert,
)

import db.reap_agentic_ledger as ledger  # noqa: E402
import routes.buyer_api as buyer_api  # noqa: E402
from db.database import database  # noqa: E402


def _pg_url() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _raw_connection():
    import asyncpg

    return await asyncpg.connect(_pg_url())


async def test_the_upsert_takes_its_first_arm_here(retire_calls):
    """THE ARM PRODUCTION TAKES. `uq_buyer_identity_links_agent_ref_hash` is a real unique index
    on this engine, so the `ON CONFLICT … DO UPDATE` statement is what runs and what repoints —
    the fallback is never reached. Proved by making the fallback's own statements fatal: if
    anything below the first arm executes, this test raises rather than quietly passing on a
    path production does not use.
    """
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    seen = []
    real_execute = database.execute

    async def execute(query, *args, **kwargs):
        seen.append(str(query))
        return await real_execute(query, *args, **kwargs)

    database.execute = execute
    try:
        assert await upsert(REAL_BUYER) == ref_hash()
    finally:
        database.execute = real_execute

    assert any("ON CONFLICT (agent_id, agent_user_ref_hash)" in q for q in seen)
    # The fallback's UPDATE is a SQLAlchemy construct, not a string — its str() is the compiled
    # statement, and an UPDATE on this table appearing here would mean the first arm failed.
    assert not any(
        "UPDATE buyer_identity_links" in q.replace("\n", " ") for q in seen
    ), "the fallback arm ran on Postgres; the upsert did not take"

    assert retire_calls == [(MINTED_BUYER, "buyer_link_repointed")]
    assert dict(await enrollment_row(enrollment_id))["status"] == "dead"


async def test_a_second_connection_sees_the_retirement():
    """NOT TRANSACTIONAL, BY DESIGN, so the rows must really be gone rather than gone within one
    session's view. `retire_buyer_refs_for_buyer` runs ordered autocommit statements — no
    `database.transaction()`, because `databases` 0.7.0 shares a connection across child tasks
    and bypasses the query lock — and this is the assertion that turns that design note into a
    measurement. A raw asyncpg connection that never saw our work does the reading.
    """
    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    await upsert(REAL_BUYER)

    conn = await _raw_connection()
    try:
        refs = await conn.fetchval(
            "SELECT COUNT(*) FROM reap_agentic_buyer_refs WHERE buyer_id = $1", MINTED_BUYER
        )
        row = await conn.fetchrow(
            "SELECT status, reap_status, hosted_url FROM reap_agentic_enrollments WHERE id = $1",
            enrollment_id,
        )
        linked = await conn.fetchval(
            "SELECT buyer_id FROM buyer_identity_links "
            "WHERE agent_id = $1 AND agent_user_ref_hash = $2",
            AGENT,
            ref_hash(),
        )
    finally:
        await conn.close()

    assert refs == 0
    assert row["status"] == "dead"
    assert row["reap_status"] == "buyer_link_repointed"
    assert row["hosted_url"] is None
    assert linked == REAL_BUYER


async def test_every_wp4c_statement_prepares():
    """PREPARE, against the real schema. This is the check SQLite structurally cannot make: an
    ambiguous parameter type, a column that does not exist, a comparison Postgres refuses
    outright — none of them are visible until the planner sees the statement.

    The statements are read OFF THE MODULES, not copied here. A copy would keep passing after
    somebody edited the real one, which is the entire failure mode this guards.
    """
    import re

    bind_re = re.compile(r":([a-zA-Z_][a-zA-Z0-9_]*)")
    statements = {
        "ledger._SELECT_BUYER_REFS_FOR_BUYER_SQL": ledger._SELECT_BUYER_REFS_FOR_BUYER_SQL,
        "ledger._SELECT_LIVE_ENROLLMENTS_FOR_REF_SQL": (
            ledger._SELECT_LIVE_ENROLLMENTS_FOR_REF_SQL
        ),
        "ledger._DELETE_BUYER_REF_SQL": ledger._DELETE_BUYER_REF_SQL,
        "buyer_api._EXISTING_LINK_BUYER_ID_SQL": buyer_api._EXISTING_LINK_BUYER_ID_SQL,
        "buyer_api._ANY_LINK_FOR_BUYER_SQL": buyer_api._ANY_LINK_FOR_BUYER_SQL,
    }

    conn = await _raw_connection()
    try:
        for name, sql in statements.items():
            order = []

            def repl(match):
                if match.group(1) not in order:
                    order.append(match.group(1))
                return f"${order.index(match.group(1)) + 1}"

            positional = bind_re.sub(repl, sql)
            try:
                await conn.prepare(positional)
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"{name} does not plan: {type(exc).__name__}: {exc}")
    finally:
        await conn.close()


async def test_the_fallback_arm_is_unreachable_on_postgres(retire_calls):
    """A BASE PROPERTY, PINNED — not a defect WP4c introduced, and not one it fixes.

    `_upsert_buyer_identity_link`'s second write path is an update-then-insert for "an
    environment without ON CONFLICT". On THIS engine it cannot work, and the reason is the
    `databases` 0.7.0 rowcount rule this repo already has a note about: `await
    database.execute(<a SQLAlchemy UPDATE>)` returns **None** here, whatever it matched. Measured:
    the UPDATE lands and the row IS repointed, and the call still answers None.

    So `if int(updated or 0) > 0` is False for an update that succeeded, the function falls
    through to its INSERT, that INSERT violates `uq_buyer_identity_links_agent_ref_hash`, and the
    whole call returns None — for a repoint it actually performed.

    ── WHY THIS IS A TEST AND NOT A FIX ────────────────────────────────────────────────────

    Production takes the FIRST arm (the test above proves it), so this path is dead code on the
    engine that matters; changing it means changing a base function's return contract on a live
    buyer surface, which is not WP4c's scope. What is NOT acceptable is leaving it undescribed:
    the consequence for this rail is that on the one engine where the fallback could run, a
    repoint happens and the hook does NOT fire, because the hook is inside the guard that reads
    False. That is the brief's rule ("never fire when the upsert failed") applied faithfully to a
    predicate that is lying, and an operator reading the runbook should be able to find out why.

    The assertion is written so that FIXING the base makes this test fail loudly rather than
    silently pass, which is the point of pinning a defect rather than commenting on it.
    """
    real_execute = database.execute

    async def execute(query, *args, **kwargs):
        if "ON CONFLICT (agent_id, agent_user_ref_hash)" in str(query):
            raise RuntimeError("pretend this engine has no ON CONFLICT")
        return await real_execute(query, *args, **kwargs)

    await seed_link(agent_id=AGENT, ref_hash=ref_hash(), buyer_id=MINTED_BUYER)
    await seed_buyer_ref(buyer_id=MINTED_BUYER, reap_buyer_ref=REAP_REF)
    enrollment_id = await seed_enrollment(reap_buyer_ref=REAP_REF)

    database.execute = execute
    try:
        result = await upsert(REAL_BUYER)
    finally:
        database.execute = real_execute

    assert result is None, (
        "the fallback arm now reports its UPDATE on Postgres — the base defect this test pins "
        "has been fixed, and the WP4c hook should move out of the rowcount guard with it"
    )
    # The UPDATE really did land, which is what makes the None a defect rather than a refusal.
    assert await database.fetch_val(
        """
        SELECT buyer_id FROM buyer_identity_links
         WHERE agent_id = :a AND agent_user_ref_hash = :h
        """,
        {"a": AGENT, "h": ref_hash()},
    ) == REAL_BUYER
    # And therefore the hook did not fire: the repoint is invisible to the guard it sits behind.
    assert retire_calls == []
    assert dict(await enrollment_row(enrollment_id))["status"] == "active"
