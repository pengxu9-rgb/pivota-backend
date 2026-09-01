"""Production-dialect gate: every provider this code can WRITE, `orders` accepts.

THE DEFECT THIS CLOSES. `orders` has carried CHECK check_psp_used_valid_provider
since db/migrations/006_psp_fields_constraints.sql:

    CHECK (psp_used IS NULL OR psp_used IN
           ('stripe','adyen','checkout','paypal','braintree'))

Five names, frozen. The code moved on. `orders.psp_used` is written from one
place — routes/order_routes._resolve_active_order_psp copies
merchant_psps.provider into it via db.orders.create_order — and
`POST /merchant/onboarding/setup-psp`, the self-service route that allows role
`merchant`, took `psp_type: str` with NO allowlist and persisted it with
status='active'. services.merchant_psp_config_service.fetch_active_runtime_merchant_psp
filters on status only, so the value came straight back out.

Two concretely reachable providers were refused by the CHECK:

  * 'antom'  — in SUPPORTED_CANONICAL_PSPS, a provider this repo really supports.
  * 'square' — advertised by setup-psp's own capabilities map and by the
               ConnectPSPRequest comment, with no PSP adapter anywhere.

Reproduced against Postgres 15 on 2026-09-01: setup-psp returned 200
"connected successfully" for both, and then create_order died with

    new row for relation "orders" violates check constraint
    "check_psp_used_valid_provider"

— the identical failure mode as the psp_id-format defect fixed in 20f4542c: the
merchant saves, validates, sees "connected", and every order creation 500s. The
two ends are fixed differently and both halves are asserted below: 'antom' is
real, so migration 208 widens the constraint to admit it; 'square' is not, so the
endpoint refuses it with a 400 at onboarding.

A THIRD instance, latent: the capability-gated deferred lane writes
CAPABILITY_DEFERRED_PSP_PROVIDER ('protocol_deferred') to psp_used and
`_capability_deferred_psp_id()` to psp_id, and BOTH violated their constraints —
so turning AGENT_CHECKOUT_CAPABILITY_GATE on would have 500'd every order it was
meant to enable. Covered below too.

WHY THIS FILE IS A POSTGRES GATE AND NOT A UNIT TEST. The rule is a database
CHECK constraint. A fake connection, a recorded-SQL double, and SQLite all accept
`psp_used = 'square'` happily — the defect is invisible to every engine except
the one production runs. So this file EXECUTES the real route handler and then a
real `create_order()` INSERT against Postgres.
tests/test_setup_psp_rejects_unsupported_provider.py carries the fast,
engine-free half (the endpoint allowlist); it cannot see the constraint.

MUTANTS THESE KILL:
* delete SETUP_PSP_ALLOWED_PROVIDERS' check in routes/employee_store_psp_fixes.py
  -> test_square_is_refused_at_onboarding fails: the route returns 200 and the
     subsequent real INSERT dies with the production error verbatim.
* add 'square' to SETUP_PSP_ALLOWED_PROVIDERS
  -> the same test fails at the INSERT, for the same reason.
* drop 'antom' from migration 208's list
  -> test_antom_onboards_and_then_really_sells fails at the real INSERT.
* revert _capability_deferred_psp_id to `f"{merchant_id}:protocol_deferred"`
  -> test_the_deferred_lane_sentinel_is_insertable fails on check_psp_id_format.
* drop 'protocol_deferred' from migration 208's list
  -> the same test fails on check_psp_used_valid_provider.
* let the fixture stand in for the constraint (build the tables but skip the
  migration) -> test_the_constraint_is_installed_and_widened fails, and every
  rejection assertion checks the CONSTRAINT NAME, so a row rejected for any other
  reason reports as a fixture problem rather than passing as proof.

🚨 THESE GATE FILES SHARE ONE DATABASE. `metadata.create_all` for the tables the
`db.*` modules own (`orders`, `merchant_onboarding`); main.py's OWN
`CREATE TABLE IF NOT EXISTS` literal, lifted from its AST, for `merchant_psps`,
which application bootstrap owns rather than a migration — never a hand-copied
stub, which drifts from the real schema silently. Rows are prefixed and DELETEd,
and the constraint this file installs is dropped again on the way out: siblings
(tests/test_acp_checkout_sessions_postgres.py) insert orders/merchant_psps rows
of their own, and leaving a live constraint behind would make their result depend
on collection ORDER.

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_psp_used_valid_provider_postgres.py

Never point this at prod.
"""

