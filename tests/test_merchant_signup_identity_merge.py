"""Unit tests for public merchant signup identity resolution.

Covers the passwordless-claim path that unblocks channel partners whose email
already exists as a credential-less account (e.g. a CRM/partner contact that was
onboarded without a login). Before the fix such an account could never be
converted — every merchant signup bounced off a permanent 409 asking for a
password that was never set.
"""

import asyncio

import pytest
from fastapi import HTTPException

from routes.merchant_onboarding_routes import resolve_public_merchant_identity_merge
from utils.auth import hash_password


def _run(existing_user, entered_password, existing_onboarding=None):
    return asyncio.run(
        resolve_public_merchant_identity_merge(
            existing_user=existing_user,
            existing_onboarding=existing_onboarding,
            entered_password=entered_password,
        )
    )


def test_no_existing_user_is_a_fresh_signup():
    user, merge = _run(None, "whatever")
    assert user is None and merge is None


def test_merchant_role_reuses_without_password_check():
    existing = {"id": 1, "role": "merchant", "password_hash": hash_password("secret")}
    user, merge = _run(existing, entered_password=None)
    assert user is existing
    assert merge is None


def test_passwordless_agent_is_claimable_without_a_password():
    # The channel-partner case: an agent account created WITHOUT a login.
    existing = {"id": 7, "role": "agent", "password_hash": None, "merchant_id": None}
    user, merge = _run(existing, entered_password="new-merchant-pass")
    assert user is existing
    assert merge is not None
    assert merge.get("claimed_passwordless") is True
    assert merge.get("converted_from_role") == "agent"


def test_passwordless_agent_claimable_even_with_empty_string_hash():
    existing = {"id": 8, "role": "agent", "password_hash": "   "}
    user, merge = _run(existing, entered_password=None)
    assert user is existing
    assert merge.get("claimed_passwordless") is True


def test_agent_with_credential_requires_correct_password():
    existing = {"id": 9, "role": "agent", "password_hash": hash_password("real-pass")}

    # Wrong / missing password -> actionable 409, not a claim.
    with pytest.raises(HTTPException) as exc:
        _run(existing, entered_password="wrong")
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "MERCHANT_IDENTITY_PASSWORD_VERIFICATION_REQUIRED"

    # Correct password -> verified conversion (not a passwordless claim).
    user, merge = _run(existing, entered_password="real-pass")
    assert user is existing
    assert merge.get("claimed_passwordless") is None
    assert merge.get("converted_from_role") == "agent"


def test_internal_role_never_auto_claimed_even_when_passwordless():
    # Privileged accounts must go through an explicit admin merge regardless of
    # whether they happen to be credential-less.
    existing = {"id": 10, "role": "admin", "password_hash": None}
    with pytest.raises(HTTPException) as exc:
        _run(existing, entered_password="anything")
    assert exc.value.status_code == 409
    assert exc.value.detail["resolution"] == "admin_merge_required"
