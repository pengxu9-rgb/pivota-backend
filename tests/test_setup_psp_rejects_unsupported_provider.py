"""`POST /merchant/onboarding/setup-psp` must refuse a provider `orders` refuses.

This is the fast, engine-free half of the fix. It runs on the default SQLite
suite and covers the WRITER: the endpoint allowlist, and the agreement between
that allowlist and migration 208's CHECK list.

It cannot see the constraint itself — SQLite does not enforce it, and a recorded-
SQL double never reaches a database at all. The READER half, which executes a
real `create_order()` INSERT against Postgres and watches the CHECK fire, is
tests/test_psp_used_valid_provider_postgres.py.

THE DEFECT. This route allows role `merchant` for self-service onboarding and
took `psp_type: str` with NO allowlist, persisting it verbatim as
merchant_psps.provider with status='active'. Every sibling door has always had
one (/admin/psp/connect: stripe/adyen/checkout/paypal;
/merchant/integrations/psp/connect: SUPPORTED_CANONICAL_PSPS). The value comes
straight back out at order creation and is written to orders.psp_used, whose
CHECK knew five names. 'square' — advertised by this route's own capabilities map
and by the ConnectPSPRequest comment, with no PSP adapter anywhere in this repo —
saved, validated, showed "connected", and then 500'd every order creation. Same
shape as the psp_id-format defect fixed in 20f4542c.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]

MIGRATION_208 = REPO_ROOT / "db" / "migrations" / "208_orders_psp_used_valid_provider.sql"


def _startup_merchant_psps_ddl() -> str:
    """main.py's own `CREATE TABLE IF NOT EXISTS merchant_psps` literal.

    Lifted from main.py's AST, never hand-copied: `merchant_psps` is built by
    application bootstrap rather than by a migration or by `metadata`, and a stub
    written here would drift from the real column set silently. SQLite accepts
    this statement verbatim (its type affinity tolerates JSONB and
    TIMESTAMP WITH TIME ZONE), so no invented DDL is needed to run it.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"), filename="main.py")
    found = [
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


@pytest.fixture
async def _merchant_lookup_table():
    """The two tables this route reads before it writes.

    `merchant_onboarding` so the route's own lookup can return a real 404: the
    positive tests below need to prove they got PAST the allowlist, and without
    the table the route dies on "no such table" and wraps it in a 500 — a much
    weaker signal, since a guard that rejected everything and then blew up for an
    unrelated reason produces the same thing. A genuine 404 from the route's own
    merchant lookup says exactly one thing: the allowlist let the provider through.

    `merchant_psps` because the route queries it for an existing row before
    persisting, and that query is on the path to the psp_id assertion below.
    """
    from sqlalchemy import create_engine

    from db.database import DATABASE_URL as DB_URL, database
    from db.merchant_onboarding import merchant_onboarding

    engine = create_engine(str(DB_URL).replace("sqlite+aiosqlite:", "sqlite:"))
    merchant_onboarding.create(engine, checkfirst=True)
    engine.dispose()

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await database.execute(_startup_merchant_psps_ddl())
    try:
        yield
    finally:
        if not was_connected and database.is_connected:
            await database.disconnect()


def _migration_208_providers() -> tuple:
    """The provider list migration 208 teaches orders.psp_used, read from the file."""
    body = MIGRATION_208.read_text(encoding="utf-8")
    in_list = re.search(r"psp_used IN \(([^)]*)\)", body, re.DOTALL)
    assert in_list, "migration 208 no longer contains a `psp_used IN (...)` list"
    return tuple(re.findall(r"'([a-z_]+)'", in_list.group(1)))


def test_square_is_not_in_the_allowlist() -> None:
    from routes.employee_store_psp_fixes import SETUP_PSP_ALLOWED_PROVIDERS

    assert "square" not in SETUP_PSP_ALLOWED_PROVIDERS


def test_the_allowlist_is_the_canonical_set_plus_paypal() -> None:
    from routes.employee_store_psp_fixes import SETUP_PSP_ALLOWED_PROVIDERS
    from services.merchant_psp_config_service import SUPPORTED_CANONICAL_PSPS

    # Derived, not retyped: adding a provider to SUPPORTED_CANONICAL_PSPS must not
    # silently leave this door narrower than its siblings.
    assert SETUP_PSP_ALLOWED_PROVIDERS == frozenset(SUPPORTED_CANONICAL_PSPS | {"paypal"})
    assert "antom" in SETUP_PSP_ALLOWED_PROVIDERS


def test_the_endpoint_never_accepts_what_the_constraint_refuses() -> None:
    """THE invariant. Every provider the door takes, `orders` must be able to store.

    This is the whole defect in one assertion. It reads migration 208's list from
    the file rather than restating it, so widening the endpoint without widening
    the constraint — the exact mistake that produced this bug — turns it red.
    """
    from routes.employee_store_psp_fixes import SETUP_PSP_ALLOWED_PROVIDERS

    allowed_by_constraint = set(_migration_208_providers())
    assert allowed_by_constraint, "parsed zero providers out of migration 208"
    unstorable = set(SETUP_PSP_ALLOWED_PROVIDERS) - allowed_by_constraint
    assert not unstorable, (
        f"setup-psp accepts {sorted(unstorable)}, which orders.psp_used refuses — "
        "every order creation for such a merchant would 500 on "
        "check_psp_used_valid_provider"
    )


def test_the_deferred_sentinel_is_storable_too() -> None:
    """The capability-gated lane's provider must also survive the constraint."""
    from routes.order_routes import CAPABILITY_DEFERRED_PSP_PROVIDER

    assert CAPABILITY_DEFERRED_PSP_PROVIDER in _migration_208_providers()


def test_the_deferred_sentinel_psp_id_matches_the_orders_format_rule() -> None:
    """`orders.check_psp_id_format` is `^psp_[a-z0-9]+_[a-z0-9]{12}$` (migration 006).

    The sentinel used to be `f"{merchant_id}:protocol_deferred"`, which the CHECK
    refuses outright — so turning AGENT_CHECKOUT_CAPABILITY_GATE on would have
    500'd every order it was meant to enable.
    """
    from routes.order_routes import _capability_deferred_psp_id

    for merchant_id in ("merch_abc", "merch_c5e24a8d3738d73b", "M-With-Caps_123"):
        psp_id = _capability_deferred_psp_id(merchant_id)
        assert re.fullmatch(r"psp_[a-z0-9]+_[a-z0-9]{12}", psp_id), (merchant_id, psp_id)

    # Deterministic per merchant, and distinct between merchants.
    assert _capability_deferred_psp_id("merch_abc") == _capability_deferred_psp_id("merch_abc")
    assert _capability_deferred_psp_id("merch_abc") != _capability_deferred_psp_id("merch_xyz")


@pytest.mark.parametrize("provider", ["square", "braintree", "mollie", "", "  ", "stripe; DROP"])
async def test_the_route_rejects_an_unsupported_provider_before_touching_the_db(
    provider: str,
) -> None:
    """A 400 at onboarding, not a 500 at the merchant's first sale.

    `current_user` is a signed-in merchant — the self-service case this route
    exists for, and the one that was unguarded. The rejection must land BEFORE the
    merchant lookup, so no database is needed here: if the guard were moved below
    that lookup this test would fail on the missing table rather than pass.

    'braintree' is in the orders CHECK (migration 006 put it there and 208 keeps
    it, because dropping a name is a narrowing) but has no adapter in this repo,
    so the door must still refuse it. The constraint list is a floor, not a menu.
    """
    from routes.employee_store_psp_fixes import ConnectPSPRequest, setup_merchant_psp

    with pytest.raises(HTTPException) as excinfo:
        await setup_merchant_psp(
            ConnectPSPRequest(
                merchant_id="merch_never_created",
                psp_type=provider,
                api_key="sk_test_" + "a" * 24,
                test_mode=True,
            ),
            current_user={"role": "merchant", "merchant_id": "merch_never_created"},
        )
    assert excinfo.value.status_code == 400, excinfo.value.detail
    assert "Unsupported PSP provider" in str(excinfo.value.detail), excinfo.value.detail


@pytest.mark.parametrize("provider", ["stripe", "adyen", "checkout", "paypal", "antom"])
async def test_a_supported_provider_passes_the_allowlist(provider: str, _merchant_lookup_table) -> None:
    """The positive counterpart: the guard must not refuse everything.

    A guard that rejected every provider would kill all six mutants above while
    breaking onboarding entirely. These calls get PAST the allowlist and fail
    later, on the merchant lookup — a 404, never the allowlist's 400.
    """
    from routes.employee_store_psp_fixes import ConnectPSPRequest, setup_merchant_psp

    with pytest.raises(HTTPException) as excinfo:
        await setup_merchant_psp(
            ConnectPSPRequest(
                merchant_id="merch_never_created",
                psp_type=provider,
                api_key="sk_test_" + "a" * 24,
                test_mode=True,
                account_id="acct_x",  # adyen/checkout demand one
            ),
            current_user={"role": "merchant", "merchant_id": "merch_never_created"},
        )
    assert excinfo.value.status_code == 404, excinfo.value.detail
    assert "Merchant not found" in str(excinfo.value.detail), excinfo.value.detail


async def test_a_new_psp_id_comes_from_the_canonical_generator(
    _merchant_lookup_table, monkeypatch
) -> None:
    """The route must not mint its own psp_id.

    `orders.check_psp_id_format` is `^psp_[a-z0-9]+_[a-z0-9]{12}$` — exactly TWELVE
    trailing characters. This route minted `f"psp_{psp_type}_{uuid4().hex[:8]}"`,
    EIGHT, so the row saved, the PSP validated, the portal said "connected", and
    every order creation 500'd at the merchant's first sale. Passing None delegates
    to `_generate_psp_id`, which honours the rule.

    Asserted on the ARGUMENT handed to persist_canonical_merchant_psp rather than
    on the returned id: `None` is what makes the canonical generator run, and a
    route that re-introduced a conforming-but-hand-rolled id would still be the
    same class of defect — two generators to keep in sync. The constraint itself
    is executed in tests/test_psp_used_valid_provider_postgres.py.
    """
    import routes.employee_store_psp_fixes as module

    captured = {}

    async def fake_persist(**kwargs):
        captured.update(kwargs)
        # The real helper's contract: it returns the row it wrote, psp_id filled in.
        return {"psp_id": kwargs.get("psp_id") or "psp_stripe_abcdef123456"}

    monkeypatch.setattr(module, "persist_canonical_merchant_psp", fake_persist)

    from db.database import database

    await database.execute(
        "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email)"
        " VALUES (:m, 'psp id gate', 'pspid-gate@example.invalid')",
        {"m": "merch_pspid_gate"},
    )
    try:
        await module.setup_merchant_psp(
            module.ConnectPSPRequest(
                merchant_id="merch_pspid_gate",
                psp_type="stripe",
                api_key="sk_test_" + "a" * 24,
                test_mode=True,
            ),
            current_user={"role": "merchant", "merchant_id": "merch_pspid_gate"},
        )
    finally:
        await database.execute(
            "DELETE FROM merchant_onboarding WHERE merchant_id = :m",
            {"m": "merch_pspid_gate"},
        )

    assert captured, "the route never reached persist_canonical_merchant_psp"
    assert captured["psp_id"] is None, (
        f"the route minted its own psp_id ({captured['psp_id']!r}) instead of "
        "delegating to _generate_psp_id"
    )


def test_the_canonical_generator_satisfies_the_orders_format_rule() -> None:
    """The positive counterpart: delegation is only a fix if the delegate is right."""
    from services.merchant_psp_config_service import _generate_psp_id

    for provider in ("stripe", "adyen", "checkout", "paypal", "antom"):
        psp_id = _generate_psp_id(provider)
        assert re.fullmatch(r"psp_[a-z0-9]+_[a-z0-9]{12}", psp_id), psp_id
        assert psp_id.startswith(f"psp_{provider}_")


@pytest.mark.parametrize(
    "provider",
    [
        "other_manual_test",  # a REAL active production row, observed 2026-09-02
        "Square",
        "Checkout.com",
        "protocol_deferred",
        "",
        "___",
        "  MoLLie  ",
    ],
)
def test_the_generator_conforms_for_a_provider_it_was_never_designed_for(provider) -> None:
    """The provider SEGMENT is `[a-z0-9]+` — no underscores, no uppercase, no dots.

    _generate_psp_id interpolated the provider raw, so it minted an id its OWN
    constraint refuses for anything that is not pure lowercase alphanumeric:

        _generate_psp_id("other_manual_test") -> 'psp_other_manual_test_...'  REJECTED

    Found the hard way. Production carries an ACTIVE merchant_psps row with
    provider 'other_manual_test' (merch_9b3c4e68b9f76d79), and
    scripts/audit_malformed_psp_ids.py --repair — the tool whose entire job is to
    turn a malformed psp_id into a canonical one — CRASHED on it, because the
    "canonical" id it produced was itself malformed. The repair could not be
    applied to the row that needed it most.

    This is the same writer/reader disagreement as everything else in this change,
    but one level down: in the function every other fix delegates to as the
    authority on what "canonical" means.
    """
    from services.merchant_psp_config_service import _generate_psp_id

    psp_id = _generate_psp_id(provider)
    assert re.fullmatch(r"psp_[a-z0-9]+_[a-z0-9]{12}", psp_id), (provider, psp_id)


def test_sanitising_did_not_change_the_id_shape_for_supported_providers() -> None:
    """The fix must be invisible where nothing was wrong.

    Every provider the endpoint accepts is pure lowercase alphanumeric, so the
    sanitiser is a no-op on all of them — the segment must still be the provider
    name itself, not a mangled version of it.
    """
    from routes.employee_store_psp_fixes import SETUP_PSP_ALLOWED_PROVIDERS
    from services.merchant_psp_config_service import _generate_psp_id

    for provider in sorted(SETUP_PSP_ALLOWED_PROVIDERS):
        assert _generate_psp_id(provider).startswith(f"psp_{provider}_"), provider


@pytest.mark.parametrize("provider", ["STRIPE", " Stripe ", "AnToM"])
async def test_case_and_whitespace_are_normalised_the_way_the_row_will_be(
    provider: str, _merchant_lookup_table
) -> None:
    """`persist_canonical_merchant_psp` lowercases the provider before writing it.

    So the allowlist must be checked on the same normalised form, or a caller
    sending "ANTOM" is refused for a value the row would never have carried.
    """
    from routes.employee_store_psp_fixes import ConnectPSPRequest, setup_merchant_psp

    with pytest.raises(HTTPException) as excinfo:
        await setup_merchant_psp(
            ConnectPSPRequest(
                merchant_id="merch_never_created",
                psp_type=provider,
                api_key="sk_test_" + "a" * 24,
                test_mode=True,
                account_id="acct_x",
            ),
            current_user={"role": "merchant", "merchant_id": "merch_never_created"},
        )
    assert excinfo.value.status_code == 404, excinfo.value.detail
