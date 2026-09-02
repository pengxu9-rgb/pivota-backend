"""Production-dialect gate: the psp_id merchant self-service mints must be one
`orders` will accept.

THE DEFECT THIS CLOSES. `orders` has carried CHECK check_psp_id_format since
db/migrations/006_psp_fields_constraints.sql:

    CHECK (psp_id IS NULL OR psp_id ~* '^psp_[a-z0-9]+_[a-z0-9]{12}$')

-- exactly TWELVE trailing characters. The canonical generator honours it
(services/merchant_psp_config_service._generate_psp_id). But
`POST /merchant/onboarding/setup-psp`, the self-service onboarding route that
allows role `merchant`, minted its own id inline as
`f"psp_{psp_type}_{uuid.uuid4().hex[:8]}"` -- EIGHT characters. `merchant_psps`
had no matching constraint, so the bad id was written, the PSP validated, the
portal showed "connected", and NOTHING failed until the merchant's first sale:
order creation copies merchant_psps.psp_id straight into orders.psp_id
(routes/order_routes._resolve_active_order_psp -> db.orders.create_order), where
Postgres rejected it. Reproduced in production on 2026-08-29 for
merch_c5e24a8d3738d73b: every checkout 500'd with

    new row for relation "orders" violates check constraint "check_psp_id_format"
    DETAIL: Failing row contains (ORD_103E0AE75A4A26F4, merch_c5e24a8d3738d73b,
            store_merch_c5_1787964361, psp_stripe_30cc4106, ...)

WHY THIS FILE IS A POSTGRES GATE AND NOT A UNIT TEST. The rule is a database
CHECK constraint. A fake connection, a recorded-SQL double, or SQLite (which has
no `~*` operator) all accept `psp_stripe_30cc4106` happily -- the defect is
invisible to every engine except the one production runs. So this file EXECUTES
the real route handler and then a real `create_order()` INSERT against Postgres.
tests/test_setup_psp_mints_canonical_psp_id.py carries the fast, engine-free half
(the route delegates to the canonical generator); it cannot see the constraint.

MUTANTS THESE KILL:
* restore the inline `uuid.uuid4().hex[:8]` in routes/employee_store_psp_fixes.py
  -> test_self_service_onboarding_mints_an_id_orders_accepts fails at the real
     INSERT, with the production error verbatim.
* widen `{12}` to `{8}` or `+` in _generate_psp_id
  -> the same test fails on the regex assertion pinned to migration 006's text.
* delete migration 207's constraint
  -> test_merchant_psps_rejects_a_malformed_id_at_write_time fails.
* let the fixture stand in for the constraint (build the tables but skip the
  migrations) -> test_both_constraints_are_installed fails, and every rejection
  assertion checks the CONSTRAINT NAME, so a row rejected for any other reason
  (a too-narrow column, a NOT NULL) reports as a fixture problem rather than
  passing as proof.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` for tables the
`db.*` modules own (`orders`, `merchant_onboarding`); main.py's OWN
`CREATE TABLE IF NOT EXISTS` literal, lifted from its AST, for `merchant_psps`,
which application bootstrap owns rather than a migration -- never a hand-copied
stub, which drifts from the real schema silently. Rows are prefixed and DELETEd.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_psp_id_format_constraint_postgres.py

Never point this at prod.
"""

from __future__ import annotations

import ast
import os
import re
import uuid
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "SQLite has no `~*` operator, so it cannot enforce the CHECK under test"
    ),
)

_MERCHANT_ID = "merch_pspfmt_gate"
_ROW_PREFIX = "pspfmt_"

# The ONE rule, in the ONE place production states it. Asserted below to be
# present verbatim in BOTH migration files, so this literal cannot drift away
# from the constraint it claims to describe.
PSP_ID_FORMAT_REGEX = r"^psp_[a-z0-9]+_[a-z0-9]{12}$"

ORDERS_CONSTRAINT = "check_psp_id_format"
MERCHANT_PSPS_CONSTRAINT = "check_merchant_psps_psp_id_format"


def _migration(name: str) -> str:
    path = REPO_ROOT / "db" / "migrations" / name
    assert path.exists(), f"{name} is gone — this gate describes a rule that no longer has a source"
    return path.read_text(encoding="utf-8")


