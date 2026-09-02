"""Six admin wallet endpoints reported success for a row that does not exist.

    routes/admin_wallet_management.py
      verify_merchant_wallet         -> {"status": "verified"}
      verify_agent_wallet            -> {"status": "verified"}
      update_merchant_wallet_status  -> {"status": "updated", "new_status": ...}
      update_agent_wallet_status     -> {"status": "updated", "new_status": ...}
      delete_merchant_wallet         -> {"status": "deleted"}
      delete_agent_wallet            -> {"status": "deleted"}

Each ran a single `database.execute` of an UPDATE or DELETE with no RETURNING
clause and then returned its success shape unconditionally. Call any of them with
a wallet_id that exists nowhere and you got a 200 describing work that never
happened. The verify pair is the worst of the six, because "verified" is an
assertion about a wallet's provenance rather than a state change: a caller acting
on it believes an address was checked.

WHY THE CODE COULD NOT HAVE CHECKED, which is the reason RETURNING is the fix
rather than a rowcount. `databases` 0.7.0 on asyncpg implements `execute` as
`fetchval`, so an UPDATE or DELETE without RETURNING yields None whether it moved
one row or none. Measured on PG 15, and asserted below by
`test_execute_without_returning_cannot_tell_a_hit_from_a_miss` so that nobody
"simplifies" the fix back into a shape that cannot work:

    UPDATE ... WHERE <matches a row>     -> None
    UPDATE ... WHERE <matches nothing>   -> None
    DELETE ... WHERE <matches a row>     -> None
    DELETE ... WHERE <matches nothing>   -> None
    UPDATE ... RETURNING wallet_id, via fetch_one, hit  -> Record
    UPDATE ... RETURNING wallet_id, via fetch_one, miss -> None

So there is no rowcount to consult here at all. `RETURNING wallet_id` read back
with `fetch_one` is the repo's existing answer — db/product_quality_backfill_jobs.py
uses exactly this to learn whether a row was touched, and says so in a comment
about the same asyncpg behaviour.

WHY NO EXISTING GATE COULD SEE IT. `tests/test_repo_sql_prepare_postgres.py`
PREPAREs statements, and PREPARE is Parse+Describe: it validates TYPES, never
execution. `UPDATE merchant_wallets SET status = $1 WHERE wallet_id = $2` plans
perfectly against an empty table, and "how many rows did that move" is not a
question a plan can answer. This is the same limit that let the status-vocabulary
defect through in #1995 — see tests/test_wallet_status_vocabulary_postgres.py,
this file's sibling — and it is why both files EXECUTE the handlers.

NOT A LIVE OUTAGE, and this file should not imply one: `admin_wallet_management`
is imported by nothing — `main.py` never mounts it — so these endpoints are
unreachable today. That bounds the urgency and not the correctness; the router is
one import away from being live.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_wallet_admin_missing_row_postgres.py

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
MERCHANT = f"wmr_m_{RUN}"
AGENT = f"wmr_a_{RUN}"
MERCHANT_WALLET = f"wmr_mw_{RUN}"
AGENT_WALLET = f"wmr_aw_{RUN}"
# A second wallet per table that no test ever targets, for the same reason the
# sibling file keeps one: with a single row per table, a WHERE clause matching
# EVERY row is indistinguishable from one matching the right row. It earns its
# keep twice over here, because the DELETE handlers are under test — an
# unscoped DELETE would empty the table and every other assertion would still
# pass.
MERCHANT_BYSTANDER = f"wmr_mwb_{RUN}"
AGENT_BYSTANDER = f"wmr_awb_{RUN}"

# A wallet_id that exists in neither table. This is the whole input to half the
# tests below, so it is built from the same per-run nonce as everything else:
# a hardcoded "does_not_exist" would collide with a concurrent run of this file.
MISSING = f"wmr_missing_{RUN}"


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

    # Both tables must exist before a single test runs. Every 404 assertion below
    # is "the handler raised HTTPException(404)", and a handler whose UPDATE hits
    # a MISSING TABLE raises too — a different exception, but a fixture that
    # silently built nothing would still leave the suite looking meaningful. The
    # migration loop above swallows every error by design, so this is the only
    # place that failure can surface.
    for table in ("merchant_wallets", "agent_wallets"):
        exists = await database.fetch_one(
            "SELECT to_regclass(:t) AS oid", {"t": table}
        )
        assert exists["oid"] is not None, (
            f"{table} was not created — migration 022 failed to apply, and every "
            "test in this file would pass for the wrong reason."
        )

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


async def _seed(status: str = "pending") -> None:
    """Two wallets per table: the target and an untouched bystander.

    Both start `verified_at IS NULL`, which is what makes the verify assertions
    mean something — a row seeded already-verified would read as verified after a
    handler that did nothing at all.
    """
    from db.agents import agents
    from db.database import database
    from db.merchant_onboarding import merchant_onboarding

    await database.execute(
        merchant_onboarding.insert().values(
            merchant_id=MERCHANT,
            business_name="Wallet Missing-Row Fixture",
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
            agent_name="Wallet Missing-Row Fixture",
            agent_type="custom",
            api_key=f"wmr_key_{RUN}",
            api_key_hash=f"wmr_hash_{RUN}",
        )
    )
    # Distinct addresses: migration 022 declares UNIQUE(merchant_id, network,
    # address) and UNIQUE(agent_id, network, address), so the bystander cannot
    # reuse the target's address.
    for wallet_id, address in ((MERCHANT_WALLET, "0xtarget"), (MERCHANT_BYSTANDER, "0xbystander")):
        await database.execute(
            """
            INSERT INTO merchant_wallets (wallet_id, merchant_id, network, address, status)
            VALUES (:w, :m, 'base', :addr, :s)
            """,
            {"w": wallet_id, "m": MERCHANT, "s": status, "addr": address},
        )
    for wallet_id, address in ((AGENT_WALLET, "0xtarget"), (AGENT_BYSTANDER, "0xbystander")):
        await database.execute(
            """
            INSERT INTO agent_wallets (wallet_id, agent_id, network, address, status)
            VALUES (:w, :a, 'base', :addr, :s)
            """,
            {"w": wallet_id, "a": AGENT, "s": status, "addr": address},
        )


async def _row(table: str, wallet_id: str):
    from db.database import database

    return await database.fetch_one(
        f"SELECT wallet_id, status, verified_at FROM {table} WHERE wallet_id = :w",
        {"w": wallet_id},
    )


async def _count(table: str, owner_column: str, owner: str) -> int:
    from db.database import database

    row = await database.fetch_one(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {owner_column} = :o", {"o": owner}
    )
    return int(row["n"])


def _handlers():
    """Every handler under test, as (label, callable taking one wallet_id).

    The status pair takes a body as well, so it is bound here rather than at each
    call site — `inactive` is deliberately NOT the seeded value, so a status
    assertion cannot pass on a row that was never touched.
    """
    from routes.admin_wallet_management import (
        WalletStatusUpdate,
        delete_agent_wallet,
        delete_merchant_wallet,
        update_agent_wallet_status,
        update_merchant_wallet_status,
        verify_agent_wallet,
        verify_merchant_wallet,
    )

    return [
        ("verify_merchant_wallet", verify_merchant_wallet),
        ("verify_agent_wallet", verify_agent_wallet),
        (
            "update_merchant_wallet_status",
            lambda w: update_merchant_wallet_status(w, WalletStatusUpdate(status="inactive")),
        ),
        (
            "update_agent_wallet_status",
            lambda w: update_agent_wallet_status(w, WalletStatusUpdate(status="inactive")),
        ),
        ("delete_merchant_wallet", delete_merchant_wallet),
        ("delete_agent_wallet", delete_agent_wallet),
    ]


# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label", [h[0] for h in _handlers()])
async def test_a_missing_wallet_is_a_404_not_a_success(label):
    """The defect, at its bluntest: a wallet_id that exists nowhere.

    Before the fix every one of these returned its 200 success shape. The
    tables are deliberately NOT empty — `_seed` puts four real wallets in place
    — so a handler cannot pass this by failing on an empty table, and the
    bystander check below proves the 404 was not bought by refusing everything.
    """
    from fastapi import HTTPException

    await _seed()
    handler = dict(_handlers())[label]

    with pytest.raises(HTTPException) as caught:
        await handler(MISSING)
    assert caught.value.status_code == 404, caught.value.detail

    # A 404 that also destroyed something would satisfy the line above. Nothing
    # in either table may have moved: same row count, same statuses, still
    # unverified.
    assert await _count("merchant_wallets", "merchant_id", MERCHANT) == 2
    assert await _count("agent_wallets", "agent_id", AGENT) == 2
    for table, wallet_id in (
        ("merchant_wallets", MERCHANT_WALLET),
        ("merchant_wallets", MERCHANT_BYSTANDER),
        ("agent_wallets", AGENT_WALLET),
        ("agent_wallets", AGENT_BYSTANDER),
    ):
        row = await _row(table, wallet_id)
        assert row["status"] == "pending"
        assert row["verified_at"] is None


async def test_verify_still_verifies_a_wallet_that_exists():
    """The positive counterpart, without which a 404 could be unconditional.

    A handler that raised 404 for EVERY input would pass the test above. This
    asserts the success path reaches the database: status moves `pending` ->
    `active` and `verified_at` stops being NULL.
    """
    from routes.admin_wallet_management import verify_agent_wallet, verify_merchant_wallet

    await _seed()

    for handler, table, wallet_id, bystander in (
        (verify_merchant_wallet, "merchant_wallets", MERCHANT_WALLET, MERCHANT_BYSTANDER),
        (verify_agent_wallet, "agent_wallets", AGENT_WALLET, AGENT_BYSTANDER),
    ):
        body = await handler(wallet_id)
        assert body["status"] == "verified"
        assert body["wallet_id"] == wallet_id

        row = await _row(table, wallet_id)
        assert row["status"] == "active"
        assert row["verified_at"] is not None

        # Scoped to ONE wallet: the bystander is still pending and unverified.
        other = await _row(table, bystander)
        assert other["status"] == "pending"
        assert other["verified_at"] is None


async def test_status_update_still_updates_a_wallet_that_exists():
    """As above, for the status pair. Seeded `pending`, written `inactive`."""
    from routes.admin_wallet_management import (
        WalletStatusUpdate,
        update_agent_wallet_status,
        update_merchant_wallet_status,
    )

    await _seed()

    for handler, table, wallet_id, bystander in (
        (update_merchant_wallet_status, "merchant_wallets", MERCHANT_WALLET, MERCHANT_BYSTANDER),
        (update_agent_wallet_status, "agent_wallets", AGENT_WALLET, AGENT_BYSTANDER),
    ):
        body = await handler(wallet_id, WalletStatusUpdate(status="inactive"))
        assert body["status"] == "updated"
        assert body["new_status"] == "inactive"

        assert (await _row(table, wallet_id))["status"] == "inactive"
        assert (await _row(table, bystander))["status"] == "pending"


async def test_delete_still_deletes_a_wallet_that_exists_and_only_that_one():
    """The delete pair, and the sharpest statement of the whole fix.

    Deleting twice is the assertion that matters: the first call must succeed
    and the second must 404. A handler that always returned success passes
    neither half, and one that always raised 404 fails the first. It also proves
    the 404 above is about the ROW rather than about the request shape, because
    the same wallet_id produces both answers depending only on whether the row
    is there.
    """
    from fastapi import HTTPException

    from routes.admin_wallet_management import delete_agent_wallet, delete_merchant_wallet

    await _seed()

    for handler, table, owner_column, owner, wallet_id, bystander in (
        (
            delete_merchant_wallet,
            "merchant_wallets",
            "merchant_id",
            MERCHANT,
            MERCHANT_WALLET,
            MERCHANT_BYSTANDER,
        ),
        (delete_agent_wallet, "agent_wallets", "agent_id", AGENT, AGENT_WALLET, AGENT_BYSTANDER),
    ):
        body = await handler(wallet_id)
        assert body["status"] == "deleted"
        assert body["wallet_id"] == wallet_id

        assert await _row(table, wallet_id) is None, "the row survived a reported delete"
        # The bystander survives — an unscoped DELETE would have taken it too,
        # and every other assertion in this test would still have passed.
        assert await _row(table, bystander) is not None
        assert await _count(table, owner_column, owner) == 1

        with pytest.raises(HTTPException) as caught:
            await handler(wallet_id)
        assert caught.value.status_code == 404, caught.value.detail
        # Still exactly one row: the failed second delete took nothing with it.
        assert await _count(table, owner_column, owner) == 1


async def test_execute_without_returning_cannot_tell_a_hit_from_a_miss():
    """The premise of the fix, asserted rather than assumed.

    `databases` 0.7.0 on asyncpg implements `execute` as `fetchval`, so an
    UPDATE or DELETE with no RETURNING clause returns None no matter how many
    rows it moved. That is why these handlers could not simply have checked a
    rowcount, and why the fix is RETURNING read back through `fetch_one`.

    If a future `databases` upgrade starts returning a usable rowcount, this
    fails and whoever upgraded can decide whether the simpler shape is now
    available — instead of the comment in the router quietly going stale.
    """
    from db.database import database

    await _seed()

    hit = await database.execute(
        "UPDATE merchant_wallets SET status = 'active' WHERE wallet_id = :w",
        {"w": MERCHANT_WALLET},
    )
    miss = await database.execute(
        "UPDATE merchant_wallets SET status = 'active' WHERE wallet_id = :w",
        {"w": MISSING},
    )
    assert hit is None and miss is None, (
        f"execute now distinguishes a hit ({hit!r}) from a miss ({miss!r}) — "
        "see the docstring"
    )
    # ...and the hit really did land, so the None above is not because both
    # statements were no-ops.
    assert (await _row("merchant_wallets", MERCHANT_WALLET))["status"] == "active"

    # The shape the handlers use instead, on the same two inputs.
    hit_row = await database.fetch_one(
        "UPDATE merchant_wallets SET status = 'inactive' WHERE wallet_id = :w RETURNING wallet_id",
        {"w": MERCHANT_WALLET},
    )
    miss_row = await database.fetch_one(
        "UPDATE merchant_wallets SET status = 'inactive' WHERE wallet_id = :w RETURNING wallet_id",
        {"w": MISSING},
    )
    assert hit_row is not None and hit_row["wallet_id"] == MERCHANT_WALLET
    assert miss_row is None
