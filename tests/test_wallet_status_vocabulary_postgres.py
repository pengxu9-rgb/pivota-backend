"""Two admin endpoints accepted wallet statuses the database rejects.

    routes/admin_wallet_management.py
      update_merchant_wallet_status   accepted ("active", "suspended", "closed")
      update_agent_wallet_status      accepted ("active", "suspended", "closed")

    db/migrations/022_wallet_infrastructure.sql
      merchant_wallets.status  CHECK (status IN ('pending','active','inactive'))
      agent_wallets.status     CHECK (status IN ('pending','active','inactive'))

So TWO of the three documented values could never be written. The endpoint
validated the request, built a perfectly plannable UPDATE, and the database threw
a CheckViolationError.

WHY NO EXISTING GATE COULD SEE IT, which is the reason this file exists rather
than a line in the PREPARE sweep. `tests/test_repo_sql_prepare_postgres.py`
PREPAREs statements, and PREPARE is Parse+Describe: it validates TYPES, never
VALUES. `UPDATE merchant_wallets SET status = $1` plans perfectly whatever $1
turns out to be — the CHECK is not consulted until execution. This is the exact
limit that file documents, and a value defect is what falls through it.

WHICH SIDE TO CONVERGE ON. The database's — but this was a choice between two
valid fixes, not the only one available. Widening the CHECK to admit 'suspended'
is a shape the repo already uses (db/migrations/108_channel_partners.sql declares
a four-value status CHECK, mirrored by `_PARTNER_STATUSES`). Converging on the
database is the lower-risk half because nothing reads a suspended or closed
wallet, and this file already agreed with the database everywhere else:

  * the wallet-stats endpoint in this very file counts `active`, `pending` and
    `inactive`, so a "suspended" wallet would not have appeared in any of its
    three buckets even if the write had somehow succeeded;
  * services/wallet_service.py reads only `status = 'active'`;
  * no migration ever widens the CHECK;
  * neither word appears anywhere else in the WALLET lane (both are common
    elsewhere in the repo — hence the alternative above).

Mapping, for anyone who was relying on the old words. "suspended" is `inactive`,
and that one is exact: `status = 'active'` is the only read, so every non-active
value denies identically. "closed" is NOT exact — the nearest thing is the DELETE
endpoint on the same router, which removes the row instead of marking it
terminal, and `wallet_verification_logs.wallet_id` carries no foreign key, so its
audit rows survive as orphans. A durable closed state would want the CHECK
widened, not a DELETE.

NOT A LIVE OUTAGE, and this file should not imply one: `admin_wallet_management`
is imported by nothing — `main.py` never mounts it — so these endpoints are
unreachable today. The value of fixing it is that the router is one import away
from being live, and this defect is invisible to every static check the repo has.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_wallet_status_vocabulary_postgres.py

Never point this at prod — it writes.
"""

from __future__ import annotations

import os
import uuid
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

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN = uuid.uuid4().hex[:8]
MERCHANT = f"wsv_m_{RUN}"
AGENT = f"wsv_a_{RUN}"
MERCHANT_WALLET = f"wsv_mw_{RUN}"
AGENT_WALLET = f"wsv_aw_{RUN}"
# A second wallet per table that no test ever targets. Its only job is to stay
# where it was put: without it, every table holds exactly one row and a WHERE
# clause that matches EVERY row is indistinguishable from one that matches the
# right row. Found by mutation audit — `WHERE wallet_id = :wallet_id` could be
# replaced with `WHERE TRUE` and nothing went red.
MERCHANT_BYSTANDER = f"wsv_mwb_{RUN}"
AGENT_BYSTANDER = f"wsv_awb_{RUN}"

