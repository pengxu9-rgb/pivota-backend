"""require_approved_merchant must let billing-EXEMPT merchants through even when
their merchant_onboarding status is not 'approved'.

Regression: App Store shell merchants are created as `pending_verification`.
Gating billing on approval 403'd /api/billing/plans before it could report the
merchant is exempt, so the portal fell back to the paid/Stripe billing UI and
showed a "pending_verification" error — a Shopify 1.2.1 exposure for every App
Store merchant.
"""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import routes.billing_routes as billing_routes


def _creds() -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials="jwt-token")


@pytest.mark.asyncio
async def test_exempt_pending_merchant_passes_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing_routes, "decode_token", lambda t: {"role": "merchant", "merchant_id": "merch_shopify_x"})

    async def fake_onboarding(mid: str):
        return {"status": "pending_verification", "contact_email": "x@pivota.invalid"}

    async def fake_free(mid: str):
        return True  # App Store / exempt

    monkeypatch.setattr(billing_routes, "get_merchant_onboarding", fake_onboarding)
    monkeypatch.setattr(billing_routes, "_merchant_is_billing_free", fake_free)

    result = await billing_routes.require_approved_merchant(
        x_merchant_api_key=None, credentials=_creds()
    )
    assert result["merchant_id"] == "merch_shopify_x"
    assert result.get("billing_exempt") is True


@pytest.mark.asyncio
async def test_non_exempt_pending_merchant_still_403s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing_routes, "decode_token", lambda t: {"role": "merchant", "merchant_id": "merch_byo"})

    async def fake_onboarding(mid: str):
        return {"status": "pending_verification", "contact_email": "byo@example.com"}

    async def fake_free(mid: str):
        return False  # BYO / not exempt

    monkeypatch.setattr(billing_routes, "get_merchant_onboarding", fake_onboarding)
    monkeypatch.setattr(billing_routes, "_merchant_is_billing_free", fake_free)

    with pytest.raises(HTTPException) as exc:
        await billing_routes.require_approved_merchant(x_merchant_api_key=None, credentials=_creds())
    assert exc.value.status_code == 403
    assert "pending_verification" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_approved_non_exempt_merchant_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(billing_routes, "decode_token", lambda t: {"role": "merchant", "merchant_id": "merch_paid"})

    async def fake_onboarding(mid: str):
        return {"status": "approved", "contact_email": "paid@example.com"}

    async def fake_free(mid: str):
        return False

    monkeypatch.setattr(billing_routes, "get_merchant_onboarding", fake_onboarding)
    monkeypatch.setattr(billing_routes, "_merchant_is_billing_free", fake_free)

    result = await billing_routes.require_approved_merchant(x_merchant_api_key=None, credentials=_creds())
    assert result["status"] == "approved"
    assert result["merchant_id"] == "merch_paid"
