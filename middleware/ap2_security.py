"""
AP2 Security Middleware
Validates AP2 protocol requests and enforces security policies
"""
import logging
from typing import Optional, Callable
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

class AP2SecurityMiddleware(BaseHTTPMiddleware):
    """
    Middleware for AP2 protocol security validation
    
    Features:
    - Validates agent consent tokens
    - Verifies request signatures
    - Enforces nonce uniqueness
    - Checks wallet authorization
    """
    
    def __init__(self, app: ASGIApp, enabled: bool = False):
        super().__init__(app)
        self.enabled = enabled
        if enabled:
            logger.info("✅ AP2 Security Middleware enabled")
        else:
            logger.info("⚠️ AP2 Security Middleware disabled (ENABLE_AP2_ROUTES=false)")
    
    async def dispatch(self, request: Request, call_next: Callable):
        """Process request through AP2 security validation"""
        
        # Skip if middleware is disabled
        if not self.enabled:
            return await call_next(request)
        
        # Only validate AP2 protocol routes
        if not request.url.path.startswith("/ap2/"):
            return await call_next(request)
        
        # Define public endpoints that don't require authentication
        public_endpoints = [
            "/ap2/status",
            "/ap2/protocols",
            # Consent bootstrap: /ap2/consent/grant is the endpoint that ISSUES a
            # consent token, so it cannot require X-Agent-Consent. It authenticates
            # itself by verifying X-AP2-Signature against the agent's registered
            # public key (agents.public_key) and fails closed, so it is exempt from
            # the middleware's consent-token requirement rather than left unguarded.
            "/ap2/consent/grant",
        ]
        
        # Check if this is a public endpoint. The prefix exemptions are
        # GET-only on purpose: only read routes live under these prefixes today
        # (GET /ap2/transaction/{id}, GET /ap2/receipt/{id}), so any future write
        # route added under them fails closed (stays authenticated) instead of
        # silently inheriting public status.
        is_public = (
            request.url.path in public_endpoints or
            (request.url.path.startswith("/ap2/transaction/") and request.method == "GET") or
            (request.url.path.startswith("/ap2/receipt/") and request.method == "GET") or
            request.url.path == "/ap2/x402/quote"
        )

        # Consent-only endpoints: authenticated READS that need a valid consent
        # token but NOT a per-request signature/nonce. A nonce on an idempotent GET
        # would make each read single-use; these are gated by consent plus the
        # route's own ownership check (e.g. wallet ownership). Write routes are
        # never consent-only — they require the full signature + nonce.
        is_consent_only = (
            request.url.path == "/ap2/wallet/balance" and request.method == "GET"
        )

        try:
            # Extract headers
            consent_token = request.headers.get("X-Agent-Consent")
            signature = request.headers.get("X-AP2-Signature")
            nonce = request.headers.get("X-AP2-Nonce")
            wallet_address = request.headers.get("X-Wallet-Address")

            # Required headers. Non-public routes always need a consent token; the
            # full (write) tier additionally needs X-AP2-Signature + X-AP2-Nonce.
            if not is_public:
                if not consent_token:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Missing X-Agent-Consent header"
                    )

                if not is_consent_only:
                    if not signature:
                        raise HTTPException(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Missing X-AP2-Signature header"
                        )

                    if not nonce:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Missing X-AP2-Nonce header"
                        )
            
            # Attach headers to request state for route handlers
            request.state.consent_token = consent_token
            request.state.signature = signature
            request.state.nonce = nonce
            request.state.wallet_address = wallet_address
            
            # Log AP2 request
            logger.info(
                f"AP2 Request: {request.method} {request.url.path} "
                f"[consent={bool(consent_token)}, nonce={nonce}]"
            )
            
            # Process request
            response = await call_next(request)
            
            # Add AP2 protocol headers to response
            response.headers["X-AP2-Version"] = "0.1"
            response.headers["X-Protocol-Support"] = "AP2,X-402"
            
            return response
            
        except HTTPException as e:
            logger.warning(f"AP2 security validation failed: {e.detail}")
            return JSONResponse(
                status_code=e.status_code,
                content={
                    "error": e.detail,
                    "protocol": "AP2",
                    "version": "0.1"
                }
            )
        except Exception as e:
            logger.error(f"AP2 middleware error: {e}", exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "error": "Internal AP2 processing error",
                    "protocol": "AP2",
                    "version": "0.1"
                }
            )


async def verify_ap2_consent(request: Request) -> dict:
    """
    Dependency function to verify AP2 consent token
    
    Usage:
        @router.post("/ap2/transaction")
        async def create_transaction(
            request: Request,
            consent: dict = Depends(verify_ap2_consent)
        ):
            agent_id = consent["agent_id"]
            ...
    
    Returns:
        Consent data including agent_id, scope, and expiry
    
    Raises:
        HTTPException: If consent is invalid
    """
    consent_token = getattr(request.state, "consent_token", None) or \
        request.headers.get("X-Agent-Consent")

    if not consent_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing agent consent"
        )

    from services.consent_service import consent_service

    try:
        return await consent_service.verify_consent(consent_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e)
        )


