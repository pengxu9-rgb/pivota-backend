"""
AP2 Protocol Routes
Implements Agent Payment Protocol v2 endpoints for payment processing
"""
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, HTTPException, Depends, status
from pydantic import BaseModel, Field
from db.database import database
from middleware.ap2_security import verify_ap2_signature

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ap2",
    tags=["AP2 Protocol"]
)


# ========================
# Request/Response Models
# ========================

class AP2MandateChain(BaseModel):
    """
    A user-signed AP2 Intent→Cart→Payment mandate chain authorizing a transaction
    (ADR-012 authority layer). Each mandate is a signed object; the *_signature
    fields are the issuer's detached base64 signatures over them.
    """
    intent: Dict[str, Any]
    intent_signature: str
    cart: Dict[str, Any]
    cart_signature: str
    payment: Dict[str, Any]
    payment_signature: str


class AP2PaymentRequest(BaseModel):
    """AP2 payment request"""
    merchant_id: str = Field(..., description="Target merchant ID")
    amount: float = Field(..., description="Payment amount", gt=0)
    currency: str = Field(..., description="Currency code (USD, EUR, etc.)")
    product_id: Optional[str] = Field(None, description="Product ID if purchasing product")
    metadata: Optional[dict] = Field(None, description="Additional payment metadata")
    mandate_chain: Optional[AP2MandateChain] = Field(
        None,
        description="Optional AP2 Intent→Cart→Payment mandate chain authorizing this transaction"
    )


class AP2TransactionResponse(BaseModel):
    """AP2 transaction response"""
    transaction_id: str = Field(..., description="Unique transaction ID")
    status: str = Field(..., description="Transaction status (pending, completed, failed)")
    receipt_url: Optional[str] = Field(None, description="Receipt URL")
    signature: Optional[str] = Field(None, description="Platform signature")


class AP2ExchangeRateRequest(BaseModel):
    """X-402 exchange rate request"""
    from_currency: str = Field(..., description="Source currency")
    to_currency: str = Field(..., description="Target currency")
    amount: float = Field(..., description="Amount to convert", gt=0)


class AP2ExchangeRateResponse(BaseModel):
    """X-402 exchange rate response"""
    from_currency: str
    to_currency: str
    rate: float = Field(..., description="Exchange rate")
    converted_amount: float = Field(..., description="Converted amount")
    timestamp: str = Field(..., description="Rate timestamp")


# ========================
# Public Endpoints
# ========================

@router.get("/status")
async def get_ap2_status():
    """
    Get AP2 protocol status and capabilities
    
    Public endpoint - no authentication required
    """
    return {
        "protocol": "AP2",
        "version": "0.1",
        "status": "active",
        "capabilities": [
            "agent_consent",
            "signature_verification",
            "wallet_payments",
            "x402_exchange"
        ],
        "supported_protocols": ["AP2", "X-402"],
        "timestamp": datetime.utcnow().isoformat()
    }


@router.get("/protocols")
async def list_supported_protocols():
    """
    List all supported protocols and their versions
    
    Public endpoint - no authentication required
    """
    # Advertise only functional endpoints. `/ap2/wallet/balance` and
    # `/ap2/x402/exchange` exist but return 501 (not-implemented stubs), so they
    # are intentionally omitted from this catalog — do not re-add until built
    # (see docs/AP2_ENABLEMENT.md §6).
    return {
        "protocols": [
            {
                "name": "AP2",
                "version": "0.1",
                "description": "Agent Payment Protocol v2",
                "endpoints": [
                    "/ap2/transaction/initiate",
                    "/ap2/transaction/confirm"
                ]
            },
            {
                "name": "X-402",
                "version": "0.1",
                "description": "Payment Required Protocol",
                "endpoints": [
                    "/ap2/x402/quote"
                ]
            }
        ]
    }


# ========================
# Agent Consent Endpoints
# ========================

