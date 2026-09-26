from datetime import datetime, timedelta, timezone

from services.commerce_capability_resolver import (
    ROUTE_AGENT_CHECKOUT_ELIGIBLE,
    ROUTE_USER_TAKEOVER_REQUIRED,
    checkout_audit_decision,
    resolve_merchant_capability,
)


NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _item(evidence_type, payload, *, hours=24, confidence=None, evidence_id="e1"):
    return {
        "evidence_id": evidence_id,
        "evidence_type": evidence_type,
        "payload_jsonb": payload,
        "confidence": confidence,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=hours),
    }


def test_jolse_like_pre_address_challenge_requires_user_takeover():
    evidence = [
        _item("commerce_platform", {"platform": "cafe24", "checkout_provider": "cafe24"}),
        _item(
            "commerce_checkout_route",
            {"audit_scope": "merchant_checkout", "status": "security_challenged_pre_address", "challenge_stage": "pre_address"},
            evidence_id="route-evidence",
        ),
    ]

    resolved = resolve_merchant_capability(
        merchant_id="merchant:jolse", evidence=evidence, now=NOW,
    )

    assert resolved["agent_route_policy"] == ROUTE_USER_TAKEOVER_REQUIRED
    assert resolved["payment_capability"] == "unverified"
    assert resolved["evidence_ids"] == ["e1", "route-evidence"]
    assert checkout_audit_decision(
        merchant_id="merchant:jolse", evidence=evidence, now=NOW,
    )["should_audit"] is False


def test_only_explicit_merchant_authorization_allows_agent_checkout():
    evidence = [_item(
        "commerce_integration_authorization",
        {"agent_checkout_authorized": True, "authorization_scope": "merchant_authorized", "mode": "ucp"},
        confidence=100,
    )]

    resolved = resolve_merchant_capability(
        merchant_id="merchant:authorized", evidence=evidence, now=NOW,
    )

    assert resolved["agent_route_policy"] == ROUTE_AGENT_CHECKOUT_ELIGIBLE
    assert resolved["payment_capability"] == "merchant_authorized_revalidation_required"


def test_expired_checkout_evidence_is_rescheduled_once_per_merchant():
    expired = _item(
        "commerce_checkout_route", {"status": "guest_route_detected"}, hours=-1,
    )
    decision = checkout_audit_decision(
        merchant_id="merchant:expired", evidence=[expired], now=NOW,
    )

    assert decision == {
        "should_audit": True,
        "reason": "missing_or_expired_merchant_checkout_evidence",
    }
