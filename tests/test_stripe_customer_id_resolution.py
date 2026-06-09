"""Regression tests for Stripe-customer-id read/write symmetry.

Checkout persists `merchants.stripe_customer_id` via a permissive
`(merchant_id OR contact_email)` match, so the row carrying the id can be keyed
by the onboarding contact_email while its `merchant_id` is NULL/divergent. The
paid-audit gate and overage billing previously read with a strict `merchant_id`
match and missed that row, surfacing as `missing_stripe_customer`. These tests
pin the merchant_id-first, contact_email-fallback resolution.
"""

from __future__ import annotations

import db.merchant_onboarding as merchant_onboarding_module
import services.merchant_credit_balance_service as mcb


async def test_direct_merchant_id_hit_skips_fallback(monkeypatch):
    """A customer id on the merchant_id-matched row resolves with no fallback."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {"stripe_customer_id": "cus_direct"}

    async def fail_fetch_one(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("fallback query should not run on a direct hit")

    async def fail_onboarding(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("onboarding lookup should not run on a direct hit")

    monkeypatch.setattr(mcb, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(mcb.database, "fetch_one", fail_fetch_one)
    monkeypatch.setattr(
        merchant_onboarding_module, "get_merchant_onboarding", fail_onboarding
    )

    assert await mcb._stripe_customer_id_for_direct_merchant("merch_x") == "cus_direct"


async def test_email_fallback_when_merchant_id_row_has_no_customer(monkeypatch):
    """When the merchant_id row lacks the id, fall back to the contact_email row."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {"stripe_customer_id": ""}

    async def fake_onboarding(merchant_id):
        assert merchant_id == "merch_x"
        return {"contact_email": "Peng@Chydan.com"}

    async def fake_fetch_one(query, params):
        assert params["contact_email"] == "Peng@Chydan.com"
        return {"stripe_customer_id": "cus_email"}

    monkeypatch.setattr(mcb, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(
        merchant_onboarding_module, "get_merchant_onboarding", fake_onboarding
    )
    monkeypatch.setattr(mcb.database, "fetch_one", fake_fetch_one)

    assert await mcb._stripe_customer_id_for_direct_merchant("merch_x") == "cus_email"


async def test_returns_empty_when_no_customer_anywhere(monkeypatch):
    """No id on either the merchant_id row or the contact_email row → empty."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {}

    async def fake_onboarding(_merchant_id):
        return {"contact_email": "peng@chydan.com"}

    async def fake_fetch_one(_query, _params):
        return None

    monkeypatch.setattr(mcb, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(
        merchant_onboarding_module, "get_merchant_onboarding", fake_onboarding
    )
    monkeypatch.setattr(mcb.database, "fetch_one", fake_fetch_one)

    assert await mcb._stripe_customer_id_for_direct_merchant("merch_x") == ""


async def test_no_contact_email_returns_empty(monkeypatch):
    """No onboarding contact_email means no fallback target → empty."""

    async def fake_fetch_billing_row(*_args, **_kwargs):
        return {}

    async def fake_onboarding(_merchant_id):
        return None

    async def fail_fetch_one(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("fallback query should not run without a contact_email")

    monkeypatch.setattr(mcb, "_fetch_merchant_billing_row", fake_fetch_billing_row)
    monkeypatch.setattr(
        merchant_onboarding_module, "get_merchant_onboarding", fake_onboarding
    )
    monkeypatch.setattr(mcb.database, "fetch_one", fail_fetch_one)

    assert await mcb._stripe_customer_id_for_direct_merchant("merch_x") == ""
