"""
Federated buyer identity — an agent registers its own user-token issuer.

Portal (self-serve, agent-portal JWT; same owner-or-admin rule as routes/agent_keys.py):
  GET    /agents/{agent_id}/identity-issuers          list the agent's issuers
  PUT    /agents/{agent_id}/identity-issuers          register / replace one (validates shape
                                                      + dereferences the JWKS before storing)
  DELETE /agents/{agent_id}/identity-issuers/{id}     disable one

Gateway (server-to-server, X-Internal-Key = AGENT_AUTH_INTROSPECT_INTERNAL_KEY — the key the
gateway already holds for API-key introspection, so no new secret):
  GET    /agent/internal/identity-issuers             every ACTIVE (agent_id, issuer, jwks,
                                                      aud, algs, azp, scopes) binding

What this buys: a Minds user token (minted by Minds' issuer) is accepted on create_checkout
when presented with Minds' API key — the user never leaves Minds' UI for a Pivota sign-in.
What it must never do: accept issuer X's tokens from agent Y. The binding is the row.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from db import agent_identity_issuers as store
from db.agent_identity_issuers import IssuerValidationError
from utils.auth import get_current_user

router = APIRouter(tags=["agent-identity-issuers"])
internal_router = APIRouter(prefix="/agent/internal", tags=["agent-internal-auth"])


class IssuerRegistrationBody(BaseModel):
    issuer: str
    jwks_uri: str
    audience: str
    algs: Optional[List[str]] = None
    authorized_party: Optional[str] = None
    required_scopes: Optional[List[str]] = None


def _require_owner_or_admin(current_user: Dict[str, Any], agent_id: str) -> None:
    user_agent_id = current_user.get("agent_id") or current_user.get("email")
    if user_agent_id != agent_id and current_user.get("role") not in ["admin", "employee"]:
        raise HTTPException(status_code=403, detail="Not authorized to manage identity issuers for this agent")


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) else None


def _public(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for k in ("last_jwks_ok_at", "created_at", "updated_at"):
        out[k] = _iso(out.get(k))
    return out


@router.get("/agents/{agent_id}/identity-issuers")
async def list_identity_issuers(agent_id: str, current_user: dict = Depends(get_current_user)):
    _require_owner_or_admin(current_user, agent_id)
    rows = await store.list_issuers_for_agent(agent_id)
    return {
        "status": "success",
        "issuers": [_public(r) for r in rows],
        # What the agent's backend sends, spelled out once so the portal and the docs agree.
        "how_to_present": {
            "header": "X-Agent-User-JWT",
            "alongside": "X-Agent-API-Key (the agent's own key)",
            "token_requirements": {
                "iss": "exactly the registered issuer",
                "aud": "exactly the registered audience",
                "alg": "one of the registered algs (asymmetric)",
                "claims": ["iss", "sub", "aud", "exp", "iat"],
                "kid": "present and resolvable in the registered JWKS",
            },
        },
    }


@router.put("/agents/{agent_id}/identity-issuers")
async def register_identity_issuer(
    agent_id: str,
    body: IssuerRegistrationBody,
    current_user: dict = Depends(get_current_user),
):
    _require_owner_or_admin(current_user, agent_id)
    try:
        reg = store.normalize_registration(body.model_dump())
    except IssuerValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "INVALID_ISSUER_REGISTRATION", "field": exc.field, "message": str(exc)},
        )

    # One active owner per issuer string. A second agent claiming the same issuer is refused
    # rather than silently sharing a binding (that is exactly the ambiguity this table removes).
    owner = await store.find_active_owner(reg.issuer)
    if owner and owner != agent_id:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ISSUER_ALREADY_REGISTERED",
                "message": "This issuer is already registered to another agent. Contact support if you own it.",
            },
        )

    # Dereference the JWKS NOW: an issuer whose keys cannot be fetched is an issuer whose tokens
    # can never verify, and the time to say so is at registration, not at the first checkout.
    try:
        await store.dereference_jwks(reg.jwks_uri)
    except IssuerValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "JWKS_UNREACHABLE", "field": exc.field, "message": str(exc)},
        )

    row = await store.upsert_issuer(agent_id, reg, jwks_ok=True)
    return {"status": "success", "issuer": _public(row)}


@router.delete("/agents/{agent_id}/identity-issuers/{issuer_id}")
async def disable_identity_issuer(agent_id: str, issuer_id: int, current_user: dict = Depends(get_current_user)):
    _require_owner_or_admin(current_user, agent_id)
    ok = await store.disable_issuer(agent_id, issuer_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "ISSUER_NOT_FOUND"})
    return {"status": "success", "disabled": issuer_id}


# ── internal registry (gateway) ───────────────────────────────────────────────

def _expected_internal_key() -> str:
    return str(os.getenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY") or "").strip()


def _require_internal_key(x_internal_key: Optional[str]) -> None:
    expected = _expected_internal_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "CONFIG_MISSING", "message": "AGENT_AUTH_INTROSPECT_INTERNAL_KEY is not configured"},
        )
    provided = str(x_internal_key or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "FORBIDDEN", "message": "Missing or invalid X-Internal-Key"},
        )


@internal_router.get("/identity-issuers")
async def internal_identity_issuer_registry(x_internal_key: Optional[str] = Header(None, alias="X-Internal-Key")):
    """Every ACTIVE agent↔issuer binding, in the gateway verifier's issuer-entry shape."""
    _require_internal_key(x_internal_key)
    rows = await store.list_active_registry()
    return {
        "status": "success",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "issuers": [
            {
                "agent_id": r["agent_id"],
                "iss": r["issuer"],
                "jwksUri": r["jwks_uri"],
                "aud": r["audience"],
                "algs": r["algs"],
                "azp": r.get("authorized_party"),
                "requiredScopes": r.get("required_scopes"),
                "updated_at": _iso(r.get("updated_at")),
            }
            for r in rows
        ],
    }