@router.post("/consent/grant")
async def grant_consent(request: Request):
    """
    Grant agent consent for payment operations
    
    Requires:
    - X-AP2-Signature: Agent signature
    - X-AP2-Nonce: Unique nonce
    
    The signature must be the base64 signature over the canonical AP2 payload
    (see services/ap2_signing.py) -- for this route that is the canonical JSON
    of {agent_id, scope, duration_hours, nonce} -- made with the private key
    matching the agent's registered public key (agents.public_key).

    Returns consent token for subsequent requests
    """
    from services.consent_service import (
        consent_service,
        InvalidSignatureError,
        NonceReplayError,
        UnsupportedAlgorithmError,
    )
    from services.ap2_signing import ALLOWED_AP2_ALGORITHMS, is_supported_ap2_algorithm

    # Extract headers
    signature = request.headers.get("X-AP2-Signature")
    nonce = request.headers.get("X-AP2-Nonce")

    if not signature or not nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required headers (X-AP2-Signature, X-AP2-Nonce)"
        )

    # Parse request body
    body = await request.json()
    agent_id = body.get("agent_id")
    scope = body.get("scope", ["read"])
    duration_hours = body.get("duration_hours", 24)
    algorithm = body.get("algorithm", "ES256")
    spending_limit = body.get("spending_limit")  # optional cap (USD, pilot); None = no limit

    if not agent_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing agent_id"
        )

    if not is_supported_ap2_algorithm(algorithm):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported algorithm: {algorithm} "
                f"(expected one of {', '.join(ALLOWED_AP2_ALGORITHMS)})"
            ),
        )

    # Resolve the agent's registered AP2 identity — a DID (agents.did preferred;
    # the key is resolved from it) or a legacy raw PEM. Verification is only
    # meaningful against material we already trust, never one the caller sends.
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

    try:
        # Create consent (verifies the signature against the registered key)
        consent_data = await consent_service.create_consent(
            agent_id=agent_id,
            scope=scope,
            duration_hours=duration_hours,
            signature=signature,
            nonce=nonce,
            public_key=public_key,
            algorithm=algorithm,
            spending_limit=spending_limit,
        )

        return {
            "consent_token": consent_data["token"],
            "expires_at": consent_data["expires_at"],
            "scope": consent_data["scope"]
        }

    except InvalidSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature"
        )
    except UnsupportedAlgorithmError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except NonceReplayError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Nonce already used"
        )
    except Exception as e:
        logger.error(f"Consent grant failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to grant consent"
        )


@router.post("/consent/revoke")
async def revoke_consent(
    request: Request,
    _sig: bool = Depends(verify_ap2_signature),
):
    """
    Revoke agent consent.

    Revocation is a mutating lifecycle action, so it is signature-authenticated
    like the transaction routes — not merely token-bearing. Requires (all enforced
    by Depends(verify_ap2_signature)):
    - X-Agent-Consent: the consent token to revoke (also identifies the agent),
    - X-AP2-Signature: proves the consent's own agent (verified against its
      resolved key), so a leaked token alone cannot revoke,
    - X-AP2-Nonce: replay protection.
    """
    from services.consent_service import consent_service

    # verify_ap2_signature already resolved + verified the agent from this token.
    consent_token = request.headers.get("X-Agent-Consent")

    try:
        await consent_service.revoke_consent(consent_token)
        return {"status": "revoked"}
        
    except Exception as e:
        logger.error(f"Consent revoke failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke consent"
        )


# ========================
# Transaction Endpoints
# ========================