_ALL_STATUSES = ("active", "inactive", "pending")


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy.schema import CreateIndex, CreateTable
    from sqlalchemy.dialects import postgresql

    from db.database import database, metadata
    import db.agents  # noqa: F401  — registers `agents`
    import db.merchant_onboarding  # noqa: F401  — registers `merchant_onboarding`

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    # The wallet tables come from migration 022, whose foreign keys point at
    # merchant_onboarding(merchant_id) and agents(agent_id). Both parents are
    # built from the repo's own models — TABLE **and** INDEXES. The second half
    # is load-bearing and easy to miss: both parent keys are declared
    # `unique=True, index=True`, which SQLAlchemy emits as a separate
    # CREATE UNIQUE INDEX, so CreateTable alone leaves them non-unique and
    # migration 022 is rejected with "there is no unique constraint matching
    # given keys for referenced table".
    for name in ("merchant_onboarding", "agents"):
        table = metadata.tables[name]
        for statement in [CreateTable(table)] + [CreateIndex(i) for i in table.indexes]:
            try:
                await database.execute(str(statement.compile(dialect=postgresql.dialect())))
            except Exception:
                pass  # a sibling gate file built it already; same source, same shape

    migration = (
        REPO_ROOT / "db" / "migrations" / "022_wallet_infrastructure.sql"
    ).read_text(encoding="utf-8")
    for statement in migration.split(";"):
        if statement.strip():
            try:
                await database.execute(statement)
            except Exception:
                pass

    # The CHECK is the whole subject of this file, so its presence is asserted
    # rather than assumed — without it every test below would pass vacuously.
    for table in ("merchant_wallets", "agent_wallets"):
        row = await database.fetch_one(
            """
            SELECT pg_get_constraintdef(c.oid) AS def
            FROM pg_constraint c
            WHERE c.conrelid = CAST(:t AS regclass)
              AND c.contype = 'c'
              AND pg_get_constraintdef(c.oid) ILIKE '%status%'
            """,
            {"t": table},
        )
        assert row is not None, (
            f"{table} was built without its status CHECK — this fixture cannot "
            "test a constraint that is not there. Migration 022 failed to apply."
        )
        assert "'inactive'" in row["def"] and "'suspended'" not in row["def"], row["def"]

    try:
        yield database
    finally:
        await database.execute(
            "DELETE FROM merchant_wallets WHERE merchant_id = :m", {"m": MERCHANT}
        )
        await database.execute(
            "DELETE FROM agent_wallets WHERE agent_id = :a", {"a": AGENT}
        )
        await database.execute(
            "DELETE FROM merchant_onboarding WHERE merchant_id = :m", {"m": MERCHANT}
        )
        await database.execute("DELETE FROM agents WHERE agent_id = :a", {"a": AGENT})
        # Leave the handle as we found it: `databases` shares one connection
        # process-wide, so an unconditional disconnect breaks sibling gate files.
        if not was_connected:
            await database.disconnect()


async def _seed(initial: str = "pending", bystander: str = "active") -> None:
    from db.agents import agents
    from db.database import database
    from db.merchant_onboarding import merchant_onboarding

    await database.execute(
        merchant_onboarding.insert().values(
            merchant_id=MERCHANT,
            business_name="Wallet Status Fixture",
            contact_email="ops@example.com",
            status="approved",
            # Explicit: `databases` compiles the INSERT without running
            # SQLAlchemy's Python-side defaults, so a NOT NULL column carrying
            # only `default=False` would be sent as NULL.
            apm_enabled=False,
        )
    )
    await database.execute(
        agents.insert().values(
            agent_id=AGENT,
            agent_name="Wallet Status Fixture",
            agent_type="custom",
            api_key=f"wsv_key_{RUN}",
            api_key_hash=f"wsv_hash_{RUN}",
        )
    )
    # Distinct addresses: migration 022 declares UNIQUE(merchant_id, network,
    # address) on merchant_wallets and UNIQUE(agent_id, network, address) on
    # agent_wallets, so the bystander cannot reuse the target's address.
    for wallet_id, wallet_status, address in (
        (MERCHANT_WALLET, initial, "0xtarget"),
        (MERCHANT_BYSTANDER, bystander, "0xbystander"),
    ):
        await database.execute(
            """
            INSERT INTO merchant_wallets (wallet_id, merchant_id, network, address, status)
            VALUES (:w, :m, 'base', :addr, :s)
            """,
            {"w": wallet_id, "m": MERCHANT, "s": wallet_status, "addr": address},
        )
    for wallet_id, wallet_status, address in (
        (AGENT_WALLET, initial, "0xtarget"),
        (AGENT_BYSTANDER, bystander, "0xbystander"),
    ):
        await database.execute(
            """
            INSERT INTO agent_wallets (wallet_id, agent_id, network, address, status)
            VALUES (:w, :a, 'base', :addr, :s)
            """,
            {"w": wallet_id, "a": AGENT, "s": wallet_status, "addr": address},
        )


async def _status(table: str, wallet_id: str) -> str:
    from db.database import database

    row = await database.fetch_one(
        f"SELECT status FROM {table} WHERE wallet_id = :w", {"w": wallet_id}
    )
    return row["status"]


