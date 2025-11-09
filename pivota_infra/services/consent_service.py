"""
Consent Management Service for AP2 Protocol
Handles consent validation, usage tracking, and nonce replay protection
"""
import json
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from decimal import Decimal

from db.database import database

logger = logging.getLogger(__name__)


class ConsentService:
    """Manage AP2 consent tokens and nonce tracking"""
    
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