@router.post("/transaction/initiate", response_model=AP2TransactionResponse)
async def initiate_transaction(
    payment: AP2PaymentRequest,
    request: Request,
    _sig: bool = Depends(verify_ap2_signature),
):
    """
    Initiate AP2 payment transaction

    Requires (all enforced by Depends(verify_ap2_signature)):
    - X-Agent-Consent: Valid consent token
    - X-AP2-Signature: signature over the canonical payload of the request body
      + nonce (see services/ap2_signing.py), against the agent's registered key
    - X-AP2-Nonce: Unique nonce (verified, then consumed, by the dependency)
    """
    from decimal import Decimal
    from services.consent_service import consent_service
    from services.crypto_service import crypto_service

    consent_token = request.headers.get("X-Agent-Consent")
    nonce = request.headers.get("X-AP2-Nonce")

    # Authorize the payment against the CONSENT — not just token validity. The
    # signature was already verified against the agent resolved from this token
    # (verify_ap2_signature); validate_consent additionally enforces that the
    # consent is active + unexpired, its scope permits `create_payment`, and the
    # amount is within the remaining spending limit (spending_limit - spent_amount).
    # Fails closed (403). Merchant/currency scoping is the mandate path's job
    # (below), not the plain consent's — the consent primitive carries neither.
    ok, reason, consent_row = await consent_service.validate_consent(
        consent_token, action="create_payment", amount=Decimal(str(payment.amount))
    )
    if not ok:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)
    agent_id = consent_row["agent_id"]

    # AP2 mandate authority (ADR-012 slice 3): if the request carries a
    # user-signed Intent→Cart→Payment mandate chain, it must authorize THIS
    # transaction. Deny by default — the presenting agent's trusted-issuer set is
    # passed straight through (an empty set denies every mandate; it is NEVER
    # coerced to None, which would skip the issuer-trust check). Absent a chain,
    # behavior is unchanged (consent-scope based) for backward compatibility.
    if payment.mandate_chain is not None:
        from services.ap2_mandate import verify_mandate_chain, MandateError
        from services.ap2_identity import is_did
        from db.ap2_trusted_issuers import get_trusted_issuers
        from decimal import Decimal

        # A mandate's `subject` is the agent's DID. Resolve the agent's DID
        # identity (agents.did preferred, legacy DID-in-public_key as fallback —
        # the SAME value verify_ap2_signature verified the request signature
        # against). agent_id is the INTERNAL id (VARCHAR-50, too short to be a
        # DID), so it must NOT be used as the subject. The trusted-issuer
        # registry stays keyed by the internal agent_id.
        agent_did = await consent_service.get_agent_identity(agent_id)
        if not is_did(agent_did):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Mandate authorization requires a DID-identity agent",
            )

        mc = payment.mandate_chain
        trusted_issuers = await get_trusted_issuers(agent_id)
        try:
            verified = await verify_mandate_chain(
                mc.intent, mc.intent_signature,
                mc.cart, mc.cart_signature,
                mc.payment, mc.payment_signature,
                agent_did=agent_did,           # the agent's resolved DID (subject binding)
                trusted_issuers=trusted_issuers,  # pass directly — empty set = deny all
                action="create_payment",
            )
            # Bind the signed authority to THIS concrete transaction — a valid
            # chain for a different cart must not authorize this one.
            cart = verified["cart"]
            if str(cart.get("merchant_id")) != str(payment.merchant_id):
                raise MandateError("cart.merchant_id does not match the transaction merchant")
            if Decimal(str(cart.get("total"))) != Decimal(str(payment.amount)):
                raise MandateError("cart.total does not match the transaction amount")
            if str(cart.get("currency")) != str(payment.currency):
                raise MandateError("cart.currency does not match the transaction currency")
        except MandateError as exc:
            logger.warning(f"AP2 mandate authorization failed for agent {agent_id}: {exc}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Mandate authorization failed: {exc}",
            )

    # Create transaction
    transaction_id = f"ap2_txn_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{nonce[:8]}"
    
    insert_query = """
        INSERT INTO x402_transactions (
            transaction_id,
            agent_id,
            merchant_id,
            amount,
            currency,
            product_id,
            status,
            metadata,
            created_at
        ) VALUES (
            :transaction_id,
            :agent_id,
            :merchant_id,
            :amount,
            :currency,
            :product_id,
            'pending',
            :metadata,
            NOW()
        )
    """
    
    await database.execute(
        insert_query,
        {
            "transaction_id": transaction_id,
            "agent_id": agent_id,
            "merchant_id": payment.merchant_id,
            "amount": Decimal(str(payment.amount)),
            "currency": payment.currency,
            "product_id": payment.product_id,
            "metadata": payment.metadata
        }
    )
    
    # Generate receipt signature
    receipt_data = {
        "transaction_id": transaction_id,
        "amount": payment.amount,
        "currency": payment.currency,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    signature = crypto_service.sign_receipt(receipt_data)
    
    return AP2TransactionResponse(
        transaction_id=transaction_id,
        status="pending",
        receipt_url=f"/ap2/receipt/{transaction_id}",
        signature=signature
    )


@router.post("/transaction/confirm")
async def confirm_transaction(
    request: Request,
    _sig: bool = Depends(verify_ap2_signature),
):
    """
    Confirm pending transaction

    Requires (signature/consent/nonce enforced by Depends(verify_ap2_signature)):
    - X-Agent-Consent: Valid consent token
    - X-AP2-Signature: signature over the canonical payload of the request body
      ({transaction_id}) + nonce, against the agent's registered key
    - X-AP2-Nonce: Unique nonce (verified, then consumed, by the dependency)
    - X-Wallet-Address: Agent wallet address
    """
    from services.wallet_service import wallet_service

    # Signature, consent and nonce-replay are handled by verify_ap2_signature.
    consent_token = request.headers.get("X-Agent-Consent")

    # Parse body
    body = await request.json()
    transaction_id = body.get("transaction_id")
    wallet_address = request.headers.get("X-Wallet-Address")
    
    if not transaction_id or not wallet_address:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing transaction_id or wallet_address"
        )
    
    # Verify wallet
    from decimal import Decimal
    from services.consent_service import consent_service
    consent_data = await consent_service.verify_consent(consent_token)
    agent_id = consent_data["agent_id"]

    is_valid = await wallet_service.verify_agent_wallet(agent_id, wallet_address)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Wallet not authorized"
        )

    confirmed_at = datetime.utcnow()

    # Settle atomically: the status flip AND the consent spend-tracking commit
    # together, so a completed transaction can never leave the spending limit
    # un-debited, and a replay/concurrent confirm can never double-count.
    async with database.transaction():
        txn = await database.fetch_one(
            """SELECT transaction_id, agent_id, amount, currency, status
               FROM x402_transactions WHERE transaction_id = :transaction_id""",
            {"transaction_id": transaction_id},
        )
        if not txn:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found",
            )
        # The transaction must belong to the consenting agent — a valid consent
        # for agent A must not confirm agent B's transaction. (The prior handler
        # matched on transaction_id ALONE.)
        if str(txn["agent_id"]) != str(agent_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Transaction does not belong to this agent",
            )
        # Idempotency / replay guard: only a pending transaction can be settled.
        if txn["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Transaction is already {txn['status']}",
            )

        # Re-check the consent is still active and permits the action (e.g. not
        # revoked/expired since initiate). The AUTHORITATIVE, race-safe limit
        # enforcement is the atomic debit below — a non-atomic amount check here
        # would let concurrent confirms of different txns both pass a stale read.
        ok, reason, _ = await consent_service.validate_consent(
            consent_token, action="create_payment", agent_id=agent_id,
        )
        if not ok:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)

        # Flip to completed only if STILL pending; RETURNING tells us whether WE
        # settled it (vs a concurrent confirm that won the race).
        settled = await database.fetch_one(
            """UPDATE x402_transactions
               SET status = 'completed', wallet_address = :wallet_address,
                   confirmed_at = :confirmed_at
               WHERE transaction_id = :transaction_id AND status = 'pending'
               RETURNING transaction_id""",
            {
                "transaction_id": transaction_id,
                "wallet_address": wallet_address,
                "confirmed_at": confirmed_at,
            },
        )
        if settled is None:
            # Lost the race — another confirm settled it first. Do NOT debit again.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Transaction already completed",
            )

        # We won the settlement — atomically debit the spending budget, enforcing
        # the limit IN the write (spent_amount + amount <= spending_limit). Zero
        # rows => the debit would exceed the limit (a concurrent confirm consumed
        # it since initiate), so fail closed — raising rolls back the settle above.
        if not await consent_service.debit_within_limit(consent_token, Decimal(str(txn["amount"]))):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient spending limit",
            )

    return {
        "transaction_id": transaction_id,
        "status": "completed",
        "timestamp": confirmed_at.isoformat(),
    }