from __future__ import annotations

import ast
import os
import re
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
        "SQLite does not enforce the CHECK under test"
    ),
)

_MERCHANT_ID = "merch_pspprov_gate"

ORDERS_PROVIDER_CONSTRAINT = "check_psp_used_valid_provider"
ORDERS_PSP_ID_CONSTRAINT = "check_psp_id_format"

MIGRATION_208 = "208_orders_psp_used_valid_provider.sql"

# The providers migration 208 teaches `orders`. Asserted below to be exactly the
# list the migration file states, so this literal cannot drift away from the
# constraint it claims to describe.
WIDENED_PROVIDERS = (
    "stripe",
    "adyen",
    "checkout",
    "paypal",
    "braintree",
    "antom",
    "protocol_deferred",
)


def _migration(name: str) -> str:
    path = REPO_ROOT / "db" / "migrations" / name
    assert path.exists(), f"{name} is gone — this gate describes a rule with no source"
    return path.read_text(encoding="utf-8")


def _startup_merchant_psps_ddl() -> str:
    """main.py's own `CREATE TABLE IF NOT EXISTS merchant_psps` literal.

    Lifted from main.py's AST, not copied: `merchant_psps` is created by
    application bootstrap rather than by a migration or by `metadata`, and a
    hand-copied stub drifts from the real column set and types silently.
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

    The constraint is the thing under test, so it is never retyped here.
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

    # The REAL constraints, from the REAL migration files, applied in the order a
    # real database sees them: 006 states the narrow list (a bare ADD, so drop
    # first), then 208 widens it. Running 006 first is deliberate — it proves 208
    # actually replaces the narrow constraint rather than merely being present on
    # a database that never had one.
    await database.execute(
        f"ALTER TABLE orders DROP CONSTRAINT IF EXISTS {ORDERS_PROVIDER_CONSTRAINT}"
    )
    await database.execute(
        f"ALTER TABLE orders DROP CONSTRAINT IF EXISTS {ORDERS_PSP_ID_CONSTRAINT}"
    )
    migration_006 = _migration("006_psp_fields_constraints.sql")
    await database.execute(_constraint_statement(migration_006, ORDERS_PROVIDER_CONSTRAINT))
    await database.execute(_constraint_statement(migration_006, ORDERS_PSP_ID_CONSTRAINT))
    # 208 carries its own guard, so it is applied whole and is idempotent.
    await database.execute(_migration(MIGRATION_208))

    await _cleanup()
    await database.execute(
        "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email)"
        " VALUES (:merchant_id, 'PSP provider gate', 'pspprov-gate@example.invalid')"
        " ON CONFLICT (merchant_id) DO NOTHING",
        {"merchant_id": _MERCHANT_ID},
    )
    try:
        yield
    finally:
        await _cleanup()
        # See the header: these gate files share ONE database.
        await database.execute(
            f"ALTER TABLE orders DROP CONSTRAINT IF EXISTS {ORDERS_PROVIDER_CONSTRAINT}"
        )
        await database.execute(
            f"ALTER TABLE orders DROP CONSTRAINT IF EXISTS {ORDERS_PSP_ID_CONSTRAINT}"
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


def _order_values(psp_used: str, psp_id: str) -> dict:
    """The PSP-bearing subset of what routes/order_routes.py hands create_order."""
    return {
        "merchant_id": _MERCHANT_ID,
        "customer_email": "buyer@example.invalid",
        "shipping_address": {"line1": "1 Test St", "country": "US"},
        "items": [{"sku": "sku-1", "quantity": 1, "price": 10.0}],
        "subtotal": 10.0,
        "total": 10.0,
        "currency": "USD",
        "psp_used": psp_used,
        "psp_id": psp_id,
    }


async def _run_self_service_onboarding(provider: str):
    """Drive the REAL self-service endpoint as a signed-in merchant."""
    from routes.employee_store_psp_fixes import ConnectPSPRequest, setup_merchant_psp

    return await setup_merchant_psp(
        ConnectPSPRequest(
            merchant_id=_MERCHANT_ID,
            psp_type=provider,
            api_key="sk_test_" + "a" * 24,
            test_mode=True,
            # adyen/checkout demand one; harmless for the rest.
            account_id="acct_pspprov_gate",
        ),
        current_user={"role": "merchant", "merchant_id": _MERCHANT_ID},
    )


def test_the_migration_states_exactly_the_list_this_file_asserts_on() -> None:
    # If the migration's list and this file's literal drift apart, the constraint
    # that fires in production is not the one the tests below reason about.
    body = _migration(MIGRATION_208)
    statement = _constraint_statement(body, ORDERS_PROVIDER_CONSTRAINT)
    # Only the IN list — the surrounding DO block quotes constraint and table
    # names too, and those are not part of the vocabulary under test.
    in_list = re.search(r"psp_used IN \(([^)]*)\)", statement, re.DOTALL)
    assert in_list, statement
    quoted = re.findall(r"'([a-z_]+)'", in_list.group(1))
    assert tuple(quoted) == WIDENED_PROVIDERS, quoted


async def test_the_constraint_is_installed_and_widened() -> None:
    # Non-vacuity: if the fixture built the tables but 208 did not land, every
    # acceptance assertion below would pass for the wrong reason.
    from db.database import database

    row = await database.fetch_one(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint"
        " WHERE conname = :n AND conrelid = to_regclass('orders')",
        {"n": ORDERS_PROVIDER_CONSTRAINT},
    )
    assert row is not None, f"{ORDERS_PROVIDER_CONSTRAINT} is not installed"
    definition = dict(row)["def"]
    for provider in WIDENED_PROVIDERS:
        assert f"'{provider}'" in definition, (definition, provider)


async def test_migration_208_is_idempotent() -> None:
    # schema_guard runs this same logic on EVERY boot. Applying it twice must not
    # error, and — because `orders` is the table every checkout writes — the
    # second application must not touch the constraint at all.
    from db.database import database

    before = dict(
        await database.fetch_one(
            "SELECT oid FROM pg_constraint WHERE conname = :n"
            " AND conrelid = to_regclass('orders')",
            {"n": ORDERS_PROVIDER_CONSTRAINT},
        )
    )["oid"]
    await database.execute(_migration(MIGRATION_208))
    after = dict(
        await database.fetch_one(
            "SELECT oid FROM pg_constraint WHERE conname = :n"
            " AND conrelid = to_regclass('orders')",
            {"n": ORDERS_PROVIDER_CONSTRAINT},
        )
    )["oid"]
    assert before == after, (
        "the second apply dropped and re-added the constraint — that is an ACCESS "
        "EXCLUSIVE lock on `orders` once per instance start"
    )


async def test_the_schema_guard_twin_really_installs_it() -> None:
    """The twin is the ONLY applier in production, and it must be EXECUTED here.

    Production fast mode skips db/migrations/ entirely, so on prod the migration
    file above never runs — db/schema_guard.ensure_required_schema_light() is what
    installs this constraint. The existing coverage for that function
    (tests/test_schema_guard_psp_columns.py) runs it against a DummyDB that
    RECORDS SQL strings without executing them, so a DO block that is syntactically
    invalid, or that guards itself into never firing, would pass there and fail on
    boot. This runs the real function against real Postgres.
    """
    from db import schema_guard as sg
    from db.database import database

    assert sg.IS_POSTGRES, "the guard's postgres branch is what is under test"

    # Start from no constraint at all — the state a fast-mode database is in.
    await database.execute(
        f"ALTER TABLE orders DROP CONSTRAINT IF EXISTS {ORDERS_PROVIDER_CONSTRAINT}"
    )

    await sg.ensure_required_schema_light()

    row = await database.fetch_one(
        "SELECT oid, pg_get_constraintdef(oid) AS def, convalidated FROM pg_constraint"
        " WHERE conname = :n AND conrelid = to_regclass('orders')",
        {"n": ORDERS_PROVIDER_CONSTRAINT},
    )
    assert row is not None, "the startup guard did not install the constraint"
    installed = dict(row)
    for provider in WIDENED_PROVIDERS:
        assert f"'{provider}'" in installed["def"], (installed["def"], provider)
    # NOT VALID is load-bearing: on a database where 006 never ran this is an ADD,
    # and a validating ADD would scan rows written across years of an
    # unconstrained vocabulary — aborting the boot this guard exists to protect.
    assert installed["convalidated"] is False, (
        "the startup guard added a VALIDATING constraint — a legacy row outside the "
        "list would now abort the boot"
    )

    # Every boot runs this. The second pass must not re-take an ACCESS EXCLUSIVE
    # lock on `orders`, the table every checkout writes.
    await sg.ensure_required_schema_light()
    after = dict(
        await database.fetch_one(
            "SELECT oid FROM pg_constraint WHERE conname = :n"
            " AND conrelid = to_regclass('orders')",
            {"n": ORDERS_PROVIDER_CONSTRAINT},
        )
    )["oid"]
    assert after == installed["oid"], (
        "the guard dropped and re-added the constraint on the second boot"
    )


async def test_antom_onboards_and_then_really_sells() -> None:
    """THE regression, half one. 'antom' is real; onboard, then really sell.

    SCOPE — and one thing this deliberately does NOT assert. The pair that reaches
    `create_order` is (merchant_psps.provider, merchant_psps.psp_id). This file's
    subject is the PROVIDER half. The psp_id half has its own live defect on this
    branch: setup-psp still mints `f"psp_{psp_type}_{uuid4().hex[:8]}"` — an
    EIGHT-character suffix that `orders.check_psp_id_format` (twelve) refuses.
    That is the defect commit 20f4542c fixes, and 20f4542c is NOT an ancestor of
    this branch (it lives on claude/elastic-knuth-d1c1d1, unmerged). Asserting on
    the route-minted psp_id here would either duplicate that fix or ship a
    permanently red test, so the INSERT below carries a psp_id from the repo's own
    canonical generator — the exact value the route will pass once 20f4542c lands.
    tests/test_psp_id_format_constraint_postgres.py on that branch owns the psp_id
    axis. Both must land for a self-service-onboarded merchant to sell.
    """
    from db.orders import create_order
    from services.merchant_psp_config_service import _generate_psp_id

    response = await _run_self_service_onboarding("antom")
    assert response["status"] == "success", response

    # The route wrote an ACTIVE row, and the real resolver hands its provider to
    # order creation. No allowlist stands between the two.
    from routes.order_routes import _resolve_active_order_psp

    provider, _resolved_psp_id = await _resolve_active_order_psp(_MERCHANT_ID, None)
    assert provider == "antom"

    # Because an allowlist in Python is not the database, the real INSERT the real
    # order route performs must actually land.
    order_id = await create_order(_order_values(provider, _generate_psp_id(provider)))
    assert order_id

    from db.database import database

    stored = dict(
        await database.fetch_one(
            "SELECT psp_used FROM orders WHERE order_id = :o", {"o": order_id}
        )
    )
    assert stored["psp_used"] == "antom"


async def test_square_is_refused_at_onboarding() -> None:
    """THE regression, half two. 'square' has no adapter; the door must say so.

    Before the fix this returned 200 and wrote an active merchant_psps row.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _run_self_service_onboarding("square")
    assert excinfo.value.status_code == 400, excinfo.value.detail
    assert "square" in str(excinfo.value.detail).lower(), excinfo.value.detail

    # Nothing was persisted — the merchant does not get an active PSP row that
    # can never charge.
    from db.database import database

    rows = await database.fetch_all(
        "SELECT provider FROM merchant_psps WHERE merchant_id = :m", {"m": _MERCHANT_ID}
    )
    assert [dict(r)["provider"] for r in rows] == []


