"""
Declared merchant mode (store_less) onboarding tests.

Covers:
  (a) store_less register with NO store_url -> approved, operating_mode='store_less',
      store_url NULL, auto-approved at low confidence, auto-KYB NOT called.
  (b) storefront register still REQUIRES store_url (422 without it).
  (c) operating_mode normalization (bad value -> 'storefront').

Style mirrors tests/test_merchant_profile_route.py: call the route coroutine
directly with monkeypatched DB + helpers (no live DB).
"""

import pytest
from fastapi import HTTPException

import routes.merchant_onboarding_routes as module
import utils.auto_kyb_validator as kyb
from routes.merchant_onboarding_routes import (
    MerchantRegisterRequest,
    STORE_LESS_APPROVAL_CONFIDENCE,
)


def _patch_common(monkeypatch, captured):
    """Stub out the shared onboarding side-effects so register_merchant runs DB-free."""

    async def fake_execute(*args, **kwargs):
        return None

    async def fake_fetch_one(*args, **kwargs):
        # No duplicate store_url, no existing rows.
        return None

    async def fake_get_active_onboarding_by_email(email):
        return None

    async def fake_get_user_auth_binding(email):
        return None

    async def fake_resolve_identity_merge(**kwargs):
        return None, None

    async def fake_get_merchant_onboarding(merchant_id):
        return None

    async def fake_create(merchant_dict):
        captured["create_dict"] = dict(merchant_dict)
        return "merch_test_storeless"

    async def fake_update_kyc_status(merchant_id, status, *a, **k):
        captured.setdefault("kyc_status_calls", []).append((merchant_id, status))
        return True

    async def fake_sync_user(**kwargs):
        return None

    monkeypatch.setattr(module.database, "execute", fake_execute)
    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module, "get_active_onboarding_by_email", fake_get_active_onboarding_by_email)
    monkeypatch.setattr(module, "get_user_auth_binding", fake_get_user_auth_binding)
    monkeypatch.setattr(module, "resolve_public_merchant_identity_merge", fake_resolve_identity_merge)
    monkeypatch.setattr(module, "get_merchant_onboarding", fake_get_merchant_onboarding)
    monkeypatch.setattr(module, "create_merchant_onboarding", fake_create)
    monkeypatch.setattr(module, "update_kyc_status", fake_update_kyc_status)
    monkeypatch.setattr(module, "sync_merchant_auth_user", fake_sync_user)


class _DummyBackgroundTasks:
    def add_task(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_store_less_register_without_store_url(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)

    # auto-KYB MUST NOT be called for store_less.
    async def boom_auto_kyb(*args, **kwargs):
        raise AssertionError("auto_kyb_pre_approval should NOT be called for store_less")

    monkeypatch.setattr(kyb, "auto_kyb_pre_approval", boom_auto_kyb)

    req = MerchantRegisterRequest(
        business_name="Declared Brand Co",
        region="US",
        contact_email="brand@example.com",
        password="hunter2hunter2",
        operating_mode="store_less",
    )

    resp = await module.register_merchant(req, _DummyBackgroundTasks())

    assert resp["status"] == "success"
    assert resp["merchant_id"] == "merch_test_storeless"
    assert resp["auto_approved"] is True
    assert resp["confidence_score"] == STORE_LESS_APPROVAL_CONFIDENCE

    # Row persisted with store_less + NULL store_url.
    create_dict = captured["create_dict"]
    assert create_dict["operating_mode"] == "store_less"
    assert create_dict["store_url"] is None

    # Auto-approved at low confidence (status flipped to approved via update_kyc_status).
    assert ("merch_test_storeless", "approved") in captured.get("kyc_status_calls", [])


@pytest.mark.asyncio
async def test_storefront_register_requires_store_url(monkeypatch):
    captured = {}
    _patch_common(monkeypatch, captured)

    req = MerchantRegisterRequest(
        business_name="Storefront Co",
        region="US",
        contact_email="store@example.com",
        password="hunter2hunter2",
        # operating_mode defaults to 'storefront'; no store_url provided
    )

    with pytest.raises(HTTPException) as exc:
        await module.register_merchant(req, _DummyBackgroundTasks())

    assert exc.value.status_code == 422
    assert "store_url" in str(exc.value.detail).lower()
    # No row should have been created.
    assert "create_dict" not in captured


@pytest.mark.asyncio
async def test_storefront_register_with_store_url_unchanged(monkeypatch):
    """storefront path still runs auto-KYB and creates a normal row."""
    captured = {}
    _patch_common(monkeypatch, captured)

    async def fake_auto_kyb(business_name, store_url, region):
        captured["auto_kyb_called_with"] = (business_name, store_url, region)
        return {
            "approved": True,
            "confidence_score": 0.85,
            "validation_results": {"url_validation": {"valid": True, "message": "ok"}},
            "requires_full_kyb": True,
            "full_kyb_deadline": "2030-01-01T00:00:00",
        }

    monkeypatch.setattr(kyb, "auto_kyb_pre_approval", fake_auto_kyb)

    req = MerchantRegisterRequest(
        business_name="Storefront Co",
        store_url="https://storefront-co.example",
        region="US",
        contact_email="store@example.com",
        password="hunter2hunter2",
    )

    resp = await module.register_merchant(req, _DummyBackgroundTasks())

    assert resp["status"] == "success"
    # storefront path DID call auto-KYB
    assert "auto_kyb_called_with" in captured
    create_dict = captured["create_dict"]
    assert create_dict["operating_mode"] == "storefront"
    assert create_dict["store_url"] == "https://storefront-co.example"


def test_operating_mode_normalization():
    # bad / unknown value -> storefront
    assert MerchantRegisterRequest(
        business_name="X", store_url="https://x.example", region="US",
        contact_email="x@example.com", operating_mode="garbage",
    ).operating_mode == "storefront"

    # case / whitespace tolerated
    assert MerchantRegisterRequest(
        business_name="X", region="US", contact_email="x@example.com",
        operating_mode="  Store_Less  ",
    ).operating_mode == "store_less"

    # empty -> storefront
    assert MerchantRegisterRequest(
        business_name="X", store_url="https://x.example", region="US",
        contact_email="x@example.com", operating_mode="",
    ).operating_mode == "storefront"

    # default when omitted
    assert MerchantRegisterRequest(
        business_name="X", store_url="https://x.example", region="US",
        contact_email="x@example.com",
    ).operating_mode == "storefront"


@pytest.mark.asyncio
async def test_validate_store_url_handles_none():
    """Defensive guard: None/empty store_url returns clean 'not approvable', no crash."""
    ok, msg = await kyb.validate_store_url(None)
    assert ok is False
    ok2, msg2 = await kyb.validate_store_url("")
    assert ok2 is False

    match, _msg, score = kyb.check_name_url_match("Brand", None)
    assert match is False
    assert score == 0.0