@router.get("/transaction/{transaction_id}")
async def get_transaction_status(transaction_id: str):
    """
    Get transaction status
    
    Public endpoint - no authentication required
    """
    query = """
        SELECT transaction_id, agent_id, merchant_id, amount, currency,
               status, created_at, confirmed_at
        FROM x402_transactions
        WHERE transaction_id = :transaction_id
    """
    
    result = await database.fetch_one(query, {"transaction_id": transaction_id})
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found"
        )
    
    return dict(result)


# ========================
# X-402 Exchange Endpoints
# ========================

@router.post("/x402/quote", response_model=AP2ExchangeRateResponse)
async def get_exchange_quote(exchange_request: AP2ExchangeRateRequest):
    """
    Get exchange rate quote (X-402 protocol)
    
    Public endpoint - no authentication required
    """
    from_ccy = exchange_request.from_currency.upper()
    to_ccy = exchange_request.to_currency.upper()

    # Same currency is a legitimate 1:1 — no rate snapshot needed.
    if from_ccy == to_ccy:
        return AP2ExchangeRateResponse(
            from_currency=from_ccy,
            to_currency=to_ccy,
            rate=1.0,
            converted_amount=exchange_request.amount,
            timestamp=datetime.utcnow().isoformat(),
        )

    # Query latest exchange rate snapshot
    query = """
        SELECT rates, base_currency, created_at
        FROM x402_exchange_rates
        WHERE base_currency = :base_currency
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY created_at DESC
        LIMIT 1
    """
    result = await database.fetch_one(query, {"base_currency": from_ccy})

    if not result:
        # FAIL CLOSED. Previously this silently returned a fabricated 1:1 quote.
        # The x402_exchange_rates table is fed ONLY by a manual admin endpoint (no
        # scheduled sync), so an unsynced/stale table quoted EVERY pair at par —
        # a made-up FX rate served from a public endpoint. Never quote a rate we
        # don't have.
        logger.error(
            "x402/quote: no fresh exchange-rate snapshot for %s — failing closed",
            from_ccy,
        )
        raise HTTPException(
            status_code=503,
            detail=f"Exchange rate for {from_ccy} is unavailable or stale.",
        )

    rates_data = json.loads(result["rates"]) if isinstance(result["rates"], str) else result["rates"]
    if to_ccy not in rates_data:
        # FAIL CLOSED: pair not in the snapshot (was a silent 1:1).
        logger.error(
            "x402/quote: %s not in snapshot for %s — failing closed", to_ccy, from_ccy
        )
        raise HTTPException(
            status_code=503,
            detail=f"Exchange rate {from_ccy}->{to_ccy} is unavailable.",
        )

    rate = float(rates_data[to_ccy])
    converted_amount = exchange_request.amount * rate
    return AP2ExchangeRateResponse(
        from_currency=from_ccy,
        to_currency=to_ccy,
        rate=rate,
        converted_amount=converted_amount,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.post("/x402/exchange")
async def execute_exchange(request: Request):
    """
    Execute currency exchange (X-402 protocol)

    Not yet implemented. When built, this is a money-moving WRITE and must adopt
    the full signed contract like the transaction routes — add
    ``Depends(verify_ap2_signature)`` (consent + X-AP2-Signature + X-AP2-Nonce).
    Until then it returns 501; the middleware already header-gates it as a
    non-public write route.
    """
    # TODO: Implement X-402 exchange execution (with Depends(verify_ap2_signature)).
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="X-402 exchange execution not yet implemented"
    )


