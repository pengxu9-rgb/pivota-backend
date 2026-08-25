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


def test_idle_claim_returns_204_through_the_http_layer(monkeypatch):
    # The defect lived in FastAPI's response_model validation, so a direct
    # call to claim_commerce_probe cannot catch it — the request must cross
    # the HTTP layer for validation to run.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routes import store_audit_commerce_probe_internal as commerce_module

    monkeypatch.setenv("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY", "test-key")

    async def no_claim(**_kwargs):
        return None

    monkeypatch.setattr(commerce_module, "claim_next_pending_verification", no_claim)
    app = FastAPI()
    app.include_router(commerce_module.router)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/internal/store-audit/commerce-probes/claims",
        json={"worker_id": "commerce-worker-1"},
        headers={"X-Internal-Key": "test-key"},
    )
    assert response.status_code == 204
    assert response.content == b""
