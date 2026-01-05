"""
Multi-PSP Orchestrator
Handles payment routing with primary/backup PSP strategy for higher success rates
"""

from typing import Dict, Any, Optional, Tuple, List
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime

from adapters.psp_adapter import PSPAdapter, get_psp_adapter, PaymentIntent
from db.merchant_onboarding import get_merchant_onboarding
from db.database import database
from utils.logger import logger
import hashlib


@dataclass
class PSPConfig:
    """PSP configuration"""
    psp_type: str
    api_key: str
    priority: int  # 1 = primary, 2 = backup, etc.
    is_active: bool
    merchant_account: Optional[str] = None  # For Adyen


class MultiPSPOrchestrator:
    """
    Orchestrates payment processing across multiple PSPs
    
    Features:
    - Automatic failover from primary to backup PSPs
    - Smart routing based on transaction amount, currency, region
    - Success rate tracking per PSP
    - Automatic PSP selection optimization
    """
    
    def __init__(self, merchant_id: str):
        self.merchant_id = merchant_id
        self.psp_configs: List[PSPConfig] = []
        
    async def load_psp_configs(self):
        """
        Load all PSP configurations for this merchant.

        New behavior:
        - Primary source of truth is merchant_psps (one row per PSP).
        - merchant_onboarding.psp_* fields are kept only as a legacy fallback
          for very old merchants that don't have merchant_psps records yet.
        """
        self.psp_configs = []

        # 1) Preferred source: merchant_psps table
        try:
            psps = await database.fetch_all(
                """
                SELECT provider, api_key, account_id, status, connected_at
                FROM merchant_psps
                WHERE merchant_id = :merchant_id AND status = 'active'
                ORDER BY connected_at ASC
                """,
                {"merchant_id": self.merchant_id},
            )
        except Exception as e:
            logger.error(f"Failed to load merchant_psps for {self.merchant_id}: {e}")
            psps = []

        for idx, psp in enumerate(psps, start=1):
            # asyncpg returns Record objects; normalize to dict
            psp_dict = dict(psp)
            api_key = psp_dict.get("api_key")
            provider = (psp_dict.get("provider") or "").lower()
            if not api_key or not provider:
                continue

            # Stripe should generally be treated as primary if present
            base_priority = idx
            priority = 1 if provider == "stripe" else base_priority + 1

            merchant_account = (
                psp_dict.get("account_id") if provider == "adyen" else None
            )

            self.psp_configs.append(
                PSPConfig(
                    psp_type=provider,
                    api_key=api_key,
                    priority=priority,
                    is_active=True,
                    merchant_account=merchant_account,
                )
            )

        # 2) Legacy fallback: use merchant_onboarding.psp_* fields
        if not self.psp_configs:
            merchant = await get_merchant_onboarding(self.merchant_id)
            if not merchant:
                raise ValueError(f"Merchant {self.merchant_id} not found")

            if merchant.get("psp_connected"):
                primary_key = merchant.get("psp_sandbox_key") or merchant.get("psp_key")
                if primary_key:
                    self.psp_configs.append(
                        PSPConfig(
                            psp_type=merchant.get("psp_type", "stripe"),
                            api_key=primary_key,
                            priority=1,
                            is_active=True,
                            merchant_account=merchant.get("adyen_merchant_account"),
                        )
                    )

            backup_psps = merchant.get("backup_psps", [])
            for i, backup in enumerate(backup_psps, start=2):
                if backup.get("is_active"):
                    self.psp_configs.append(
                        PSPConfig(
                            psp_type=backup["psp_type"],
                            api_key=backup["api_key"],
                            priority=i,
                            is_active=True,
                            merchant_account=backup.get("merchant_account"),
                        )
                    )

        # Sort by priority so failover order is deterministic
        self.psp_configs.sort(key=lambda x: x.priority)

        logger.info(
            f"Loaded {len(self.psp_configs)} PSP configs for merchant {self.merchant_id}"
        )
    
    async def create_payment_intent(
        self,
        amount: Decimal,
        currency: str,
        metadata: Dict[str, Any],
        preferred_psps: Optional[List[str]] = None,
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str], str]:
        """
        Create payment intent with automatic PSP failover
        
        Returns: (success, payment_intent, error, psp_used)
        """
        await self.load_psp_configs()

        # Reorder configs based on preferred_psps (from routing UI) if provided
        if preferred_psps:
            order_map = {
                name.lower(): idx for idx, name in enumerate(preferred_psps, start=1)
            }
            default_base = len(order_map) + 1
            for cfg in self.psp_configs:
                if cfg.psp_type in order_map:
                    cfg.priority = order_map[cfg.psp_type]
                else:
                    # Push unspecified providers to the back, keeping relative order
                    cfg.priority = default_base + cfg.priority

            self.psp_configs.sort(key=lambda x: x.priority)

        if not self.psp_configs:
            return False, None, "No PSP configured for merchant", "none"
        
        order_id_for_log = str(metadata.get("order_id") or "").strip() or None
        route_id_for_log = str(metadata.get("route_id") or "").strip() or None
        agent_id_for_log = str(metadata.get("agent_id") or "").strip() or None

        async def _log_attempt_start(*, attempt_number: int, psp_name: str) -> Optional[str]:
            if not order_id_for_log:
                return None
            try:
                attempt_id = (
                    "att_"
                    + hashlib.md5(
                        f"{order_id_for_log}:{psp_name}:{attempt_number}:{datetime.utcnow().isoformat()}".encode()
                    ).hexdigest()[:12]
                )
                await database.execute(
                    """
                    INSERT INTO payment_attempts (
                        attempt_id, order_id, route_id, agent_id,
                        psp_name, attempt_number, status,
                        amount, currency, created_at
                    ) VALUES (
                        :attempt_id, :order_id, :route_id, :agent_id,
                        :psp_name, :attempt_number, :status,
                        :amount, :currency, NOW()
                    )
                    """,
                    {
                        "attempt_id": attempt_id,
                        "order_id": order_id_for_log,
                        "route_id": route_id_for_log,
                        "agent_id": agent_id_for_log,
                        "psp_name": psp_name,
                        "attempt_number": attempt_number,
                        "status": "pending",
                        "amount": float(amount),
                        "currency": currency,
                    },
                )
                return attempt_id
            except Exception as e:
                logger.warning(
                    {"order_id": order_id_for_log, "psp": psp_name, "error": str(e)},
                    "Failed to log payment attempt start (best-effort)",
                )
                return None

        async def _log_attempt_finish(
            *,
            attempt_id: Optional[str],
            status: str,
            response_time_ms: Optional[int] = None,
            error_message: Optional[str] = None,
        ) -> None:
            if not attempt_id:
                return
            try:
                await database.execute(
                    """
                    UPDATE payment_attempts
                    SET status = :status,
                        response_time_ms = :response_time_ms,
                        error_code = :error_code,
                        error_message = :error_message,
                        completed_at = NOW()
                    WHERE attempt_id = :attempt_id
                    """,
                    {
                        "attempt_id": attempt_id,
                        "status": status,
                        "response_time_ms": response_time_ms,
                        "error_code": "PSP_ERROR" if status == "failed" else None,
                        "error_message": error_message,
                    },
                )
            except Exception as e:
                logger.warning(
                    {"attempt_id": attempt_id, "error": str(e)},
                    "Failed to log payment attempt finish (best-effort)",
                )

        # Try each PSP in priority order
        attempt_number = 0
        for config in self.psp_configs:
            attempt_number += 1
            try:
                logger.info(f"Attempting payment with {config.psp_type} (priority {config.priority})")

                # Get PSP adapter
                psp_adapter = get_psp_adapter(
                    config.psp_type,
                    config.api_key,
                    merchant_account=config.merchant_account
                )

                # Attempt payment
                attempt_id = await _log_attempt_start(attempt_number=attempt_number, psp_name=config.psp_type)
                start_ts = datetime.utcnow()
                success, payment_intent, error = await psp_adapter.create_payment_intent(
                    amount=amount,
                    currency=currency,
                    metadata={
                        **metadata,
                        "psp_priority": config.priority,
                        "psp_type": config.psp_type
                    }
                )
                response_time_ms = int((datetime.utcnow() - start_ts).total_seconds() * 1000)

                if success:
                    logger.info(f"Payment intent created successfully with {config.psp_type}")
                    await _log_attempt_finish(
                        attempt_id=attempt_id,
                        status="success",
                        response_time_ms=response_time_ms,
                    )

                    # Log success for analytics
                    await self._log_psp_attempt(
                        psp_type=config.psp_type,
                        success=True,
                        priority=config.priority,
                        amount=amount,
                        currency=currency
                    )
                    
                    return True, payment_intent, None, config.psp_type
                else:
                    logger.warning(f"{config.psp_type} failed: {error}")
                    await _log_attempt_finish(
                        attempt_id=attempt_id,
                        status="failed",
                        response_time_ms=response_time_ms,
                        error_message=error,
                    )

                    # Log failure
                    await self._log_psp_attempt(
                        psp_type=config.psp_type,
                        success=False,
                        priority=config.priority,
                        amount=amount,
                        currency=currency,
                        error=error
                    )
                    
                    # Continue to next PSP
                    continue

            except Exception as e:
                logger.error(f"Exception with {config.psp_type}: {e}")
                continue
        
        # All PSPs failed
        return False, None, "All PSPs failed", "none"
    
    async def _log_psp_attempt(
        self,
        psp_type: str,
        success: bool,
        priority: int,
        amount: Decimal,
        currency: str,
        error: Optional[str] = None
    ):
        """Log PSP attempt for analytics"""
        try:
            # TODO: Create psp_attempts table for tracking
            log_entry = {
                "merchant_id": self.merchant_id,
                "psp_type": psp_type,
                "success": success,
                "priority": priority,
                "amount": float(amount),
                "currency": currency,
                "error": error,
                "timestamp": datetime.now()
            }
            
            # This helps merchants see which PSP performs best
            logger.info(f"PSP attempt logged: {log_entry}")
            
        except Exception as e:
            logger.error(f"Failed to log PSP attempt: {e}")
    
    async def get_psp_performance(self, days: int = 30) -> Dict[str, Any]:
        """
        Get PSP performance analytics
        
        Returns success rates, avg response times, etc. for each PSP
        """
        # TODO: Query psp_attempts table
        return {
            "primary_psp": {
                "name": "stripe",
                "success_rate": 98.5,
                "avg_response_time_ms": 450,
                "total_attempts": 1250
            },
            "backup_psps": [],
            "overall_success_rate": 98.5,
            "failover_count": 0
        }


# Helper function for order creation
async def create_payment_with_failover(
    merchant_id: str,
    amount: Decimal,
    currency: str,
    metadata: Dict[str, Any],
    preferred_psps: Optional[List[str]] = None,
) -> Tuple[bool, Optional[PaymentIntent], Optional[str], str]:
    """
    Convenience function to create payment with multi-PSP support
    
    Usage in order_routes.py:
    success, payment_intent, error, psp_used = await create_payment_with_failover(
        merchant_id=merchant_id,
        amount=total,
        currency=currency,
        metadata={...}
    )
    """
    orchestrator = MultiPSPOrchestrator(merchant_id)
    return await orchestrator.create_payment_intent(
        amount, currency, metadata, preferred_psps=preferred_psps
    )
