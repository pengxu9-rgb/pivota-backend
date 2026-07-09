"""
Payment Routing Service - Phase 4
Handles PSP selection, failover, and routing metrics
"""
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
import asyncio
import json
import hashlib
import random
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from collections import defaultdict

from databases import Database
from observability.reliability_metrics import (
    record_payment_attempt,
    record_payment_fallback,
    record_payment_timeout,
    set_payment_circuit,
    record_retry_attempt,
)
from core.reliability.budget import RequestBudget

logger = logging.getLogger(__name__)


def _payment_attempt_agent_id(agent_id: Optional[str]) -> Optional[str]:
    value = str(agent_id or "").strip()
    if not value or value.startswith("agent_internal_trusted_"):
        return None
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        val = int(raw) if raw else default
    except Exception:
        val = default
    return max(min_value, min(max_value, val))


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        val = float(raw) if raw else default
    except Exception:
        val = default
    return max(min_value, min(max_value, val))


PAYMENT_ROUTING_V2_ENABLED = _env_bool("PAYMENT_ROUTING_V2_ENABLED", False)
PAYMENT_ROUTING_V2_ALLOWLIST = {
    s.strip() for s in (os.getenv("PAYMENT_ROUTING_V2_MERCHANT_ALLOWLIST", "") or "").split(",") if s.strip()
}
PAYMENT_ROUTING_MAX_ATTEMPTS_TOTAL = _env_int(
    "PAYMENT_ROUTING_MAX_ATTEMPTS_TOTAL",
    3,
    min_value=1,
    max_value=10,
)
PAYMENT_ROUTING_COOLDOWN_SECONDS = _env_float(
    "PAYMENT_ROUTING_COOLDOWN_SECONDS",
    60.0,
    min_value=1.0,
    max_value=3600.0,
)
PAYMENT_ROUTING_CIRCUIT_FAILURE_THRESHOLD = _env_int(
    "PAYMENT_ROUTING_CIRCUIT_FAILURE_THRESHOLD",
    3,
    min_value=1,
    max_value=100,
)
PAYMENT_ROUTING_CIRCUIT_WINDOW_SECONDS = _env_float(
    "PAYMENT_ROUTING_CIRCUIT_WINDOW_SECONDS",
    45.0,
    min_value=1.0,
    max_value=3600.0,
)
PAYMENT_ROUTING_CIRCUIT_OPEN_SECONDS = _env_float(
    "PAYMENT_ROUTING_CIRCUIT_OPEN_SECONDS",
    60.0,
    min_value=1.0,
    max_value=3600.0,
)
PAYMENT_ROUTING_HALF_OPEN_PROBES = _env_int(
    "PAYMENT_ROUTING_HALF_OPEN_PROBES",
    1,
    min_value=1,
    max_value=20,
)
RELIABILITY_BUDGET_ENABLED = _env_bool("RELIABILITY_BUDGET_ENABLED", False)
PAYMENT_TOTAL_BUDGET_MS = _env_int("PAYMENT_TOTAL_BUDGET_MS", 3500, min_value=100, max_value=60000)


