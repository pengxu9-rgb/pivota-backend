"""
Internal agent auth introspection endpoints.

This router is server-to-server only and must be protected by X-Internal-Key.
It reuses db.agents.get_agent_by_key so key validation stays consistent with
Agent Portal / Employee Portal managed keys.
"""

from __future__ import annotations

import hmac
import os
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from db.agents import get_agent_by_key

router = APIRouter(prefix="/agent/internal/auth", tags=["agent-internal-auth"])

_API_KEY_PATTERN = re.compile(r"^ak_(live_)?[0-9a-f]{64}$")


class IntrospectRequest(BaseModel):
    api_key: str


class IntrospectResponse(BaseModel):
    valid: bool
    agent_id: Optional[str] = None
    is_active: Optional[bool] = None
    auth_source: Optional[str] = None


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


def _resolve_is_active(agent: Dict[str, Any]) -> bool:
    if not isinstance(agent, dict):
        return False
    is_active = agent.get("is_active")
    if is_active is None:
        status_value = agent.get("status")
        if status_value is None:
            return True
        return str(status_value).strip().lower() == "active"
    return bool(is_active)


@router.post("/introspect", response_model=IntrospectResponse)
async def introspect_agent_api_key(
    payload: IntrospectRequest,
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key"),
) -> IntrospectResponse:
    _require_internal_key(x_internal_key)

    raw_key = str(payload.api_key or "").strip()
    if not raw_key:
        return IntrospectResponse(valid=False, auth_source="missing")

    # Keep the same external key format contract as get_agent_context.
    if not _API_KEY_PATTERN.match(raw_key):
        return IntrospectResponse(valid=False, auth_source="format_invalid")

    metrics: Dict[str, Any] = {}
    agent = await get_agent_by_key(raw_key, metrics_out=metrics)
    if not agent:
        return IntrospectResponse(
            valid=False,
            auth_source=str(metrics.get("auth_source") or "not_found"),
        )

    return IntrospectResponse(
        valid=True,
        agent_id=str(agent.get("agent_id") or "").strip() or None,
        is_active=_resolve_is_active(agent),
        auth_source=str(metrics.get("auth_source") or "unknown"),
    )