async def test_square_would_still_be_rejected_by_orders() -> None:
    """The control. Without it, the test above passes on a DB with no constraint.

    This is byte-for-byte what the pre-fix route produced: an active merchant_psps
    row for 'square', resolved by the real resolver, handed to the real INSERT.
    """
    from db.database import database
    from db.orders import create_order
    from routes.order_routes import _resolve_active_order_psp
    from services.merchant_psp_config_service import _generate_psp_id

    psp_id = _generate_psp_id("square")
    await database.execute(
        "INSERT INTO merchant_psps (psp_id, merchant_id, provider, name, status, connected_at)"
        " VALUES (:psp_id, :m, 'square', 'Square Account', 'active', NOW())",
        {"psp_id": psp_id, "m": _MERCHANT_ID},
    )
    # The resolver has no allowlist of its own — it filters on status='active'
    # only. This is what made the endpoint the whole defence.
    provider, resolved_psp_id = await _resolve_active_order_psp(_MERCHANT_ID, None)
    assert provider == "square"

    with pytest.raises(Exception) as excinfo:
        await create_order(_order_values(provider, resolved_psp_id))
    # Named, so a fixture defect cannot masquerade as the constraint firing.
    assert ORDERS_PROVIDER_CONSTRAINT in str(excinfo.value), str(excinfo.value)


