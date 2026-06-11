"""The audit launch/preview endpoints resolve the portal's 'self' sentinel.

The merchant AI-Readiness portal sends merchant_id='self' (documenting that the
backend attaches the real merchant_id server-side). The endpoints must resolve
it to the authenticated merchant rather than 403, while still rejecting any
real cross-tenant merchant_id.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from routes.audit_runs_routes import _resolve_audit_merchant_id


def test_self_resolves_to_authenticated_merchant():
    assert _resolve_audit_merchant_id("self", "merch_abc") == "merch_abc"


def test_exact_match_passes_through():
    assert _resolve_audit_merchant_id("merch_abc", "merch_abc") == "merch_abc"


def test_cross_tenant_id_is_forbidden():
    with pytest.raises(HTTPException) as exc:
        _resolve_audit_merchant_id("merch_other", "merch_abc")
    assert exc.value.status_code == 403


def test_empty_non_self_value_is_forbidden():
    # Only the literal 'self' sentinel is special; anything else must match.
    with pytest.raises(HTTPException):
        _resolve_audit_merchant_id("", "merch_abc")