def _startup_merchant_psps_ddl() -> str:
    """main.py's own `CREATE TABLE IF NOT EXISTS merchant_psps` literal.

    Lifted from main.py's AST, not copied: `merchant_psps` is created by
    application bootstrap rather than by a migration or by `metadata`, and a
    hand-copied stub drifts from the real column set and types silently. Same
    technique as `_startup_ddl` in tests/test_repo_sql_prepare_postgres.py.
    """
    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"), filename="main.py")
    found: List[str] = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "CREATE TABLE IF NOT EXISTS merchant_psps" in node.value
    ]
    assert len(found) == 1, (
        "expected exactly one merchant_psps CREATE TABLE literal in main.py, found "
        f"{len(found)} — the fixture can no longer build the real table"
    )
    return found[0]


def _constraint_statement(body: str, constraint_name: str) -> str:
    """The real ADD CONSTRAINT statement, taken from the real migration file.

    The constraint is the thing under test, so it is never retyped here. If the
    migration stops containing it, this raises rather than quietly testing a
    rule the test itself invented.
    """
    from db.sql_migrations import split_statements

    for statement in split_statements(body):
        if constraint_name in statement and "ADD CONSTRAINT" in statement.upper():
            return statement
    raise AssertionError(f"no ADD CONSTRAINT {constraint_name} statement in the migration")


@pytest.fixture(autouse=True)
async def _db():
    from sqlalchemy import create_engine

    from db.database import database, metadata
    import db.merchant_onboarding  # noqa: F401  (registers merchant_onboarding)
    import db.orders  # noqa: F401  (registers orders)

    engine = create_engine(DATABASE_URL)
    # Tables db.* modules own. Never hand-rolled — see the header warning.
    metadata.create_all(engine, checkfirst=True)
    engine.dispose()

    # Connect/disconnect PER TEST: the suite runs each test on a fresh event loop
    # (asyncio_default_fixture_loop_scope=function), and an asyncpg pool that
    # outlives its loop fails with "attached to a different loop".
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()

    await database.execute(_startup_merchant_psps_ddl())

    # The REAL constraints, from the REAL migration files. Both are idempotent:
    # 006's is a bare ADD, so drop first; 207's carries its own IF NOT EXISTS.
    await database.execute(
        f"ALTER TABLE orders DROP CONSTRAINT IF EXISTS {ORDERS_CONSTRAINT}"
    )
    await database.execute(
        _constraint_statement(_migration("006_psp_fields_constraints.sql"), ORDERS_CONSTRAINT)
    )
    await database.execute(_migration("207_merchant_psps_psp_id_format.sql"))

    await _cleanup()
    await database.execute(
        "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email)"
        " VALUES (:merchant_id, 'PSP format gate', 'pspfmt-gate@example.invalid')"
        " ON CONFLICT (merchant_id) DO NOTHING",
        {"merchant_id": _MERCHANT_ID},
    )
    try:
        yield
    finally:
        await _cleanup()
        # 207's constraint is dropped again on the way out. These gate files share
        # ONE database and siblings insert merchant_psps rows with short, made-up
        # ids (tests/test_acp_checkout_sessions_postgres.py); leaving a live
        # constraint behind would make this file's result depend on collection
        # ORDER, which is exactly the free-riding fixture the header warns about.
        await database.execute(
            f"ALTER TABLE merchant_psps DROP CONSTRAINT IF EXISTS {MERCHANT_PSPS_CONSTRAINT}"
        )
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _cleanup() -> None:
    from db.database import database

    await database.execute("DELETE FROM orders WHERE merchant_id = :m", {"m": _MERCHANT_ID})
    await database.execute("DELETE FROM merchant_psps WHERE merchant_id = :m", {"m": _MERCHANT_ID})
    await database.execute(
        "DELETE FROM merchant_onboarding WHERE merchant_id = :m", {"m": _MERCHANT_ID}
    )


def _order_values(psp_id: str) -> dict:
    """The PSP-bearing subset of what routes/order_routes.py hands create_order."""
    return {
        "merchant_id": _MERCHANT_ID,
        "customer_email": "buyer@example.invalid",
        "shipping_address": {"line1": "1 Test St", "country": "US"},
        "items": [{"sku": "sku-1", "quantity": 1, "price": 10.0}],
        "subtotal": 10.0,
        "total": 10.0,
        "currency": "USD",
        "psp_used": "stripe",
        "psp_id": psp_id,
    }


async def _run_self_service_onboarding() -> str:
    """Drive the REAL self-service endpoint and return the psp_id it minted."""
    from routes.employee_store_psp_fixes import ConnectPSPRequest, setup_merchant_psp

    response = await setup_merchant_psp(
        ConnectPSPRequest(
            merchant_id=_MERCHANT_ID,
            psp_type="stripe",
            api_key="sk_test_" + "a" * 24,
            test_mode=True,
        ),
        # The route's own self-service case: a signed-in merchant, not an employee.
        current_user={"role": "merchant", "merchant_id": _MERCHANT_ID},
    )
    assert response["status"] == "success", response
    return response["psp_id"]