class PaymentErrorCategory(Enum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    BUSINESS = "business"
    PSP = "psp"
    UNKNOWN = "unknown"


@dataclass
class RetryDecision:
    category: PaymentErrorCategory
    retryable: bool
    fallbackable: bool
    reason: str

class RoutingStrategy(Enum):
    PRIORITY = "priority"  # Use PSP order
    COST = "cost"  # Cheapest fees first
    PERFORMANCE = "performance"  # Fastest response time


class PaymentRoutingService:
    """
    Service for intelligent payment routing with failover support
    """
    
    def __init__(self, database: Database):
        self.database = database
        self.psp_adapters = {}  # Will be populated with actual PSP adapters
        self._v2_failure_events: Dict[str, List[float]] = defaultdict(list)
        self._v2_circuit_open_until: Dict[str, float] = {}
        self._v2_half_open_remaining: Dict[str, int] = {}
        
    async def select_psp(
        self, 
        agent_id: str, 
        merchant_id: Optional[str] = None,
        amount: float = 0,
        currency: str = "USD"
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Select the best PSP for a payment based on routing configuration
        
        Returns: (selected_psp, route_config)
        """
        # Get routing configuration
        route_config = await self._get_route_config(agent_id, merchant_id)
        
        if not route_config:
            # Create default route if none exists
            route_config = await self._create_default_route(agent_id, merchant_id)
        
        # Get PSP priority list
        psp_priority = route_config.get("psp_priority", [])
        # Handle JSONB/text payloads where psp_priority may be a JSON string
        if isinstance(psp_priority, str):
            try:
                psp_priority = json.loads(psp_priority)
            except Exception:
                logger.error(f"Failed to parse psp_priority JSON for route {route_config.get('route_id')}")
                psp_priority = []
            # Normalize back into route_config for downstream callers
            route_config["psp_priority"] = psp_priority
        if not psp_priority:
            psp_priority = [
                {"psp": "stripe", "priority": 1},
                {"psp": "adyen", "priority": 2},
                {"psp": "paypal", "priority": 3}
            ]
        
        # Sort by priority
        sorted_psps = sorted(psp_priority, key=lambda x: x.get("priority", 999))
        
        # Apply routing strategy
        strategy = route_config.get("routing_strategy", "priority")
        if merchant_id:
            strategy = RoutingStrategy.PRIORITY.value
        
        if strategy == RoutingStrategy.COST.value:
            # TODO: Implement cost-based routing
            sorted_psps = await self._sort_by_cost(sorted_psps, amount, currency)
        elif strategy == RoutingStrategy.PERFORMANCE.value:
            # Sort by recent performance metrics
            sorted_psps = await self._sort_by_performance(sorted_psps)
        
        # Check PSP availability
        for psp_config in sorted_psps:
            psp_name = psp_config.get("psp")
            if await self._is_psp_available(psp_name):
                return psp_name, route_config
        
        # If no PSP available, return the first one anyway (will fail but log attempt)
        return sorted_psps[0].get("psp") if sorted_psps else "stripe", route_config
    
    async def execute_with_failover(
        self,
        payment_request: Dict[str, Any],
        agent_id: str,
        merchant_id: Optional[str] = None,
        preferred_psp: Optional[str] = None,
        request_budget: Optional[RequestBudget] = None,
    ) -> Dict[str, Any]:
        """
        Execute payment with automatic failover to secondary PSPs
        """
        if self._v2_enabled_for_merchant(merchant_id):
            return await self._execute_with_failover_v2(
                payment_request=payment_request,
                agent_id=agent_id,
                merchant_id=merchant_id,
                preferred_psp=preferred_psp,
                request_budget=request_budget,
            )

        order_id = payment_request.get("order_id")
        amount = payment_request.get("amount", 0)
        currency = payment_request.get("currency", "USD")
        
        # Get routing configuration
        selected_psp, route_config = await self.select_psp(agent_id, merchant_id, amount, currency)
        route_id = route_config.get("route_id")
        max_retries = int(route_config.get("max_retries", 2) or 2)
        psp_priority = route_config.get("psp_priority", [])
        if isinstance(psp_priority, str):
            try:
                psp_priority = json.loads(psp_priority)
            except Exception:
                psp_priority = []
        
        # Sort PSPs by priority
        sorted_psps = sorted(psp_priority, key=lambda x: x.get("priority", 999))
        if not sorted_psps:
            sorted_psps = [{"psp": selected_psp, "priority": 1}]
        preferred_norm = str(preferred_psp or "").strip().lower()
        if preferred_norm:
            preferred_entries = [p for p in sorted_psps if str(p.get("psp") or "").strip().lower() == preferred_norm]
            other_entries = [p for p in sorted_psps if str(p.get("psp") or "").strip().lower() != preferred_norm]
            sorted_psps = preferred_entries + other_entries
        
        attempt_number = 0
        last_error = None
        pending_fallback_from: Optional[str] = None
        last_attempted_psp = str(selected_psp or "").strip().lower() or "unknown"

        for psp_config in sorted_psps[:max_retries + 1]:  # Try primary + retries
            psp_name = str(psp_config.get("psp") or "").strip().lower()
            if not psp_name:
                continue
            if pending_fallback_from and pending_fallback_from != psp_name:
                record_payment_fallback(
                    from_psp=pending_fallback_from,
                    to_psp=psp_name,
                    reason="legacy_retry",
                )
            pending_fallback_from = None
            attempt_number += 1
            last_attempted_psp = psp_name
            attempt_id = self._generate_attempt_id(str(order_id), attempt_number)
            logger.info(
                "payment.routing.attempt",
                extra={
                    "event": "payment.routing.attempt",
                    "mode": "legacy",
                    "order_id": str(order_id or ""),
                    "route_id": str(route_id or ""),
                    "attempt_number": attempt_number,
                    "psp": psp_name,
                },
            )
            
            # Log attempt start
            await self._log_payment_attempt(
                attempt_id=attempt_id,
                order_id=order_id,
                route_id=route_id,
                agent_id=agent_id,
                psp_name=psp_name,
                attempt_number=attempt_number,
                status="pending",
                amount=amount,
                currency=currency
            )
            
            try:
                # Execute payment with PSP
                start_time = datetime.utcnow()
                start_mono = time.monotonic()
                result = await self._execute_with_psp(psp_name, payment_request)
                response_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                record_payment_attempt(
                    psp=psp_name,
                    result="success",
                    error_category="none",
                    duration_seconds=max(0.0, time.monotonic() - start_mono),
                )
                
                # Log successful attempt
                await self._update_payment_attempt(
                    attempt_id=attempt_id,
                    status="success",
                    response_time_ms=response_time_ms
                )
                
                # Update route metrics
                await self.update_route_metrics(route_id, {
                    "success": True,
                    "psp": psp_name,
                    "response_time_ms": response_time_ms,
                    "attempt_number": attempt_number
                })
                logger.info(
                    "payment.routing.success",
                    extra={
                        "event": "payment.routing.success",
                        "mode": "legacy",
                        "order_id": str(order_id or ""),
                        "route_id": str(route_id or ""),
                        "attempt_number": attempt_number,
                        "psp": psp_name,
                        "response_time_ms": response_time_ms,
                    },
                )
                
                return {
                    "success": True,
                    "psp_used": psp_name,
                    "attempt_number": attempt_number,
                    "response_time_ms": response_time_ms,
                    **result
                }
                
            except Exception as e:
                # Log failed attempt
                error_message = str(e)
                last_error = error_message
                error_category = self._classify_error_message(error_message).value
                if error_category == PaymentErrorCategory.TIMEOUT.value:
                    record_payment_timeout(psp=psp_name, stage="execute_with_failover")
                record_payment_attempt(
                    psp=psp_name,
                    result="failed",
                    error_category=error_category,
                )
                logger.info(
                    "payment.routing.failure",
                    extra={
                        "event": "payment.routing.failure",
                        "mode": "legacy",
                        "order_id": str(order_id or ""),
                        "route_id": str(route_id or ""),
                        "attempt_number": attempt_number,
                        "psp": psp_name,
                        "error_category": error_category,
                        "error_message": error_message[:400],
                    },
                )
                
                await self._update_payment_attempt(
                    attempt_id=attempt_id,
                    status="failed",
                    error_code="PSP_ERROR",
                    error_message=error_message
                )
                
                # Continue to next PSP if available
                if attempt_number < len(sorted_psps) and attempt_number <= max_retries:
                    record_retry_attempt(domain="payment", category=error_category)
                    pending_fallback_from = psp_name
                    logger.info(
                        "payment.routing.legacy_retry",
                        extra={
                            "event": "payment.routing.legacy_retry",
                            "order_id": str(order_id or ""),
                            "route_id": str(route_id or ""),
                            "failed_psp": psp_name,
                            "attempt_number": attempt_number,
                            "reason": error_category,
                        },
                    )
                    await asyncio.sleep(0.5)  # Brief delay before retry
                    continue
                else:
                    break
        
        # All attempts failed
        await self.update_route_metrics(route_id, {
            "success": False,
            "psp": last_attempted_psp,
            "attempt_number": attempt_number,
            "error": last_error
        })
        logger.warning(
            "payment.routing.exhausted",
            extra={
                "event": "payment.routing.exhausted",
                "mode": "legacy",
                "order_id": str(order_id or ""),
                "route_id": str(route_id or ""),
                "attempts": attempt_number,
                "last_psp": last_attempted_psp,
                "error": last_error or "all_failed",
            },
        )
        
        return {
            "success": False,
            "error": last_error or "All PSPs failed",
            "attempts": attempt_number,
            "last_psp": last_attempted_psp
        }

    def _v2_enabled_for_merchant(self, merchant_id: Optional[str]) -> bool:
        if not PAYMENT_ROUTING_V2_ENABLED:
            return False
        if not PAYMENT_ROUTING_V2_ALLOWLIST:
            return True
        mid = str(merchant_id or "").strip()
        return bool(mid and mid in PAYMENT_ROUTING_V2_ALLOWLIST)

    @staticmethod
    def _classify_error_message(message: str) -> PaymentErrorCategory:
        msg = str(message or "").lower()
        if "timeout" in msg:
            return PaymentErrorCategory.TIMEOUT
        if any(k in msg for k in ("connection", "refused", "reset", "dns", "network")):
            return PaymentErrorCategory.CONNECTION
        if any(
            k in msg
            for k in (
                "insufficient funds",
                "declined",
                "do_not_honor",
                "not allowed",
                "fraud",
                "card_declined",
            )
        ):
            return PaymentErrorCategory.BUSINESS
        if any(k in msg for k in ("500", "502", "503", "504", "server error", "5xx")):
            return PaymentErrorCategory.PSP
        return PaymentErrorCategory.UNKNOWN

    def _retry_decision(self, exc: Exception) -> RetryDecision:
        if isinstance(exc, asyncio.TimeoutError):
            return RetryDecision(
                category=PaymentErrorCategory.TIMEOUT,
                retryable=True,
                fallbackable=True,
                reason="timeout",
            )
        category = self._classify_error_message(str(exc))
        if category in {PaymentErrorCategory.TIMEOUT, PaymentErrorCategory.CONNECTION, PaymentErrorCategory.PSP}:
            return RetryDecision(
                category=category,
                retryable=True,
                fallbackable=True,
                reason=category.value,
            )
        if category == PaymentErrorCategory.BUSINESS:
            return RetryDecision(
                category=category,
                retryable=False,
                fallbackable=False,
                reason="business_decline",
            )
        return RetryDecision(
            category=PaymentErrorCategory.UNKNOWN,
            retryable=False,
            fallbackable=False,
            reason="unknown",
        )

    def _v2_prune_failures(self, psp_name: str, now_mono: float) -> None:
        events = self._v2_failure_events.get(psp_name, [])
        if not events:
            return
        cutoff = now_mono - PAYMENT_ROUTING_CIRCUIT_WINDOW_SECONDS
        self._v2_failure_events[psp_name] = [ts for ts in events if ts >= cutoff]

    def _v2_circuit_state(self, psp_name: str, now_mono: Optional[float] = None) -> str:
        now = float(now_mono) if now_mono is not None else time.monotonic()
        open_until = float(self._v2_circuit_open_until.get(psp_name, 0.0) or 0.0)
        if open_until > now:
            state = "open"
        elif open_until > 0.0 and int(self._v2_half_open_remaining.get(psp_name, 0) or 0) > 0:
            state = "half_open"
        else:
            state = "closed"
        set_payment_circuit(psp=psp_name, state=state)
        return state

    def _v2_record_success(self, psp_name: str) -> None:
        self._v2_failure_events.pop(psp_name, None)
        self._v2_circuit_open_until.pop(psp_name, None)
        self._v2_half_open_remaining.pop(psp_name, None)
        set_payment_circuit(psp=psp_name, state="closed")

    def _v2_record_failure(self, psp_name: str, *, timeout: bool = False) -> None:
        now_mono = time.monotonic()
        self._v2_prune_failures(psp_name, now_mono)
        events = self._v2_failure_events.setdefault(psp_name, [])
        events.append(now_mono)
        self._v2_prune_failures(psp_name, now_mono)

        if timeout:
            self._v2_circuit_open_until[psp_name] = now_mono + PAYMENT_ROUTING_CIRCUIT_OPEN_SECONDS
            self._v2_half_open_remaining[psp_name] = PAYMENT_ROUTING_HALF_OPEN_PROBES
            set_payment_circuit(psp=psp_name, state="open")
            return

        if len(events) >= PAYMENT_ROUTING_CIRCUIT_FAILURE_THRESHOLD:
            self._v2_circuit_open_until[psp_name] = now_mono + PAYMENT_ROUTING_CIRCUIT_OPEN_SECONDS
            self._v2_half_open_remaining[psp_name] = PAYMENT_ROUTING_HALF_OPEN_PROBES
            set_payment_circuit(psp=psp_name, state="open")

    async def _execute_with_failover_v2(
        self,
        *,
        payment_request: Dict[str, Any],
        agent_id: str,
        merchant_id: Optional[str],
        preferred_psp: Optional[str],
        request_budget: Optional[RequestBudget],
    ) -> Dict[str, Any]:
        order_id = payment_request.get("order_id")
        amount = payment_request.get("amount", 0)
        currency = payment_request.get("currency", "USD")

        if RELIABILITY_BUDGET_ENABLED and request_budget is None:
            request_budget = RequestBudget.from_total_ms(PAYMENT_TOTAL_BUDGET_MS)

        selected_psp, route_config = await self.select_psp(agent_id, merchant_id, amount, currency)
        route_id = route_config.get("route_id")
        max_retries = int(route_config.get("max_retries", 2) or 2)
        max_attempts_total = min(max_retries + 1, PAYMENT_ROUTING_MAX_ATTEMPTS_TOTAL)
        psp_priority = route_config.get("psp_priority", [])
        if isinstance(psp_priority, str):
            try:
                psp_priority = json.loads(psp_priority)
            except Exception:
                psp_priority = []
        sorted_psps = sorted(psp_priority, key=lambda x: x.get("priority", 999))
        if not sorted_psps:
            sorted_psps = [{"psp": selected_psp, "priority": 1}]
        preferred_norm = str(preferred_psp or "").strip().lower()
        if preferred_norm:
            preferred_entries = [p for p in sorted_psps if str(p.get("psp") or "").strip().lower() == preferred_norm]
            other_entries = [p for p in sorted_psps if str(p.get("psp") or "").strip().lower() != preferred_norm]
            sorted_psps = preferred_entries + other_entries

        visited_psps: set[str] = set()
        attempt_number = 0
        last_error: Optional[str] = None
        last_failed_psp: Optional[str] = None
        last_attempted_psp = str(selected_psp or "").strip().lower() or "unknown"

        for psp_config in sorted_psps:
            psp_name = str(psp_config.get("psp") or "").strip().lower()
            if not psp_name or psp_name in visited_psps:
                continue
            if attempt_number >= max_attempts_total:
                break

            circuit_state = self._v2_circuit_state(psp_name)
            if circuit_state == "open":
                continue
            if circuit_state == "half_open":
                probes_remaining = int(self._v2_half_open_remaining.get(psp_name, 0) or 0)
                if probes_remaining <= 0:
                    continue
                self._v2_half_open_remaining[psp_name] = max(0, probes_remaining - 1)

            visited_psps.add(psp_name)
            attempt_number += 1
            last_attempted_psp = psp_name
            attempt_id = self._generate_attempt_id(str(order_id), attempt_number)
            if last_failed_psp:
                record_payment_fallback(
                    from_psp=last_failed_psp,
                    to_psp=psp_name,
                    reason="v2_failover",
                )
                last_failed_psp = None
            logger.info(
                "payment.routing.attempt",
                extra={
                    "event": "payment.routing.attempt",
                    "mode": "v2",
                    "order_id": str(order_id or ""),
                    "route_id": str(route_id or ""),
                    "attempt_number": attempt_number,
                    "attempt_limit": max_attempts_total,
                    "psp": psp_name,
                    "visited_psps": sorted(visited_psps),
                },
            )

            await self._log_payment_attempt(
                attempt_id=attempt_id,
                order_id=order_id,
                route_id=route_id,
                agent_id=agent_id,
                psp_name=psp_name,
                attempt_number=attempt_number,
                status="pending",
                amount=amount,
                currency=currency,
            )

            started_mono = time.monotonic()
            route_timeout_ms = int(route_config.get("timeout_ms") or 30000)
            timeout_seconds = max(0.05, float(route_timeout_ms) / 1000.0)
            if RELIABILITY_BUDGET_ENABLED and request_budget is not None:
                timeout_seconds = request_budget.timeout_seconds(
                    default_seconds=timeout_seconds,
                    min_seconds=0.05,
                    max_seconds=30.0,
                )

            try:
                result = await asyncio.wait_for(
                    self._execute_with_psp(psp_name, payment_request),
                    timeout=timeout_seconds,
                )
                response_time_ms = int((time.monotonic() - started_mono) * 1000)
                await self._update_payment_attempt(
                    attempt_id=attempt_id,
                    status="success",
                    response_time_ms=response_time_ms,
                )
                self._v2_record_success(psp_name)
                record_payment_attempt(
                    psp=psp_name,
                    result="success",
                    error_category="none",
                    duration_seconds=max(0.0, time.monotonic() - started_mono),
                )

                await self.update_route_metrics(
                    route_id,
                    {
                        "success": True,
                        "psp": psp_name,
                        "response_time_ms": response_time_ms,
                        "attempt_number": attempt_number,
                    },
                )
                logger.info(
                    "payment.routing.success",
                    extra={
                        "event": "payment.routing.success",
                        "mode": "v2",
                        "order_id": str(order_id or ""),
                        "route_id": str(route_id or ""),
                        "attempt_number": attempt_number,
                        "attempt_limit": max_attempts_total,
                        "psp": psp_name,
                        "response_time_ms": response_time_ms,
                        "visited_psps": sorted(visited_psps),
                    },
                )
                return {
                    "success": True,
                    "psp_used": psp_name,
                    "attempt_number": attempt_number,
                    "response_time_ms": response_time_ms,
                    "attempts_limit": max_attempts_total,
                    "visited_psps": sorted(visited_psps),
                    **result,
                }
            except Exception as exc:
                decision = self._retry_decision(exc)
                last_error = str(exc)
                last_failed_psp = psp_name
                await self._update_payment_attempt(
                    attempt_id=attempt_id,
                    status="failed",
                    error_code=decision.category.value.upper(),
                    error_message=last_error,
                )
                if decision.category == PaymentErrorCategory.TIMEOUT:
                    record_payment_timeout(psp=psp_name, stage="v2_execute")
                self._v2_record_failure(
                    psp_name,
                    timeout=decision.category == PaymentErrorCategory.TIMEOUT,
                )
                record_payment_attempt(
                    psp=psp_name,
                    result="failed",
                    error_category=decision.category.value,
                    duration_seconds=max(0.0, time.monotonic() - started_mono),
                )
                logger.info(
                    "payment.routing.failure",
                    extra={
                        "event": "payment.routing.failure",
                        "mode": "v2",
                        "order_id": str(order_id or ""),
                        "route_id": str(route_id or ""),
                        "attempt_number": attempt_number,
                        "attempt_limit": max_attempts_total,
                        "psp": psp_name,
                        "error_category": decision.category.value,
                        "retryable": decision.retryable,
                        "fallbackable": decision.fallbackable,
                        "reason": decision.reason,
                        "error_message": (last_error or "")[:400],
                    },
                )
                if decision.retryable:
                    record_retry_attempt(domain="payment", category=decision.category.value)
                if decision.retryable and decision.fallbackable and attempt_number < max_attempts_total:
                    await asyncio.sleep(min(0.5, PAYMENT_ROUTING_COOLDOWN_SECONDS / 10.0))
                    continue
                break

        await self.update_route_metrics(
            route_id,
            {
                "success": False,
                "psp": last_failed_psp or last_attempted_psp,
                "attempt_number": attempt_number,
                "error": last_error or "all_failed",
            },
        )
        logger.warning(
            "payment.routing.exhausted",
            extra={
                "event": "payment.routing.exhausted",
                "mode": "v2",
                "order_id": str(order_id or ""),
                "route_id": str(route_id or ""),
                "attempts": attempt_number,
                "attempt_limit": max_attempts_total,
                "visited_psps": sorted(visited_psps),
                "last_psp": last_failed_psp or last_attempted_psp,
                "error": last_error or "all_failed",
            },
        )
        return {
            "success": False,
            "error": last_error or "All PSPs failed",
            "attempts": attempt_number,
            "attempts_limit": max_attempts_total,
            "visited_psps": sorted(visited_psps),
            "no_backjump": True,
            "last_psp": last_failed_psp or last_attempted_psp,
        }
    
    async def update_route_metrics(self, route_id: str, attempt_data: Dict[str, Any]):
        """
        Update routing metrics based on payment attempt results
        """
        psp_name = attempt_data.get("psp")
        success = attempt_data.get("success", False)
        response_time_ms = attempt_data.get("response_time_ms")
        
        # Get current 5-minute window
        current_time = datetime.utcnow()
        period_start = current_time.replace(second=0, microsecond=0)
        period_start = period_start.replace(minute=(period_start.minute // 5) * 5)
        
        # Check if metrics record exists
        existing = await self.database.fetch_one(
            """
            SELECT id FROM psp_performance_metrics
            WHERE psp_name = :psp_name AND period_start = :period_start
            """,
            {"psp_name": psp_name, "period_start": period_start}
        )
        
        if existing:
            # Update existing metrics
            if success:
                await self.database.execute(
                    """
                    UPDATE psp_performance_metrics
                    SET total_attempts = total_attempts + 1,
                        successful_attempts = successful_attempts + 1,
                        avg_response_time_ms = (
                            (avg_response_time_ms * successful_attempts + :response_time)::INTEGER / (successful_attempts + 1)
                        ),
                        success_rate = (successful_attempts + 1)::NUMERIC * 100 / (total_attempts + 1)
                    WHERE psp_name = :psp_name AND period_start = :period_start
                    """,
                    {
                        "psp_name": psp_name,
                        "period_start": period_start,
                        "response_time": response_time_ms or 0
                    }
                )
            else:
                await self.database.execute(
                    """
                    UPDATE psp_performance_metrics
                    SET total_attempts = total_attempts + 1,
                        failed_attempts = failed_attempts + 1,
                        success_rate = successful_attempts::NUMERIC * 100 / (total_attempts + 1)
                    WHERE psp_name = :psp_name AND period_start = :period_start
                    """,
                    {"psp_name": psp_name, "period_start": period_start}
                )
        else:
            # Create new metrics record
            await self.database.execute(
                """
                INSERT INTO psp_performance_metrics (
                    psp_name, period_start, total_attempts, 
                    successful_attempts, failed_attempts,
                    avg_response_time_ms, success_rate
                ) VALUES (
                    :psp_name, :period_start, 1,
                    :successful, :failed,
                    :response_time, :success_rate
                )
                """,
                {
                    "psp_name": psp_name,
                    "period_start": period_start,
                    "successful": 1 if success else 0,
                    "failed": 0 if success else 1,
                    "response_time": response_time_ms or 0,
                    "success_rate": 100.0 if success else 0.0
                }
            )
    
    async def get_optimal_route(self, criteria: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get optimal routing configuration based on criteria
        Future: Implement ML-based routing optimization
        """
        # TODO: Implement intelligent routing based on:
        # - Historical success rates
        # - Average response times
        # - Cost per transaction
        # - Geographic location
        # - Transaction amount tiers
        
        return {
            "recommended_psp": None,
            "reasoning": "Routing telemetry is not measured yet",
            "metrics": {
                "payment_telemetry_reported": False
            }
        }
    
    # Private helper methods
    
    async def _get_route_config(self, agent_id: str, merchant_id: Optional[str]) -> Optional[Dict]:
        """
        Get routing configuration for agent / merchant.

        Priority rules:
        - If merchant_id is provided, prefer the most recent merchant-level route
          (so that changes saved from the Merchant Portal Integrations UI always
          take effect for all agents).
        - If no merchant-level route is found, fall back to the latest agent-level
          route for this agent (legacy behaviour).
        """
        # 1) Prefer merchant-specific route when merchant_id is known
        if merchant_id:
            merchant_route = await self.database.fetch_one(
                """
                SELECT route_id, psp_priority, routing_strategy, max_retries, timeout_ms, metadata
                FROM payment_routes
                WHERE is_active = true
                  AND merchant_id = :merchant_id
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"merchant_id": merchant_id},
            )
            if merchant_route:
                return dict(merchant_route)

        # 2) Fallback to agent-specific route
        if agent_id:
            agent_route = await self.database.fetch_one(
                """
                SELECT route_id, psp_priority, routing_strategy, max_retries, timeout_ms, metadata
                FROM payment_routes
                WHERE is_active = true
                  AND agent_id = :agent_id
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"agent_id": agent_id},
            )
            if agent_route:
                return dict(agent_route)

        # 3) No route found
        return None
    
    async def _create_default_route(self, agent_id: str, merchant_id: Optional[str]) -> Dict:
        """Create default routing configuration.

        Behavior:
        - If merchant_id is provided and merchant_psps has active entries,
          use those PSPs in "last connected first" order for psp_priority.
        - Otherwise fall back to a generic priority list (stripe > adyen > paypal).
        """
        psp_priority: List[Dict[str, Any]] = []

        # Prefer merchant-specific PSP configuration when available
        if merchant_id:
            try:
                psps = await self.database.fetch_all(
                    """
                    SELECT provider
                    FROM merchant_psps
                    WHERE merchant_id = :merchant_id AND status = 'active'
                    ORDER BY connected_at DESC
                    """,
                    {"merchant_id": merchant_id},
                )

                seen: set[str] = set()
                priority = 1
                for row in psps:
                    provider = (row["provider"] or "").lower()
                    if not provider or provider in seen:
                        continue
                    psp_priority.append({"psp": provider, "priority": priority})
                    seen.add(provider)
                    priority += 1
            except Exception as e:
                logger.error(f"Failed to load merchant_psps for default route {merchant_id}: {e}")

        # Fallback when no merchant-specific PSPs are found
        if not psp_priority:
            psp_priority = [
                {"psp": "stripe", "priority": 1},
                {"psp": "adyen", "priority": 2},
                {"psp": "paypal", "priority": 3},
            ]

        route_id = f"route_{hashlib.md5(f'{agent_id}{merchant_id}{datetime.utcnow()}'.encode()).hexdigest()[:12]}"

        await self.database.execute(
            """
            INSERT INTO payment_routes (
                route_id, agent_id, merchant_id, psp_priority, routing_strategy
            ) VALUES (
                :route_id, :agent_id, :merchant_id, :psp_priority, 'priority'
            )
            """,
            {
                "route_id": route_id,
                "agent_id": agent_id,
                "merchant_id": merchant_id,
                "psp_priority": json.dumps(psp_priority),
            },
        )

        return {
            "route_id": route_id,
            "psp_priority": psp_priority,
            "routing_strategy": "priority",
            "max_retries": 2,
        }
    
    async def _is_psp_available(self, psp_name: str) -> bool:
        """Check if PSP is currently available"""
        # Check recent failure rate
        recent_metrics = await self.database.fetch_one(
            """
            SELECT success_rate, total_attempts
            FROM psp_performance_metrics
            WHERE psp_name = :psp_name
            AND period_start >= :cutoff
            ORDER BY period_start DESC
            LIMIT 1
            """,
            {
                "psp_name": psp_name,
                "cutoff": datetime.utcnow() - timedelta(minutes=10)
            }
        )
        
        if not recent_metrics:
            return True  # No recent data, assume available
        
        # Consider PSP unavailable if success rate < 50% or no attempts
        metrics = dict(recent_metrics)
        return metrics.get("success_rate", 0) >= 50 and metrics.get("total_attempts", 0) > 0
    
    async def _sort_by_cost(self, psps: List[Dict], amount: float, currency: str) -> List[Dict]:
        """Sort PSPs by transaction cost"""
        # TODO: Implement cost calculation based on:
        # - Fixed fees
        # - Percentage fees
        # - Currency conversion rates
        # - Volume discounts
        return psps
    
    async def _sort_by_performance(self, psps: List[Dict]) -> List[Dict]:
        """Sort PSPs by recent performance metrics"""
        psp_metrics = {}
        
        for psp_config in psps:
            psp_name = psp_config.get("psp")
            
            # Get recent performance
            metrics = await self.database.fetch_one(
                """
                SELECT AVG(success_rate) as avg_success_rate,
                       AVG(avg_response_time_ms) as avg_response_time
                FROM psp_performance_metrics
                WHERE psp_name = :psp_name
                AND period_start >= :cutoff
                """,
                {
                    "psp_name": psp_name,
                    "cutoff": datetime.utcnow() - timedelta(hours=1)
                }
            )
            
            if metrics:
                m = dict(metrics)
                # Score based on success rate (weight: 70%) and speed (weight: 30%)
                score = (m.get("avg_success_rate", 0) * 0.7) + ((1000 - m.get("avg_response_time", 1000)) / 10 * 0.3)
                psp_metrics[psp_name] = score
            else:
                psp_metrics[psp_name] = 50  # Default middle score
        
        # Sort by performance score
        return sorted(psps, key=lambda x: psp_metrics.get(x.get("psp"), 0), reverse=True)
    
    async def _execute_with_psp(self, psp_name: str, payment_request: Dict) -> Dict:
        """Execute a payment with a specific PSP.

        FAIL-CLOSED: this previously SIMULATED execution with ``random.random()``
        and returned a fabricated ``transaction_id`` + ``status="success"`` without
        ever contacting a PSP. That is unsafe now that agents/partners are live —
        the mounted endpoint ``POST /agents/{id}/payments/route`` would report a
        fake "payment succeeded" with no money moved. Real charges go through the
        hosted-checkout -> merchant PSP path (``initiate_merchant_payment``), NOT
        this routing shim. Until real PSP adapters are wired here we fail closed,
        so no fabricated success can be returned; the failover caller converts this
        into an honest failure response.
        """
        raise NotImplementedError(
            f"PSP execution via payment routing is not implemented for '{psp_name}'; "
            "the simulated success path is disabled (fail-closed). Use the "
            "hosted-checkout / initiate_merchant_payment path for real payments."
        )
    
    def _generate_attempt_id(self, order_id: str, attempt_number: int) -> str:
        """Generate unique attempt ID"""
        return f"att_{hashlib.md5(f'{order_id}{attempt_number}{datetime.utcnow()}'.encode()).hexdigest()[:12]}"
    
    async def _log_payment_attempt(self, **kwargs):
        """Log payment attempt to database"""
        kwargs["agent_id"] = _payment_attempt_agent_id(kwargs.get("agent_id"))
        await self.database.execute(
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
            kwargs
        )
    
    async def _update_payment_attempt(self, attempt_id: str, **kwargs):
        """Update payment attempt record"""
        set_clauses = []
        params = {"attempt_id": attempt_id}
        
        for key, value in kwargs.items():
            set_clauses.append(f"{key} = :{key}")
            params[key] = value
        
        if set_clauses:
            await self.database.execute(
                f"""
                UPDATE payment_attempts
                SET {', '.join(set_clauses)}, completed_at = NOW()
                WHERE attempt_id = :attempt_id
                """,
                params
            )
    
    # ========================================================================
    # [Phase 4++] Dual-routing support - NEW METHODS
    # All existing Phase 4 methods above remain unchanged
    # ========================================================================
    
    async def get_merchant_routing_policy(self, merchant_id: str) -> Dict[str, Any]:
        """
        [Phase 4++] Get merchant routing policy from routing_policies table
        
        Args:
            merchant_id: Merchant ID
            
        Returns:
            Dict with merchant routing policy or empty dict
        """
        try:
            result = await self.database.fetch_one(
                """
                SELECT policy, is_active, priority
                FROM routing_policies
                WHERE owner_type = 'merchant' AND owner_id = :merchant_id AND is_active = true
                """,
                {"merchant_id": merchant_id}
            )
            
            if result:
                policy = json.loads(result['policy']) if isinstance(result['policy'], str) else result['policy']
                return {
                    **policy,
                    '_priority': result['priority'],
                    '_active': result['is_active']
                }
            
            return {}
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to get merchant routing policy: {e}")
            return {}
    
    async def get_agent_routing_policy(self, agent_id: str) -> Dict[str, Any]:
        """
        [Phase 4++] Get agent routing policy from routing_policies table
        
        Args:
            agent_id: Agent ID
            
        Returns:
            Dict with agent routing policy or empty dict
        """
        try:
            result = await self.database.fetch_one(
                """
                SELECT policy, is_active, priority
                FROM routing_policies
                WHERE owner_type = 'agent' AND owner_id = :agent_id AND is_active = true
                """,
                {"agent_id": agent_id}
            )
            
            if result:
                policy = json.loads(result['policy']) if isinstance(result['policy'], str) else result['policy']
                return {
                    **policy,
                    '_priority': result['priority'],
                    '_active': result['is_active']
                }
            
            return {}
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to get agent routing policy: {e}")
            return {}
    
    async def check_agent_whitelisted(self, agent_id: str) -> bool:
        """
        [Phase 4++] Check if agent has routing override permission
        
        Args:
            agent_id: Agent ID
            
        Returns:
            bool: True if agent can override merchant rules
        """
        try:
            result = await self.database.fetch_one(
                """
                SELECT routing_override_enabled
                FROM agents
                WHERE agent_id = :agent_id
                """,
                {"agent_id": agent_id}
            )
            
            return result['routing_override_enabled'] if result else False
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to check agent whitelist: {e}")
            return False
    
    async def resolve_dual_routing(
        self,
        merchant_id: str,
        agent_id: str,
        amount: float,
        currency: str
    ) -> Tuple[str, Dict[str, Any]]:
        """
        [Phase 4++] Resolve PSP selection using dual-side routing engine
        
        Args:
            merchant_id: Merchant ID
            agent_id: Agent ID
            amount: Payment amount
            currency: Currency code
            
        Returns:
            Tuple of (selected_psp, routing_decision)
        """
        from core.routing_engine import DualRoutingEngine
        
        try:
            # Get merchant and agent policies
            merchant_policy = await self.get_merchant_routing_policy(merchant_id)
            agent_policy = await self.get_agent_routing_policy(agent_id)
            
            # Check if agent is whitelisted for overrides
            agent_whitelisted = await self.check_agent_whitelisted(agent_id)
            
            # Get available PSPs (from existing route config)
            route_config = await self._get_route_config(agent_id, merchant_id)
            available_psps = route_config.get('psp_priority', [])
            
            if not available_psps:
                # Fall back to default PSPs
                available_psps = [
                    {"psp": "stripe", "priority": 1},
                    {"psp": "adyen", "priority": 2},
                    {"psp": "paypal", "priority": 3}
                ]
            
            # Use DualRoutingEngine to resolve
            engine = DualRoutingEngine(
                merchant_rules=merchant_policy,
                agent_rules=agent_policy,
                available_psps=available_psps,
                agent_whitelisted=agent_whitelisted
            )
            
            routing_decision = engine.resolve()
            
            # Log routing decision
            await self.log_routing_decision(
                merchant_id=merchant_id,
                agent_id=agent_id,
                decision=routing_decision,
                amount=amount,
                currency=currency
            )
            
            selected_psp = routing_decision.get('selected_psp', 'stripe')
            
            logger.info(
                f"[Phase 4++] Dual routing resolved: merchant={merchant_id}, agent={agent_id}, "
                f"selected={selected_psp}, conflicts={routing_decision.get('conflict_detected')}"
            )
            
            return selected_psp, routing_decision
            
        except Exception as e:
            logger.error(f"[Phase 4++] Dual routing resolution failed: {e}")
            # Fall back to single routing
            return await self.select_psp(agent_id, merchant_id, amount, currency)
    
    async def route_transaction_dual(
        self,
        payment_request: Dict[str, Any],
        merchant_id: str,
        agent_id: str
    ) -> Dict[str, Any]:
        """
        [Phase 4++] Route transaction using dual-side routing
        Backward-compatible wrapper that falls back to single-routing if no dual rules
        
        Args:
            payment_request: Payment request details
            merchant_id: Merchant ID
            agent_id: Agent ID
            
        Returns:
            Payment result with routing trace
        """
        try:
            # Check if dual routing policies exist
            merchant_policy = await self.get_merchant_routing_policy(merchant_id)
            agent_policy = await self.get_agent_routing_policy(agent_id)
            
            if not merchant_policy and not agent_policy:
                # No dual routing policies, use existing single routing
                logger.info(f"[Phase 4++] No dual routing policies found, using single routing")
                return await self.execute_with_failover(
                    agent_id=agent_id,
                    payment_request=payment_request,
                    preferred_psp=None
                )
            
            # Use dual routing
            amount = payment_request.get('amount', 0)
            currency = payment_request.get('currency', 'USD')
            
            selected_psp, routing_decision = await self.resolve_dual_routing(
                merchant_id=merchant_id,
                agent_id=agent_id,
                amount=amount,
                currency=currency
            )
            
            # Add routing context to payment request
            payment_request['metadata'] = payment_request.get('metadata', {})
            payment_request['metadata']['routing_context'] = {
                'log_id': routing_decision.get('log_id'),
                'dual_routing': True,
                'conflicts_detected': routing_decision.get('conflict_detected', False),
                'resolution_method': routing_decision.get('resolution_method')
            }
            payment_request['metadata']['merchant_id'] = merchant_id
            payment_request['metadata']['agent_id'] = agent_id
            
            # Execute payment with selected PSP
            result = await self.execute_with_failover(
                agent_id=agent_id,
                payment_request=payment_request,
                preferred_psp=selected_psp
            )
            
            # Add routing decision to result
            result['routing_decision'] = routing_decision
            
            return result
            
        except Exception as e:
            logger.error(f"[Phase 4++] Dual routing transaction failed: {e}")
            # Fall back to single routing on error
            return await self.execute_with_failover(
                agent_id=agent_id,
                payment_request=payment_request,
                preferred_psp=None
            )
    
    async def log_routing_decision(
        self,
        merchant_id: str,
        agent_id: str,
        decision: Dict[str, Any],
        amount: float,
        currency: str,
        order_id: Optional[str] = None
    ) -> int:
        """
        [Phase 4++] Log routing decision to routing_logs table
        
        Args:
            merchant_id: Merchant ID
            agent_id: Agent ID
            decision: Routing decision from DualRoutingEngine
            amount: Payment amount
            currency: Currency code
            order_id: Optional order ID
            
        Returns:
            Log ID
        """
        try:
            # Extract data from decision
            considered_psps = [
                psp.get('psp') for psp in decision.get('decision_trace', [{}])[-1].get('output_psps', [])
                if isinstance(psp, dict)
            ]
            
            result = await self.database.execute(
                """
                INSERT INTO routing_logs (
                    merchant_id, agent_id, order_id,
                    considered_psps, chosen_psp, decision_trace,
                    merchant_rules_applied, agent_rules_applied,
                    conflict_detected, resolution_method,
                    execution_time_ms, created_at
                ) VALUES (
                    :merchant_id, :agent_id, :order_id,
                    :considered_psps, :chosen_psp, :decision_trace,
                    :merchant_rules_applied, :agent_rules_applied,
                    :conflict_detected, :resolution_method,
                    :execution_time_ms, NOW()
                )
                RETURNING id
                """,
                {
                    "merchant_id": merchant_id,
                    "agent_id": agent_id,
                    "order_id": order_id,
                    "considered_psps": json.dumps(considered_psps),
                    "chosen_psp": decision.get('selected_psp'),
                    "decision_trace": json.dumps(decision.get('decision_trace', [])),
                    "merchant_rules_applied": json.dumps(decision.get('merchant_rules_applied', {})),
                    "agent_rules_applied": json.dumps(decision.get('agent_rules_applied', {})),
                    "conflict_detected": decision.get('conflict_detected', False),
                    "resolution_method": decision.get('resolution_method'),
                    "execution_time_ms": decision.get('execution_time_ms', 0)
                }
            )
            
            # Add log ID to decision for reference
            decision['log_id'] = result
            
            logger.info(f"[Phase 4++] Logged routing decision with ID {result}")
            return result
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to log routing decision: {e}")
            return 0
    
    async def get_routing_conflicts(
        self,
        days: int = 30,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        [Phase 4++] Get recent routing conflicts for monitoring
        
        Args:
            days: Number of days to look back
            limit: Maximum number of conflicts to return
            
        Returns:
            List of routing conflicts with details
        """
        try:
            results = await self.database.fetch_all(
                """
                SELECT 
                    rl.id,
                    rl.merchant_id,
                    rl.agent_id,
                    rl.order_id,
                    rl.chosen_psp,
                    rl.decision_trace,
                    rl.resolution_method,
                    rl.created_at,
                    rl.merchant_id as merchant_name,
                    rl.agent_id as agent_name
                FROM routing_logs rl
                WHERE rl.conflict_detected = true
                AND rl.created_at > CURRENT_TIMESTAMP - (CAST(:days AS INTEGER) || ' days')::INTERVAL
                ORDER BY rl.created_at DESC
                LIMIT :limit
                """,
                {"days": days, "limit": limit}
            )
            
            conflicts = []
            for row in results:
                decision_trace = json.loads(row['decision_trace']) if isinstance(row['decision_trace'], str) else row['decision_trace']
                
                # Extract conflicts from trace
                conflicts_from_trace = []
                for item in decision_trace:
                    if isinstance(item, dict) and 'conflicts' in item:
                        conflicts_from_trace.extend(item['conflicts'])
                
                conflicts.append({
                    "id": row['id'],
                    "merchant_id": row['merchant_id'],
                    "merchant_name": row['merchant_name'],
                    "agent_id": row['agent_id'],
                    "agent_name": row['agent_name'],
                    "order_id": row['order_id'],
                    "chosen_psp": row['chosen_psp'],
                    "conflicts": conflicts_from_trace,
                    "resolution_method": row['resolution_method'],
                    "created_at": row['created_at'].isoformat() if row['created_at'] else None
                })
            
            return conflicts
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to get routing conflicts: {e}")
            return []
    
    async def simulate_routing(
        self,
        merchant_id: str,
        agent_id: str,
        test_scenarios: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        [Phase 4++] Simulate routing decisions without executing payments
        
        Args:
            merchant_id: Merchant ID
            agent_id: Agent ID
            test_scenarios: List of test scenarios with amount, currency, etc.
            
        Returns:
            List of simulated routing results
        """
        results = []
        
        for scenario in test_scenarios:
            try:
                selected_psp, routing_decision = await self.resolve_dual_routing(
                    merchant_id=merchant_id,
                    agent_id=agent_id,
                    amount=scenario.get('amount', 100.00),
                    currency=scenario.get('currency', 'USD')
                )
                
                results.append({
                    "scenario": scenario,
                    "selected_psp": selected_psp,
                    "conflict_detected": routing_decision.get('conflict_detected'),
                    "conflicts": routing_decision.get('conflicts'),
                    "resolution_method": routing_decision.get('resolution_method'),
                    "execution_time_ms": routing_decision.get('execution_time_ms')
                })
                
            except Exception as e:
                results.append({
                    "scenario": scenario,
                    "error": str(e)
                })
        
        return results
    
    # [Phase 4++] End of dual-routing extensions
