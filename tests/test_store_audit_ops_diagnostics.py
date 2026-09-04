"""The admin-only Store Audit lane diagnostics.

This endpoint exists because verification_runs.error_message is the ONLY place
a blocked probe's reason is recorded, and Cloud SQL is private-IP only. It reads
production diagnostic state, so the two things worth pinning are that it refuses
everyone who is not an admin, and that it cannot become a way to read the
payload fields the receipt path refuses to store.

Tokens are REAL signed JWTs. The `test-token` placeholder has a pytest-only
bypass in utils.auth that returns role=admin, which would make every refusal
claim here vacuous.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

_DIAG = "/ops/store-audit/domain-diagnostics"
_COVERAGE = "/ops/store-audit/checkout-tier-coverage"

_ADMITTED = ("super_admin", "admin")
_REFUSED = ("employee", "outsourced", "agent", "merchant", "buyer")


@pytest.fixture(scope="module")
def client() -> TestClient:
    import main

    return TestClient(main.app)


def _token(role: str) -> str:
    from utils.auth import create_access_token

    return create_access_token(
        {"sub": f"u-{role}", "email": f"{role}@example.com", "role": role}
    )


@pytest.fixture
def no_rows(monkeypatch):
    """Drive the real handler with an empty lane so the ADMIT tests exercise
    the shipped path rather than a stub of it."""
    from routes import store_audit_ops as mod

    calls: List[Dict[str, Any]] = []

    async def fake_history(**kwargs):
        calls.append(kwargs)
        return []

    async def fake_coverage():
        return {"active_ucp_routes": 0, "routes_with_proven_merchant": 0}

    monkeypatch.setattr(mod, "fetch_verification_history_for_domain", fake_history)
    monkeypatch.setattr(mod, "summarize_ucp_route_merchant_coverage", fake_coverage)
    return calls


# ---------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------


@pytest.mark.parametrize("role", _REFUSED)
def test_non_admin_roles_are_refused_on_both_endpoints(client, role, no_rows):
    headers = {"Authorization": f"Bearer {_token(role)}"}
    assert client.get(_DIAG, params={"domain": "shop.example"},
                      headers=headers).status_code == 403
    assert client.get(_COVERAGE, headers=headers).status_code == 403
    # Refused before any lookup ran — not merely filtered out of the response.
    assert no_rows == []


def test_an_unauthenticated_caller_is_refused(client, no_rows):
    assert client.get(_DIAG, params={"domain": "shop.example"}).status_code in (401, 403)
    assert client.get(_COVERAGE).status_code in (401, 403)
    assert no_rows == []


@pytest.mark.parametrize("role", _ADMITTED)
def test_admin_roles_reach_the_handler(client, role, no_rows):
    headers = {"Authorization": f"Bearer {_token(role)}"}
    res = client.get(_DIAG, params={"domain": "shop.example"}, headers=headers)
    assert res.status_code == 200
    assert res.json()["domain"] == "shop.example"
    # The positive counterpart to the refusals above: the lookup actually ran.
    assert len(no_rows) == 1


# ---------------------------------------------------------------------
# What it will not hand back
# ---------------------------------------------------------------------


def test_sensitive_payload_keys_are_redacted(client, monkeypatch):
    """Keyed on the SAME set the receipt path refuses writes on. A key this
    system distrusts on the way in must not be readable on the way out."""
    from routes import store_audit_ops as mod

    async def fake_history(**_kwargs):
        return [{
            "verify_id": "v1", "audit_run_id": "a1", "status": "blocked",
            "error_message": "tool_error",
            "evidence_jsonb": {
                "stage": "verifier_execute",
                "token": "super-secret-bearer",
                "cookies": {"session": "abc"},
                "rawResponse": {"body": "..."},
                "nested": [{"authorization": "Bearer xyz"}],
                "reason": "profile_redirected",
            },
            "retry_count": 1, "max_retries": 1, "product_key": None,
            "route_kind": "ucp", "is_active": True, "route_merchant_id": None,
            "claimed_by_worker": "ucp-crawl-1",
            "created_at": None, "completed_at": None,
        }]

    monkeypatch.setattr(mod, "fetch_verification_history_for_domain", fake_history)
    res = client.get(_DIAG, params={"domain": "shop.example"},
                     headers={"Authorization": f"Bearer {_token('admin')}"})
    assert res.status_code == 200
    ev = res.json()["attempts"][0]["evidence"]
    assert ev["token"] == "[redacted]"
    assert ev["cookies"] == "[redacted]"
    assert ev["rawResponse"] == "[redacted]"
    assert ev["nested"][0]["authorization"] == "[redacted]"
    # The diagnosis itself must survive — redacting it would defeat the point.
    assert ev["reason"] == "profile_redirected"
    assert ev["stage"] == "verifier_execute"
    assert "super-secret-bearer" not in res.text
    assert "Bearer xyz" not in res.text


def test_the_variant_id_is_reported_as_a_boolean_never_a_value(client, monkeypatch):
    """Whether a variant was carried is the diagnostic fact; the value is a
    merchant's product identifier and has no business on this wire."""
    from routes import store_audit_ops as mod

    async def fake_history(**_kwargs):
        return [{
            "verify_id": "v1", "audit_run_id": "a1", "status": "succeeded",
            "error_message": None, "evidence_jsonb": None,
            "retry_count": 0, "max_retries": 1,
            "product_key": "gid://shopify/ProductVariant/51086327775448",
            "route_kind": "ucp", "is_active": True, "route_merchant_id": "m1",
            "claimed_by_worker": None, "created_at": None, "completed_at": None,
        }]

    monkeypatch.setattr(mod, "fetch_verification_history_for_domain", fake_history)
    res = client.get(_DIAG, params={"domain": "shop.example"},
                     headers={"Authorization": f"Bearer {_token('admin')}"})
    assert res.json()["attempts"][0]["carried_variant"] is True
    assert "51086327775448" not in res.text


