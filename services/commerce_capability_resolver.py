"""Resolve Store Audit evidence into conservative Commerce Index capability.

This module is deliberately pure: workers/receipts create redacted evidence,
while this resolver decides what agents may claim or attempt.  In particular,
cart success is never treated as checkout or payment authorization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

ROUTE_DISCOVERY_ONLY = "discovery_only"
ROUTE_MERCHANT_HANDOFF = "merchant_handoff"
ROUTE_USER_TAKEOVER_REQUIRED = "user_takeover_required"
ROUTE_AGENT_CHECKOUT_ELIGIBLE = "agent_checkout_eligible"

EVIDENCE_PLATFORM = "commerce_platform"
EVIDENCE_CHECKOUT_ROUTE = "commerce_checkout_route"
EVIDENCE_CARTABILITY = "commerce_cartability"
EVIDENCE_INTEGRATION_AUTHORIZATION = "commerce_integration_authorization"


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _active(items: Iterable[Dict[str, Any]], now: datetime) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for item in items:
        expires = item.get("expires_at")
        if not isinstance(expires, datetime) or _utc(expires) <= now:
            continue
        out.append(item)
    return sorted(out, key=lambda item: _utc(item.get("created_at")), reverse=True)


def _latest(items: Iterable[Dict[str, Any]], evidence_type: str) -> Optional[Dict[str, Any]]:
    return next((item for item in items if item.get("evidence_type") == evidence_type), None)


def resolve_merchant_capability(
    *, merchant_id: str, evidence: Iterable[Dict[str, Any]], now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return a capability snapshot suitable for agent routing.

    ``agent_checkout_eligible`` requires an explicit merchant-authorized
    integration evidence item. Public storefront evidence can at most yield a
    merchant handoff; a security challenge requires user takeover.
    """
    if not merchant_id:
        raise ValueError("merchant_id is required")
    at = _utc(now)
    current = _active(evidence, at)
    platform = _latest(current, EVIDENCE_PLATFORM)
    checkout = _latest(current, EVIDENCE_CHECKOUT_ROUTE)
    authorization = _latest(current, EVIDENCE_INTEGRATION_AUTHORIZATION)
    platform_payload = dict((platform or {}).get("payload_jsonb") or {})
    checkout_payload = dict((checkout or {}).get("payload_jsonb") or {})
    authorization_payload = dict((authorization or {}).get("payload_jsonb") or {})
    checkout_status = str(checkout_payload.get("status") or "unknown")
    challenge = checkout_payload.get("challenge_stage")
    authorized = (
        (authorization or {}).get("confidence") == 100
        and authorization_payload.get("agent_checkout_authorized") is True
        and authorization_payload.get("authorization_scope") == "merchant_authorized"
    )
    if authorized:
        policy = ROUTE_AGENT_CHECKOUT_ELIGIBLE
    elif challenge or checkout_status in {"security_challenged", "security_challenged_pre_address", "blocked"}:
        policy = ROUTE_USER_TAKEOVER_REQUIRED
    elif checkout_status == "guest_route_detected":
        policy = ROUTE_MERCHANT_HANDOFF
    else:
        policy = ROUTE_DISCOVERY_ONLY
    return {
        "merchant_id": merchant_id,
        "commerce_platform": str(platform_payload.get("platform") or "unknown"),
        "checkout_provider": str(platform_payload.get("checkout_provider") or "unknown"),
        "guest_checkout_mode": checkout_status,
        "security_challenge_mode": challenge,
        "integration_mode": (
            str(authorization_payload.get("mode") or "authorized_integration")
            if authorized else "public_storefront"
        ),
        "agent_route_policy": policy,
        "payment_capability": (
            "merchant_authorized_revalidation_required" if authorized else "unverified"
        ),
        "evidence_ids": [
            str(item.get("evidence_id"))
            for item in (platform, checkout, authorization) if item
        ],
        "next_checkout_audit_at": (checkout or {}).get("expires_at"),
    }


def checkout_audit_decision(
    *, merchant_id: str, evidence: Iterable[Dict[str, Any]], now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Schedule one checkout-route audit per merchant, never per SKU."""
    if not merchant_id:
        raise ValueError("merchant_id is required")
    current = _active(evidence, _utc(now))
    checkout = _latest(current, EVIDENCE_CHECKOUT_ROUTE)
    if checkout:
        return {
            "should_audit": False,
            "reason": "fresh_merchant_checkout_evidence",
            "next_eligible_at": checkout.get("expires_at"),
            "evidence_id": checkout.get("evidence_id"),
        }
    return {
        "should_audit": True,
        "reason": "missing_or_expired_merchant_checkout_evidence",
    }
