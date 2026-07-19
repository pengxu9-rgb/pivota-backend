"""
AP2 agent signing-key backfill (#1442) — first-party/partner **pilot** provisioning.

ADR-012 roots the AP2 signed rail in **self-sovereign DID/VC identity, never a
platform-issued secret** — an external agent presents a `did:key`/`did:web`, and
Pivota resolves the key from it with no per-agent approval. A raw ES256/Ed25519 PEM
in `agents.public_key` survives ONLY as the carve-out ADR-012 explicitly permits:
"an explicit first-party/partner **pilot** backfill, never the external model."

So this is an **admin** endpoint (not agent self-serve): an operator provisions a
raw signing PEM for a named pilot agent. It writes the SAME `agents.public_key`
column `consent_service.get_agent_identity` reads (used only when `agents.did` is
unset). Always mounted, independent of `ENABLE_AP2_ROUTES` — provisioning precedes
the flip.

For the scalable external path — approve a handful of trusted ISSUERS, not each
agent — see the DID + mandate model in `docs/AP2_ENABLEMENT.md` §2 / ADR-012.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status

from services.crypto_service import crypto_service
from utils.auth import get_current_employee

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ap2", tags=["AP2", "Admin"])

_SUPPORTED_ALGORITHMS = ("ES256", "Ed25519")


@router.post("/agent-signing-key")
async def admin_set_agent_signing_key(
    body: dict = Body(...),
    admin: dict = Depends(get_current_employee),
):
    """Admin backfill of a pilot agent's AP2 signing public key (#1442).

    Body (JSON): `agent_id` (required — the pilot agent to provision), `public_key`
    (required — PEM SubjectPublicKeyInfo for ES256; PEM / base64-32 / raw-32 for
    Ed25519), `algorithm` (optional, default `ES256`).

    Admin-gated (`get_current_employee`). 400 on a missing/invalid field; 404 if no
    such agent; 503 if the write fails (e.g. the AP2 agent schema — migration 021 —
    is not applied on this environment).
    """
    from db.agents import get_agent, set_agent_public_key

    agent_id = str((body or {}).get("agent_id") or "").strip()
    public_key = str((body or {}).get("public_key") or "").strip()
    algorithm = str((body or {}).get("algorithm") or "ES256").strip()

    if not agent_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing agent_id")
    if not public_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing public_key")
    if algorithm not in _SUPPORTED_ALGORITHMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported algorithm {algorithm!r} (use ES256 or Ed25519)",
        )
    # Validate with the SAME loader the verify path uses — a stored key that passes
    # here is provably usable, so an operator can't provision a broken key that then
    # 401s every grant.
    try:
        crypto_service.validate_public_key(public_key, algorithm)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid public_key: {e}")

    if await get_agent(agent_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"No such agent: {agent_id}")

    try:
        await set_agent_public_key(agent_id, public_key)
    except Exception as e:  # never leak internals; hint at the likely schema gap
        logger.error("ap2 signing-key backfill write failed agent=%s err=%s", agent_id, str(e)[:200])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not persist the signing key (is the AP2 agent schema applied? migration 021_ap2_security.sql)",
        )

    logger.info(
        "ap2 signing-key backfilled agent=%s algorithm=%s by_admin=%s",
        agent_id, algorithm, (admin or {}).get("id") or (admin or {}).get("employee_id") or "?",
    )
    return {"status": "registered", "agent_id": agent_id, "algorithm": algorithm}
