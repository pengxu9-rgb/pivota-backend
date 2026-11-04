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
    receipt = _receipt(steps=[
        {"step": "storefront_access", "status": "passed", "reason": "storefront_loaded"},
        {"step": "checkout", "status": "blocked", "reason": "challenge"},
    ])
    assert receipt.checkout.status == "security_challenged_pre_address"
    assert receipt.cart.currency == "USD"
    assert receipt.steps[0].step == "storefront_access"


def test_receipt_accepts_truthful_required_selection_failure():
    receipt = _receipt(
        cart={"status": "selection_required"},
        checkout={"status": "unavailable"},
        steps=[{
            "step": "add_to_cart",
            "status": "failed",
            "reason": "required_selection_unresolved",
        }],
    )
    assert receipt.cart.status == "selection_required"
    assert receipt.steps[0].reason == "required_selection_unresolved"


def test_receipt_accepts_cart_item_not_observed_failure():
    receipt = _receipt(
        cart={"status": "unknown"},
        checkout={"status": "unavailable"},
        steps=[{
            "step": "add_to_cart",
            "status": "failed",
            "reason": "cart_item_not_observed",
        }],
    )
    assert receipt.cart.status == "unknown"
    assert receipt.steps[0].reason == "cart_item_not_observed"


def test_receipt_rejects_free_form_or_duplicate_journey_steps():
    with pytest.raises(ValidationError):
        _receipt(steps=[{"step": "checkout", "status": "passed", "reason": "https://secret"}])
    with pytest.raises(ValidationError, match="must be unique"):
        _receipt(steps=[
            {"step": "checkout", "status": "passed", "reason": "checkout_reached"},
            {"step": "checkout", "status": "failed", "reason": "checkout_route_missing"},
        ])


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


async def test_a_cart_receipt_on_a_run_without_a_product_key_is_refused_and_logs_its_code(monkeypatch, caplog):
    # 2026-11-04: a scheduled probe's receipt was answered 409 and the worker logged only
    # "receipt_rejected"; the code never reached any log. It must now be visible.
    import logging

    from fastapi import HTTPException

    from routes import store_audit_commerce_probe_internal as commerce_module

    async def claimed(**_kwargs):
        return {"audit_run_id": "audit-123", "merchant_id": "merchant:jolse", "product_key": None}

    async def evidence(**_kwargs):
        return "evidence-1"

    monkeypatch.setattr(commerce_module, "get_claimed_verification_run", claimed)
    monkeypatch.setattr(commerce_module, "insert_evidence_item", evidence)
    caplog.set_level(logging.WARNING, logger=commerce_module.__name__)

    with pytest.raises(HTTPException) as refused:
        await commerce_module._persist_receipt(_receipt())

    assert refused.value.status_code == 409
    assert refused.value.detail == {"error": "PRODUCT_NOT_RESOLVED"}
    assert "store_audit_commerce_probe_conflict code=PRODUCT_NOT_RESOLVED verify=verify-1" in caplog.text


_FULL_VERIFY = "verify-1234567890abcdef"


def _conflict_log(caplog):
    return [r.getMessage() for r in caplog.records if "store_audit_commerce_probe_conflict" in r.getMessage()]


async def _refused(call):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as refused:
        await call
    assert refused.value.status_code == 409
    return refused.value.detail["error"]


async def test_every_receipt_conflict_logs_only_its_code_and_a_run_id_prefix(monkeypatch, caplog):
    import logging

    from routes import store_audit_commerce_probe_internal as commerce_module

    caplog.set_level(logging.WARNING, logger=commerce_module.__name__)
    receipt = _receipt(verification_run_id=_FULL_VERIFY, probe_id=f"{_FULL_VERIFY}:attempt:1")

    async def none(**_kwargs):
        return None

    async def evidence(**_kwargs):
        return "evidence-1"

    async def lease_lost(**_kwargs):
        return False

    monkeypatch.setattr(commerce_module, "insert_evidence_item", evidence)
    monkeypatch.setattr(commerce_module, "get_verification_run_for_worker", none)

    # Not claimed (lease gone, no earlier receipt for this probe).
    monkeypatch.setattr(commerce_module, "get_claimed_verification_run", none)
    assert await _refused(commerce_module._persist_receipt(receipt)) == "VERIFICATION_NOT_CLAIMED"

    # Claimed by a prospect, never a resolved merchant.
    async def prospect(**_kwargs):
        return {"audit_run_id": "audit-123", "merchant_id": "prospect_sacr", "product_key": "pk"}

    monkeypatch.setattr(commerce_module, "get_claimed_verification_run", prospect)
    assert await _refused(commerce_module._persist_receipt(receipt)) == "MERCHANT_NOT_RESOLVED"

    # Evidence written, then the terminal transition finds the lease gone.
    async def claimed(**_kwargs):
        return {"audit_run_id": "audit-123", "merchant_id": "merchant:sacr-a", "product_key": "pk"}

    monkeypatch.setattr(commerce_module, "get_claimed_verification_run", claimed)
    monkeypatch.setattr(commerce_module, "mark_verification_succeeded", lease_lost)
    assert await _refused(commerce_module._persist_receipt(receipt)) == "VERIFICATION_NOT_CLAIMED"

    assert _conflict_log(caplog) == [
        "store_audit_commerce_probe_conflict code=VERIFICATION_NOT_CLAIMED verify=verify-1",
        "store_audit_commerce_probe_conflict code=MERCHANT_NOT_RESOLVED verify=verify-1",
        "store_audit_commerce_probe_conflict code=VERIFICATION_NOT_CLAIMED verify=verify-1",
    ]
    assert _FULL_VERIFY not in caplog.text


