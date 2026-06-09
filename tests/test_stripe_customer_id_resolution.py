"""Tests for the shared Stripe-customer-id resolver.

`merchants.merchant_id` is not a code-maintained link to the `merch_` identity
(which lives in `merchant_onboarding`), and checkout persists
`stripe_customer_id` via a permissive `(merchant_id OR contact_email)` match. A
strict `merchant_id`-only read can miss the row the id landed on, surfacing as
`missing_stripe_customer` at the paid-audit gate and breaking overage billing.

`billing_routes.resolve_merchant_stripe_customer_id` is the single resolver:
merchant_id first, then the onboarding contact_email row that actually carries a
customer id. These tests pin that behavior and that the credit-balance reader
delegates to it.
"""

from __future__ import annotations

import routes.billing_routes as billing
import services.merchant_credit_balance_service as mcb


class _FakeDB:
    """Minimal db stub exposing fetch_one for the email-fallback query."""

    def __init__(self, email_customer_id: str | None = None) -> None:
        self.email_customer_id = email_customer_id
        self.fetch_one_calls = 0

    async def fetch_one(self, query, params=None):
        self.fetch_one_calls += 1
        if self.email_customer_id is None:
            return None
        return {"stripe_customer_id": self.email_customer_id}


async def test_direct_merchant_id_hit_skips_fallback(monkeypatch):
    """A customer id on the merchant_id-matched row resolves with no fallback."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {"stripe_customer_id": "cus_direct"}

    async def fail_onboarding(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("onboarding lookup should not run on a direct hit")

    monkeypatch.setattr(billing, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(billing, "get_merchant_onboarding", fail_onboarding)
    db = _FakeDB()

    result = await billing.resolve_merchant_stripe_customer_id(db, "merch_x")
    assert result == "cus_direct"
    assert db.fetch_one_calls == 0  # no email fallback query


async def test_email_fallback_when_merchant_id_row_has_no_customer(monkeypatch):
    """When the merchant_id row lacks the id, fall back to the contact_email row."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {"stripe_customer_id": ""}

    async def fake_onboarding(merchant_id):
        assert merchant_id == "merch_x"
        return {"contact_email": "Peng@Chydan.com"}

    monkeypatch.setattr(billing, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(billing, "get_merchant_onboarding", fake_onboarding)
    db = _FakeDB(email_customer_id="cus_email")

    result = await billing.resolve_merchant_stripe_customer_id(db, "merch_x")
    assert result == "cus_email"
    assert db.fetch_one_calls == 1


async def test_returns_empty_when_no_customer_anywhere(monkeypatch):
    """No id on either the merchant_id row or the contact_email row -> empty."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {}

    async def fake_onboarding(_merchant_id):
        return {"contact_email": "peng@chydan.com"}

    monkeypatch.setattr(billing, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(billing, "get_merchant_onboarding", fake_onboarding)
    db = _FakeDB(email_customer_id=None)

    result = await billing.resolve_merchant_stripe_customer_id(db, "merch_x")
    assert result == ""


async def test_no_onboarding_row_returns_empty(monkeypatch):
    """Crawled agent_seed:: merchants have no onboarding row -> no fallback, empty."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {}

    async def fake_onboarding(_merchant_id):
        return None

    monkeypatch.setattr(billing, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(billing, "get_merchant_onboarding", fake_onboarding)
    db = _FakeDB(email_customer_id="cus_should_not_be_used")

    result = await billing.resolve_merchant_stripe_customer_id(db, "agent_seed::acme")
    assert result == ""
    assert db.fetch_one_calls == 0  # no email -> no fallback query


async def test_credit_balance_reader_delegates_to_shared_resolver(monkeypatch):
    """merchant_credit_balance_service resolves via the shared billing resolver."""

    async def fake_resolver(db, merchant_id):
        assert merchant_id == "merch_x"
        return "cus_delegated"

    monkeypatch.setattr(mcb, "resolve_merchant_stripe_customer_id", fake_resolver)
    assert await mcb._stripe_customer_id_for_direct_merchant("merch_x") == "cus_delegated"
