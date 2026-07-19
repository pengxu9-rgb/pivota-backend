"""
AP2 trusted-issuer enrollment (#1495) — the "approve issuers, not agents" surface.

`ap2_mandate.verify_mandate_chain` only honors a mandate whose ISSUER DID is in the
presenting agent's trusted set. That set is the union of the agent's own issuers
and a platform-**global** tier (`db.ap2_trusted_issuers`). A global issuer is
trusted to authorize ANY agent's mandate — so a frontier / app platform is enrolled
ONCE and covers its whole agent fleet, with no per-agent approval (ADR-012).

Admin-gated (`get_current_employee`), with **DID-resolution proof-of-control**: a
`did:web` issuer is trusted only if its domain serves a `.well-known/did.json`
resolving to that DID (proof the domain controls it); a `did:key` is
self-certifying. Always mounted, independent of `ENABLE_AP2_ROUTES` — trust
provisioning precedes the flip. Revocation is effective immediately (the
transaction route reads the trusted set live per request).
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from services.ap2_identity import is_did, resolve_agent_identity
from utils.auth import get_current_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ap2/trusted-issuers", tags=["AP2", "Admin"])

_SCOPES = ("global", "agent")


def _admin_id(admin: dict) -> str:
    a = admin or {}
    return a.get("sub") or a.get("employeeId") or a.get("employee_id") or a.get("id") or "?"


@router.post("")
async def enroll_trusted_issuer(
    body: dict = Body(...),
    admin: dict = Depends(get_current_employee),
):
    """Enroll a trusted issuer (#1495). Body (JSON): `issuer_did` (required, a DID),
    `scope` (`global` default | `agent`), `agent_id` (required when scope=`agent`).

    Admin-gated. Resolves the issuer DID as proof-of-control before trusting it —
    400 if it doesn't resolve. 404 if a scope=`agent` target agent is unknown.
    """
    from db.agents import get_agent
    from db.ap2_trusted_issuers import add_global_trusted_issuer, add_trusted_issuer

    issuer_did = str((body or {}).get("issuer_did") or "").strip()
    # scope is REQUIRED (no default): a scope-less request must not silently grant
    # the broadest trust. 'global' is fleet-wide; 'agent' is a single binding.
    scope = str((body or {}).get("scope") or "").strip().lower()
    agent_id = str((body or {}).get("agent_id") or "").strip()

    if not issuer_did or not is_did(issuer_did):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="issuer_did must be a DID (did:key:… / did:web:…)")
    if scope not in _SCOPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="scope is required and must be 'global' or 'agent'")
    if scope == "agent" and not agent_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="scope='agent' requires agent_id")

    # Proof-of-control: resolve the issuer DID before trusting it. A did:web resolves
    # ONLY if the domain serves a .well-known/did.json whose id == the DID (proof the
    # domain controls it — services/ap2_did_web.py); a did:key is self-certifying
    # (the key IS the id). Unresolvable → refuse, so a typo'd / dead / unowned issuer
    # is never trusted.
    try:
        await resolve_agent_identity(issuer_did)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"issuer DID does not resolve (proof-of-control failed): {str(e)[:200]}",
        )

    admin_id = _admin_id(admin)
    if scope == "agent":
        if await get_agent(agent_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No such agent: {agent_id}")
        await add_trusted_issuer(agent_id, issuer_did, metadata={"enrolled_by": admin_id})
    else:
        # A did:key's proof-of-control is vacuous (self-certifying — anyone can mint
        # one), so trusting a bare did:key FLEET-WIDE trusts whoever holds that key
        # to vouch for EVERY agent. Prefer did:web (domain-anchored) for a global
        # issuer; log loudly so the audit trail flags it.
        if issuer_did.startswith("did:key:"):
            logger.warning(
                "ap2 trusted-issuer GLOBAL enrollment of a bare did:key issuer=%s by_admin=%s "
                "— did:key proof-of-control is vacuous; a did:web issuer is preferred for a "
                "fleet-wide trust grant", issuer_did, admin_id,
            )
        await add_global_trusted_issuer(issuer_did, metadata={"enrolled_by": admin_id})

    logger.info(
        "ap2 trusted-issuer enrolled issuer=%s scope=%s agent=%s by_admin=%s",
        issuer_did, scope, agent_id or "-", admin_id,
    )
    return {"status": "trusted", "issuer_did": issuer_did, "scope": scope, "agent_id": agent_id or None}


@router.post("/revoke")
async def revoke_trusted_issuer_route(
    body: dict = Body(...),
    admin: dict = Depends(get_current_employee),
):
    """Revoke a trusted issuer (#1495). Body: `issuer_did` (required), `scope`
    (`global` default | `agent`), `agent_id` (required when scope=`agent`).
    Effective on the next transaction (the trusted set is read live). Revoking an
    un-trusted issuer is a harmless no-op."""
    from db.ap2_trusted_issuers import revoke_global_trusted_issuer, revoke_trusted_issuer

    issuer_did = str((body or {}).get("issuer_did") or "").strip()
    scope = str((body or {}).get("scope") or "").strip().lower()  # required (no default)
    agent_id = str((body or {}).get("agent_id") or "").strip()

    if not issuer_did:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing issuer_did")
    if scope not in _SCOPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="scope is required and must be 'global' or 'agent'")
    if scope == "agent" and not agent_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="scope='agent' requires agent_id")

    if scope == "agent":
        await revoke_trusted_issuer(agent_id, issuer_did)
    else:
        await revoke_global_trusted_issuer(issuer_did)

    logger.info(
        "ap2 trusted-issuer revoked issuer=%s scope=%s agent=%s by_admin=%s",
        issuer_did, scope, agent_id or "-", _admin_id(admin),
    )
    return {"status": "revoked", "issuer_did": issuer_did, "scope": scope, "agent_id": agent_id or None}


@router.get("")
async def list_trusted_issuers_route(
    agent_id: str = Query(default=None),
    admin: dict = Depends(get_current_employee),
):
    """List trusted issuers for ops/audit. With `agent_id` → that agent's rows plus
    the global tier; without → every row. Each entry: `{issuer_did, scope, agent_id,
    status}`."""
    from db.ap2_trusted_issuers import list_trusted_issuers

    return {"trusted_issuers": await list_trusted_issuers((agent_id or "").strip() or None)}