async def test_a_claim_on_a_route_without_a_merchant_storefront_logs_target_unavailable(monkeypatch, caplog):
    import logging

    from fastapi import Response

    from routes import store_audit_commerce_probe_internal as commerce_module

    monkeypatch.setenv("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY", "test-key")
    caplog.set_level(logging.WARNING, logger=commerce_module.__name__)
    blocked = []

    async def claim(**_kwargs):
        return {"verify_id": _FULL_VERIFY, "execution_route_id": "route-1", "merchant_id": "merchant:sacr-a"}

    async def ucp_route(**_kwargs):
        return {"route_kind": "ucp", "endpoint_normalized": "https://sacr-a.example/"}

    async def mark_blocked(**kwargs):
        blocked.append(kwargs)
        return True

    monkeypatch.setattr(commerce_module, "claim_next_pending_verification", claim)
    monkeypatch.setattr(commerce_module, "fetch_execution_route", ucp_route)
    monkeypatch.setattr(commerce_module, "mark_verification_blocked", mark_blocked)

    request = commerce_module.CommerceProbeClaimRequest(worker_id="commerce-worker-1")
    assert await _refused(commerce_module.claim_commerce_probe(request, Response(), x_internal_key="test-key")) == "TARGET_UNAVAILABLE"
    assert blocked and blocked[0]["verify_id"] == _FULL_VERIFY
    assert _conflict_log(caplog) == ["store_audit_commerce_probe_conflict code=TARGET_UNAVAILABLE verify=verify-1"]


@pytest.mark.parametrize("idempotency_key, completes_parent", [
    ("store_readiness:audit-123", True),
    ("store_audit_commerce_reprobe:merchant:sacr-a:2026-11-04", False),
    (None, False),
])
async def test_only_the_store_readiness_probe_completes_its_parent_run(monkeypatch, idempotency_key, completes_parent):
    # A scheduled re-probe reuses the route's last_audit_run_id, which is the Store Readiness run:
    # its receipt must not rewrite that run's status or report.
    import db.merchant_audit_runs as audit_runs

    from routes import store_audit_commerce_probe_internal as commerce_module

    async def claimed(**_kwargs):
        return {"audit_run_id": "audit-123", "merchant_id": "merchant:sacr-a", "product_key": "pk",
                "idempotency_key": idempotency_key}

    async def evidence(**_kwargs):
        return "evidence-1"

    async def succeeded(**_kwargs):
        return True

    async def no_evidence(**_kwargs):
        return []

    async def parent(*, run_id):
        return {"run_id": run_id, "merchant_id": "merchant:sacr-a", "subject_type": "store_readiness"}

    completed = []

    async def complete(**kwargs):
        completed.append(kwargs)

    monkeypatch.setattr(commerce_module, "get_claimed_verification_run", claimed)
    monkeypatch.setattr(commerce_module, "insert_evidence_item", evidence)
    monkeypatch.setattr(commerce_module, "mark_verification_succeeded", succeeded)
    monkeypatch.setattr(commerce_module, "fetch_active_commerce_evidence", no_evidence)
    monkeypatch.setattr(audit_runs, "fetch_audit_run_by_id", parent)
    monkeypatch.setattr(audit_runs, "record_audit_run_completed", complete)

    response = await commerce_module._persist_receipt(_receipt())

    assert response.verification_status == "succeeded"
    assert [c["run_id"] for c in completed] == (["audit-123"] if completes_parent else [])
