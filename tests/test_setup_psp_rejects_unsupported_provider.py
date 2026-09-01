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


@pytest.fixture
async def _merchant_lookup_table():
    """Just `merchant_onboarding`, so the route's own lookup can return a real 404.

    The positive tests below need to prove they got PAST the allowlist. Without
    this the route dies on "no such table" and wraps it in a 500, which is a much
    weaker signal — it would also be produced by a guard that rejected everything
    and then blew up for an unrelated reason. A genuine 404 from the route's own
    merchant lookup says exactly one thing: the allowlist let the provider
    through.
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
