import pytest
from pydantic import ValidationError

from routes.store_audit_commerce_probe_internal import CommerceProbeReceipt


def _receipt(**overrides):
    value = {
        "audit_run_id": "audit-123",
        "verification_run_id": "verify-123",
        "worker_id": "commerce-worker-1",
        "probe_id": "verify-123:attempt:1",
        "verifier_id": "commerce_checkout_probe",
        "verification_status": "succeeded",
        "observed_at": "2026-08-24T00:00:00Z",
        "platform": {"platform": "cafe24", "checkout_provider": "cafe24"},
        "checkout": {"status": "security_challenged_pre_address", "challenge_stage": "pre_address"},
        "cart": {"status": "verified", "quantity": 1, "cart_price": 15.2, "currency": "USD"},
    }
    value.update(overrides)
    return CommerceProbeReceipt.model_validate(value)


def test_receipt_is_structured_and_redacted():
    receipt = _receipt()
    assert receipt.checkout.status == "security_challenged_pre_address"
    assert receipt.cart.currency == "USD"


@pytest.mark.parametrize("value", [
    "customer name Jane Doe",
    "card number 4111 1111 1111 1111",
    "phone +1 415 555 0123",
    "https://merchant.example/checkout?token=secret",
])
def test_receipt_rejects_free_form_or_sensitive_checkout_data(value):
    with pytest.raises(ValidationError):
        _receipt(checkout={"status": value})


def test_successful_probe_requires_merchant_checkout_evidence():
    with pytest.raises(ValidationError, match="requires checkout evidence"):
        _receipt(checkout=None)
