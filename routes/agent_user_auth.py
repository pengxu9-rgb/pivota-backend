"""
Agent-user identity bridging for Agent API routes.

This is distinct from Agent API key authentication:
- Agent API key (X-API-Key) authenticates the *calling agent/tool*.
- Agent user JWT (X-Agent-User-JWT) authenticates the *end-user* of that tool.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel

from services.agent_user_jwt import AgentUserJwtError, AgentUserIdentity, verify_agent_user_jwt


class AgentUserContext(BaseModel):
    agent_user_ref: str
    issuer: Optional[str] = None
    subject: Optional[str] = None


def get_agent_user_identity(
    x_agent_user_jwt: Optional[str] = Header(None, alias="X-Agent-User-JWT"),
) -> Optional[AgentUserIdentity]:
    token = (x_agent_user_jwt or "").strip()
    if not token:
        return None
    try:
        return verify_agent_user_jwt(token)
    except AgentUserJwtError as e:
        msg = str(e) or "Invalid agent user token"
        if "missing jwks configuration" in msg.lower():
            raise HTTPException(
                status_code=500,
                detail={"error": "AGENT_USER_JWKS_NOT_CONFIGURED", "message": "Agent user JWKS not configured"},
            )
        # Do not echo token or claims back to callers.
        raise HTTPException(
            status_code=401,
            detail={"error": "AGENT_USER_JWT_INVALID", "message": msg},
        )


def get_agent_user_context(
    ident: Optional[AgentUserIdentity] = None,
    x_agent_user_jwt: Optional[str] = Header(None, alias="X-Agent-User-JWT"),
) -> Optional[AgentUserContext]:
    if ident is None:
        ident = get_agent_user_identity(x_agent_user_jwt=x_agent_user_jwt)
    if ident is None:
        return None
    return AgentUserContext(agent_user_ref=ident.agent_user_ref, issuer=ident.issuer, subject=ident.subject)
