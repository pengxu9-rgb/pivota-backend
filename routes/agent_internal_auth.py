"""
Internal agent auth introspection endpoints.

This router is server-to-server only and must be protected by X-Internal-Key.
It reuses db.agents.get_agent_by_key so key validation stays consistent with
Agent Portal / Employee Portal managed keys, and routes.agent_auth's
verify_checkout_token_claims so a checkout token is judged by the same function
that authenticates it on the agent API.
"""

from __future__ import annotations

import hmac
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from db.agents import AgentAuthLookupTransientError, agent_is_active, get_agent, get_agent_by_key
from routes.agent_auth import verify_checkout_token_claims
from utils.transient_errors import db_busy_http_exception

router = APIRouter(prefix="/agent/internal/auth", tags=["agent-internal-auth"])

_API_KEY_PATTERN = re.compile(r"^ak_(live_)?[0-9a-f]{64}$")


class IntrospectRequest(BaseModel):
    # Exactly one of the two. `api_key` is the original contract; `checkout_token` lets the gateway
    # verify an X-Checkout-Token it cannot check itself (it does not hold CHECKOUT_TOKEN_SECRET).
    api_key: Optional[str] = None
    checkout_token: Optional[str] = None


class IntrospectResponse(BaseModel):
    valid: bool
    agent_id: Optional[str] = None
    is_active: Optional[bool] = None
    auth_source: Optional[str] = None
    # Checkout-token verdicts only. `scopes` is what the token was minted for (every mint today is
    # ["checkout"]); the gateway enforces it, as get_agent_context does for agent-API paths.
    scopes: Optional[List[str]] = None
    merchant_ids: Optional[List[str]] = None
    expires_at: Optional[int] = None


def _expected_internal_key() -> str:
    return str(os.getenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY") or "").strip()


def _require_internal_key(x_internal_key: Optional[str]) -> None:
    expected = _expected_internal_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "CONFIG_MISSING",
                "message": "AGENT_AUTH_INTROSPECT_INTERNAL_KEY is not configured",
            },
        )
    provided = str(x_internal_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Missing or invalid X-Internal-Key"},
        )


@router.post("/introspect", response_model=IntrospectResponse)
async def introspect_agent_api_key(
    payload: IntrospectRequest,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> IntrospectResponse:
    _require_internal_key(x_internal_key)

    raw_checkout_token = str(payload.checkout_token or "").strip()
    if raw_checkout_token:
        if str(payload.api_key or "").strip():
            # Two credentials in one question has no single answer; refuse rather than pick one.
            return IntrospectResponse(valid=False, auth_source="ambiguous")
        return await _introspect_checkout_token(raw_checkout_token)

    raw_key = str(payload.api_key or "").strip()
    if not raw_key:
        return IntrospectResponse(valid=False, auth_source="missing")

    # Keep the same external key format contract as get_agent_context.
    if not _API_KEY_PATTERN.match(raw_key):
        return IntrospectResponse(valid=False, auth_source="format_invalid")

    metrics: Dict[str, Any] = {}
    try:
        agent = await get_agent_by_key(raw_key, metrics_out=metrics)
    except AgentAuthLookupTransientError:
        raise db_busy_http_exception()
    if not agent:
        return IntrospectResponse(
            valid=False,
            auth_source=str(metrics.get("auth_source") or "not_found"),
        )

    return IntrospectResponse(
        valid=True,
        agent_id=str(agent.get("agent_id") or "").strip() or None,
        is_active=agent_is_active(agent),
        auth_source=str(metrics.get("auth_source") or "unknown"),
    )


def _string_list(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    return [str(v).strip() for v in value if str(v or "").strip()]


async def _introspect_checkout_token(token: str) -> IntrospectResponse:
    try:
        claims = verify_checkout_token_claims(token)
    except HTTPException as exc:
        if exc.status_code >= 500:
            # Secret not configured: our failure, not a verdict on the token. The gateway reads a 5xx
            # as "introspection unavailable" and refuses the request, which is the fail-closed answer.
            raise
        return IntrospectResponse(valid=False, auth_source="checkout_token_invalid")

    agent_id = str(claims.get("agent_id") or "").strip()
    agent = await get_agent(agent_id)
    if not agent:
        return IntrospectResponse(valid=False, auth_source="checkout_token_agent_not_found")

    exp = claims.get("exp")
    return IntrospectResponse(
        valid=True,
        agent_id=agent_id,
        is_active=agent_is_active(agent),
        auth_source="checkout_token",
        scopes=_string_list(claims.get("scopes")),
        merchant_ids=_string_list(claims.get("merchant_ids")),
        expires_at=int(exp) if exp else None,
    )
