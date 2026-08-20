"""
Agent-user identity bridging for Agent API routes.

This is distinct from Agent API key authentication:
- Agent API key (X-API-Key) authenticates the *calling agent/tool*.
- Agent user JWT (X-Agent-User-JWT) authenticates the *end-user* of that tool.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

from db.agents import resolve_agent_id_by_api_key
from services.agent_user_jwt import AgentUserJwtError, AgentUserIdentity, verify_agent_user_jwt_for_agent


class AgentUserContext(BaseModel):
    agent_user_ref: str
    issuer: Optional[str] = None
    subject: Optional[str] = None


async def get_agent_user_identity(
    x_agent_user_jwt: Optional[str] = Header(None, alias="X-Agent-User-JWT"),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> Optional[AgentUserIdentity]:
    token = (x_agent_user_jwt or "").strip()
    if not token:
        return None
    # The CALLING agent decides which federated issuers are acceptable: a user token from an issuer
    # registered to agent A is accepted only with agent A's key. Resolved through the same cached
    # hash lookup the API-key auth itself uses, so this adds no second source of truth for "who
    # is calling". No key (or an unknown key) ⇒ only the global env issuer can verify.
    agent_id = await resolve_agent_id_by_api_key(x_api_key) if x_api_key else None
    try:
        return await verify_agent_user_jwt_for_agent(token, agent_id)
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
    x_agent_user_jwt: Optional[str] = Header(None, alias="X-Agent-User-JWT"),
    ident: Optional[AgentUserIdentity] = Depends(get_agent_user_identity),
) -> Optional[AgentUserContext]:
    if ident is None:
        return None
    return AgentUserContext(agent_user_ref=ident.agent_user_ref, issuer=ident.issuer, subject=ident.subject)