# ---------------------------------------------------------------------------
@pytest.mark.parametrize("new_status", _ALL_STATUSES)
async def test_every_accepted_status_can_actually_be_written(new_status):
    """The defect, at its bluntest.

    Before the fix `inactive` was REJECTED by the endpoint (400) while
    `suspended` and `closed` were accepted and then rejected by the database.
    Every value the endpoint accepts must survive the round trip.

    THE ROW MUST START SOMEWHERE ELSE. An earlier version seeded every wallet
    `pending` and then ran this with `new_status="pending"` — asserting the row
    reads `pending` after writing `pending` to a row that was already `pending`.
    A mutation audit proved it vacuous: disabling the UPDATE entirely left that
    parametrization green while the other two went red. Setup that seeds the
    state the code under test is supposed to produce tests nothing. Both the
    target and the bystander now start on values that differ from `new_status`
    and from each other, which is always possible with three statuses.
    """
    from routes.admin_wallet_management import (
        WalletStatusUpdate,
        update_agent_wallet_status,
        update_merchant_wallet_status,
    )

    initial, bystander = [s for s in _ALL_STATUSES if s != new_status]
    await _seed(initial=initial, bystander=bystander)

    body = await update_merchant_wallet_status(
        MERCHANT_WALLET, WalletStatusUpdate(status=new_status)
    )
    assert body["new_status"] == new_status
    assert await _status("merchant_wallets", MERCHANT_WALLET) == new_status

    body = await update_agent_wallet_status(
        AGENT_WALLET, WalletStatusUpdate(status=new_status)
    )
    assert body["new_status"] == new_status
    assert await _status("agent_wallets", AGENT_WALLET) == new_status

    # The UPDATE is scoped to ONE wallet. Both bystanders started on a value that
    # is not `new_status`, so a WHERE clause matching every row shows up here.
    assert await _status("merchant_wallets", MERCHANT_BYSTANDER) == bystander
    assert await _status("agent_wallets", AGENT_BYSTANDER) == bystander


@pytest.mark.parametrize("rejected", ["suspended", "closed"])
async def test_the_old_vocabulary_is_refused_before_it_reaches_the_database(rejected):
    """The other half, and the one that pins the fix.

    `suspended` and `closed` must now be refused with a 400. Asserting only that
    the valid values work would stay true if the validator were deleted outright
    — the write would simply fail deeper, with a 500 instead of a 400, and the
    row would be left alone either way. So this asserts the STATUS CODE and that
    the stored value did not move.
    """
    from fastapi import HTTPException

    from routes.admin_wallet_management import (
        WalletStatusUpdate,
        update_agent_wallet_status,
        update_merchant_wallet_status,
    )

    await _seed()

    for handler, wallet_id, table in (
        (update_merchant_wallet_status, MERCHANT_WALLET, "merchant_wallets"),
        (update_agent_wallet_status, AGENT_WALLET, "agent_wallets"),
    ):
        with pytest.raises(HTTPException) as caught:
            await handler(wallet_id, WalletStatusUpdate(status=rejected))
        assert caught.value.status_code == 400
        assert "inactive" in str(caught.value.detail), caught.value.detail
        assert await _status(table, wallet_id) == "pending", "the row was modified"


async def test_the_database_really_would_have_rejected_the_old_values():
    """The premise, asserted against the live CHECK rather than assumed.

    If the constraint ever widens to admit `suspended`, this fails and whoever
    widened it can decide whether the endpoint should accept it too — instead of
    the two drifting apart again silently.
    """
    import asyncpg

    from db.database import database

    await _seed()

    # The UPDATE must MATCH A ROW. An earlier version of this test used a
    # wallet_id that exists nowhere, so it updated zero rows, so the CHECK was
    # never evaluated and the test passed for the wrong reason — it would have
    # passed against a table with no constraint at all.
    for table, wallet_id in (
        ("merchant_wallets", MERCHANT_WALLET),
        ("agent_wallets", AGENT_WALLET),
    ):
        for rejected in ("suspended", "closed"):
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await database.execute(
                    f"UPDATE {table} SET status = :s WHERE wallet_id = :w",
                    {"s": rejected, "w": wallet_id},
                )
        # ...and the row is untouched, which also proves it was there to update.
        assert await _status(table, wallet_id) == "pending"


async def test_the_endpoint_vocabulary_matches_the_constraint_exactly():
    """Pins the two lists together, so neither can drift without the other.

    A test over the live constraint text rather than a second hardcoded list —
    a copy would just be a third place to get it wrong.
    """
    import re

    from db.database import database

    from routes.admin_wallet_management import _WALLET_STATUSES

    for table in ("merchant_wallets", "agent_wallets"):
        row = await database.fetch_one(
            """
            SELECT pg_get_constraintdef(c.oid) AS def
            FROM pg_constraint c
            WHERE c.conrelid = CAST(:t AS regclass)
              AND c.contype = 'c'
              AND pg_get_constraintdef(c.oid) ILIKE '%status%'
            """,
            {"t": table},
        )
        allowed = set(re.findall(r"'([a-z_]+)'", row["def"]))
        assert allowed == set(_WALLET_STATUSES), (
            f"{table} CHECK allows {sorted(allowed)} but the endpoints accept "
            f"{sorted(_WALLET_STATUSES)}"
        )