# ---------------------------------------------------------------------
# It must not lie about an empty answer
# ---------------------------------------------------------------------


def test_an_empty_result_says_it_cannot_tell_never_probed_from_lookup_failure(
    client, no_rows,
):
    """The underlying reader returns [] for BOTH, and a diagnostic that quietly
    conflates them is exactly the failure it was built to stop."""
    res = client.get(_DIAG, params={"domain": "shop.example"},
                     headers={"Authorization": f"Bearer {_token('admin')}"})
    note = res.json()["note"].lower()
    assert "never been probed" in note and "lookup failed" in note


def test_a_failed_coverage_lookup_is_not_reported_as_zero(client, monkeypatch):
    """`0 routes` is a finding; a failed query that RENDERS as 0 is a lie that
    would be read as "nothing to enable"."""
    from routes import store_audit_ops as mod

    async def failing():
        return {"active_ucp_routes": -1, "routes_with_proven_merchant": -1}

    monkeypatch.setattr(mod, "summarize_ucp_route_merchant_coverage", failing)
    res = client.get(_COVERAGE, headers={"Authorization": f"Bearer {_token('admin')}"})
    body = res.json()
    assert body["active_ucp_routes"] == -1
    assert "not measurements" in body["note"].lower()


# ---------------------------------------------------------------------
# Domain normalization — the only free input
# ---------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("https://Shop.Example/path?q=1", "shop.example"),
    ("www.shop.example", "shop.example"),
    ("HTTP://WWW.Shop.Example:443", "shop.example"),
    ("shop.example.", "shop.example"),
    ("user@shop.example", "shop.example"),
])
def test_domain_is_normalized_to_the_host_the_lane_keys_on(raw, expected):
    from routes.store_audit_ops import _normalize_domain

    assert _normalize_domain(raw) == expected


def test_a_domain_that_normalizes_to_nothing_never_queries(client, no_rows):
    res = client.get(_DIAG, params={"domain": "://"},
                     headers={"Authorization": f"Bearer {_token('admin')}"})
    assert res.status_code == 200
    assert res.json()["attempts"] == []
    assert no_rows == []
