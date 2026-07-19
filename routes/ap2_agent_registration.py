"""
AP2 agent signing-key registration (#1442) — Blocker 1 for `ENABLE_AP2_ROUTES`.

Persists an agent's ES256/Ed25519 **signing** public key into `agents.public_key`
so `POST /ap2/consent/grant` (which resolves the key via
`consent_service.get_agent_identity`) stops failing closed with
"Agent has no registered public key". This is the AP2 **signing** key — NOT a PSP
publishable key.

Security: the caller authenticates with its OWN agent credential
(`Depends(get_agent_context)` → the `ak_live_…` API key / checkout token), and the
key is written for **that authenticated agent only**. Any `agent_id` in the body is
ignored, so one agent can never register (and thereby control signature
verification for) another agent's identity.

This surface is **always mounted** and independent of `ENABLE_AP2_ROUTES` —
provisioning must happen BEFORE the AP2 flag is flipped.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status

from routes.agent_auth import AgentContext, get_agent_context
from services.crypto_service import crypto_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/ap2", tags=["AP2"])

_SUPPORTED_ALGORITHMS = ("ES256", "Ed25519")


@router.post("/signing-key")
async def register_signing_key(
    body: dict = Body(...),
    context: AgentContext = Depends(get_agent_context),
):
    """Register (or rotate) the calling agent's AP2 signing public key.

    Body (JSON): `public_key` (required — PEM SubjectPublicKeyInfo for ES256;
    PEM / base64-32 / raw-32 for Ed25519), `algorithm` (optional, default `ES256`).

    The key is bound to the **authenticated** agent (`context.agent_id`); any
    `agent_id` in the body is ignored. 400 on a missing/invalid key or unsupported
    algorithm; 503 if the write fails (e.g. the AP2 agent schema — migration 021 —
    is not applied on this environment).
    """
    from db.agents import set_agent_public_key

    agent_id = context.agent_id
    public_key = str((body or {}).get("public_key") or "").strip()
    algorithm = str((body or {}).get("algorithm") or "ES256").strip()

    if not public_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing public_key")
    if algorithm not in _SUPPORTED_ALGORITHMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported algorithm {algorithm!r} (use ES256 or Ed25519)",
        )
    # Validate with the SAME loader the verify path uses — a stored key that passes
    # here is provably usable, so an agent can't register a broken key that then
    # 401s every grant.
    try:
        crypto_service.validate_public_key(public_key, algorithm)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid public_key: {e}")

    try:
        await set_agent_public_key(agent_id, public_key)
    except Exception as e:  # never leak internals; hint at the likely schema gap
        logger.error("ap2 signing-key registration write failed agent=%s err=%s", agent_id, str(e)[:200])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not persist the signing key (is the AP2 agent schema applied? migration 021_ap2_security.sql)",
        )

    logger.info("ap2 signing-key registered agent=%s algorithm=%s", agent_id, algorithm)
    return {"status": "registered", "agent_id": agent_id, "algorithm": algorithm}