def test_the_two_migrations_state_the_same_rule() -> None:
    # Writer and reader must agree, and this file's literal must be theirs. If
    # any of the three drifts, the constraint that fires in production is not the
    # one the tests below assert on.
    orders_body = _migration("006_psp_fields_constraints.sql")
    psps_body = _migration("207_merchant_psps_psp_id_format.sql")
    assert PSP_ID_FORMAT_REGEX in orders_body, "migration 006 no longer states this regex"
    assert PSP_ID_FORMAT_REGEX in psps_body, "migration 207 no longer states this regex"


async def test_both_constraints_are_installed() -> None:
    # Non-vacuity: if the fixture built the tables but the constraints did not
    # land, every rejection assertion below would pass for the wrong reason.
    from db.database import database

    rows = await database.fetch_all(
        "SELECT conname FROM pg_constraint WHERE conname IN (:a, :b)",
        {"a": ORDERS_CONSTRAINT, "b": MERCHANT_PSPS_CONSTRAINT},
    )
    names = {row["conname"] for row in rows}
    assert names == {ORDERS_CONSTRAINT, MERCHANT_PSPS_CONSTRAINT}, names


async def test_self_service_onboarding_mints_an_id_orders_accepts() -> None:
    """THE regression. Onboard through the real route, then really sell."""
    from db.orders import create_order

    psp_id = await _run_self_service_onboarding()

    # The id itself must satisfy the rule `orders` enforces...
    assert re.match(PSP_ID_FORMAT_REGEX, psp_id, re.IGNORECASE), psp_id
    assert psp_id.startswith("psp_stripe_")
    assert len(psp_id.rsplit("_", 1)[1]) == 12, (
        f"{psp_id!r} — the production defect was an EIGHT-character suffix"
    )

    # ...and, because a regex in a test is not the database, the real INSERT the
    # real order route performs must actually land.
    order_id = await create_order(_order_values(psp_id))
    assert order_id

    from db.database import database

    stored = await database.fetch_one(
        "SELECT psp_id FROM orders WHERE order_id = :o", {"o": order_id}
    )
    assert stored["psp_id"] == psp_id


async def test_the_pre_fix_id_shape_is_still_rejected_by_orders() -> None:
    """The control. Without it, the test above passes on a DB with no constraint."""
    from db.orders import create_order

    # Byte-for-byte the expression routes/employee_store_psp_fixes.py used to run.
    pre_fix_psp_id = f"psp_stripe_{uuid.uuid4().hex[:8]}"
    assert not re.match(PSP_ID_FORMAT_REGEX, pre_fix_psp_id, re.IGNORECASE)

    with pytest.raises(Exception) as excinfo:
        await create_order(_order_values(pre_fix_psp_id))
    message = str(excinfo.value)
    # Named, so a fixture defect cannot masquerade as the constraint firing.
    assert ORDERS_CONSTRAINT in message, message


async def test_merchant_psps_rejects_a_malformed_id_at_write_time() -> None:
    """The deeper fix: the writer refuses what the reader would refuse.

    Before migration 207 this INSERT succeeded and the merchant only found out at
    their first sale.
    """
    from db.database import database

    with pytest.raises(Exception) as excinfo:
        await database.execute(
            "INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, status)"
            " VALUES (:psp_id, :merchant_id, 'stripe', 'Stripe Account', 'active')",
            {
                # The exact id production wrote for merch_c5e24a8d3738d73b.
                "psp_id": "psp_stripe_30cc4106",
                "merchant_id": _MERCHANT_ID,
            },
        )
    assert MERCHANT_PSPS_CONSTRAINT in str(excinfo.value), str(excinfo.value)


async def test_a_canonical_id_is_accepted_by_merchant_psps() -> None:
    # The constraint must not be so tight it rejects what the generator makes --
    # a rule nothing can satisfy would break onboarding for everyone.
    from db.database import database
    from services.merchant_psp_config_service import _generate_psp_id

    psp_id = _generate_psp_id("stripe")
    await database.execute(
        "INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, status)"
        " VALUES (:psp_id, :merchant_id, 'stripe', 'Stripe Account', 'active')",
        {"psp_id": psp_id, "merchant_id": _MERCHANT_ID},
    )
    stored = await database.fetch_one(
        "SELECT psp_id FROM merchant_psps WHERE psp_id = :p", {"p": psp_id}
    )
    assert stored is not None
