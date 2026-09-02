"""`POST /merchant/onboarding/setup-psp` must mint a psp_id `orders` will accept.

The engine-free half of the gate. `orders` enforces
`^psp_[a-z0-9]+_[a-z0-9]{12}$` (CHECK check_psp_id_format, migration 006) and
order creation copies merchant_psps.psp_id straight into orders.psp_id, so a
short id written here is a 500 the merchant meets at their FIRST SALE, not at
onboarding. This route used to mint `uuid.uuid4().hex[:8]` -- eight characters --
and production merchant merch_c5e24a8d3738d73b lost every checkout to it on
2026-08-29.

WHAT THIS FILE CAN AND CANNOT SEE. It drives the real route through the real
`persist_canonical_merchant_psp`, with only `database` doubled, and reads the
psp_id out of the INSERT the service actually built -- so it kills the mutant
that puts the inline uuid slice back. It CANNOT see the CHECK constraint: the
rule lives in Postgres, and no double enforces it. That half is
tests/test_psp_id_format_constraint_postgres.py, which executes a real order
INSERT. Neither file is sufficient alone; this one is the fast one, and it is the
one that runs in the SQLite sweep.

The regex is read from the migration rather than retyped, so it cannot drift
away from the constraint production actually applies.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_MIGRATION_006 = (REPO_ROOT / "db" / "migrations" / "006_psp_fields_constraints.sql").read_text(
    encoding="utf-8"
)
PSP_ID_FORMAT_REGEX = r"^psp_[a-z0-9]+_[a-z0-9]{12}$"


def test_the_regex_this_file_asserts_is_the_one_orders_enforces() -> None:
    assert PSP_ID_FORMAT_REGEX in _MIGRATION_006, (
        "migration 006 no longer states this regex — every assertion below is "
        "now about a rule production does not have"
    )
    assert "check_psp_id_format" in _MIGRATION_006


def _install_fake_database(monkeypatch, *, existing_row):
    """Double only `database`. Everything from the route down is the real code."""
    import routes.employee_store_psp_fixes as route_module
    import services.merchant_psp_config_service as service_module

    executed = []

    async def fake_fetch_one(query, values=None):
        text = " ".join(str(query).split())
        if "FROM merchant_onboarding" in text:
            return {"merchant_id": "merch_selfservice"}
        if "FROM merchant_psps" in text:
            return existing_row
        return None

    async def fake_fetch_all(query, values=None):
        # `fetch_canonical_merchant_psp` falls back to a merchant+provider scan
        # when no psp_id is supplied. Left unpatched it reaches the real engine
        # and the route's blanket `except Exception` reports it as a 500 —
        # i.e. a hole in the double looks exactly like the defect under test.
        return [existing_row] if existing_row else []

    async def fake_execute(query, values=None):
        executed.append((" ".join(str(query).split()), dict(values or {})))

    @asynccontextmanager
    async def fake_transaction():
        yield

    # One shared `database` object backs both modules, so patching its methods
    # covers the route AND the service. Patch the object, not either module's
    # name for it.
    assert route_module.database is service_module.database
    monkeypatch.setattr(route_module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(route_module.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(route_module.database, "execute", fake_execute)
    monkeypatch.setattr(route_module.database, "transaction", lambda: fake_transaction())
    return route_module, executed


async def _setup_psp(route_module, **overrides):
    request = route_module.ConnectPSPRequest(
        merchant_id="merch_selfservice",
        psp_type=overrides.pop("psp_type", "stripe"),
        api_key="sk_test_" + "a" * 24,
        test_mode=True,
        **overrides,
    )
    return await route_module.setup_merchant_psp(
        request,
        # The self-service case the route exists to support: a signed-in merchant.
        current_user={"role": "merchant", "merchant_id": "merch_selfservice"},
    )


def _insert_values(executed):
    return next(
        values
        for query, values in executed
        if query.upper().startswith("INSERT INTO MERCHANT_PSPS")
    )


async def test_a_new_psp_gets_a_canonical_twelve_character_id(monkeypatch) -> None:
    route_module, executed = _install_fake_database(monkeypatch, existing_row=None)

    response = await _setup_psp(route_module)

    assert response["status"] == "success"
    written = _insert_values(executed)["psp_id"]
    # The id the SERVICE actually wrote, not the one the route reported.
    assert written == response["psp_id"]
    assert re.match(PSP_ID_FORMAT_REGEX, written, re.IGNORECASE), written
    assert len(written.rsplit("_", 1)[1]) == 12, (
        f"{written!r} — the production defect was an EIGHT-character suffix"
    )


# DERIVED, not hardcoded — and this is the one place this file diverges from
# 20f4542c, deliberately.
#
# That commit listed ["stripe", "paypal", "square"] here, because on its branch
# the route accepted ANY provider string and `square` was a fair example of one
# it took. This branch adds SETUP_PSP_ALLOWED_PROVIDERS, which refuses `square`
# with a 400: it has no PSP adapter anywhere in this repo, so a merchant who
# "connected" it got a row that could never charge (see
# tests/test_setup_psp_rejects_unsupported_provider.py).
#
# The two changes merge without a textual conflict and are still incompatible:
# git would happily produce a tree where this test asserts `square` gets a
# conforming id while the route returns 400 for it. Deriving the list from the
# allowlist resolves that permanently and makes the test's own name literally
# true — whichever providers the route accepts, all of them must get a
# conforming id. Anyone merging the other branch should keep THIS version.
def _providers_the_route_accepts():
    from routes.employee_store_psp_fixes import SETUP_PSP_ALLOWED_PROVIDERS

    return sorted(SETUP_PSP_ALLOWED_PROVIDERS)


@pytest.mark.parametrize("psp_type", _providers_the_route_accepts())
async def test_every_provider_the_route_accepts_gets_a_conforming_id(
    monkeypatch, psp_type
) -> None:
    # `[a-z0-9]+` covers the provider segment too, so a provider that is not
    # lowercase alphanumeric would fail `orders` just as a short suffix does.
    route_module, executed = _install_fake_database(monkeypatch, existing_row=None)

    # adyen and checkout refuse to save without one; harmless for the others.
    await _setup_psp(route_module, psp_type=psp_type, account_id="acct_selfservice")

    written = _insert_values(executed)["psp_id"]
    assert re.match(PSP_ID_FORMAT_REGEX, written, re.IGNORECASE), written
    assert written.startswith(f"psp_{psp_type}_")


async def test_a_provider_the_route_refuses_never_reaches_the_generator() -> None:
    """The counterpart: `square` is now a 400, not a conforming id.

    Without this, replacing the hardcoded list above with a derived one would
    silently DROP the `square` case rather than re-express it.
    """
    from fastapi import HTTPException

    from routes.employee_store_psp_fixes import (
        SETUP_PSP_ALLOWED_PROVIDERS,
        ConnectPSPRequest,
        setup_merchant_psp,
    )

    assert "square" not in SETUP_PSP_ALLOWED_PROVIDERS
    with pytest.raises(HTTPException) as excinfo:
        await setup_merchant_psp(
            ConnectPSPRequest(
                merchant_id="merch_selfservice",
                psp_type="square",
                api_key="sk_test_" + "a" * 24,
                test_mode=True,
            ),
            current_user={"role": "merchant", "merchant_id": "merch_selfservice"},
        )
    assert excinfo.value.status_code == 400, excinfo.value.detail


async def test_an_existing_row_keeps_its_own_psp_id(monkeypatch) -> None:
    # The fix must not turn an upsert into a new identity: re-saving credentials
    # has to land on the row `orders` already references.
    existing = {
        "psp_id": "psp_stripe_abcdef123456",
        "merchant_id": "merch_selfservice",
        "provider": "stripe",
        "name": "Stripe Account",
        "api_key": "sk_test_old",
        "account_id": None,
        "secret_key": None,
        "capabilities": "payments",
        "status": "active",
        "connected_at": None,
        "environment": "test",
        "provider_config": None,
        "validation_status": "unknown",
        "validation_error": None,
        "last_validated_at": None,
    }
    route_module, executed = _install_fake_database(monkeypatch, existing_row=existing)

    response = await _setup_psp(route_module)

    assert response["psp_id"] == "psp_stripe_abcdef123456"
    assert not any(
        query.upper().startswith("INSERT INTO MERCHANT_PSPS") for query, _ in executed
    ), "an upsert must UPDATE the existing row, not mint a second identity"


async def test_the_route_no_longer_builds_a_psp_id_of_its_own() -> None:
    """Source pin on the delivering line.

    The behavioural tests above go through `persist_canonical_merchant_psp`, so
    they would still pass if the route reintroduced an inline mint that happened
    to be twelve characters. The rule is not "twelve characters here too" -- it
    is that ONE generator owns the format, so the next change to it cannot leave
    a second copy behind.
    """
    source = (REPO_ROOT / "routes" / "employee_store_psp_fixes.py").read_text(encoding="utf-8")
    delivering = [
        line
        for line in source.splitlines()
        if "psp_id=" in line and not line.lstrip().startswith("#")
    ]
    assert delivering, "the setup-psp call no longer passes psp_id at all"
    for line in delivering:
        assert 'f"psp_' not in line, f"inline psp_id construction is back: {line.strip()}"
