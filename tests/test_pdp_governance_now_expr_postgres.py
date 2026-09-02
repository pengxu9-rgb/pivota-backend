"""`CURRENT_TIMESTAMP` is the one spelling both engines accept, so these
statements need no dialect split at all.

WHAT CHANGED. Eight DML statements in services/pdp_governance_service.py were
f-strings over a `_sql_now_expr()` helper that returned `"NOW()"` on Postgres and
`"CURRENT_TIMESTAMP"` on SQLite. An f-string is invisible to
tests/test_repo_sql_prepare_postgres.py, so none of the eight had ever been
PREPAREd. They are now plain literals reading `CURRENT_TIMESTAMP`, the helper is
deleted (it had no other caller), and the sweep collects them.

WHY NOT THE `if IS_POSTGRES:` SPLIT that db/product_quality_backfill_jobs.py
uses. That pattern is for a difference that is REAL — there, SQLite's CAST
resolves the unknown type name JSONB to NUMERIC affinity and silently stores 0,
so the two dialects genuinely need different text. Here there is no such
difference to preserve:

    Postgres   NOW() = CURRENT_TIMESTAMP  ->  true, both timestamptz
    SQLite     CURRENT_TIMESTAMP          ->  works
               NOW()                      ->  no such function

`CURRENT_TIMESTAMP` is simply the portable spelling of what both branches already
meant. Writing two module-level constants to say the same thing twice would be
worse than the f-string it replaced. Split only where the dialects actually
disagree.

Note also what this does NOT change: the SQLite branch already emitted
`CURRENT_TIMESTAMP`, so on SQLite these statements are byte-identical to before.
Only the Postgres text moves, and only between two spellings of one function.

WHAT IS DELIBERATELY LEFT ALONE, so it is not mistaken for an oversight. Eleven
f-string statements remain in that module and every one is genuinely dynamic:
five join a WHERE clause from a list (`' AND '.join(clauses)`), one interpolates
a computed `where`, and five build an optional `LIMIT` through
`_sql_limit_clause`, which returns `"LIMIT :limit"` or `""`.

That last group looks convertible — `LIMIT :limit` with None bound — and is not.
Measured on both engines:

    Postgres   SELECT ... LIMIT NULL   ->  unbounded, as wanted
    SQLite     ... LIMIT ?, (None,)    ->  IntegrityError: datatype mismatch

There is no single static form: SQLite reads a negative limit as unbounded and
Postgres rejects one. Making those static means splitting five large statements
in two, which buys five collected statements at the price of ten copies to keep
in step — a worse trade than leaving them, and stated here so the next person
does not re-derive it.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
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
MERCHANT = f"nowexpr_{RUN}"
GROUP = f"pg_nowexpr_{RUN}"


@pytest.fixture(autouse=True)
async def _db():
    from db.database import database

    # `product_group_members` is owned by migration 045, not by
    # `ensure_pdp_governance_tables()` (which builds the pdp_* tables and does
    # not touch this one). Applied from the migration file rather than restated
    # here, so the fixture cannot drift from the shape the statements write. The
    # file has no foreign keys and no dollar-quoted body, so splitting it on `;`
    # is safe.
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    migration = (
        REPO_ROOT / "db" / "migrations" / "045_product_groups.sql"
    ).read_text(encoding="utf-8")
    for statement in migration.split(";"):
        if statement.strip():
            try:
                await database.execute(statement)
            except Exception:
                # Already built by a sibling gate file in this process; same
                # source, so the shape is identical. Never DROP — shared DB.
                pass
    try:
        yield database
    finally:
        await database.execute(
            "DELETE FROM product_group_members WHERE merchant_id = :m", {"m": MERCHANT}
        )
        # Leave the handle as we found it. `databases` shares one connection
        # across the process, so an unconditional disconnect here breaks the
        # sibling gate files that ran before this one.
        if not was_connected:
            await database.disconnect()


async def test_the_two_spellings_really_are_one_function_on_postgres():
    """The premise the conversion rests on, asserted against the live server.

    If this ever stops holding, the eight statements changed alongside it are
    wrong and this test is where that shows up — rather than in a timestamp
    nobody looks at.
    """
    from db.database import database

    row = await database.fetch_one(
        "SELECT NOW() = CURRENT_TIMESTAMP AS identical, "
        "pg_typeof(NOW())::text AS now_type, "
        "pg_typeof(CURRENT_TIMESTAMP)::text AS current_type"
    )
    assert row["identical"] is True
    assert row["now_type"] == row["current_type"] == "timestamp with time zone"


async def test_the_upsert_stamps_updated_at_and_is_idempotent():
    """`_upsert_product_group_member` — the INSERT ... ON CONFLICT DO UPDATE arm.

    PREPARE would say this plans; it cannot say the row is written or that
    `updated_at` carries a real time. Both are asserted here, on the statement
    that ships.
    """
    from db.database import database
    from services.pdp_governance_service import _upsert_product_group_member

    await _upsert_product_group_member(GROUP, MERCHANT, "shopify", f"p-{RUN}")

    row = await database.fetch_one(
        """
        SELECT product_group_id, is_primary, updated_at
        FROM product_group_members
        WHERE merchant_id = :m AND platform = 'shopify' AND platform_product_id = :p
        """,
        {"m": MERCHANT, "p": f"p-{RUN}"},
    )
    assert row is not None, "the upsert wrote nothing"
    assert row["product_group_id"] == GROUP
    # The VALUE, not just presence. `is not None` is satisfied by any constant —
    # a mutation audit showed all eight converted statements could be pointed at
    # a hardcoded '2000-01-01' and every test here, plus the 37 existing
    # pdp_governance tests, stayed green. CURRENT_TIMESTAMP means transaction
    # start, so the stamp must sit within seconds of now.
    age = abs((datetime.now(timezone.utc) - row["updated_at"]).total_seconds())
    assert age < 120, f"updated_at is {age:.0f}s from now — not a live timestamp"

    # Same key again: the conflict arm, which carries its own CURRENT_TIMESTAMP.
    second_group = f"{GROUP}_moved"
    await _upsert_product_group_member(second_group, MERCHANT, "shopify", f"p-{RUN}")

    rows = await database.fetch_all(
        """
        SELECT product_group_id FROM product_group_members
        WHERE merchant_id = :m AND platform = 'shopify' AND platform_product_id = :p
        """,
        {"m": MERCHANT, "p": f"p-{RUN}"},
    )
    assert len(rows) == 1, "the conflict arm inserted a duplicate instead of updating"
    assert rows[0]["product_group_id"] == second_group


async def test_setting_a_primary_clears_the_others_and_stamps_both_statements():
    """`_set_product_group_primary` — two converted UPDATEs, and the second must
    not be swallowed by the first.

    Pinned with a member that has to END UP FALSE: asserting only that the chosen
    member is TRUE stays true if the clearing UPDATE is deleted outright.
    """
    from db.database import database
    from services.pdp_governance_service import (
        _set_product_group_primary,
        _upsert_product_group_member,
    )

    for suffix in ("a", "b"):
        await _upsert_product_group_member(GROUP, MERCHANT, "shopify", f"p{suffix}-{RUN}")
    await database.execute(
        """
        UPDATE product_group_members SET is_primary = TRUE
        WHERE merchant_id = :m AND platform_product_id = :p
        """,
        {"m": MERCHANT, "p": f"pa-{RUN}"},
    )

    await _set_product_group_primary(GROUP, f"{MERCHANT}|shopify|pb-{RUN}")

    rows = {
        row["platform_product_id"]: row
        for row in await database.fetch_all(
            """
            SELECT platform_product_id, is_primary, updated_at
            FROM product_group_members WHERE merchant_id = :m
            """,
            {"m": MERCHANT},
        )
    }
    assert rows[f"pb-{RUN}"]["is_primary"] is True
    assert rows[f"pa-{RUN}"]["is_primary"] is False, "the clearing UPDATE did not run"
    # Both converted UPDATEs stamp a LIVE timestamp — see the note above.
    for key, row in rows.items():
        age = abs((datetime.now(timezone.utc) - row["updated_at"]).total_seconds())
        assert age < 120, f"{key} updated_at is {age:.0f}s from now"
