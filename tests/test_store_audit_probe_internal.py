import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from routes import store_audit_probe_internal as receipt_module


@pytest.fixture(autouse=True)
def _stub_receipt_transaction(monkeypatch):
    @asynccontextmanager
    async def no_transaction():
        yield

    monkeypatch.setattr(receipt_module, "_receipt_transaction", no_transaction)

    async def default_claimed_route(**_kwargs):
        return {
            "execution_route_id": "route-1",
            "normalized_domain": "shop.example",
            "endpoint_normalized": "https://shop.example/api/ucp/mcp",
        }

    monkeypatch.setattr(receipt_module, "fetch_execution_route", default_claimed_route)


def _receipt(**overrides):
    payload = {
        "audit_run_id": "audit-1",
        "verification_run_id": "verify-1",
        "worker_id": "store-audit-worker-1",
        "probe_id": "probe-20260823-001",
        "verifier_id": "ucp_probe",
        "verification_status": "succeeded",
        "observed_at": "2026-08-23T12:00:00Z",
        "route": {
            "normalized_domain": "shop.example",
            "route_kind": "ucp",
            "endpoint": "https://shop.example/api/ucp/mcp",
            "expires_at": "2026-08-24T12:00:00Z",
        },
        "acceptance_signal": {
            "evidence_type": "acceptance_signal",
            "evidence_level": "tested",
            "payload": {"capabilities": ["create_checkout"], "priced_facts": {"currency": "USD"}},
        },
    }
    payload.update(overrides)
    return receipt_module.UcpProbeReceipt.model_validate(payload)


def test_receipt_rejects_raw_or_session_result_data():
    with pytest.raises(ValidationError, match="raw, session, or URL"):
        _receipt(
            acceptance_signal={
                "evidence_type": "acceptance_signal",
                "evidence_level": "tested",
                "payload": {"continue_url": "https://checkout.example/session"},
            },
        )


def test_receipt_rejects_url_in_evidence_payload_and_invalid_route_identity():
    with pytest.raises(ValidationError, match="URL data"):
        _receipt(
            acceptance_signal={
                "evidence_type": "acceptance_signal",
                "evidence_level": "tested",
                "payload": {"description": "see https://checkout.example/session"},
            },
        )
    with pytest.raises(ValidationError, match="without credentials"):
        _receipt(route={
            "normalized_domain": "shop.example",
            "route_kind": "ucp",
            "endpoint": "https://user:pass@shop.example/api/ucp/mcp",
        })


def test_receipt_is_hidden_until_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", raising=False)
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")
    with pytest.raises(HTTPException) as exc_info:
        receipt_module._require_receipt_key("test-key")
    assert exc_info.value.status_code == 404