# ========================
# Wallet Endpoints
# ========================

@router.get("/wallet/balance")
async def get_wallet_balance(request: Request):
    """
    Get agent wallet balance

    Not yet implemented. `agent_wallets` (migration 022) is an ADDRESS REGISTRY —
    it records which wallet addresses belong to an agent (columns: `address`,
    `network`, `status`, `custodian`, `custodian_account_id`, …) and has NO
    balance store. There is also no balance *source* wired anywhere in this
    codebase (no on-chain RPC, no custodian client), so there is nothing to read
    a balance from. The prior handler `SELECT`ed non-existent columns
    (`balance`, `currency`, `last_updated`) filtered on a non-existent column
    (`wallet_address`; the real column is `address`), so it 500'd against real
    Postgres — and its unit test passed only because a FakeDB fabricated the row.

    Returning a fabricated or always-zero balance would be worse than none, so
    this fails honestly with 501 — mirroring `x402/exchange`. When built it needs
    a real balance source (a live on-chain/custodian read keyed by
    `agent_wallets.network` + `address`, or `custodian` + `custodian_account_id`,
    or a settlement-populated ledger) and must keep the consent-only gate: re-add
    the consent verification (the prior handler is in git history) or a
    ``Depends(...)``. Until then the middleware already header-gates this path as
    a consent-only read (see `middleware/ap2_security.py`). See
    `docs/AP2_ENABLEMENT.md` §6.
    """
    # TODO: Implement wallet balance once a real balance source exists (see docstring).
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Wallet balance is not available (no balance source wired)",
    )


@router.get("/receipt/{transaction_id}")
async def get_transaction_receipt(transaction_id: str):
    """
    Get transaction receipt with platform signature
    
    Public endpoint - no authentication required
    """
    from services.crypto_service import crypto_service
    
    # Query transaction
    query = """
        SELECT transaction_id, agent_id, merchant_id, amount, currency,
               status, created_at, confirmed_at
        FROM x402_transactions
        WHERE transaction_id = :transaction_id
    """
    
    result = await database.fetch_one(query, {"transaction_id": transaction_id})
    
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found"
        )
    
    receipt_data = dict(result)
    
    # Sign receipt
    signature = crypto_service.sign_receipt(receipt_data)
    
    return {
        "receipt": receipt_data,
        "signature": signature,
        "protocol": "AP2",
        "version": "0.1"
    }

