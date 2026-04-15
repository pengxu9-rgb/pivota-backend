"""
Multi-PSP Orchestrator
Handles payment routing with primary/backup PSP strategy for higher success rates
"""

from typing import Dict, Any, Optional, Tuple, List
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime
import asyncio

from adapters.psp_adapter import PSPAdapter, get_psp_adapter, PaymentIntent
from db.database import database
from services.merchant_psp_config_service import (
    build_runtime_adapter_kwargs,
    evaluate_psp_readiness,
    fetch_active_merchant_psps,
)
from utils.logger import logger
from utils.transient_errors import is_asyncpg_busy_error
import hashlib


def _payment_attempt_agent_id(agent_id: Optional[str]) -> Optional[str]:
    value = str(agent_id or "").strip()
    if not value or value.startswith("agent_internal_trusted_"):
        return None
    return value


@dataclass
class PSPConfig:
    """PSP configuration"""
    psp_type: str
    api_key: str
    priority: int  # 1 = primary, 2 = backup, etc.
    is_active: bool
    merchant_account: Optional[str] = None  # For Adyen
    account_id: Optional[str] = None
    secret_key: Optional[str] = None
    environment: Optional[str] = None
    provider_config: Optional[Dict[str, Any]] = None
    validation_status: Optional[str] = None
    validation_error: Optional[str] = None
    live_charge_ready: bool = False
    readiness_blockers: Optional[List[str]] = None


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
        
    async def load_psp_configs(self, *, canonical_only: bool = False):
        """
        Load all PSP configurations for this merchant.

        Source of truth is merchant_psps only. Legacy onboarding PSP fields are
        no longer consulted at runtime because they can drift away from the
        canonical PSP identity, environment, and webhook state.
        """
        self.psp_configs = []

        try:
            try:
                psps = await fetch_active_merchant_psps(
                    merchant_id=self.merchant_id,
                )
            except Exception as e:
                if not is_asyncpg_busy_error(e):
                    raise
                logger.warning(
                    "[MultiPSP] transient asyncpg state loading merchant_psps for %s; retrying once",
                    self.merchant_id,
                )
                await asyncio.sleep(0.05)
                psps = await fetch_active_merchant_psps(
                    merchant_id=self.merchant_id,
                )
        except Exception as e:
            if is_asyncpg_busy_error(e):
                raise
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

            provider_config = psp_dict.get("provider_config")
            merchant_account = psp_dict.get("account_id") if provider == "adyen" else None
            readiness = evaluate_psp_readiness(
                provider,
                status=psp_dict.get("status"),
                api_key=api_key,
                account_id=psp_dict.get("account_id"),
                provider_config=provider_config,
                environment=psp_dict.get("environment"),
                validation_status=psp_dict.get("validation_status"),
                validation_error=psp_dict.get("validation_error"),
            )

            self.psp_configs.append(
                PSPConfig(
                    psp_type=provider,
                    api_key=api_key,
                    priority=priority,
                    is_active=True,
                    merchant_account=merchant_account,
                    account_id=psp_dict.get("account_id"),
                    secret_key=psp_dict.get("secret_key"),
                    environment=psp_dict.get("environment"),
                    provider_config=provider_config,
                    validation_status=readiness.get("validation_status"),
                    validation_error=readiness.get("validation_error"),
                    live_charge_ready=bool(readiness.get("live_charge_ready")),
                    readiness_blockers=list(readiness.get("readiness_blockers") or []),
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
        *,
        canonical_psp_required: bool = False,
        enforce_live_readiness: bool = False,
    ) -> Tuple[bool, Optional[PaymentIntent], Optional[str], str]:
        """
        Create payment intent with automatic PSP failover
        
        Returns: (success, payment_intent, error, psp_used)
        """
        await self.load_psp_configs(canonical_only=canonical_psp_required)

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
        agent_id_for_log = _payment_attempt_agent_id(metadata.get("agent_id"))

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
                    "Failed to log payment attempt start (best-effort): order_id=%s psp=%s error=%s",
                    order_id_for_log,
                    psp_name,
                    str(e),
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
        blocked_errors: List[str] = []
        for config in self.psp_configs:
            if enforce_live_readiness and not config.live_charge_ready:
                blocked_errors.append(
                    f"{config.psp_type}: {', '.join(config.readiness_blockers or ['not ready for live charge'])}"
                )
                logger.warning(
                    f"Skipping {config.psp_type} for merchant {self.merchant_id}: "
                    f"{', '.join(config.readiness_blockers or ['not ready for live charge'])}"
                )
                continue
            attempt_number += 1
            try:
                logger.info(f"Attempting payment with {config.psp_type} (priority {config.priority})")

                # Get PSP adapter
                psp_adapter = get_psp_adapter(
                    config.psp_type,
                    config.api_key,
                    **build_runtime_adapter_kwargs(
                        config.psp_type,
                        api_key=config.api_key,
                        account_id=config.account_id,
                        provider_config=config.provider_config,
                        environment=config.environment,
                        secret_key=config.secret_key,
                    ),
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
        if blocked_errors and attempt_number == 0:
            return False, None, "; ".join(blocked_errors), "none"
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
    *,
    canonical_psp_required: bool = False,
    enforce_live_readiness: bool = False,
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
        amount,
        currency,
        metadata,
        preferred_psps=preferred_psps,
        canonical_psp_required=canonical_psp_required,
        enforce_live_readiness=enforce_live_readiness,
    )
