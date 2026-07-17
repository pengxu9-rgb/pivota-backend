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
        ]
        
        # Check if this is a public endpoint
        is_public = (
            request.url.path in public_endpoints or
            (request.url.path.startswith("/ap2/transaction/") and request.method == "GET") or
            request.url.path.startswith("/ap2/receipt/") or
            request.url.path == "/ap2/x402/quote"
        )
        
        try:
            # Extract headers
            consent_token = request.headers.get("X-Agent-Consent")
            signature = request.headers.get("X-AP2-Signature")
            nonce = request.headers.get("X-AP2-Nonce")
            wallet_address = request.headers.get("X-Wallet-Address")
            
            # Validate required headers (for non-public endpoints only)
            if not is_public:
                if not consent_token:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Missing X-Agent-Consent header"
                    )
                
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
    Dependency function to verify AP2 request signature
    
    Returns:
        True if signature is valid
    
    Raises:
        HTTPException: If signature is invalid
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

    from services.consent_service import consent_service
    from services.crypto_service import crypto_service

    consent = await verify_ap2_consent(request)
    agent_id = consent["agent_id"]

    try:
        public_key = await consent_service.get_agent_public_key(agent_id)
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

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    payload = {**body, "nonce": nonce}
    algorithm = body.get("algorithm", "ES256")

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


async def verify_wallet_authorization(
    request: Request,
    wallet_address: str,
    agent_id: str
) -> dict:
    """
    Dependency function to verify wallet authorization
    
    Args:
        request: FastAPI request object
        wallet_address: Wallet address to verify
        agent_id: Agent ID from consent
    
    Returns:
        Wallet data including balance and status
    
    Raises:
        HTTPException: If wallet is not authorized
    """
    from db.database import database
    
    # Check agent wallet
    query = """
        SELECT wallet_id, agent_id, wallet_address, balance, status
        FROM agent_wallets
        WHERE agent_id = :agent_id AND wallet_address = :wallet_address
    """
    
    wallet = await database.fetch_one(
        query,
        {
            "agent_id": agent_id,
            "wallet_address": wallet_address
        }
    )
    
    if not wallet:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Wallet not authorized for this agent"
        )
    
    if wallet["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Wallet is {wallet['status']}"
        )
    
    return dict(wallet)