def test_receipt_persists_domain_route_and_never_associates_prospect(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")
    observed = {}

    async def fake_claimed(**_kwargs):
        return {
            "audit_run_id": "audit-1",
            "merchant_id": "prospect_abcdef012345",
            "execution_route_id": "route-1",
        }

    async def fake_route(**kwargs):
        observed["route"] = kwargs
        return {"execution_route_id": "route-1"}

    async def fake_attach(**kwargs):
        observed["attach"] = kwargs
        return True

    async def fake_evidence(**kwargs):
        observed["evidence"] = kwargs
        return "evidence-1"

    async def fake_succeeded(**kwargs):
        observed["succeeded"] = kwargs
        return True

    monkeypatch.setattr(receipt_module, "get_claimed_verification_run", fake_claimed)
    monkeypatch.setattr(receipt_module, "upsert_execution_route", fake_route)
    monkeypatch.setattr(receipt_module, "attach_execution_route_to_claimed_verification", fake_attach)
    monkeypatch.setattr(receipt_module, "insert_evidence_item", fake_evidence)
    monkeypatch.setattr(receipt_module, "mark_verification_succeeded", fake_succeeded)

    result = asyncio.run(
        receipt_module.receive_ucp_probe_receipt(_receipt(), x_internal_key="test-key")
    )

    assert result.verification_status == "succeeded"
    assert result.execution_route_id == "route-1"
    assert result.evidence_id == "evidence-1"
    assert "merchant_id" not in observed["route"]
    assert observed["evidence"]["merchant_id"] is None
    assert observed["evidence"]["evidence_type"] == "acceptance_signal"
    assert observed["evidence"]["evidence_level"] == "tested"
    assert observed["evidence"]["payload"]["signal"]["priced_facts"] == {"currency": "USD"}


def test_receipt_requires_current_worker_claim(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")

    async def fake_claimed(**_kwargs):
        return None

    async def fake_previous(**_kwargs):
        return None

    monkeypatch.setattr(receipt_module, "get_claimed_verification_run", fake_claimed)
    monkeypatch.setattr(receipt_module, "get_verification_run_for_worker", fake_previous)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            receipt_module.receive_ucp_probe_receipt(_receipt(), x_internal_key="test-key")
        )
    assert exc_info.value.status_code == 409


def test_receipt_rejects_endpoint_or_domain_switch_after_claim(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")

    async def fake_claimed(**_kwargs):
        return {"audit_run_id": "audit-1", "execution_route_id": "route-1"}

    async def must_not_upsert(**_kwargs):
        raise AssertionError("route identity mismatch must not persist a route")

    monkeypatch.setattr(receipt_module, "get_claimed_verification_run", fake_claimed)
    monkeypatch.setattr(receipt_module, "upsert_execution_route", must_not_upsert)
    mismatched = _receipt(route={
        "normalized_domain": "shop.example",
        "route_kind": "ucp",
        "endpoint": "https://new-endpoint.example/api/ucp/mcp",
    })
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(receipt_module.receive_ucp_probe_receipt(
            mismatched, x_internal_key="test-key",
        ))
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error"] == "CLAIMED_ROUTE_IDENTITY_MISMATCH"


def test_clean_no_route_result_deactivates_claimed_route(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")
    observed = {}

    async def fake_claimed(**_kwargs):
        return {"audit_run_id": "audit-1", "execution_route_id": "route-1"}

    async def fake_deactivate(**kwargs):
        observed["deactivate"] = kwargs
        return True

    async def fake_success(**_kwargs):
        return True

    monkeypatch.setattr(receipt_module, "get_claimed_verification_run", fake_claimed)
    monkeypatch.setattr(receipt_module, "deactivate_execution_route", fake_deactivate)
    monkeypatch.setattr(receipt_module, "mark_verification_succeeded", fake_success)
    receipt = _receipt(
        route=None,
        acceptance_signal=None,
        reason="not_ucp_reachable",
    )
    result = asyncio.run(receipt_module.receive_ucp_probe_receipt(
        receipt, x_internal_key="test-key",
    ))
    assert result.verification_status == "succeeded"
    assert observed["deactivate"]["execution_route_id"] == "route-1"


def test_receipt_acknowledges_lost_success_response_idempotently(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")

    async def fake_claimed(**_kwargs):
        return None

    async def fake_previous(**_kwargs):
        return {
            "audit_run_id": "audit-1",
            "status": "succeeded",
            "execution_route_id": "route-1",
            "evidence_jsonb": {"probe_id": "probe-20260823-001"},
        }

    monkeypatch.setattr(receipt_module, "get_claimed_verification_run", fake_claimed)
    monkeypatch.setattr(receipt_module, "get_verification_run_for_worker", fake_previous)
    result = asyncio.run(
        receipt_module.receive_ucp_probe_receipt(_receipt(), x_internal_key="test-key")
    )
    assert result.verification_status == "succeeded"
    assert result.execution_route_id == "route-1"


def test_claim_returns_only_domain_and_claim_context(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")
    observed = {}

    async def fake_claim(**kwargs):
        observed["claim"] = kwargs
        return {
            "verify_id": "verify-1",
            "audit_run_id": "audit-1",
            "execution_route_id": "route-1",
            "retry_count": 1,
            "product_key": "gid://shopify/ProductVariant/123",
        }

    async def fake_route(**kwargs):
        observed["route"] = kwargs
        return {"normalized_domain": "shop.example"}

    monkeypatch.setattr(receipt_module, "claim_next_pending_verification", fake_claim)
    monkeypatch.setattr(receipt_module, "fetch_execution_route", fake_route)
    response = type("ResponseStub", (), {"status_code": 200})()
    result = asyncio.run(receipt_module.claim_ucp_probe(
        receipt_module.UcpProbeClaimRequest(worker_id="worker-1"),
        response=response,
        x_internal_key="test-key",
    ))
    assert result.audit_run_id == "audit-1"
    assert result.probe_id == "verify-1:attempt:2"
    assert result.brand_domain == "shop.example"
    assert result.variant_gid == "gid://shopify/ProductVariant/123"
    assert observed["claim"]["verifier_id"] == "ucp_probe"