async def verify_ap2_signature(request: Request) -> bool:
    """
    Dependency function to verify an AP2 request signature.

    Enforces the SAME signed-payload contract as the consent-grant path
    (services/ap2_signing.py): the signature is checked over the canonical JSON
    of the request body with ``nonce`` injected from the ``X-AP2-Nonce`` header
    and ``algorithm`` excluded. The signing agent is resolved from the consent
    token (via verify_ap2_consent) and the signature is verified against that
    agent's REGISTERED public key (agents.public_key) -- never a caller-supplied
    key.

    Ordering matters: the signature is verified BEFORE the nonce is consumed, so
    a bad signature can never burn a victim's nonce (a replay-style DoS).

    Returns:
        True if the signature is valid.

    Raises:
        HTTPException: 401 (missing/invalid signature, unknown/keyless agent,
            invalid consent), 400 (missing nonce or unsupported algorithm),
            409 (nonce replay).
    """
    signature = getattr(request.state, "signature", None) or \
        request.headers.get("X-AP2-Signature")
    nonce = getattr(request.state, "nonce", None) or \
        request.headers.get("X-AP2-Nonce")

    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing request signature"
        )

    if not nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-AP2-Nonce header"
        )

    from db.database import database
    from services.consent_service import consent_service
    from services.crypto_service import crypto_service
    from services.ap2_signing import (
        ALLOWED_AP2_ALGORITHMS,
        build_ap2_signed_payload,
        is_supported_ap2_algorithm,
    )
    from services.ap2_identity import is_did, resolve_agent_identity

    consent = await verify_ap2_consent(request)
    agent_id = consent["agent_id"]

    try:
        public_key = await consent_service.get_agent_identity(agent_id)
    except LookupError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown agent"
        )

    if not public_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Agent has no registered public key for AP2 signature verification"
        )

    # A DID identity (ADR-012) carries the verification key IN the identity:
    # resolve it to a PEM and take the algorithm FROM the DID (authoritative),
    # mirroring the consent-grant path (consent_service.create_consent). Without
    # this the transaction/* routes 401 every DID agent — they could grant
    # consent but never sign a transaction. Anything unresolvable fails closed.
    resolved_algorithm = None
    if is_did(public_key):
        try:
            public_key, resolved_algorithm = await resolve_agent_identity(public_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unresolvable DID identity: {exc}",
            )

    # Parse the body for the signed payload. A missing/invalid body is treated as
    # an empty object so the payload is still {nonce: ...} rather than an error.
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # For a DID agent the DID's key type is authoritative; a raw-PEM (pilot)
    # agent falls back to the body's declared algorithm.
    algorithm = resolved_algorithm or body.get("algorithm", "ES256")
    if not is_supported_ap2_algorithm(algorithm):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported algorithm: {algorithm} "
                f"(expected one of {', '.join(ALLOWED_AP2_ALGORITHMS)})"
            ),
        )

    payload = build_ap2_signed_payload(body, nonce)

    # Reject an already-used nonce up front. This SELECT does not consume the
    # nonce, so doing it before signature verification is safe.
    existing = await database.fetch_one(
        "SELECT 1 AS present FROM nonce_tracker WHERE nonce = :nonce",
        {"nonce": nonce},
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nonce already used (replay attack detected)"
        )

    is_valid = crypto_service.verify_agent_signature(
        public_key=public_key,
        signature=signature,
        payload=payload,
        algorithm=algorithm
    )

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature"
        )

    # Consume the nonce only AFTER the signature verifies (mirrors create_consent
    # and verify_ap2_nonce). A bad signature above returns without inserting.
    await database.execute(
        """INSERT INTO nonce_tracker (nonce, used_at, request_path)
           VALUES (:nonce, NOW(), :path)""",
        {"nonce": nonce, "path": request.url.path},
    )

    logger.info(f"✅ AP2 signature verified for agent {agent_id}")
    return True


async def verify_ap2_nonce(request: Request, nonce: str) -> None:
    """
    Dependency function to verify AP2 nonce uniqueness
    
    Args:
        request: FastAPI request object
        nonce: Nonce value to check
    
    Raises:
        HTTPException: If nonce has already been used
    """
    from db.database import database
    
    # Check if nonce exists in database
    query = """
        SELECT COUNT(*) as count
        FROM nonce_tracker
        WHERE nonce = :nonce
    """
    
    result = await database.fetch_one(query, {"nonce": nonce})
    
    if result and result["count"] > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nonce already used (replay attack detected)"
        )
    
    # Record nonce
    insert_query = """
        INSERT INTO nonce_tracker (nonce, used_at, request_path)
        VALUES (:nonce, NOW(), :path)
    """
    
    await database.execute(
        insert_query,
        {
            "nonce": nonce,
            "path": request.url.path
        }
    )
