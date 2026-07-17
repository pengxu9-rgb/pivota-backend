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
from services.ap2_did import is_did_key, resolve_did_key

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

    async def create_consent(
        self,
        agent_id: str,
        scope: list,
        duration_hours: int = 24,
        signature: str = None,
        nonce: str = None,
        public_key: str = None,
        algorithm: str = "ES256"
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
            # did:key identities carry the verification key in the identifier
            # itself (ADR-012, first DID slice). Resolve it to a PEM and take
            # the algorithm FROM the DID — the DID, not the caller, is
            # authoritative about the key type. A malformed DID fails closed.
            if is_did_key(public_key):
                try:
                    public_key, algorithm = resolve_did_key(public_key)
                except ValueError as exc:
                    raise InvalidSignatureError(
                        f"Unresolvable did:key identity: {exc}"
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
                   consent_id, agent_id, scope, status, created_at, expires_at
               ) VALUES (
                   :consent_id, :agent_id, :scope, 'active', NOW(), :expires_at
               )""",
            {
                "consent_id": consent_id,
                "agent_id": agent_id,
                "scope": json.dumps({"actions": scope}),
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
        
        if consent["expires_at"] and consent["expires_at"] < datetime.utcnow():
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
        agent_id: str,
        consent_token: str,
        action: str,
        amount: Optional[Decimal] = None
    ) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Validate consent token and check permissions
        
        Args:
            agent_id: Agent ID
            consent_token: Consent token ID
            action: Action to perform (e.g., 'create_payment')
            amount: Transaction amount (if applicable)
            
        Returns:
            (is_valid, error_message, consent_data)
        """
        try:
            # Query consent
            consent = await database.fetch_one(
                """SELECT * FROM agent_consents 
                   WHERE consent_id = :consent_id AND agent_id = :agent_id""",
                {"consent_id": consent_token, "agent_id": agent_id}
            )
            
            if not consent:
                return False, "Consent not found", None
            
            # Check status
            if consent["status"] != "active":
                return False, f"Consent is {consent['status']}", None
            
            # Check expiration
            if consent["expires_at"] and consent["expires_at"] < datetime.now():
                return False, "Consent has expired", None
            
            # Check scope
            scope = consent["scope"]
            if isinstance(scope, str):
                scope = json.loads(scope)
            
            allowed_actions = scope.get("actions", [])
            if action not in allowed_actions:
                return False, f"Action '{action}' not permitted", None
            
            # Check spending limit
            if amount and consent["spending_limit"]:
                remaining = Decimal(consent["spending_limit"]) - Decimal(consent["spent_amount"])
                if amount > remaining:
                    return False, f"Insufficient spending limit (remaining: {remaining})", None
            
            return True, None, dict(consent)
            
        except Exception as e:
            logger.error(f"Consent validation error: {e}")
            return False, str(e), None
    
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
    
    async def increment_usage(
        self,
        consent_id: str,
        amount: Decimal
    ):
        """
        Update consent usage after successful transaction
        
        Args:
            consent_id: Consent ID
            amount: Transaction amount
        """
        try:
            await database.execute(
                """UPDATE agent_consents 
                   SET spent_amount = spent_amount + :amount,
                       nonce_counter = nonce_counter + 1
                   WHERE consent_id = :consent_id""",
                {"consent_id": consent_id, "amount": float(amount)}
            )
        except Exception as e:
            logger.error(f"Failed to update consent usage: {e}")
    
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