async def test_the_deferred_lane_sentinel_is_insertable() -> None:
    """The third instance: AGENT_CHECKOUT_CAPABILITY_GATE's deferred order.

    `_resolve_active_order_psp` returns (CAPABILITY_DEFERRED_PSP_PROVIDER,
    _capability_deferred_psp_id(merchant_id)) for a capability-gated deferred
    order with no merchant_psps row. Before this change BOTH values violated
    their constraints, so the flag could not create a single order.

    This asserts the values are INSERTABLE. It is not a statement that the
    deferred lane works — that lane is default-off and unexercised.
    """
    from db.orders import create_order
    from routes.order_routes import (
        CAPABILITY_DEFERRED_PSP_PROVIDER,
        _capability_deferred_psp_id,
    )

    provider = CAPABILITY_DEFERRED_PSP_PROVIDER
    psp_id = _capability_deferred_psp_id(_MERCHANT_ID)

    order_id = await create_order(_order_values(provider, psp_id))
    assert order_id

    from db.database import database

    stored = dict(
        await database.fetch_one(
            "SELECT psp_used, psp_id FROM orders WHERE order_id = :o", {"o": order_id}
        )
    )
    assert stored["psp_used"] == "protocol_deferred"
    assert stored["psp_id"] == psp_id


async def test_the_old_deferred_psp_id_is_still_rejected() -> None:
    """The control for the sentinel. The pre-fix id must still be refused."""
    from db.orders import create_order

    pre_fix_psp_id = f"{_MERCHANT_ID}:protocol_deferred"
    with pytest.raises(Exception) as excinfo:
        await create_order(_order_values("protocol_deferred", pre_fix_psp_id))
    assert ORDERS_PSP_ID_CONSTRAINT in str(excinfo.value), str(excinfo.value)


async def test_every_provider_the_endpoint_accepts_the_constraint_accepts() -> None:
    """The invariant, stated once. No provider may pass the door and fail the DB.

    A per-provider list here would go stale the day someone adds a PSP; this
    iterates SETUP_PSP_ALLOWED_PROVIDERS itself, so widening the endpoint without
    widening migration 208 turns this red.
    """
    from db.database import database
    from db.orders import create_order
    from routes.employee_store_psp_fixes import SETUP_PSP_ALLOWED_PROVIDERS
    from services.merchant_psp_config_service import _generate_psp_id

    assert SETUP_PSP_ALLOWED_PROVIDERS, "an empty allowlist would make this vacuous"
    for provider in sorted(SETUP_PSP_ALLOWED_PROVIDERS):
        psp_id = _generate_psp_id(provider)
        order_id = await create_order(_order_values(provider, psp_id))
        stored = dict(
            await database.fetch_one(
                "SELECT psp_used FROM orders WHERE order_id = :o", {"o": order_id}
            )
        )
        assert stored["psp_used"] == provider
