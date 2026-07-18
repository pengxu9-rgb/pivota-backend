"""
Consent Management Service for AP2 Protocol
Handles consent validation, usage tracking, and nonce replay protection
"""
import base64
import json
import logging
import secrets
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from decimal import Decimal

from db.database import database
from services.crypto_service import crypto_service
from services.ap2_signing import (
    ALLOWED_AP2_ALGORITHMS,
    build_ap2_signed_payload,
    is_supported_ap2_algorithm,
)
from services.ap2_identity import is_did, resolve_agent_identity

logger = logging.getLogger(__name__)


class InvalidSignatureError(ValueError):
    """Signature did not verify against the agent's registered public key."""


class NonceReplayError(ValueError):
    """Nonce has already been used (replay protection)."""


class UnsupportedAlgorithmError(ValueError):
    """Requested signature algorithm is not in ALLOWED_AP2_ALGORITHMS."""


class ConsentService:
    """Manage AP2 consent tokens and nonce tracking"""

    async def get_agent_public_key(self, agent_id: str) -> Optional[str]:
        """
        Fetch the agent's registered AP2 public key (agents.public_key,
        migration 021). Signatures must only ever be verified against a
        key registered ahead of time — never one supplied by the caller.

        Returns:
            The registered key (PEM/base64), or None if the agent has none

        Raises:
            LookupError: If the agent does not exist
        """
        row = await database.fetch_one(
            "SELECT public_key FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        if not row:
            raise LookupError(f"Agent not found: {agent_id}")
        return row["public_key"]

    async def get_agent_identity(self, agent_id: str) -> Optional[str]:
        """
        Resolve the agent's AP2 identity/verification material (ADR-012).

        The DID is the agent's stable identity; verification keys are resolved
        FROM it (did:key offline, did:web fetched+cached). So the source of record
        is **agents.did** (migration 183). This prefers agents.did and falls back
        to a DID-or-PEM stored in agents.public_key, keeping every existing agent
        working during the transition:

        - agents.did set to a DID          -> that DID (identity; caller resolves the key),
        - else agents.public_key is a DID  -> that DID (legacy pilot placement),
        - else agents.public_key is a PEM  -> that PEM (raw-key pilot fallback),
        - neither                           -> None (fail closed: no registered identity).

        Callers keep the existing `is_did(...)` branch: a DID is resolved to a key
        via resolve_agent_identity; a PEM is used directly.

        Raises:
            LookupError: If the agent does not exist
        """
        row = await database.fetch_one(
            "SELECT did, public_key FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        if not row:
            raise LookupError(f"Agent not found: {agent_id}")
        # dict() first, then .get — the SELECT always includes `did` in
        # production; converting to a dict makes the access safe for both
        # asyncpg Records and any legacy row shape without it.
        row = dict(row)
        did = row.get("did")
        if did and is_did(did):
            return did
        return row.get("public_key")

    async def create_consent(
        self,
        agent_id: str,
        scope: list,
        duration_hours: int = 24,
        signature: str = None,
        nonce: str = None,
        public_key: str = None,
        algorithm: str = "ES256",
        spending_limit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Create new agent consent with signature verification

        Args:
            agent_id: Agent identifier
            scope: List of permitted actions
            duration_hours: Consent validity period
            signature: Agent's signature (base64)
            nonce: Unique nonce
            public_key: Agent's public key (PEM)
            algorithm: ES256 or Ed25519
            spending_limit: Optional cap on total spend authorized by this consent
                (assumed USD for the pilot, per ADR-013's single-currency ledger).
                None = no limit. Enforced at initiate/confirm via validate_consent
                against (spending_limit - spent_amount).

        Returns:
            Consent data including consent_id and expiry
        """
        # A signature without a key to check it against must FAIL CLOSED, never
        # silently skip verification and still mint a token. (The grant route
        # already 401s a keyless agent before calling this, but the primitive
        # must not degrade to "no verification" for any caller.)
        if signature and not public_key:
            raise InvalidSignatureError(
                "Signature provided without a registered public key to verify against"
            )

        # Verify signature if provided
        if signature and public_key:
            # DID identities carry the verification key in the identity itself
            # (ADR-012): did:key resolves offline; did:web resolves from the
            # agent's DID document (network, cached, fail-closed). The algorithm
            # comes FROM the DID — the DID, not the caller, is authoritative
            # about the key type. Anything unresolvable fails closed.
            if is_did(public_key):
                try:
                    public_key, algorithm = await resolve_agent_identity(public_key)
                except ValueError as exc:
                    raise InvalidSignatureError(
                        f"Unresolvable DID identity: {exc}"
                    )

            if not is_supported_ap2_algorithm(algorithm):
                raise UnsupportedAlgorithmError(
                    f"Unsupported algorithm: {algorithm} "
                    f"(expected one of {', '.join(ALLOWED_AP2_ALGORITHMS)})"
                )

            # Canonical AP2 signed payload (see services/ap2_signing.py). For the
            # grant body {agent_id, scope, duration_hours} this is exactly the
            # historical four-key shape {agent_id, scope, duration_hours, nonce}.
            payload = build_ap2_signed_payload(
                {
                    "agent_id": agent_id,
                    "scope": scope,
                    "duration_hours": duration_hours,
                },
                nonce,
            )

            is_valid = crypto_service.verify_agent_signature(
                public_key=public_key,
                signature=signature,
                payload=payload,
                algorithm=algorithm
            )

            if not is_valid:
                raise InvalidSignatureError("Invalid signature")

            logger.info(f"✅ Signature verified for agent {agent_id}")

        # Verify nonce uniqueness. NB: verification above runs BEFORE the nonce
        # is consumed, so a bad signature can never burn a victim's nonce.
        if nonce:
            existing_nonce = await database.fetch_one(
                "SELECT nonce FROM nonce_tracker WHERE nonce = :nonce",
                {"nonce": nonce}
            )
            if existing_nonce:
                raise NonceReplayError("Nonce already used")
            
            await database.execute(
                """INSERT INTO nonce_tracker (nonce, used_at, request_path)
                   VALUES (:nonce, NOW(), '/consent/grant')""",
                {"nonce": nonce}
            )
        
        # Create consent token
        consent_id = f"consent_{secrets.token_hex(16)}"
        expires_at = datetime.utcnow() + timedelta(hours=duration_hours)
        
        await database.execute(
            """INSERT INTO agent_consents (
                   consent_id, agent_id, scope, status, spending_limit, created_at, expires_at
               ) VALUES (
                   :consent_id, :agent_id, :scope, 'active', :spending_limit, NOW(), :expires_at
               )""",
            {
                "consent_id": consent_id,
                "agent_id": agent_id,
                "scope": json.dumps({"actions": scope}),
                "spending_limit": spending_limit,
                "expires_at": expires_at
            }
        )
        
        logger.info(f"✅ Consent created: {consent_id} for agent {agent_id}")
        
        return {
            "token": consent_id,
            "agent_id": agent_id,
            "scope": scope,
            "expires_at": expires_at.isoformat()
        }
    
    async def verify_consent(
        self,
        consent_token: str
    ) -> Dict[str, Any]:
        """
        Verify and return consent data
        
        Args:
            consent_token: Consent ID
            
        Returns:
            Consent data
            
        Raises:
            ValueError: If consent is invalid
        """
        consent = await database.fetch_one(
            """SELECT * FROM agent_consents 
               WHERE consent_id = :consent_id AND status = 'active'""",
            {"consent_id": consent_token}
        )
        
        if not consent:
            raise ValueError("Consent not found or inactive")
        
        expires_at = consent["expires_at"]
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at and expires_at < datetime.utcnow():
            raise ValueError("Consent has expired")
        
        return {"agent_id": consent["agent_id"], "scope": json.loads(consent["scope"])["actions"]}
    
    async def revoke_consent(
        self,
        consent_token: str
    ):
        """Revoke consent"""
        await database.execute(
            """UPDATE agent_consents 
               SET status = 'revoked', revoked_at = NOW()
               WHERE consent_id = :consent_id""",
            {"consent_id": consent_token}
        )
    
    async def validate_consent(
        self,
        consent_token: str,
        action: str,
        amount: Optional[Decimal] = None,
        agent_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Validate a consent token and authorize an action against it.

        Checks the consent is active + unexpired, that its scope permits `action`,
        and (when both an amount and a spending_limit are set) that the amount is
        within the remaining limit (spending_limit - spent_amount). The agent id
        is derived from the consent row; pass `agent_id` to additionally require
        the token belong to that agent (defense-in-depth).

        Args:
            consent_token: Consent token ID (== consent_id)
            action: Action to perform (e.g., 'create_payment')
            amount: Transaction amount (if applicable)
            agent_id: Optional expected owner; when given, the token must belong to it

        Returns:
            (is_valid, error_message, consent_row_dict)
        """
        try:
            # Query consent. agent_id is derived from the row (the request's
            # signature was already verified against the agent resolved from this
            # token); pass agent_id only to additionally pin ownership.
            if agent_id is not None:
                consent = await database.fetch_one(
                    """SELECT * FROM agent_consents
                       WHERE consent_id = :consent_id AND agent_id = :agent_id""",
                    {"consent_id": consent_token, "agent_id": agent_id},
                )
            else:
                consent = await database.fetch_one(
                    "SELECT * FROM agent_consents WHERE consent_id = :consent_id",
                    {"consent_id": consent_token},
                )
            
            if not consent:
                return False, "Consent not found", None
            
            # Check status
            if consent["status"] != "active":
                return False, f"Consent is {consent['status']}", None
            
            # Check expiration (UTC — consents are stored and checked in
            # datetime.utcnow()). On SQLite the timestamp round-trips as a string,
            # so coerce it; asyncpg (prod/Postgres) already returns a datetime.
            # Comparing against naive local datetime.now() would also spuriously
            # expire fresh consents on any host not on UTC.
            expires_at = consent["expires_at"]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at and expires_at < datetime.utcnow():
                return False, "Consent has expired", None
            
            # Check scope
            scope = consent["scope"]
            if isinstance(scope, str):
                scope = json.loads(scope)
            
            allowed_actions = scope.get("actions", [])
            if action not in allowed_actions:
                return False, f"Action '{action}' not permitted", None
            
            # Check spending limit. Guard on `is not None` — a spending_limit of 0
            # ("allow nothing") is falsy and must NOT skip the check. This is a
            # non-atomic pre-check for a clean early rejection; the authoritative,
            # race-safe limit enforcement is debit_within_limit at settle time.
            if amount is not None and consent["spending_limit"] is not None:
                remaining = Decimal(str(consent["spending_limit"])) - Decimal(str(consent["spent_amount"] or 0))
                if amount > remaining:
                    return False, f"Insufficient spending limit (remaining: {remaining})", None

            return True, None, dict(consent)

        except Exception as e:
            logger.error(f"Consent validation error: {e}")
            # Fail closed with a generic reason — don't leak internal error text.
            return False, "Consent validation failed", None
    
    async def check_nonce(
        self,
        agent_id: str,
        nonce: str
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if nonce has been used (replay protection)
        
        Args:
            agent_id: Agent ID
            nonce: Nonce value
            
        Returns:
            (is_valid, error_message)
        """
        try:
            # Check if nonce exists
            existing = await database.fetch_one(
                """SELECT * FROM nonce_tracker 
                   WHERE nonce = :nonce AND agent_id = :agent_id""",
                {"nonce": nonce, "agent_id": agent_id}
            )
            
            if existing:
                return False, "Nonce already used (replay attack detected)"
            
            # Record nonce
            await database.execute(
                """INSERT INTO nonce_tracker (nonce, agent_id, used_at, expires_at)
                   VALUES (:nonce, :agent_id, NOW(), NOW() + INTERVAL '1 hour')""",
                {"nonce": nonce, "agent_id": agent_id}
            )
            
            return True, None
            
        except Exception as e:
            logger.error(f"Nonce check error: {e}")
            return False, str(e)
    
    async def debit_within_limit(
        self,
        consent_id: str,
        amount: Decimal,
    ) -> bool:
        """
        Atomically add `amount` to the consent's spent_amount, but ONLY if it
        stays within spending_limit (a NULL limit means no cap). Returns True if
        the debit was applied, False if it would exceed the limit.

        The limit check and the increment are a SINGLE statement, so two
        concurrent debits on the same consent cannot both pass a stale read and
        overshoot the cap (the failure a separate SELECT-then-UPDATE would allow).
        Amount is bound as a Decimal (asyncpg numeric); exceptions PROPAGATE —
        callers settle inside a DB transaction that must roll back if the debit
        fails, so this must not swallow errors.
        """
        row = await database.fetch_one(
            """UPDATE agent_consents
               SET spent_amount = spent_amount + :amount,
                   nonce_counter = nonce_counter + 1
               WHERE consent_id = :consent_id
                 AND (spending_limit IS NULL OR spent_amount + :amount <= spending_limit)
               RETURNING spent_amount""",
            {"consent_id": consent_id, "amount": amount},
        )
        return row is not None
    
    async def revoke_consent(
        self,
        consent_id: str
    ):
        """
        Revoke a consent token
        
        Args:
            consent_id: Consent ID
        """
        try:
            await database.execute(
                """UPDATE agent_consents 
                   SET status = 'revoked', revoked_at = NOW()
                   WHERE consent_id = :consent_id""",
                {"consent_id": consent_id}
            )
        except Exception as e:
            logger.error(f"Failed to revoke consent: {e}")


# Singleton instance
consent_service = ConsentService()

